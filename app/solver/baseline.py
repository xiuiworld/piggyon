"""P2: baseline assignment with CP-SAT.

The objective is lexicographic, not a weighted sum: each stage is optimised,
then pinned as a constraint before the next one runs (policy.objective_order).
Weighted sums would need magic coefficients whose relative size silently
decides the outcome; pinning keeps every stage's precedence exact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from app.models.snapshot import ScenarioInputSnapshot
from app.rules import reason_codes as rc
from app.rules.eligibility import Candidate, ScenarioEvaluation

# Assignment states
ASSIGNED = "ASSIGNED"
UNASSIGNED = "UNASSIGNED"
NOT_APPLICABLE = "NOT_APPLICABLE"

# CP-SAT needs a non-zero window to answer at all; below this a stage is skipped.
_MIN_STAGE_SECONDS = 0.05


@dataclass(frozen=True)
class SolverParameters:
    random_seed: int = 7
    num_search_workers: int = 1
    max_time_seconds: int = 10

    def as_dict(self) -> dict[str, int]:
        return {
            "random_seed": self.random_seed,
            "num_search_workers": self.num_search_workers,
            "max_time_seconds": self.max_time_seconds,
        }


@dataclass
class Assignment:
    order_id: str
    service_id: str
    wagon_id: str
    slot_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "order_id": self.order_id,
            "service_id": self.service_id,
            "wagon_id": self.wagon_id,
            "slot_id": self.slot_id,
        }


@dataclass
class OrderOutcome:
    order_id: str
    input_state: str
    eligibility_state: str
    assignment_state: str
    alternative_state: str
    primary_reason_code: str
    evidence: dict = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "input_state": self.input_state,
            "eligibility_state": self.eligibility_state,
            "assignment_state": self.assignment_state,
            "alternative_state": self.alternative_state,
            "primary_reason_code": self.primary_reason_code,
            "evidence": self.evidence,
            "next_actions": self.next_actions,
        }


@dataclass
class SolveResult:
    solver_status: str
    run_state: str
    is_optimal: bool
    assignments: list[Assignment]
    order_outcomes: list[OrderOutcome]
    objective_values: dict[str, int]


_RUN_STATE_BY_SOLVER_STATUS = {
    "OPTIMAL": "SOLVED_OPTIMAL",
    "FEASIBLE": "SOLVED_FEASIBLE",
    "INFEASIBLE": "MODEL_INFEASIBLE",
    "ERROR": "ERROR",
}


def solve_baseline(
    snapshot: ScenarioInputSnapshot,
    evaluation: ScenarioEvaluation,
    parameters: SolverParameters,
) -> SolveResult:
    candidates = [c for e in evaluation.evaluations for c in e.candidates]

    if not candidates:
        # No candidate pair is not an infeasible model: it is a valid run with
        # zero assignments, and 02 §4 forbids showing it as MODEL_INFEASIBLE.
        return SolveResult(
            solver_status="OPTIMAL",
            run_state="SOLVED_OPTIMAL",
            is_optimal=True,
            assignments=[],
            order_outcomes=_build_outcomes(evaluation, {}),
            objective_values={"assigned_count": 0, "priority_score": 0},
        )

    model = cp_model.CpModel()
    x: dict[tuple[str, str], cp_model.IntVar] = {
        (c.order_id, c.slot_id): model.NewBoolVar(f"x_{c.order_id}_{c.slot_id}")
        for c in candidates
    }
    by_order: dict[str, list[Candidate]] = {}
    by_slot: dict[str, list[Candidate]] = {}
    by_wagon: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_order.setdefault(c.order_id, []).append(c)
        by_slot.setdefault(c.slot_id, []).append(c)
        by_wagon.setdefault(c.wagon_id, []).append(c)

    for order_id, group in by_order.items():
        model.Add(sum(x[(order_id, c.slot_id)] for c in group) <= 1)

    for slot_id, group in by_slot.items():
        model.Add(sum(x[(c.order_id, slot_id)] for c in group) <= 1)

    orders = {o.order_id: o for o in snapshot.orders}
    for wagon_id, group in by_wagon.items():
        wagon = next(w for w in snapshot.wagons if w.wagon_id == wagon_id)
        model.Add(
            sum(
                x[(c.order_id, c.slot_id)] * (orders[c.order_id].gross_weight_kg or 0)
                for c in group
            )
            <= wagon.max_total_weight_kg
        )

    solver = _build_solver(parameters)
    budget = _Budget(parameters.max_time_seconds)
    stages_optimal = True
    objective_values: dict[str, int] = {}
    # The best complete solution proved so far, kept because a later stage that
    # runs out of time must fall back to it rather than to nothing.
    best: dict[tuple[str, str], int] | None = None

    def snapshot_solution() -> dict[tuple[str, str], int]:
        return {key: solver.Value(var) for key, var in x.items()}

    # 1. Assign as many orders as possible.
    assigned_count = sum(x.values())
    status, optimal = _optimise(solver, model, assigned_count, budget, maximise=True)
    if status == cp_model.INFEASIBLE:
        return _infeasible(evaluation)
    if not _usable(status):
        # Nothing was proved at all, so there is no plan to report. Saying
        # "zero assignments" here would be a fabricated answer.
        return _errored(evaluation)
    stages_optimal &= optimal
    best = snapshot_solution()
    value = int(round(solver.ObjectiveValue()))
    objective_values["assigned_count"] = value
    model.Add(assigned_count == value)

    # 2. Prefer higher priority classes.
    scores = snapshot.policy.priority_scores
    priority_score = sum(
        var * getattr(scores, orders[order_id].priority_class)
        for (order_id, _), var in x.items()
    )
    # 3. Prefer the tighter due times.
    due_cost = sum(
        var * _seconds_from(snapshot, orders[order_id].due_at)
        for (order_id, _), var in x.items()
    )
    # 4. Canonical order/slot tie-break, applied one order at a time so the
    #    result is the lexicographically smallest assignment rather than
    #    whichever equal-cost solution the search happened to reach first.
    slot_rank = {
        slot_id: rank for rank, slot_id in enumerate(sorted(by_slot), start=1)
    }
    unassigned_penalty = len(slot_rank) + 1

    def canonical_cost(order_id: str):
        group = by_order[order_id]
        assigned = sum(x[(order_id, c.slot_id)] for c in group)
        ranked = sum(x[(order_id, c.slot_id)] * slot_rank[c.slot_id] for c in group)
        return ranked + unassigned_penalty * (1 - assigned)

    refinements: list[tuple[str | None, object, bool]] = [
        ("priority_score", priority_score, True),
        ("due_cost", due_cost, False),
    ] + [(None, canonical_cost(order_id), False) for order_id in sorted(by_order)]

    for name, expression, maximise in refinements:
        if budget.exhausted():
            # Out of time. The plan already proved is still valid, just not
            # refined all the way, so report it as FEASIBLE rather than
            # discarding it or claiming optimality.
            stages_optimal = False
            break

        status, optimal = _optimise(solver, model, expression, budget, maximise=maximise)
        if status == cp_model.INFEASIBLE:
            return _infeasible(evaluation)
        if not _usable(status):
            stages_optimal = False
            break

        stages_optimal &= optimal
        best = snapshot_solution()
        value = int(round(solver.ObjectiveValue()))
        if name:
            objective_values[name] = value
        model.Add(expression == value)

    assignment_by_order: dict[str, Candidate] = {}
    for c in candidates:
        if best[(c.order_id, c.slot_id)]:
            assignment_by_order[c.order_id] = c

    assignments = [
        Assignment(
            order_id=c.order_id,
            service_id=c.service_id,
            wagon_id=c.wagon_id,
            slot_id=c.slot_id,
        )
        for c in sorted(
            assignment_by_order.values(),
            key=lambda c: (c.order_id, c.service_id, c.wagon_id, c.slot_id),
        )
    ]

    solver_status = "OPTIMAL" if stages_optimal else "FEASIBLE"
    return SolveResult(
        solver_status=solver_status,
        run_state=_RUN_STATE_BY_SOLVER_STATUS[solver_status],
        is_optimal=stages_optimal,
        assignments=assignments,
        order_outcomes=_build_outcomes(evaluation, assignment_by_order),
        objective_values=objective_values,
    )


def _build_solver(parameters: SolverParameters) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = parameters.random_seed
    solver.parameters.num_search_workers = parameters.num_search_workers
    return solver


class _Budget:
    """One wall-clock budget shared by every stage.

    The lexicographic solve runs 3 fixed stages plus one per order, so setting
    `max_time_seconds` on the solver would let a request that asked for 10s
    take 10s *per stage*. The caller asked for a bound on the request.
    """

    def __init__(self, total_seconds: int) -> None:
        self._deadline = time.monotonic() + total_seconds

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def exhausted(self) -> bool:
        # Below this there is no point starting another solve.
        return self.remaining() <= _MIN_STAGE_SECONDS


def _optimise(
    solver: cp_model.CpSolver,
    model: cp_model.CpModel,
    expression,
    budget: _Budget,
    *,
    maximise: bool,
) -> tuple[int, bool]:
    if maximise:
        model.Maximize(expression)
    else:
        model.Minimize(expression)
    solver.parameters.max_time_in_seconds = max(_MIN_STAGE_SECONDS, budget.remaining())
    status = solver.Solve(model)
    return status, status == cp_model.OPTIMAL


def _usable(status: int) -> bool:
    """Whether this status came with a solution worth reading.

    UNKNOWN is the trap: it is not INFEASIBLE, but `ObjectiveValue()` still
    answers (-0.0), so treating "not infeasible" as "solved" pins a fabricated
    objective. A timed-out solve would be recorded as a plan assigning nothing.
    """
    return status in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def _seconds_from(snapshot: ScenarioInputSnapshot, moment) -> int:
    return int((moment - snapshot.as_of).total_seconds())


def _errored(evaluation: ScenarioEvaluation) -> SolveResult:
    """No solution was proved, so there is no plan to report."""
    return SolveResult(
        solver_status="ERROR",
        run_state="ERROR",
        is_optimal=False,
        assignments=[],
        order_outcomes=_build_outcomes(evaluation, {}),
        objective_values={},
    )


def _infeasible(evaluation: ScenarioEvaluation) -> SolveResult:
    return SolveResult(
        solver_status="INFEASIBLE",
        run_state="MODEL_INFEASIBLE",
        is_optimal=False,
        assignments=[],
        order_outcomes=_build_outcomes(evaluation, {}),
        objective_values={},
    )


def _build_outcomes(
    evaluation: ScenarioEvaluation,
    assignment_by_order: dict[str, Candidate],
) -> list[OrderOutcome]:
    outcomes: list[OrderOutcome] = []

    for e in sorted(evaluation.evaluations, key=lambda e: e.order_id):
        if e.input_state == "REVIEW_REQUIRED":
            outcomes.append(
                OrderOutcome(
                    order_id=e.order_id,
                    input_state=e.input_state,
                    eligibility_state=e.eligibility_state,
                    assignment_state=NOT_APPLICABLE,
                    alternative_state="NOT_SEARCHED",
                    primary_reason_code=e.primary_reason_code or rc.MISSING_REQUIRED_FIELD,
                    evidence={"missing_fields": e.missing_fields},
                    next_actions=["COMPLETE_REQUIRED_FIELDS"],
                )
            )
            continue

        if e.eligibility_state == "INELIGIBLE":
            outcomes.append(
                OrderOutcome(
                    order_id=e.order_id,
                    input_state=e.input_state,
                    eligibility_state=e.eligibility_state,
                    assignment_state=NOT_APPLICABLE,
                    alternative_state="NOT_SEARCHED",
                    primary_reason_code=e.primary_reason_code or rc.NO_ELIGIBLE_SLOT,
                    evidence={"reason_codes": e.reason_codes, "by_service": e.evidence},
                    next_actions=["REVIEW_ALTERNATIVE"],
                )
            )
            continue

        if e.order_id in assignment_by_order:
            outcomes.append(
                OrderOutcome(
                    order_id=e.order_id,
                    input_state=e.input_state,
                    eligibility_state=e.eligibility_state,
                    assignment_state=ASSIGNED,
                    alternative_state="NOT_SEARCHED",
                    primary_reason_code=rc.ASSIGNED,
                    evidence={"eligible_slot_ids": e.eligible_slot_ids},
                )
            )
            continue

        # Eligible but the solver could not fit it: capacity, by definition.
        outcomes.append(
            OrderOutcome(
                order_id=e.order_id,
                input_state=e.input_state,
                eligibility_state=e.eligibility_state,
                assignment_state=UNASSIGNED,
                alternative_state="NOT_SEARCHED",
                primary_reason_code=rc.CAPACITY_CONFLICT,
                evidence={"eligible_slot_ids": e.eligible_slot_ids},
                next_actions=["REVIEW_NEXT_SERVICE"],
            )
        )

    return outcomes
