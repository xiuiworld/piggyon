"""P3: conditional alternatives.

The differentiator (01/08 scene 4): when an order cannot be placed, say what
*approved* change would let it be reconsidered. A change outside the policy's
allowed list is refused rather than quietly applied, and the baseline plan is
never overwritten — the alternative is a derived scenario of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.hashing import result_sha256, sha256_of
from app.models.snapshot import ScenarioInputSnapshot
from app.rules import reason_codes as rc
from app.rules.eligibility import evaluate_scenario
from app.solver.baseline import SolverParameters, solve_baseline
from app.validation.plan_validator import validate_plan

ADD_ORDER_APPROVED_SERVICE = "ADD_ORDER_APPROVED_SERVICE"
CHANGE_TO_APPROVED_TERMINAL = "CHANGE_TO_APPROVED_TERMINAL"

# The destination is settled before the service, because which services can
# carry the order depends on where it is going. The canonical ORD-008
# change_set is recorded in this order regardless of how the client listed it.
CHANGE_SET_ORDER = [CHANGE_TO_APPROVED_TERMINAL, ADD_ORDER_APPROVED_SERVICE]


class PolicyViolation(Exception):
    """A requested adjustment is on the policy's forbidden list."""

    def __init__(self, forbidden: list[str]) -> None:
        super().__init__(", ".join(forbidden))
        self.forbidden = forbidden


@dataclass
class AlternativeOutcome:
    found: bool
    change_set: list[dict[str, Any]]
    reason_code: str | None = None
    alternative_scenario_id: str | None = None
    alternative_run_id: str | None = None
    alternative_snapshot: ScenarioInputSnapshot | None = None
    alternative_run: dict[str, Any] | None = None
    impacted_order_ids: list[str] | None = None
    assignment_deltas: list[dict[str, Any]] | None = None
    alternative_run_order_outcome: dict[str, Any] | None = None
    validator_status: str = "PASS"


def build_change_set(
    snapshot: ScenarioInputSnapshot,
    order_id: str,
    adjustment_types: list[str],
) -> list[dict[str, Any]]:
    """Translate requested adjustment types into concrete, pre-approved changes.

    Only what the order's own `adjustment_window` already authorises can appear
    here, so an alternative can never invent a service or a terminal.
    """
    policy = snapshot.policy
    forbidden = [t for t in adjustment_types if t in policy.forbidden_adjustments]
    if forbidden:
        raise PolicyViolation(sorted(set(forbidden)))

    order = next((o for o in snapshot.orders if o.order_id == order_id), None)
    if order is None or order.adjustment_window is None:
        return []

    window = order.adjustment_window
    requested = set(adjustment_types) & set(policy.allowed_adjustments)
    changes: list[dict[str, Any]] = []

    if CHANGE_TO_APPROVED_TERMINAL in requested:
        for terminal_id in window.alternative_destination_terminal_ids:
            changes.append(
                {"type": CHANGE_TO_APPROVED_TERMINAL, "destination_terminal_id": terminal_id}
            )

    if ADD_ORDER_APPROVED_SERVICE in requested:
        for service_id in window.alternative_service_ids:
            changes.append({"type": ADD_ORDER_APPROVED_SERVICE, "service_id": service_id})

    changes.sort(key=lambda c: (CHANGE_SET_ORDER.index(c["type"]), sorted(c.items())))
    return changes


def derive_snapshot(
    snapshot: ScenarioInputSnapshot,
    scenario_id: str,
    order_id: str,
    change_set: list[dict[str, Any]],
) -> tuple[ScenarioInputSnapshot, list[str]]:
    """Build the derived scenario and the service set opened to this order."""
    payload = snapshot.model_dump(mode="json")
    payload["scenario_id"] = scenario_id
    payload["scenario_type"] = "ALTERNATIVE"

    order_services = list(snapshot.baseline_service_ids)
    permitted = list(snapshot.baseline_service_ids)

    for change in change_set:
        if change["type"] == CHANGE_TO_APPROVED_TERMINAL:
            for order in payload["orders"]:
                if order["order_id"] == order_id:
                    # CHANGE, not ADD: the order now travels to the approved
                    # terminal instead of the original one.
                    order["destination_terminal_ids"] = [change["destination_terminal_id"]]
        elif change["type"] == ADD_ORDER_APPROVED_SERVICE:
            service_id = change["service_id"]
            if service_id not in order_services:
                order_services.append(service_id)
            if service_id not in permitted:
                permitted.append(service_id)

    payload["baseline_service_ids"] = permitted
    return ScenarioInputSnapshot.model_validate(payload), order_services


def search_alternative(
    snapshot: ScenarioInputSnapshot,
    baseline_run: dict[str, Any],
    order_id: str,
    adjustment_types: list[str],
    scenario_id: str,
    run_id: str,
    parameters: SolverParameters,
) -> AlternativeOutcome:
    change_set = build_change_set(snapshot, order_id, adjustment_types)

    if not change_set:
        # Nothing the policy allows would move this order, so the only way
        # forward would be a forbidden change.
        return AlternativeOutcome(
            found=False,
            change_set=[],
            reason_code=rc.ALTERNATIVE_REQUIRES_FORBIDDEN_CHANGE,
        )

    derived, order_services = derive_snapshot(snapshot, scenario_id, order_id, change_set)

    evaluation = evaluate_scenario(
        derived, service_ids_by_order={order_id: order_services}
    )
    result = solve_baseline(derived, evaluation, parameters)
    validation = validate_plan(
        derived,
        result.assignments,
        result.order_outcomes,
        allowed_service_ids=derived.baseline_service_ids,
    )

    assignments = [a.as_dict() for a in result.assignments]
    outcomes = [o.as_dict() for o in result.order_outcomes]
    assigned = {a["order_id"]: a for a in assignments}

    if order_id not in assigned:
        target = next((o for o in result.order_outcomes if o.order_id == order_id), None)
        return AlternativeOutcome(
            found=False,
            change_set=change_set,
            reason_code=(target.primary_reason_code if target else rc.NO_FEASIBLE_ALTERNATIVE),
        )

    deltas = _assignment_deltas(baseline_run.get("assignments", []), assignments)
    impacted = sorted({d["order_id"] for d in deltas})

    target_outcome = next(o for o in outcomes if o["order_id"] == order_id)
    target_outcome["alternative_state"] = "AVAILABLE"
    target_outcome["primary_reason_code"] = rc.ALTERNATIVE_AVAILABLE

    alternative_run = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "parent_run_id": baseline_run["run_id"],
        "solver_status": result.solver_status,
        "run_state": result.run_state,
        "is_optimal": result.is_optimal,
        "validator_status": validation.status,
        "validator_findings": [f.as_dict() for f in validation.findings],
        "reproducibility": {
            "solver_parameters": parameters.as_dict(),
            "input_snapshot_sha256": sha256_of(derived.model_dump(mode="json")),
            "policy_sha256": sha256_of(derived.policy.model_dump(mode="json")),
            "result_sha256": result_sha256(assignments, outcomes),
        },
        "assignments": assignments,
        "order_outcomes": outcomes,
        "objective_values": result.objective_values,
    }

    return AlternativeOutcome(
        found=True,
        change_set=change_set,
        alternative_scenario_id=scenario_id,
        alternative_run_id=run_id,
        alternative_snapshot=derived,
        alternative_run=alternative_run,
        impacted_order_ids=impacted,
        assignment_deltas=deltas,
        alternative_run_order_outcome=target_outcome,
        validator_status=validation.status,
    )


def _assignment_deltas(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every order whose placement changed, with both sides preserved (02 §7)."""
    before_by_order = {a["order_id"]: a for a in before}
    after_by_order = {a["order_id"]: a for a in after}

    deltas: list[dict[str, Any]] = []
    for order_id in sorted(set(before_by_order) | set(after_by_order)):
        was = before_by_order.get(order_id)
        now = after_by_order.get(order_id)

        if was == now:
            continue
        if was is None:
            change_type = "ADDED"
        elif now is None:
            change_type = "UNASSIGNED"
        else:
            change_type = "MOVED"

        deltas.append(
            {
                "order_id": order_id,
                "change_type": change_type,
                "before_assignment": was,
                "after_assignment": now,
            }
        )
    return deltas


def apply_to_baseline(
    baseline_run: dict[str, Any], order_id: str, alternative_state: str,
    alternative_scenario_id: str | None = None,
) -> dict[str, Any]:
    """Record the alternative verdict on the baseline without disturbing it.

    02 §9.5: only `alternative_state` moves. The order keeps the eligibility and
    assignment it had, so the UI shows a 조건부 대안 있음 badge beside the
    original verdict instead of replacing it.
    """
    for outcome in baseline_run.get("order_outcomes", []):
        if outcome["order_id"] == order_id:
            outcome["alternative_state"] = alternative_state
            if alternative_scenario_id:
                outcome["alternative_scenario_id"] = alternative_scenario_id
            return outcome
    return {}
