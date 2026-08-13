"""P4 gate: the generative layer, and the guarantee that it is optional."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai import explain, intake

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture
def baseline_run(client: TestClient, validated_scenario_id: str, solver_parameters: dict) -> dict:
    return client.post(
        f"/v1/scenarios/{validated_scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()


# --- intake ------------------------------------------------------------------


def test_sample_request_is_structured_with_a_gap_flagged() -> None:
    """P4 gate: a sample request yields a draft order plus one flagged gap."""
    result = intake.structure_request((SAMPLES / "intake-01.txt").read_text(encoding="utf-8"))

    draft = result["order_draft"]
    assert draft["shipper_id"] == "SHP-02"
    assert draft["origin_terminal_ids"] == ["TRM-A"]
    assert draft["destination_terminal_ids"] == ["TRM-B"]
    assert draft["gross_weight_kg"] == 18500  # 18.5t converted to kg
    assert draft["priority_class"] == "P2"
    # The text says dimensions are still being checked.
    assert draft["dimensions_mm"] is None
    assert "dimensions_mm" in result["missing_fields"]
    assert result["input_state"] == "REVIEW_REQUIRED"


def test_intake_never_invents_a_missing_measurement() -> None:
    result = intake.structure_request((SAMPLES / "intake-02.txt").read_text(encoding="utf-8"))

    assert result["order_draft"]["gross_weight_kg"] is None
    assert result["order_draft"]["dimensions_mm"] is None
    assert set(result["missing_fields"]) >= {"gross_weight_kg", "dimensions_mm"}


def test_intake_drops_values_outside_the_vocabulary() -> None:
    """A model is not trusted to respect the enum; an unknown tag becomes 확인 필요."""
    sanitised = intake._sanitise(
        {
            "compatibility_tags": ["TRAILER_HOVERCRAFT"],
            "priority_class": "P9",
            "gross_weight_kg": "무거움",
        }
    )

    assert sanitised["compatibility_tags"] is None
    assert sanitised["priority_class"] is None
    assert sanitised["gross_weight_kg"] is None


def test_intake_endpoint(client: TestClient) -> None:
    response = client.post(
        "/v1/intake/orders",
        json={"text": (SAMPLES / "intake-01.txt").read_text(encoding="utf-8")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["input_state"] == "REVIEW_REQUIRED"
    assert body["assumption_note"] == "DEMO_ASSUMPTION"


# --- explanation -------------------------------------------------------------


def test_cards_cover_every_order_without_a_key(baseline_run: dict) -> None:
    """05 §5: with no API key the template path still produces the whole demo."""
    result = explain.build_cards(baseline_run)

    assert result["source"] == "TEMPLATE"
    assert len(result["cards"]) == len(baseline_run["order_outcomes"])


def test_display_labels_follow_the_state_rules(baseline_run: dict) -> None:
    """02 §4 labels, which 08 §5 requires the demo to show."""
    labels = {c["order_id"]: c["display_label"] for c in explain.build_cards(baseline_run)["cards"]}

    assert labels["ORD-001"] == "편성 가능"
    assert labels["ORD-004"] == "편성 가능·미배정"
    assert labels["ORD-006"] == "확인 필요"
    assert labels["ORD-007"] == "기본안 불가·대안 미검토"


def test_generated_card_naming_a_foreign_order_is_replaced() -> None:
    grounded = explain._is_grounded(
        {"headline": "편성 가능", "detail": "ORD-002 때문에 밀렸습니다."},
        order_id="ORD-001",
        allowed_ids={"ORD-001", "ORD-002"},
        allowed_codes={"ASSIGNED"},
    )

    assert grounded is False


@pytest.mark.parametrize(
    "detail",
    [
        "95% 확률로 배정됩니다.",
        "비용 절감 효과가 있습니다.",
        "탄소 배출을 줄입니다.",
        "실제 운행이 가능합니다.",
        "배정을 보장합니다.",
    ],
)
def test_forbidden_claims_are_rejected(detail: str) -> None:
    """08 §8 bans these outright, so a generated card making one is discarded."""
    grounded = explain._is_grounded(
        {"headline": "편성 가능", "detail": detail},
        order_id="ORD-001",
        allowed_ids={"ORD-001"},
        allowed_codes={"ASSIGNED"},
    )

    assert grounded is False


def test_unknown_reason_code_is_rejected() -> None:
    grounded = explain._is_grounded(
        {"headline": "불가", "detail": "GRAVITY_TOO_STRONG 사유입니다."},
        order_id="ORD-001",
        allowed_ids={"ORD-001"},
        allowed_codes={"ASSIGNED"},
    )

    assert grounded is False


def test_explanation_endpoint(client: TestClient, baseline_run: dict) -> None:
    response = client.get(f"/v1/runs/{baseline_run['run_id']}/explanation")

    assert response.status_code == 200
    assert len(response.json()["cards"]) == 9


def test_ai_status_reports_availability(client: TestClient) -> None:
    response = client.get("/v1/ai/status")

    assert response.status_code == 200
    assert "llm_available" in response.json()


# --- intake contract from docs/10-ai-usage.md --------------------------------


def test_partial_dimensions_are_kept_not_discarded() -> None:
    """A stated length with an unstated width must survive as exactly that.

    Nulling the whole block loses the distinction between "not measured" and
    "not mentioned", which is the gap the operator has to chase.
    """
    draft = intake._sanitise({"dimensions_mm": {"length": 13600, "height": 3900}})

    assert draft["dimensions_mm"] == {"length": 13600, "width": None, "height": 3900}


def test_missing_fields_names_the_axis_not_the_block() -> None:
    missing = intake._missing_fields(
        {"dimensions_mm": {"length": 13600, "width": None, "height": 3900}}
    )

    assert "dimensions_mm.width" in missing
    assert "dimensions_mm" not in missing


def test_terminal_ids_outside_the_closed_list_are_dropped() -> None:
    vocabulary = {"terminal_ids": ["TRM-A (합류 터미널 A)", "TRM-B (도착 터미널 B)"]}

    draft = intake._sanitise(
        {"origin_terminal_ids": ["TRM-A"], "destination_terminal_ids": ["TRM-ZZ"]},
        vocabulary,
    )

    assert draft["origin_terminal_ids"] == ["TRM-A"]
    assert draft["destination_terminal_ids"] is None


def test_response_carries_evidence_and_assumption_fields() -> None:
    result = intake.structure_request((SAMPLES / "intake-01.txt").read_text(encoding="utf-8"))

    for key in ("field_evidence", "assumptions_flagged", "review_reasons"):
        assert key in result


def test_review_reasons_pair_each_gap_with_a_code() -> None:
    result = intake.structure_request((SAMPLES / "intake-02.txt").read_text(encoding="utf-8"))

    assert result["review_reasons"]
    assert all(r["reason_code"] == "MISSING_REQUIRED_FIELD" for r in result["review_reasons"])
    assert {r["field"] for r in result["review_reasons"]} == set(result["missing_fields"])


def test_default_vocabulary_comes_from_the_canonical_scenario() -> None:
    vocabulary = intake.default_vocabulary()

    assert any(entry.startswith("TRM-A") for entry in vocabulary["terminal_ids"])
    assert vocabulary["priority_class"] == ["P1", "P2", "P3"]
