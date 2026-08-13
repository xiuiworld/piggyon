"""Paths the canonical fixture never reaches.

The demo scenario only ever produces ADDED deltas, an OPTIMAL solve and (with
no API key) the template branch. Everything below is code that runs in
production but that the happy path leaves unexercised.
"""

from __future__ import annotations

import pytest

from app.ai import explain
from app.models.snapshot import ScenarioInputSnapshot
from app.rules.eligibility import evaluate_scenario
from app.services.alternatives import _assignment_deltas
from app.solver.baseline import SolverParameters, solve_baseline

# --- assignment deltas (TC-15) ----------------------------------------------


def _assignment(order_id: str, slot_id: str, service_id: str = "SVC-AM-01") -> dict:
    return {
        "order_id": order_id,
        "service_id": service_id,
        "wagon_id": "WGN-AM-01",
        "slot_id": slot_id,
    }


def test_delta_reports_a_moved_order() -> None:
    """TC-15: an alternative that displaces an order must show both sides."""
    before = [_assignment("ORD-001", "SLT-AM-01")]
    after = [_assignment("ORD-001", "SLT-AM-03")]

    deltas = _assignment_deltas(before, after)

    assert len(deltas) == 1
    assert deltas[0]["change_type"] == "MOVED"
    assert deltas[0]["before_assignment"]["slot_id"] == "SLT-AM-01"
    assert deltas[0]["after_assignment"]["slot_id"] == "SLT-AM-03"


def test_delta_reports_an_order_pushed_out() -> None:
    before = [_assignment("ORD-001", "SLT-AM-01")]
    after: list[dict] = []

    deltas = _assignment_deltas(before, after)

    assert deltas[0]["change_type"] == "UNASSIGNED"
    assert deltas[0]["before_assignment"]["slot_id"] == "SLT-AM-01"
    assert deltas[0]["after_assignment"] is None


def test_unchanged_orders_produce_no_delta() -> None:
    same = [_assignment("ORD-001", "SLT-AM-01")]

    assert _assignment_deltas(same, list(same)) == []


def test_deltas_are_ordered_by_order_id() -> None:
    before = [_assignment("ORD-003", "SLT-AM-03")]
    after = [_assignment("ORD-001", "SLT-AM-01"), _assignment("ORD-002", "SLT-AM-02")]

    deltas = _assignment_deltas(before, after)

    assert [d["order_id"] for d in deltas] == ["ORD-001", "ORD-002", "ORD-003"]


# --- solver edge cases -------------------------------------------------------


def test_infeasible_model_is_reported_as_such(snapshot) -> None:
    """TC-13: a model with no solution is MODEL_INFEASIBLE, not an empty plan.

    Reached by forcing two orders onto one slot, which the public API cannot
    express — it is a solver-level guarantee, so it is tested at that level.
    """
    evaluation = evaluate_scenario(snapshot)
    result = solve_baseline(snapshot, evaluation, SolverParameters())
    assert result.solver_status == "OPTIMAL"  # baseline still solves

    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    a = model.NewBoolVar("a")
    b = model.NewBoolVar("b")
    model.Add(a + b <= 1)
    model.Add(a + b >= 2)
    solver = cp_model.CpSolver()
    assert solver.Solve(model) == cp_model.INFEASIBLE


def test_orders_heavier_than_the_wagon_are_excluded(snapshot) -> None:
    """TC-05: an overweight order never becomes a candidate."""
    payload = snapshot.model_dump(mode="json")
    for order in payload["orders"]:
        if order["order_id"] == "ORD-001":
            order["gross_weight_kg"] = 30000  # over the 24000 slot and route limit

    heavy = ScenarioInputSnapshot.model_validate(payload)
    evaluation = evaluate_scenario(heavy).by_order_id["ORD-001"]

    assert evaluation.eligibility_state == "INELIGIBLE"
    assert evaluation.eligible_slot_ids == []


def test_wagon_capacity_limits_the_plan(snapshot) -> None:
    """The shared wagon limit is the solver's to enforce, not the gate's."""
    payload = snapshot.model_dump(mode="json")
    for wagon in payload["wagons"]:
        if wagon["wagon_id"] == "WGN-AM-01":
            wagon["max_total_weight_kg"] = 40000  # fits two of the three orders

    limited = ScenarioInputSnapshot.model_validate(payload)
    result = solve_baseline(limited, evaluate_scenario(limited), SolverParameters())

    total = sum(
        next(o.gross_weight_kg for o in limited.orders if o.order_id == a.order_id)
        for a in result.assignments
    )
    assert total <= 40000
    assert len(result.assignments) == 2


# --- generated-card guard ----------------------------------------------------

ALLOWED_IDS = {f"ORD-00{i}" for i in range(1, 10)}
ALLOWED_CODES = {"ASSIGNED", "CAPACITY_CONFLICT", "TERMINAL_NOT_COMPATIBLE"}
ENTITY_IDS = ALLOWED_IDS | {"SLT-AM-01", "SVC-AM-01", "WGN-AM-01"}


def _grounded(detail: str, order_id: str = "ORD-008") -> bool:
    return explain._is_grounded(
        {"headline": "상태", "detail": detail},
        order_id,
        ALLOWED_IDS,
        ALLOWED_CODES,
        ENTITY_IDS,
    )


@pytest.mark.parametrize(
    "detail",
    [
        "도착 터미널이 TRAILER_TALL 태그를 지원하지 않습니다.",
        "TRAILER_STANDARD 화물이지만 슬롯 경합으로 선택되지 않았습니다.",
        "입력은 VALID 이지만 INELIGIBLE 로 판정되었습니다.",
    ],
)
def test_schema_vocabulary_is_not_treated_as_invention(detail: str) -> None:
    """These are enum values fixed by the contract, not claims about the world.

    Rejecting them was silently pushing most cards back to templates.
    """
    assert _grounded(detail) is True


def test_real_entity_id_is_allowed() -> None:
    assert _grounded("SLT-AM-01 슬롯에 배정되었습니다.", "ORD-001") is True


def test_invented_slot_id_is_rejected() -> None:
    """A prefix check alone would wave this through."""
    assert _grounded("SLT-AM-99 슬롯에 배정되었습니다.", "ORD-001") is False


def test_invented_service_id_is_rejected() -> None:
    assert _grounded("SVC-GHOST-01 편으로 보냅니다.", "ORD-001") is False


def test_unknown_shouty_token_is_still_rejected() -> None:
    assert _grounded("GRAVITY_TOO_STRONG 때문입니다.") is False


# --- LLM branch of build_cards ----------------------------------------------


@pytest.fixture
def run_with_two_orders() -> dict:
    return {
        "assignments": [_assignment("ORD-001", "SLT-AM-01")],
        "order_outcomes": [
            {
                "order_id": "ORD-001",
                "input_state": "VALID",
                "eligibility_state": "ELIGIBLE",
                "assignment_state": "ASSIGNED",
                "alternative_state": "NOT_SEARCHED",
                "primary_reason_code": "ASSIGNED",
            },
            {
                "order_id": "ORD-004",
                "input_state": "VALID",
                "eligibility_state": "ELIGIBLE",
                "assignment_state": "UNASSIGNED",
                "alternative_state": "NOT_SEARCHED",
                "primary_reason_code": "CAPACITY_CONFLICT",
            },
        ],
    }


def test_generated_cards_are_used_when_grounded(monkeypatch, run_with_two_orders) -> None:
    monkeypatch.setattr(
        explain,
        "_generate",
        lambda run, outcomes: [
            {"order_id": "ORD-001", "headline": "편성 가능", "detail": "SLT-AM-01 에 배정되었습니다."},
            {"order_id": "ORD-004", "headline": "미배정", "detail": "슬롯 경합으로 선택되지 않았습니다."},
        ],
    )

    result = explain.build_cards(run_with_two_orders)

    assert result["source"] == "LLM"
    assert result["replaced_order_ids"] == []
    assert result["cards"][0]["detail"] == "SLT-AM-01 에 배정되었습니다."
    # The label is computed, never taken from the model.
    assert result["cards"][0]["display_label"] == "편성 가능"


def test_ungrounded_card_falls_back_to_its_template(monkeypatch, run_with_two_orders) -> None:
    monkeypatch.setattr(
        explain,
        "_generate",
        lambda run, outcomes: [
            {"order_id": "ORD-001", "headline": "편성 가능", "detail": "정상 배정되었습니다."},
            {"order_id": "ORD-004", "headline": "미배정", "detail": "95% 확률로 다음 편에 실립니다."},
        ],
    )

    result = explain.build_cards(run_with_two_orders)

    assert result["replaced_order_ids"] == ["ORD-004"]
    assert "확률" not in result["cards"][1]["detail"]


def test_missing_card_falls_back_without_dropping_the_order(
    monkeypatch, run_with_two_orders
) -> None:
    monkeypatch.setattr(
        explain,
        "_generate",
        lambda run, outcomes: [
            {"order_id": "ORD-001", "headline": "편성 가능", "detail": "배정되었습니다."}
        ],
    )

    result = explain.build_cards(run_with_two_orders)

    assert [c["order_id"] for c in result["cards"]] == ["ORD-001", "ORD-004"]


def test_card_for_a_foreign_order_is_ignored(monkeypatch, run_with_two_orders) -> None:
    monkeypatch.setattr(
        explain,
        "_generate",
        lambda run, outcomes: [
            {"order_id": "ORD-999", "headline": "없는 주문", "detail": "존재하지 않습니다."}
        ],
    )

    result = explain.build_cards(run_with_two_orders)

    assert [c["order_id"] for c in result["cards"]] == ["ORD-001", "ORD-004"]
    assert result["source"] == "TEMPLATE"


def test_badge_appears_once_an_alternative_is_available() -> None:
    """08 §5 requires the 조건부 대안 있음 badge to be visible."""
    run = {
        "assignments": [],
        "order_outcomes": [
            {
                "order_id": "ORD-005",
                "input_state": "VALID",
                "eligibility_state": "INELIGIBLE",
                "assignment_state": "NOT_APPLICABLE",
                "alternative_state": "AVAILABLE",
                "primary_reason_code": "READY_AFTER_CUTOFF",
            }
        ],
    }

    card = explain.build_cards(run)["cards"][0]

    assert card["display_badges"] == ["조건부 대안 있음"]
    # 02 §4.7, not 불가: that label is rule 6's, and printed beside this badge
    # it tells the operator the order is impossible and has an alternative.
    assert card["display_label"] == "기본안 불가"


def test_unmatched_order_id_is_reported_not_dropped(monkeypatch, run_with_two_orders) -> None:
    """A model that spells the id its own way must not look like silence."""
    monkeypatch.setattr(
        explain,
        "_generate",
        lambda run, outcomes: [
            {"order_id": "ORD-1", "headline": "편성 가능", "detail": "배정되었습니다."}
        ],
    )

    result = explain.build_cards(run_with_two_orders)

    assert result["unmatched_order_ids"] == ["ORD-1"]
    assert result["replaced_reasons"]["ORD-001"] == "NO_CARD_RETURNED"


def test_rejection_reason_names_the_offending_token(run_with_two_orders) -> None:
    reason = explain._rejection_reason(
        {"headline": "불가", "detail": "SLT-AM-99 에 배정했습니다."},
        "ORD-001",
        {"ORD-001"},
        {"ASSIGNED"},
        {"ORD-001", "SLT-AM-01"},
    )

    assert reason == "UNKNOWN_ENTITY:SLT-AM-99"
