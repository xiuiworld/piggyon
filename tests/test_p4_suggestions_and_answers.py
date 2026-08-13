"""P4(c)/(d): suggesting a change, answering a question, reading a batch.

All three let a model speak about a plan, so all three are tested for what they
refuse rather than for what they say. The wording is the model's and will vary;
the guarantees are that a suggestion stays inside the approval policy, an
ungrounded answer is withheld instead of served, and a batched draft gets the
same treatment as a single one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai import answer, intake, suggest


@pytest.fixture
def solved_run(client: TestClient, request_body: dict, solver_parameters: dict) -> dict:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    run_id = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]
    return {"scenario_id": scenario_id, "run_id": run_id}


# --- suggestions -----------------------------------------------------------


def test_suggestions_appear_only_where_something_is_approved(
    client: TestClient, solved_run: dict
) -> None:
    cards = client.get(f"/v1/runs/{solved_run['run_id']}/explanation").json()["cards"]
    by_order = {c["order_id"]: c for c in cards}

    # Approved for a later service, and the baseline could not carry it.
    assert by_order["ORD-005"]["suggested_adjustment_types"] == [
        "ADD_ORDER_APPROVED_SERVICE"
    ]
    assert by_order["ORD-005"]["suggestion"]

    # Unassigned, but nothing was ever approved for it: there is nothing to
    # suggest, and inventing a permission is exactly what must not happen.
    assert "suggested_adjustment_types" not in by_order["ORD-004"]
    # Already carried.
    assert "suggested_adjustment_types" not in by_order["ORD-001"]
    # Held at input -- the missing value has to come first.
    assert "suggested_adjustment_types" not in by_order["ORD-006"]


def test_a_suggestion_asks_for_the_change_aimed_at_the_block(
    client: TestClient, solved_run: dict
) -> None:
    cards = client.get(f"/v1/runs/{solved_run['run_id']}/explanation").json()["cards"]
    by_order = {c["order_id"]: c for c in cards}

    # Blocked on time, approved for a later service: one change, not a bundle.
    assert by_order["ORD-005"]["suggested_adjustment_types"] == [
        "ADD_ORDER_APPROVED_SERVICE"
    ]


def test_a_terminal_change_is_paired_when_nothing_runs_to_the_new_terminal(
    client: TestClient, solved_run: dict
) -> None:
    """The one case where two changes have to travel together.

    ORD-008 is approved for TRM-C, which only SVC-AC-01 serves, and that is not
    in the baseline. Re-pointing the order without opening the service leaves it
    headed somewhere no permitted train goes, so the search comes back empty
    every single time -- advice that is wrong before it is tried.
    """
    cards = client.get(f"/v1/runs/{solved_run['run_id']}/explanation").json()["cards"]
    suggested = next(c for c in cards if c["order_id"] == "ORD-008")

    assert set(suggested["suggested_adjustment_types"]) == {
        "CHANGE_TO_APPROVED_TERMINAL",
        "ADD_ORDER_APPROVED_SERVICE",
    }

    # The pair works where the terminal change alone does not, which is the
    # whole reason for pairing them.
    alone = client.post(
        f"/v1/runs/{solved_run['run_id']}/alternatives",
        json={"order_id": "ORD-008", "adjustment_types": ["CHANGE_TO_APPROVED_TERMINAL"]},
    )
    assert alone.status_code == 200
    assert alone.json()["status"] == "NO_FEASIBLE_ALTERNATIVE"

    paired = client.post(
        f"/v1/runs/{solved_run['run_id']}/alternatives",
        json={
            "order_id": "ORD-008",
            "adjustment_types": suggested["suggested_adjustment_types"],
        },
    )
    assert paired.status_code == 201, paired.text


def test_a_suggested_change_is_one_the_alternative_endpoint_accepts(
    client: TestClient, solved_run: dict
) -> None:
    """The suggestion has to be actionable, not merely well-phrased."""
    cards = client.get(f"/v1/runs/{solved_run['run_id']}/explanation").json()["cards"]
    suggested = next(c for c in cards if c["order_id"] == "ORD-005")

    accepted = client.post(
        f"/v1/runs/{solved_run['run_id']}/alternatives",
        json={
            "order_id": "ORD-005",
            "adjustment_types": suggested["suggested_adjustment_types"],
        },
    )

    assert accepted.status_code == 201, accepted.text


def test_a_type_outside_the_approval_window_is_dropped() -> None:
    run = {
        "order_outcomes": [
            {
                "order_id": "ORD-005",
                "input_state": "VALID",
                "eligibility_state": "INELIGIBLE",
                "assignment_state": "NOT_APPLICABLE",
                "primary_reason_code": "READY_AFTER_CUTOFF",
            }
        ],
        "assignments": [],
    }
    snapshot = {
        "orders": [
            {
                "order_id": "ORD-005",
                "adjustment_window": {"alternative_service_ids": ["SVC-NEXT-01"]},
            }
        ]
    }

    built = suggest.build_suggestions(run, snapshot)

    # Only what policy approved, whoever proposed it.
    assert built["suggestions"]["ORD-005"]["adjustment_types"] == [
        "ADD_ORDER_APPROVED_SERVICE"
    ]


def test_suggestions_never_advise_a_forbidden_change(
    client: TestClient, solved_run: dict
) -> None:
    cards = client.get(f"/v1/runs/{solved_run['run_id']}/explanation").json()["cards"]

    for card in cards:
        for adjustment in card.get("suggested_adjustment_types", []):
            assert adjustment in {
                "ADD_ORDER_APPROVED_SERVICE",
                "CHANGE_TO_APPROVED_TERMINAL",
            }


# --- questions -------------------------------------------------------------


def test_a_question_about_a_missing_run_is_a_404(client: TestClient) -> None:
    refused = client.post("/v1/runs/RUN-999/questions", json={"question": "왜?"})

    assert refused.status_code == 404
    assert refused.json()["code"] == "RUN_NOT_FOUND"


def test_an_empty_question_is_rejected(client: TestClient, solved_run: dict) -> None:
    refused = client.post(
        f"/v1/runs/{solved_run['run_id']}/questions", json={"question": ""}
    )

    assert refused.status_code == 400


def test_without_a_model_the_answer_says_so_rather_than_guessing(
    client: TestClient, solved_run: dict
) -> None:
    """No key configured in the test environment, so this is the served path."""
    body = client.post(
        f"/v1/runs/{solved_run['run_id']}/questions",
        json={"question": "왜 ORD-004가 밀렸나요?"},
    ).json()

    assert body["source"] == "UNAVAILABLE"
    assert body["grounded"] is False
    assert body["used_order_ids"] == []
    # It points at where the answer does exist instead of inventing one.
    assert "설명 카드" in body["answer"]


def test_an_answer_naming_something_the_run_lacks_is_withheld(monkeypatch) -> None:
    run = {
        "order_outcomes": [
            {
                "order_id": "ORD-001",
                "input_state": "VALID",
                "eligibility_state": "ELIGIBLE",
                "assignment_state": "ASSIGNED",
                "alternative_state": "NOT_SEARCHED",
                "primary_reason_code": "ASSIGNED",
            }
        ],
        "assignments": [],
    }

    monkeypatch.setattr(
        answer.client,
        "complete_json",
        lambda *a, **k: {"answer": "ORD-777은 SLT-ZZ-99에 배정되었습니다.", "used_order_ids": []},
    )

    result = answer.answer_question("아무거나", run, {"orders": []})

    assert result["grounded"] is False
    assert result["answer"] == ""
    assert result["refused_reason"].startswith("UNKNOWN_ORDER")


def test_a_forbidden_claim_is_withheld(monkeypatch) -> None:
    run = {
        "order_outcomes": [
            {
                "order_id": "ORD-001",
                "input_state": "VALID",
                "eligibility_state": "ELIGIBLE",
                "assignment_state": "ASSIGNED",
                "alternative_state": "NOT_SEARCHED",
                "primary_reason_code": "ASSIGNED",
            }
        ],
        "assignments": [],
    }

    monkeypatch.setattr(
        answer.client,
        "complete_json",
        lambda *a, **k: {"answer": "배정 확률이 높습니다.", "used_order_ids": ["ORD-001"]},
    )

    result = answer.answer_question("가능성은?", run, {"orders": []})

    assert result["grounded"] is False
    assert result["refused_reason"].startswith("FORBIDDEN_CLAIM")


def test_a_grounded_answer_is_served(monkeypatch) -> None:
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
            "answer": "ORD-004는 제약을 모두 만족했지만 슬롯이 부족해 선택되지 않았습니다.",
            "used_order_ids": ["ORD-004", "ORD-999"],
        },
    )

    result = answer.answer_question("왜?", run, {"orders": []})

    assert result["grounded"] is True
    assert "ORD-004" in result["answer"]
    # The id list is filtered the same way the sentence is.
    assert result["used_order_ids"] == ["ORD-004"]


# --- batch intake ----------------------------------------------------------


def test_without_a_model_a_batch_is_one_order(client: TestClient) -> None:
    body = client.post(
        "/v1/intake/order-batches",
        json={"text": "트레일러 3대 보냅니다. 18톤입니다.", "as_of": "2026-08-17T06:00:00+09:00"},
    ).json()

    # The rule extractor reads one document as one order and does not pretend
    # to have split anything.
    assert len(body["orders"]) == 1
    assert body["source"] == "RULE_BASED"
    assert body["truncated"] is False
    assert body["orders"][0]["assumption_note"] == "DEMO_ASSUMPTION"


def test_each_batched_draft_goes_through_the_single_order_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        intake.client,
        "complete_json",
        lambda *a, **k: {
            "orders": [
                {
                    "order_draft": {
                        "order_id": None,
                        "shipper_id": "SHP-01",
                        "origin_terminal_ids": ["TRM-A"],
                        "destination_terminal_ids": ["TRM-B"],
                        "ready_at": None,
                        "due_at": None,
                        "gross_weight_kg": 18000,
                        "dimensions_mm": None,
                        "compatibility_tags": None,
                        "priority_class": None,
                    },
                    "field_evidence": [{"field": "gross_weight_kg", "source_text": "18톤"}],
                    "assumptions_flagged": [],
                },
                {
                    "order_draft": {
                        "order_id": None,
                        "shipper_id": "SHP-99",  # not in the vocabulary
                        "origin_terminal_ids": ["TRM-A"],
                        "destination_terminal_ids": ["TRM-B"],
                        "ready_at": None,
                        "due_at": None,
                        "gross_weight_kg": None,
                        "dimensions_mm": None,
                        "compatibility_tags": None,
                        "priority_class": None,
                    },
                    "field_evidence": [],
                    "assumptions_flagged": [],
                },
            ]
        },
    )

    body = intake.structure_requests("트레일러 2대", as_of="2026-08-17T06:00:00+09:00")

    assert body["source"] == "LLM"
    assert len(body["orders"]) == 2

    first, second = body["orders"]
    assert first["order_draft"]["gross_weight_kg"] == 18000
    assert first["input_state"] == "REVIEW_REQUIRED"  # times still missing
    assert first["field_evidence"][0]["source_text"] == "18톤"

    # An id outside the closed vocabulary is dropped by the same sanitiser the
    # single-order path uses, and reported as missing rather than kept.
    assert second["order_draft"]["shipper_id"] is None
    assert "shipper_id" in second["missing_fields"]
