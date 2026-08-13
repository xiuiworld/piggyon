"""Reading a validation back, deleting a scenario, and the decision on a row.

All three exist because a screen needed them and had to misuse something else
instead: validating to read, listing to guess whether a scenario was settled,
and nothing at all for a scenario nobody wants any more.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _solved(client: TestClient, request_body: dict, solver_parameters: dict) -> tuple[str, str]:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    run_id = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]
    return scenario_id, run_id


def test_validation_can_be_read_without_recording_another_event(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id, run_id = _solved(client, request_body, solver_parameters)

    def validation_events() -> int:
        bundle = client.get(f"/v1/runs/{run_id}/export").json()
        return sum(1 for e in bundle["trace_events"] if e["event_type"] == "VALIDATION_COMPLETED")

    before = validation_events()
    read = client.get(f"/v1/scenarios/{scenario_id}/validation")

    assert read.status_code == 200
    assert [o["order_id"] for o in read.json()["orders"]] == [
        o["order_id"] for o in client.post(f"/v1/scenarios/{scenario_id}/validate").json()["orders"]
    ]
    # The POST above deliberately adds one. Reading must not have.
    assert validation_events() == before + 1


def test_reading_before_validating_says_so(client: TestClient, request_body: dict) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    refused = client.get(f"/v1/scenarios/{scenario_id}/validation")

    # 04 §1 reserves 422 for this code specifically.
    assert refused.status_code == 422
    assert refused.json()["code"] == "VALIDATION_REQUIRED"


def test_a_row_carries_the_decision_standing_on_its_run(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id, run_id = _solved(client, request_body, solver_parameters)

    def row() -> dict:
        return next(
            r for r in client.get("/v1/scenarios").json() if r["scenario_id"] == scenario_id
        )

    assert row()["decision_state"] is None

    for state in ("HELD", "ACCEPTED"):
        client.post(
            f"/v1/runs/{run_id}/decisions",
            json={
                "decision_state": state,
                "actor_role": "SCHEDULING_OPERATOR",
                "reason": "테스트",
                "selected_plan": "BASELINE",
            },
        )

    # The last one, not the first: a run can be held and then accepted, and the
    # row must not say the opposite of where the scenario ended up.
    assert row()["decision_state"] == "ACCEPTED"


def test_deleting_removes_the_scenario_and_its_run(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id, run_id = _solved(client, request_body, solver_parameters)

    assert client.delete(f"/v1/scenarios/{scenario_id}").status_code == 204

    assert client.get(f"/v1/scenarios/{scenario_id}").status_code == 404
    assert client.get(f"/v1/runs/{run_id}").status_code == 404
    assert scenario_id not in [r["scenario_id"] for r in client.get("/v1/scenarios").json()]


def test_deleting_a_parent_is_refused(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id, run_id = _solved(client, request_body, solver_parameters)
    derived_id = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    ).json()["alternative_scenario_id"]

    refused = client.delete(f"/v1/scenarios/{scenario_id}")

    assert refused.status_code == 409
    assert refused.json()["code"] == "POLICY_VIOLATION"
    assert refused.json()["details"][0]["derived_scenario_id"] == derived_id
    # Nothing was removed on the way to refusing.
    assert client.get(f"/v1/scenarios/{scenario_id}").status_code == 200

    # The child goes first, and then the parent can go.
    assert client.delete(f"/v1/scenarios/{derived_id}").status_code == 204
    assert client.delete(f"/v1/scenarios/{scenario_id}").status_code == 204


def test_deleting_an_unknown_scenario_is_a_404(client: TestClient) -> None:
    assert client.delete("/v1/scenarios/SCN-999").status_code == 404
