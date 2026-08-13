"""P2 gate: baseline CP-SAT assignment."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.rules import reason_codes as rc
from app.rules.eligibility import evaluate_scenario
from app.solver.baseline import SolverParameters, solve_baseline
from app.validation.plan_validator import validate_plan


def _solve(snapshot):
    evaluation = evaluate_scenario(snapshot)
    return solve_baseline(snapshot, evaluation, SolverParameters())


def test_assignments_match_the_fixture(snapshot, expected) -> None:
    result = _solve(snapshot)

    assert [a.as_dict() for a in result.assignments] == expected["baseline"]["assignments"]


def test_solver_reaches_optimal(snapshot) -> None:
    result = _solve(snapshot)

    assert result.solver_status == "OPTIMAL"
    assert result.run_state == "SOLVED_OPTIMAL"
    assert result.is_optimal is True


def test_order_outcomes_match_the_fixture(snapshot, expected) -> None:
    result = _solve(snapshot)
    want = expected["baseline"]["order_outcomes"]

    for outcome in result.order_outcomes:
        w = want[outcome.order_id]
        assert outcome.input_state == w["input_state"], outcome.order_id
        assert outcome.eligibility_state == w["eligibility_state"], outcome.order_id
        assert outcome.assignment_state == w["assignment_state"], outcome.order_id
        assert outcome.alternative_state == w["alternative_state"], outcome.order_id
        assert outcome.primary_reason_code == w["primary_reason_code"], outcome.order_id


def test_capacity_conflict_drops_the_lowest_priority_order(snapshot) -> None:
    """TC-08: four eligible orders, three slots. P3 is the one that loses."""
    result = _solve(snapshot)

    unassigned = [o for o in result.order_outcomes if o.assignment_state == "UNASSIGNED"]
    assert [o.order_id for o in unassigned] == ["ORD-004"]
    assert unassigned[0].primary_reason_code == rc.CAPACITY_CONFLICT
    assert unassigned[0].eligibility_state == "ELIGIBLE"


def test_objective_stages_are_lexicographic(snapshot) -> None:
    result = _solve(snapshot)

    # Three of four eligible orders fit; the kept set is P1+P1+P2 = 3+3+2.
    assert result.objective_values["assigned_count"] == 3
    assert result.objective_values["priority_score"] == 8


def test_canonical_tie_break_is_stable(snapshot) -> None:
    """TC-11: equal objective values must resolve the same way every time."""
    first = [a.as_dict() for a in _solve(snapshot).assignments]

    for _ in range(3):
        assert [a.as_dict() for a in _solve(snapshot).assignments] == first


def test_plan_validator_passes_the_baseline(snapshot) -> None:
    result = _solve(snapshot)

    validation = validate_plan(snapshot, result.assignments, result.order_outcomes)

    assert validation.status == "PASS", validation.findings


def test_plan_validator_rejects_a_duplicated_slot(snapshot) -> None:
    """The validator has to catch what a broken solver would emit."""
    result = _solve(snapshot)
    tampered = list(result.assignments)
    tampered[1].slot_id = tampered[0].slot_id

    validation = validate_plan(snapshot, tampered, result.order_outcomes)

    assert validation.status == "FAIL"
    assert any(f.check == "duplicate_slot" for f in validation.findings)


def test_plan_validator_rejects_an_out_of_scope_service(snapshot) -> None:
    """TC-14: a baseline plan may not use a service outside the baseline set."""
    result = _solve(snapshot)
    tampered = list(result.assignments)
    tampered[0].service_id = "SVC-NEXT-01"

    validation = validate_plan(snapshot, tampered, result.order_outcomes)

    assert validation.status == "FAIL"
    assert any(f.check in {"service_scope", "topology"} for f in validation.findings)


def test_zero_candidates_is_a_valid_run_not_infeasible(snapshot) -> None:
    """02 §4: an empty plan is OPTIMAL with no assignments, not MODEL_INFEASIBLE."""
    payload = snapshot.model_dump(mode="json")
    for slot in payload["slots"]:
        slot["available"] = False

    from app.models.snapshot import ScenarioInputSnapshot

    empty = ScenarioInputSnapshot.model_validate(payload)
    result = solve_baseline(empty, evaluate_scenario(empty), SolverParameters())

    assert result.assignments == []
    assert result.solver_status == "OPTIMAL"
    assert result.run_state == "SOLVED_OPTIMAL"


# --- HTTP contract -----------------------------------------------------------


def test_run_endpoint_returns_the_baseline_plan(
    client: TestClient, validated_scenario_id: str, solver_parameters: dict, expected: dict
) -> None:
    response = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["solver_status"] == "OPTIMAL"
    assert body["run_state"] == "SOLVED_OPTIMAL"
    assert body["validator_status"] == "PASS"
    assert body["assignments"] == expected["baseline"]["assignments"]


def test_run_requires_validation_first(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    response = client.post(
        f"/v1/scenarios/{scenario_id}/runs", json={"solver_parameters": solver_parameters}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_REQUIRED"


def test_multiple_workers_are_rejected(
    client: TestClient, validated_scenario_id: str, solver_parameters: dict
) -> None:
    """04 §10: only a single worker reproduces the tie-break and the hashes."""
    solver_parameters["num_search_workers"] = 4

    response = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_reproducibility_hashes_are_present_and_stable(
    client: TestClient, validated_scenario_id: str, solver_parameters: dict
) -> None:
    """TC-16: same input, same seed, single worker -> same result hash."""
    first = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()
    second = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()

    assert first["run_id"] != second["run_id"]
    assert first["reproducibility"] == second["reproducibility"]


def test_run_can_be_read_back(
    client: TestClient, validated_scenario_id: str, solver_parameters: dict
) -> None:
    created = client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()

    fetched = client.get(f"/v1/runs/{created['run_id']}")

    assert fetched.status_code == 200
    assert fetched.json()["assignments"] == created["assignments"]


def test_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/v1/runs/RUN-NOPE")

    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"
