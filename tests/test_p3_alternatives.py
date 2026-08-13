"""P3 gate: conditional alternatives."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def baseline_run_id(client: TestClient, validated_scenario_id: str, solver_parameters: dict) -> str:
    return client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]


@pytest.mark.parametrize("order_id", ["ORD-005", "ORD-008"])
def test_approved_alternative_is_found(
    client: TestClient, baseline_run_id: str, expected: dict, order_id: str
) -> None:
    want = expected["alternatives"][order_id]

    response = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives", json=want["request"]
    )

    assert response.status_code == want["http_status"], response.text
    body = response.json()
    assert body["change_set"] == want["change_set"]
    assert body["impacted_order_ids"] == want["impacted_order_ids"]
    assert body["assignment_deltas"] == want["assignment_deltas"]
    assert body["validator_status"] == "PASS"


@pytest.mark.parametrize("order_id", ["ORD-005", "ORD-008"])
def test_alternative_only_touches_the_requested_order(
    client: TestClient, baseline_run_id: str, expected: dict, order_id: str
) -> None:
    """The approved service is opened to the requesting order alone.

    Widening the baseline globally would let ORD-004 take a slot on the new
    service and report a second impacted order.
    """
    response = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"][order_id]["request"],
    )

    assert response.json()["impacted_order_ids"] == [order_id]


@pytest.mark.parametrize("order_id", ["ORD-005", "ORD-008"])
def test_baseline_keeps_its_verdict_and_gains_a_badge(
    client: TestClient, baseline_run_id: str, expected: dict, order_id: str
) -> None:
    """02 §9.5: only alternative_state moves; the main state is preserved."""
    want = expected["alternatives"][order_id]["baseline_order_update"]

    body = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"][order_id]["request"],
    ).json()

    update = body["baseline_order_update"]
    assert update["alternative_state"] == "AVAILABLE"
    assert update["eligibility_state"] == want["eligibility_state"]
    assert update["assignment_state"] == want["assignment_state"]
    assert update["primary_reason_code"] == want["primary_reason_code"]


def test_alternative_run_outcome_matches(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    want = expected["alternatives"]["ORD-005"]["alternative_run_order_outcome"]

    body = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    outcome = body["alternative_run_order_outcome"]
    for key in ("input_state", "eligibility_state", "assignment_state",
                "alternative_state", "primary_reason_code"):
        assert outcome[key] == want[key], key


@pytest.mark.parametrize("order_id", ["ORD-007", "ORD-009"])
def test_no_feasible_alternative(
    client: TestClient, baseline_run_id: str, expected: dict, order_id: str
) -> None:
    want = expected["alternatives"][order_id]

    response = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives", json=want["request"]
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "NO_FEASIBLE_ALTERNATIVE"
    assert body["alternative_state"] == "NONE"
    assert body["change_set"] == want["change_set"]
    assert body["reason_code"] == want["reason_code"]
    assert body["baseline_order_update"]["alternative_state"] == "NONE"


def test_order_without_a_window_reports_forbidden_change(
    client: TestClient, baseline_run_id: str
) -> None:
    """ORD-007 has no adjustment_window: only a banned change could help it."""
    body = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json={"order_id": "ORD-007", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    ).json()

    assert body["change_set"] == []
    assert body["reason_code"] == "ALTERNATIVE_REQUIRES_FORBIDDEN_CHANGE"


def test_forbidden_change_is_rejected(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    """TC-10: a banned relaxation is refused outright."""
    case = expected["negative_api_cases"]["TC-10"]

    response = client.post(f"/v1/runs/{baseline_run_id}/alternatives", json=case["request"])

    assert response.status_code == case["http_status"]
    assert response.json()["code"] == case["error_code"]


def test_forbidden_request_does_not_touch_the_baseline(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    """TC-10 again: the refusal leaves the existing state alone."""
    before = client.get(f"/v1/runs/{baseline_run_id}").json()["order_outcomes"]

    client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["negative_api_cases"]["TC-10"]["request"],
    )

    assert client.get(f"/v1/runs/{baseline_run_id}").json()["order_outcomes"] == before


def test_alternative_run_is_a_separate_scenario(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    """07 §1: the baseline and the alternative must not share a scenario id."""
    body = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    baseline = client.get(f"/v1/runs/{baseline_run_id}").json()
    alternative = client.get(f"/v1/runs/{body['alternative_run_id']}").json()

    assert alternative["scenario_id"] != baseline["scenario_id"]
    assert alternative["run_id"] != baseline["run_id"]


def test_baseline_never_uses_an_alternative_only_service(
    client: TestClient, baseline_run_id: str, expected: dict
) -> None:
    """08 §5 checklist: SVC-NEXT-01 must not appear in the baseline plan."""
    client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    )

    baseline = client.get(f"/v1/runs/{baseline_run_id}").json()
    assert all(a["service_id"] == "SVC-AM-01" for a in baseline["assignments"])


def test_unknown_order_is_rejected(client: TestClient, baseline_run_id: str) -> None:
    response = client.post(
        f"/v1/runs/{baseline_run_id}/alternatives",
        json={"order_id": "ORD-999", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
