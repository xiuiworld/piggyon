"""P0 gate: the canonical fixture is accepted, stored, and echoed as a scenario_id."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.canonical import load_canonical_snapshot


def test_health_reports_reachable_storage(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["storage_reachable"] is True


def test_canonical_scenario_is_created(client: TestClient, request_body: dict) -> None:
    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["scenario_id"].startswith("SCN-")
    assert body["state"] == "VALIDATION_REQUIRED"
    assert body["created_at"]
    assert response.headers["Location"] == f"/v1/scenarios/{body['scenario_id']}"


def test_snapshot_round_trips_through_the_store(
    client: TestClient, request_body: dict
) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    stored = client.app.state.store.get_scenario(scenario_id)

    assert stored is not None
    snapshot = stored["input_snapshot"]
    assert len(snapshot["orders"]) == 9
    assert len(snapshot["slots"]) == 7
    assert len(snapshot["services"]) == 3
    # The one permitted null survives storage; P1 turns it into REVIEW_REQUIRED.
    ord_006 = next(o for o in snapshot["orders"] if o["order_id"] == "ORD-006")
    assert ord_006["gross_weight_kg"] is None


def test_stored_scenario_is_readable_back(
    client: TestClient, request_body: dict
) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    response = client.get(f"/v1/scenarios/{scenario_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scenario_id"] == scenario_id
    assert body["state"] == "VALIDATION_REQUIRED"
    assert body["scenario_name"] == request_body["scenario_name"]
    assert body["baseline_service_ids"] == request_body["baseline_service_ids"]
    assert body["policy_version"] == request_body["policy_version"]
    assert body["assumption_ids"] == request_body["assumption_ids"]
    # The point of the endpoint: what comes back is the document that was sent,
    # not a re-serialisation of it. A client can re-submit this and reproduce
    # the same input_snapshot_sha256.
    assert body["input_snapshot"] == request_body["input_snapshot"]


def test_reading_a_scenario_reflects_its_state(
    client: TestClient, validated_scenario_id: str
) -> None:
    body = client.get(f"/v1/scenarios/{validated_scenario_id}").json()

    assert body["state"] == "READY_TO_SOLVE"


def test_reading_an_unknown_scenario_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/scenarios/SCN-NOPE")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "SCENARIO_NOT_FOUND"
    assert body["trace_id"]


def test_scenario_ids_are_unique_per_request(
    client: TestClient, request_body: dict
) -> None:
    first = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    second = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    assert first != second


def test_missing_required_key_is_invalid_input(
    client: TestClient, request_body: dict
) -> None:
    """A *missing* gross_weight_kg is a 400 even though null is allowed (04 §11)."""
    order = request_body["input_snapshot"]["orders"][0]
    del order["gross_weight_kg"]

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert body["trace_id"]
    assert any("gross_weight_kg" in d["location"] for d in body["details"])


def test_unknown_reference_is_rejected(client: TestClient, request_body: dict) -> None:
    request_body["input_snapshot"]["orders"][0]["destination_terminal_ids"] = ["TRM-ZZ"]

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_policy_version_mismatch_is_rejected(
    client: TestClient, request_body: dict
) -> None:
    request_body["policy_version"] = "9.9.9"

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert any(d["location"] == "policy_version" for d in body["details"])


def test_baseline_service_must_exist_in_snapshot(
    client: TestClient, request_body: dict
) -> None:
    request_body["baseline_service_ids"] = ["SVC-DOES-NOT-EXIST"]

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_unknown_field_is_rejected(client: TestClient, request_body: dict) -> None:
    request_body["input_snapshot"]["orders"][0]["gross_weigth_kg"] = 1000

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_published_contract_advertises_400_not_422(client: TestClient) -> None:
    """The front end codes against /openapi.json, so it must match 04 §9."""
    spec = client.get("/openapi.json").json()

    responses = spec["paths"]["/v1/scenarios"]["post"]["responses"]
    assert "400" in responses
    assert "422" not in responses


def test_canonical_snapshot_matches_the_documented_scale() -> None:
    """03 §2 and §8: the fixture's shape is part of the contract."""
    snapshot = load_canonical_snapshot()

    assert len(snapshot["shippers"]) == 5
    assert len(snapshot["terminals"]) == 3
    assert len(snapshot["services"]) == 3
    assert len(snapshot["wagons"]) == 3
    assert len(snapshot["slots"]) == 7
    assert len(snapshot["orders"]) == 9
    assert snapshot["baseline_service_ids"] == ["SVC-AM-01"]
    # 03 §8: only three baseline slots, which is what makes ORD-004 contend.
    baseline_slots = [s for s in snapshot["slots"] if s["wagon_id"] == "WGN-AM-01"]
    assert len(baseline_slots) == 3
