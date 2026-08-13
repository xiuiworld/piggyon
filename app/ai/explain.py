"""P4(b): operator-facing explanation cards.

The model rewrites verified results into sentences. It never decides anything:
CP-SAT already did. Every generated card is checked back against the run before
it is served, and a card that mentions an order or a reason the run does not
contain — or that makes a claim 08 §8 forbids — is replaced by its template.
"""

from __future__ import annotations

import re
from typing import Any

from app.ai import client

ALTERNATIVE_BADGE = "조건부 대안 있음"

REASON_TEXT = {
    "ASSIGNED": "제약을 모두 만족해 슬롯에 배정되었습니다.",
    "CAPACITY_CONFLICT": "제약은 만족하지만 이번 운행의 슬롯이 부족해 선택되지 않았습니다.",
    "MISSING_REQUIRED_FIELD": "필수 입력값이 비어 있어 계산 대상에서 분리했습니다.",
    "READY_AFTER_CUTOFF": "준비 시각이 이 운행의 반입 마감 이후입니다.",
    "DUE_TIME_EXCEEDED": "이 운행으로는 도착·처리 후 납기를 넘깁니다.",
    "TUNNEL_HEIGHT_EXCEEDED": "화물 높이가 경로의 통과 높이 한도를 넘습니다.",
    "TERMINAL_NOT_COMPATIBLE": "지정된 터미널이 이 화물의 취급 태그를 지원하지 않습니다.",
    "ALTERNATIVE_AVAILABLE": "승인된 변경을 적용하면 배정할 수 있습니다.",
}

# 08 §8: claims the demo must never make, whoever writes them.
FORBIDDEN_PATTERNS = [
    re.compile(r"\d+\s*%"),
    re.compile(r"확률"),
    re.compile(r"비용\s*(절감|절약)"),
    re.compile(r"탄소"),
    re.compile(r"보장"),
    re.compile(r"실제\s*운행이?\s*가능"),
]

ORDER_ID = re.compile(r"\bORD-\d{3,}\b")
REASON_CODE = re.compile(r"\b[A-Z][A-Z_]{4,}\b")

SYSTEM_PROMPT = """\
You write short Korean status cards for a rail slot planning operator.

Input is an already-verified result. You are not deciding anything; you are
describing what the solver decided.

Return JSON: {"cards": [{"order_id": string, "headline": string, "detail": string}]}

Rules:
- One card per order given, same order_id spelling, no extra orders.
- headline: at most 20 Korean characters. detail: one or two sentences.
- Use only the states, reason codes and numbers present in the input.
- Never state a probability, a percentage, a cost or carbon saving, a guarantee,
  or that real-world operation is possible.
- Never invent a slot, service, terminal or measurement.
"""


def build_cards(run: dict[str, Any]) -> dict[str, Any]:
    """Return {cards, source, replaced_order_ids}."""
    outcomes = run.get("order_outcomes", [])
    templates = {o["order_id"]: _template_card(o) for o in outcomes}

    generated = _generate(run, outcomes)
    if generated is None:
        return {
            "cards": list(templates.values()),
            "source": "TEMPLATE",
            "replaced_order_ids": [],
        }

    allowed_ids = {o["order_id"] for o in outcomes}
    allowed_codes = {o["primary_reason_code"] for o in outcomes}
    cards: dict[str, dict[str, Any]] = {}
    replaced: list[str] = []

    for card in generated:
        order_id = card.get("order_id")
        if order_id not in allowed_ids or order_id in cards:
            continue
        if _is_grounded(card, order_id, allowed_ids, allowed_codes):
            cards[order_id] = {
                "order_id": order_id,
                "headline": str(card.get("headline", ""))[:40],
                "detail": str(card.get("detail", "")),
                # The label and badge stay computed, never generated: they
                # drive the demo's 02 §4 badges and must not be paraphrased.
                "display_label": templates[order_id]["display_label"],
                "display_badges": templates[order_id]["display_badges"],
            }
        else:
            replaced.append(order_id)

    for order_id, template in templates.items():
        if order_id not in cards:
            cards[order_id] = template
            if order_id not in replaced:
                replaced.append(order_id)

    return {
        "cards": [cards[o["order_id"]] for o in outcomes],
        "source": "LLM" if len(replaced) < len(outcomes) else "TEMPLATE",
        "replaced_order_ids": sorted(replaced),
    }


def _generate(run: dict[str, Any], outcomes: list[dict]) -> list[dict] | None:
    if not outcomes:
        return None

    # Only verified, structured facts cross the boundary (05 §5).
    payload = {
        "assignments": run.get("assignments", []),
        "order_outcomes": [
            {
                key: outcome[key]
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
    }

    import json

    response = client.complete_json(
        SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False)
    )
    if not response:
        return None

    cards = response.get("cards")
    return cards if isinstance(cards, list) else None


def _is_grounded(
    card: dict[str, Any],
    order_id: str,
    allowed_ids: set[str],
    allowed_codes: set[str],
) -> bool:
    text = f"{card.get('headline', '')} {card.get('detail', '')}"

    if not text.strip():
        return False
    if any(pattern.search(text) for pattern in FORBIDDEN_PATTERNS):
        return False
    # Any order it names must exist, and must be the one it is about.
    for mentioned in ORDER_ID.findall(text):
        if mentioned not in allowed_ids or mentioned != order_id:
            return False
    # Any reason-code-looking token must be a real code from this run.
    for token in REASON_CODE.findall(text):
        if token.startswith(("ORD", "SLT", "SVC", "WGN", "TRM")):
            continue
        if token not in allowed_codes:
            return False
    return True


def _template_card(outcome: dict[str, Any]) -> dict[str, Any]:
    label = _display_label(outcome)
    reason = REASON_TEXT.get(
        outcome["primary_reason_code"],
        f"사유 코드 {outcome['primary_reason_code']}에 해당합니다.",
    )
    detail = reason
    if outcome.get("alternative_state") == "AVAILABLE":
        detail = f"{reason} 승인된 변경으로 검토할 수 있는 대안이 있습니다."

    return {
        "order_id": outcome["order_id"],
        "headline": label,
        "detail": detail,
        "display_label": label,
        "display_badges": _badges(outcome),
    }


def _display_label(outcome: dict[str, Any]) -> str:
    """02 §4 display rules.

    The INELIGIBLE label depends on whether alternatives have been searched
    yet: rule 5 (NOT_SEARCHED) reads 기본안 불가·대안 미검토, rule 6 (NONE)
    reads 불가. Rule 4 keeps the main label untouched when an alternative is
    AVAILABLE and adds a badge instead, so AVAILABLE falls through to 불가.
    """
    if outcome["input_state"] == "REVIEW_REQUIRED":
        return "확인 필요"

    eligibility = outcome["eligibility_state"]
    if eligibility == "ELIGIBLE":
        return "편성 가능" if outcome["assignment_state"] == "ASSIGNED" else "편성 가능·미배정"
    if eligibility == "INELIGIBLE":
        return "기본안 불가·대안 미검토" if outcome["alternative_state"] == "NOT_SEARCHED" else "불가"
    return "확인 필요"


def _badges(outcome: dict[str, Any]) -> list[str]:
    return [ALTERNATIVE_BADGE] if outcome.get("alternative_state") == "AVAILABLE" else []
