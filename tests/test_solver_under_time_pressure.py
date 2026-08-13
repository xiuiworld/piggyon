"""TC-17: what the solver does when it runs out of time.

The canonical fixture always finishes in milliseconds, so nothing here is
reachable through it. That is exactly why these paths shipped wrong: a
timed-out solve was reported as a plan that assigns nothing, and the time
limit applied per stage rather than to the request.
"""

from __future__ import annotations

import time

import pytest
from ortools.sat.python import cp_model

from app.rules.eligibility import evaluate_scenario
from app.solver.baseline import (
    SolverParameters,
    _Budget,
    _usable,
    solve_baseline,
)


def test_unknown_status_is_not_treated_as_a_solution() -> None:
    """`ObjectiveValue()` still answers on UNKNOWN, so "not INFEASIBLE" is
    not the same as "solved"."""
    assert _usable(cp_model.OPTIMAL) is True
    assert _usable(cp_model.FEASIBLE) is True
    assert _usable(cp_model.UNKNOWN) is False
    assert _usable(cp_model.MODEL_INVALID) is False


def test_timed_out_solve_reports_error_not_an_empty_plan(monkeypatch, snapshot) -> None:
    """A solver that proves nothing must not yield "no orders can be assigned".

    That reading is indistinguishable from a real verdict, and the operator
    would act on it.
    """
    import app.solver.baseline as baseline

    monkeypatch.setattr(
        baseline, "_optimise", lambda *a, **k: (cp_model.UNKNOWN, False)
    )

    result = solve_baseline(snapshot, evaluate_scenario(snapshot), SolverParameters())

    assert result.solver_status == "ERROR"
    assert result.run_state == "ERROR"
    assert result.is_optimal is False
    assert result.assignments == []
    # Not MODEL_INFEASIBLE: nothing proved the model has no solution.
    assert result.run_state != "MODEL_INFEASIBLE"


def test_refinement_timeout_keeps_the_plan_already_proved(monkeypatch, snapshot) -> None:
    """Stage 1 succeeded, so its plan stands; later stages just stop."""
    import app.solver.baseline as baseline

    real = baseline._optimise
    calls = {"n": 0}

    def flaky(solver, model, expression, budget, *, maximise):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(solver, model, expression, budget, maximise=maximise)
        return (cp_model.UNKNOWN, False)

    monkeypatch.setattr(baseline, "_optimise", flaky)

    result = solve_baseline(snapshot, evaluate_scenario(snapshot), SolverParameters())

    assert result.solver_status == "FEASIBLE"
    assert result.run_state == "SOLVED_FEASIBLE"
    assert result.is_optimal is False
    # Stage 1 maximised the count, so the plan it found is still a real plan.
    assert len(result.assignments) == 3


def test_budget_is_shared_across_stages() -> None:
    """A request asking for N seconds must not spend N per stage."""
    budget = _Budget(0)

    assert budget.exhausted() is True
    assert budget.remaining() <= 0


def test_whole_solve_respects_the_requested_limit(snapshot) -> None:
    """13 stages at the old per-stage limit could have run 13x over."""
    started = time.monotonic()

    solve_baseline(
        snapshot,
        evaluate_scenario(snapshot),
        SolverParameters(max_time_seconds=1),
    )

    # Generous headroom for model building; the point is it is not 13 seconds.
    assert time.monotonic() - started < 5


def test_feasible_run_cannot_be_accepted(client, validated_scenario_id, solver_parameters):
    """TC-17 contract: FEASIBLE + PASS is holdable, never acceptable."""
    run = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()

    stored = client.app.state.store.get_run(run["run_id"])
    stored["solver_status"] = "FEASIBLE"
    stored["run_state"] = "SOLVED_FEASIBLE"
    stored["is_optimal"] = False
    client.app.state.store.save_run(stored)

    body = {
        "actor_role": "SCHEDULING_OPERATOR",
        "reason": "최적성 미확정이라 보류한다.",
        "selected_plan": "BASELINE",
    }
    accepted = client.post(
        f"/v1/runs/{run['run_id']}/decisions", json={**body, "decision_state": "ACCEPTED"}
    )
    held = client.post(
        f"/v1/runs/{run['run_id']}/decisions", json={**body, "decision_state": "HELD"}
    )
    rejected = client.post(
        f"/v1/runs/{run['run_id']}/decisions", json={**body, "decision_state": "REJECTED"}
    )

    assert accepted.status_code == 409
    assert accepted.json()["code"] == "RUN_NOT_ACCEPTABLE"
    assert held.status_code == 201
    assert rejected.status_code == 201
