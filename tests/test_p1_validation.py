"""P1 gate: input validation and the eligibility gate."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.models.snapshot import ScenarioInputSnapshot
from app.rules import reason_codes as rc
from app.rules.eligibility import evaluate_scenario

BASELINE_SLOTS = ["SLT-AM-01", "SLT-AM-02", "SLT-AM-03"]


def test_every_order_matches_the_expected_states(snapshot, expected) -> None:
    outcomes = expected["baseline"]["order_outcomes"]

    for e in evaluate_scenario(snapshot).evaluations:
        want = outcomes[e.order_id]
        assert e.input_state == want["input_state"], e.order_id
        assert e.eligibility_state == want["eligibility_state"], e.order_id


@pytest.mark.parametrize(
    ("order_id", "reason_code"),
    [
        ("ORD-005", rc.READY_AFTER_CUTOFF),
        ("ORD-006", rc.MISSING_REQUIRED_FIELD),
        ("ORD-007", rc.TUNNEL_HEIGHT_EXCEEDED),
        ("ORD-008", rc.TERMINAL_NOT_COMPATIBLE),
        ("ORD-009", rc.DUE_TIME_EXCEEDED),
    ],
)
def test_primary_reason_codes(snapshot, order_id: str, reason_code: str) -> None:
    evaluation = evaluate_scenario(snapshot).by_order_id[order_id]

    assert evaluation.primary_reason_code == reason_code


@pytest.mark.parametrize("order_id", ["ORD-001", "ORD-002", "ORD-003", "ORD-004"])
def test_eligible_orders_get_all_three_baseline_slots(snapshot, order_id: str) -> None:
    evaluation = evaluate_scenario(snapshot).by_order_id[order_id]

    assert evaluation.eligible_slot_ids == BASELINE_SLOTS


def test_review_required_order_gets_no_candidates(snapshot) -> None:
    """02 §9.1: a failed order is excluded from the solve, not guessed at."""
    evaluation = evaluate_scenario(snapshot).by_order_id["ORD-006"]

    assert evaluation.eligible_slot_ids == []
    assert evaluation.missing_fields == ["gross_weight_kg"]


def test_terminal_beats_dimension_in_the_priority_order(snapshot) -> None:
    """ORD-008 breaks both rules; 02 §6.1 says TERMINAL_ outranks DIMENSION_."""
    evaluation = evaluate_scenario(snapshot).by_order_id["ORD-008"]

    assert rc.TERMINAL_NOT_COMPATIBLE in evaluation.reason_codes
    assert rc.TUNNEL_HEIGHT_EXCEEDED in evaluation.reason_codes
    assert evaluation.primary_reason_code == rc.TERMINAL_NOT_COMPATIBLE


def test_service_level_failure_does_not_emit_slot_codes(snapshot) -> None:
    """A slot code here would outrank TUNNEL_HEIGHT_EXCEEDED alphabetically."""
    evaluation = evaluate_scenario(snapshot).by_order_id["ORD-007"]

    assert rc.SLOT_HEIGHT_EXCEEDED not in evaluation.reason_codes
    assert evaluation.primary_reason_code == rc.TUNNEL_HEIGHT_EXCEEDED


def test_ord_008_is_inside_the_cutoff(snapshot) -> None:
    """The cutoff rule excludes origin handling time.

    ORD-008 is ready at 10:00 against a 10:30 cutoff. Adding the terminal's
    45-minute handling would push it over and mask its real cause.
    """
    evaluation = evaluate_scenario(snapshot).by_order_id["ORD-008"]

    assert rc.READY_AFTER_CUTOFF not in evaluation.reason_codes


def test_missing_weight_is_review_required_not_ineligible(snapshot) -> None:
    order = next(o for o in snapshot.orders if o.order_id == "ORD-006")

    assert order.gross_weight_kg is None
    evaluation = evaluate_scenario(snapshot).by_order_id["ORD-006"]
    assert evaluation.input_state == "REVIEW_REQUIRED"
    assert evaluation.eligibility_state == "NOT_EVALUATED"


def test_ready_at_after_due_at_is_an_input_error(snapshot) -> None:
    payload = snapshot.model_dump(mode="json")
    order = next(o for o in payload["orders"] if o["order_id"] == "ORD-001")
    order["ready_at"] = "2026-08-17T20:00:00+09:00"

    mutated = ScenarioInputSnapshot.model_validate(payload)
    evaluation = evaluate_scenario(mutated).by_order_id["ORD-001"]

    assert evaluation.input_state == "REVIEW_REQUIRED"
    assert rc.INVALID_TIME_RANGE in evaluation.reason_codes


# --- HTTP contract -----------------------------------------------------------


def test_validate_endpoint_reports_the_gate(client: TestClient, request_body: dict) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    response = client.post(f"/v1/scenarios/{scenario_id}/validate")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation_status"] == "COMPLETED"

    by_id = {o["order_id"]: o for o in body["orders"]}
    assert by_id["ORD-006"]["input_state"] == "REVIEW_REQUIRED"
    assert by_id["ORD-006"]["missing_fields"] == ["gross_weight_kg"]
    assert by_id["ORD-007"]["primary_reason_code"] == rc.TUNNEL_HEIGHT_EXCEEDED
    assert by_id["ORD-008"]["primary_reason_code"] == rc.TERMINAL_NOT_COMPATIBLE
    assert by_id["ORD-005"]["primary_reason_code"] == rc.READY_AFTER_CUTOFF
    assert by_id["ORD-009"]["primary_reason_code"] == rc.DUE_TIME_EXCEEDED
    for order_id in ("ORD-001", "ORD-002", "ORD-003", "ORD-004"):
        assert by_id[order_id]["eligible_slot_ids"] == BASELINE_SLOTS


def test_validate_moves_the_scenario_to_ready_to_solve(
    client: TestClient, request_body: dict
) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    client.post(f"/v1/scenarios/{scenario_id}/validate")

    assert client.app.state.store.get_scenario(scenario_id)["state"] == "READY_TO_SOLVE"


def test_validate_unknown_scenario_is_404(client: TestClient) -> None:
    response = client.post("/v1/scenarios/SCN-NOPE/validate")

    assert response.status_code == 404
    assert response.json()["code"] == "SCENARIO_NOT_FOUND"
