"""P4(d): answer a question about a solved run, from the run only.

The explanation cards say the same thing to everyone. An operator looking at a
plan has a narrower question -- why this order and not that one, what would have
to change -- and the answer is already sitting in the result; it is just spread
across five state axes, a reason code and a snapshot.

Nothing is computed here. The model is handed the verified facts and asked to
select and phrase them, and its answer goes through the same guard as every
other generated sentence: an id or a code the run does not contain, or a claim
08 §8 forbids, and the answer is refused rather than served.

Refusing matters more here than on a card. A card has a template to fall back
to; a question does not, and a plausible wrong answer to "왜 밀렸나요?" is worse
than no answer at all.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai import client, explain

MAX_QUESTION_CHARS = 500

SYSTEM_PROMPT = """\
You answer one question from a rail slot planning operator about a plan that has
already been computed.

Input is the verified result. You are not deciding anything and not computing
anything: every fact you use must appear in the input.

Return JSON: {"answer": string, "used_order_ids": [string]}

Rules:
- Answer in Korean, at most four sentences.
- Use only the ids, states, reason codes, times and measurements present in the
  input. If the input does not contain what the question asks for, say so
  plainly instead of estimating.
- Never state a probability, a percentage, a cost or carbon saving, a guarantee,
  or that real-world operation is possible.
- Never suggest relaxing a weight limit, a dimension limit, a route clearance or
  a due time. Those changes are forbidden by policy.
- used_order_ids: the orders your answer actually talks about.
"""

UNAVAILABLE_ANSWER = (
    "생성형 레이어가 설정되어 있지 않아 질문에 답할 수 없습니다. "
    "주문별 상태와 사유는 편성 화면의 설명 카드에서 확인하세요."
)


def answer_question(
    question: str,
    run: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return {answer, source, grounded, refused_reason, used_order_ids}."""
    outcomes = run.get("order_outcomes", [])

    generated = _generate(question, run, snapshot, outcomes)
    if generated is None:
        return {
            "answer": UNAVAILABLE_ANSWER,
            "source": "UNAVAILABLE",
            "grounded": False,
            "refused_reason": None,
            "used_order_ids": [],
        }

    text = str(generated.get("answer", ""))
    allowed_ids = {o["order_id"] for o in outcomes}
    allowed_codes = {o["primary_reason_code"] for o in outcomes}
    entity_ids = explain._entity_ids(run, outcomes)
    entity_ids.update(_snapshot_ids(snapshot))

    # No scope: an answer may legitimately compare two orders, which is exactly
    # what a card may not do.
    refused = explain.rejection_reason_for_text(
        text, allowed_ids, allowed_codes, entity_ids, scope_order_id=None
    )
    if refused is not None:
        return {
            "answer": "",
            "source": "LLM",
            "grounded": False,
            "refused_reason": refused,
            "used_order_ids": [],
        }

    return {
        "answer": text,
        "source": "LLM",
        "grounded": True,
        "refused_reason": None,
        "used_order_ids": [
            order_id
            for order_id in (generated.get("used_order_ids") or [])
            if order_id in allowed_ids
        ],
    }


def _snapshot_ids(snapshot: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key, field in (
        ("orders", "order_id"),
        ("services", "service_id"),
        ("wagons", "wagon_id"),
        ("slots", "slot_id"),
        ("terminals", "terminal_id"),
        ("shippers", "shipper_id"),
        ("route_constraints", "route_constraint_id"),
    ):
        ids.update(item[field] for item in snapshot.get(key, []) if field in item)
    return ids


def _facts(run: dict[str, Any], snapshot: dict[str, Any], outcomes: list[dict]) -> dict[str, Any]:
    """The verified sheet the model may read, and nothing else.

    Deliberately not the whole snapshot: the model does not need shipper
    addresses or display coordinates to explain a plan, and every field handed
    over is one more thing a sentence can be built out of.
    """
    terminals = {t["terminal_id"]: t for t in snapshot.get("terminals", [])}
    routes = {r["route_constraint_id"]: r for r in snapshot.get("route_constraints", [])}

    return {
        "baseline_service_ids": snapshot.get("baseline_service_ids", []),
        "assignments": run.get("assignments", []),
        "solver_status": run.get("solver_status"),
        "validator_status": run.get("validator_status"),
        "order_outcomes": [
            {
                key: outcome.get(key)
                for key in (
                    "order_id",
                    "input_state",
                    "eligibility_state",
                    "assignment_state",
                    "alternative_state",
                    "primary_reason_code",
                )
            }
            for outcome in outcomes
        ],
        "orders": [
            {
                "order_id": order["order_id"],
                "origin_terminal_ids": order.get("origin_terminal_ids"),
                "destination_terminal_ids": order.get("destination_terminal_ids"),
                "ready_at": order.get("ready_at"),
                "due_at": order.get("due_at"),
                "gross_weight_kg": order.get("gross_weight_kg"),
                "dimensions_mm": order.get("dimensions_mm"),
                "compatibility_tags": order.get("compatibility_tags"),
                "priority_class": order.get("priority_class"),
                "adjustment_window": order.get("adjustment_window"),
            }
            for order in snapshot.get("orders", [])
        ],
        "services": [
            {
                "service_id": service["service_id"],
                "origin_terminal_id": service.get("origin_terminal_id"),
                "destination_terminal_id": service.get("destination_terminal_id"),
                "departure_at": service.get("departure_at"),
                "arrival_at": service.get("arrival_at"),
                "planning_cutoff_at": service.get("planning_cutoff_at"),
                "route_constraint_id": service.get("route_constraint_id"),
            }
            for service in snapshot.get("services", [])
        ],
        "route_constraints": list(routes.values()),
        "terminals": [
            {
                "terminal_id": t["terminal_id"],
                "supported_tags": t.get("supported_tags"),
                "minimum_handling_minutes": t.get("minimum_handling_minutes"),
            }
            for t in terminals.values()
        ],
        "slots": [
            {
                "slot_id": slot["slot_id"],
                "wagon_id": slot.get("wagon_id"),
                "max_weight_kg": slot.get("max_weight_kg"),
                "max_dimensions_mm": slot.get("max_dimensions_mm"),
                "supported_tags": slot.get("supported_tags"),
                "available": slot.get("available"),
            }
            for slot in snapshot.get("slots", [])
        ],
        "policy": snapshot.get("policy"),
    }


def _generate(
    question: str,
    run: dict[str, Any],
    snapshot: dict[str, Any],
    outcomes: list[dict],
) -> dict[str, Any] | None:
    if not outcomes:
        return None

    user_prompt = (
        f"Question:\n{question[:MAX_QUESTION_CHARS]}\n\n"
        f"Verified result:\n{json.dumps(_facts(run, snapshot, outcomes), ensure_ascii=False)}"
    )

    response = client.complete_json(SYSTEM_PROMPT, user_prompt, max_tokens=900)
    if not response:
        return None

    return response if isinstance(response.get("answer"), str) else None
