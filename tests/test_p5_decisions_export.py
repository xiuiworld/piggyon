"""P5 gate: operator decisions, storage and the export bundle."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def baseline_run_id(client: TestClient, validated_scenario_id: str, solver_parameters: dict) -> str:
    return client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]


def _decision(**overrides) -> dict:
    return {
        "decision_state": "HELD",
        "actor_role": "SCHEDULING_OPERATOR",
        "reason": "ORD-005 대안의 실제 반입 가능 여부를 확인한다.",
        "selected_plan": "BASELINE",
        **overrides,
    }


def test_decision_is_recorded(client: TestClient, baseline_run_id: str) -> None:
    response = client.post(
        f"/v1/runs/{baseline_run_id}/decisions", json=_decision()
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["decision_state"] == "HELD"
    assert body["run_id"] == baseline_run_id
    assert body["decision_id"]


def test_optimal_and_pass_run_can_be_accepted(
    client: TestClient, baseline_run_id: str
) -> None:
    response = client.post(
        f"/v1/runs/{baseline_run_id}/decisions", json=_decision(decision_state="ACCEPTED")
    )

    assert response.status_code == 201


def test_non_optimal_run_cannot_be_accepted(
    client: TestClient, baseline_run_id: str
) -> None:
    """TC-17 / 04 §8: FEASIBLE may be held or rejected, never accepted."""
    run = client.app.state.store.get_run(baseline_run_id)
    run["solver_status"] = "FEASIBLE"
    run["run_state"] = "SOLVED_FEASIBLE"
    run["is_optimal"] = False
    client.app.state.store.save_run(run)

    accepted = client.post(
        f"/v1/runs/{baseline_run_id}/decisions", json=_decision(decision_state="ACCEPTED")
    )
    held = client.post(f"/v1/runs/{baseline_run_id}/decisions", json=_decision())

    assert accepted.status_code == 409
    assert accepted.json()["code"] == "RUN_NOT_ACCEPTABLE"
    assert held.status_code == 201


def test_validator_fail_blocks_acceptance(
    client: TestClient, baseline_run_id: str
) -> None:
    run = client.app.state.store.get_run(baseline_run_id)
    run["validator_status"] = "FAIL"
    client.app.state.store.save_run(run)

    response = client.post(
        f"/v1/runs/{baseline_run_id}/decisions", json=_decision(decision_state="ACCEPTED")
    )

    assert response.status_code == 409
    assert response.json()["code"] == "RUN_NOT_ACCEPTABLE"


def test_decision_on_unknown_run_is_404(client: TestClient) -> None:
    response = client.post("/v1/runs/RUN-NOPE/decisions", json=_decision())

    assert response.status_code == 404
    assert response.json()["code"] == "RUN_NOT_FOUND"


def test_export_bundle_has_every_section(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    """TC-18: one bundle carries input, policy, result, validation and trace."""
    client.post(f"/v1/runs/{baseline_run_id}/decisions", json=_decision())
    client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    )

    response = client.get(f"/v1/runs/{baseline_run_id}/export")

    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["scenario"]["scenario_id"]
    assert len(bundle["input_snapshot"]["orders"]) == 9
    assert bundle["policy"]["policy_version"] == "1.0.0"
    assert bundle["run"]["reproducibility"]["result_sha256"]
    assert bundle["validation_result"]["validation_status"] == "COMPLETED"
    assert len(bundle["decisions"]) == 1
    event_types = {e["event_type"] for e in bundle["trace_events"]}
    assert {"SCENARIO_CREATED", "VALIDATION_COMPLETED", "RUN_COMPLETED",
            "ALTERNATIVE_CREATED", "DECISION_RECORDED"} <= event_types


def test_export_of_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/v1/runs/RUN-NOPE/export")

    assert response.status_code == 404
