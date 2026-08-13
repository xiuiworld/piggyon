"""A badly shaped model reply costs its own sentence, never the request.

Found the hard way. The batch intake call went out without a schema, the model
answered with `field_evidence` as bare strings, and the code that keeps evidence
for surviving fields did `e.get("field")` on a `str` -- so typing an ordinary
sentence into the order screen returned a 500 and the page died with a minified
React error naming nothing.

Every surface now sends a schema, but `complete_json` keeps a schema-less retry,
so these pin the behaviour on the shape the schema was there to prevent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai import answer, explain, intake, suggest


@pytest.fixture
def solved(client: TestClient, request_body: dict, solver_parameters: dict) -> str:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    return client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]


def test_intake_survives_evidence_that_is_not_an_object(monkeypatch) -> None:
    monkeypatch.setattr(
        intake.client,
        "complete_json",
        lambda *a, **k: {
            "orders": [
                {
                    "order_draft": {"gross_weight_kg": 18000},
                    # What the model actually returned when nothing held it to
                    # a shape.
                    "field_evidence": ["18톤입니다", "합류 터미널 A로"],
                    "assumptions_flagged": [],
                }
            ]
        },
    )

    body = intake.structure_requests("트레일러 한 대", as_of="2026-08-17T06:00:00+09:00")

    assert len(body["orders"]) == 1
    assert body["orders"][0]["order_draft"]["gross_weight_kg"] == 18000
    # Unusable evidence is dropped rather than served or raised.
    assert body["orders"][0]["field_evidence"] == []


def test_a_card_that_is_a_string_costs_one_card(
    client: TestClient, solved: str, monkeypatch
) -> None:
    monkeypatch.setattr(
        explain.client,
        "complete_json",
        lambda *a, **k: {"cards": ["ORD-001은 배정되었습니다", {"order_id": "ORD-002",
                                                              "headline": "배정",
                                                              "detail": "슬롯에 배정되었습니다."}]},
    )

    body = client.get(f"/v1/runs/{solved}/explanation")

    assert body.status_code == 200
    cards = {c["order_id"]: c for c in body.json()["cards"]}
    # Every order still has a card; the unusable one fell back to its template.
    assert len(cards) == 9
    assert "ORD-001" in body.json()["replaced_order_ids"]


def test_a_suggestion_that_is_a_string_costs_one_suggestion(
    client: TestClient, solved: str, monkeypatch
) -> None:
    monkeypatch.setattr(
        suggest.client,
        "complete_json",
        lambda *a, **k: {"suggestions": ["다음 열차를 쓰세요"]},
    )

    body = client.get(f"/v1/runs/{solved}/explanation")

    assert body.status_code == 200
    # The template suggestion is still there for the orders that have one.
    card = next(c for c in body.json()["cards"] if c["order_id"] == "ORD-005")
    assert card["suggested_adjustment_types"] == ["ADD_ORDER_APPROVED_SERVICE"]


def test_an_unhashable_used_order_id_does_not_raise(monkeypatch) -> None:
    run = {
        "order_outcomes": [
            {
                "order_id": "ORD-004",
                "input_state": "VALID",
                "eligibility_state": "ELIGIBLE",
                "assignment_state": "UNASSIGNED",
                "alternative_state": "NOT_SEARCHED",
                "primary_reason_code": "CAPACITY_CONFLICT",
            }
        ],
        "assignments": [],
    }
    monkeypatch.setattr(
        answer.client,
        "complete_json",
        lambda *a, **k: {
            "answer": "ORD-004는 슬롯이 부족해 선택되지 않았습니다.",
            # A dict here used to raise on `in` against a set of strings.
            "used_order_ids": [{"order_id": "ORD-004"}, "ORD-004"],
        },
    )

    result = answer.answer_question("왜?", run, {"orders": []})

    assert result["grounded"] is True
    assert result["used_order_ids"] == ["ORD-004"]


def test_every_generative_call_sends_a_schema() -> None:
    """The guard that stops this class of bug returning.

    Each of these was written without one at some point, and each cost an
    endpoint the dashboard calls on every render.
    """
    seen: list[dict | None] = []

    class Recorder:
        def complete_json(self, *_a, schema=None, **_k):
            seen.append(schema)
            return None

    for module, call in (
        (intake, lambda: intake.structure_request("x", as_of="2026-08-17T06:00:00+09:00")),
        (intake, lambda: intake.structure_requests("x", as_of="2026-08-17T06:00:00+09:00")),
    ):
        original, module.client = module.client, Recorder()  # type: ignore[assignment]
        try:
            call()
        finally:
            module.client = original  # type: ignore[assignment]

    assert all(s is not None for s in seen), "a generative call went out unschemad"
    assert explain.CARDS_SCHEMA and suggest.SUGGESTIONS_SCHEMA and answer.ANSWER_SCHEMA
