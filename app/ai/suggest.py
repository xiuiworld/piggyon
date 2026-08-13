"""P4(c): which approved change to try first, and why.

01 §02 gives the generative layer two jobs — 후보 추천 and 결과 설명 — and only
the second was ever built. This is the first: for an order the baseline could
not carry, it says which of the changes already approved for that order is worth
trying, and in one sentence why.

It proposes. It never answers. The adjustment it names is fed to the same
alternative search an operator would have started by hand, and CP-SAT decides
whether the derived plan is feasible; a bad suggestion costs one search that
comes back `NO_FEASIBLE_ALTERNATIVE`. That asymmetry is what makes it safe to
let a model choose here at all.

Two things are checked rather than trusted: the suggested types must be a subset
of what policy already approved for that order (in code, not in the prompt), and
the sentence goes through the same guard as an explanation card.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai import client, explain

# Which approved change plausibly speaks to which failure. Not a promise that it
# works -- the solver settles that -- only an ordering, so the first thing the
# operator tries is the one aimed at the wall they actually hit.
#
# A route limit is a property of the service's route, and an approved
# destination is reached by a different service, so changing terminal can move
# an over-height load onto a route with more clearance. It often will not. That
# is the solver's answer to give.
ADJUSTMENT_FOR_REASON: dict[str, str] = {
    "READY_AFTER_CUTOFF": "ADD_ORDER_APPROVED_SERVICE",
    "DUE_TIME_EXCEEDED": "ADD_ORDER_APPROVED_SERVICE",
    "CAPACITY_CONFLICT": "ADD_ORDER_APPROVED_SERVICE",
    "SERVICE_UNAVAILABLE": "ADD_ORDER_APPROVED_SERVICE",
    "SLOT_UNAVAILABLE": "ADD_ORDER_APPROVED_SERVICE",
    "WAGON_UNAVAILABLE": "ADD_ORDER_APPROVED_SERVICE",
    "TERMINAL_NOT_COMPATIBLE": "CHANGE_TO_APPROVED_TERMINAL",
    "TERMINAL_NOT_ON_SERVICE_ROUTE": "CHANGE_TO_APPROVED_TERMINAL",
    "TUNNEL_HEIGHT_EXCEEDED": "CHANGE_TO_APPROVED_TERMINAL",
    "ROUTE_WIDTH_EXCEEDED": "CHANGE_TO_APPROVED_TERMINAL",
    "ROUTE_WEIGHT_EXCEEDED": "CHANGE_TO_APPROVED_TERMINAL",
}

TEMPLATE_TEXT = {
    "ADD_ORDER_APPROVED_SERVICE": (
        "이 운행의 시간·용량 조건에서 막혔으므로, 승인된 다른 운행을 먼저 시도해 볼 만합니다."
    ),
    "CHANGE_TO_APPROVED_TERMINAL": (
        "터미널 취급 조건이나 경로 한도에서 막혔으므로, 승인된 대체 터미널을 먼저 시도해 "
        "볼 만합니다."
    ),
}

NEUTRAL_TEXT = "승인된 변경을 하나씩 적용해 결과를 확인하세요."

PAIRED_TEXT = (
    "승인된 대체 터미널로 가는 기준 운행이 없어, 터미널만 바꾸면 실을 열차가 없습니다. "
    "터미널 변경과 운행 추가를 함께 시도해야 합니다."
)

# Enforced by the API for the same reason the explanation cards are: a prompt
# describing the shape is not the same as the shape being guaranteed, and the
# loop below reads `item.get(...)` on every entry.
SUGGESTIONS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["order_id", "adjustment_types", "reason"],
                "properties": {
                    "order_id": {"type": "string"},
                    "adjustment_types": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

SYSTEM_PROMPT = """\
You advise a rail slot planning operator on which approved change to try first
for an order the baseline plan could not carry.

You are not deciding anything. A constraint solver will test whatever is chosen
and may well reject it.

Return JSON: {"suggestions": [{"order_id": string, "adjustment_types": [string],
"reason": string}]}

Rules:
- One entry per order given, same order_id spelling, no extra orders.
- adjustment_types is the exact set to apply together, chosen from that order's
  permitted list. Prefer the smallest set that addresses the blocking reason;
  do not include a change that has nothing to do with it. Never invent a type
  and never name one that is not permitted for that order.
- When terminal_change_needs_a_service is true, a terminal change on its own
  leaves the order with no service running to the new destination, so pair it
  with the service addition or do not propose it.
- reason: one Korean sentence naming the concrete values involved -- the times,
  measurements or tags that blocked this order, and what the approved service
  offers instead -- not a restatement of the reason code. "이 변경이 도움이 될
  수 있습니다" says nothing the operator did not already know. Write it as
  something worth trying, never as something that will work.
- Every number, time and id in that sentence must be copied from order_facts,
  approved_services or baseline_services. Never write a date, a time or a
  measurement that does not appear there.
- Use only the ids, states and reason codes present in the input.
- Never state a probability, a percentage, a cost or carbon saving, a guarantee,
  or that real-world operation is possible.
"""


def permitted_adjustments(order: dict[str, Any]) -> list[str]:
    """The change types policy already approved for this order.

    Read from the order's own approval window, which is what the alternative
    endpoint enforces too -- asking for anything else is a 409, so a suggestion
    outside this list would be advice to make a forbidden request.
    """
    window = order.get("adjustment_window") or {}
    types: list[str] = []
    if window.get("alternative_service_ids"):
        types.append("ADD_ORDER_APPROVED_SERVICE")
    if window.get("alternative_destination_terminal_ids"):
        types.append("CHANGE_TO_APPROVED_TERMINAL")
    return types


def _needs_pairing(order: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Whether moving the destination is useless without also opening a service.

    Changing the destination terminal only re-points the order; it does not put
    a train on the new leg. If nothing in the validation scope already runs to
    an approved destination, the derived scenario has the order headed somewhere
    no permitted service goes, and the search comes back empty every time.

    Read off the snapshot, so this is a fact about the scenario rather than a
    guess about the solver. On the canonical fixture it is true of ORD-008:
    TRM-C is served by SVC-AC-01 alone, which is not in the baseline.
    """
    window = order.get("adjustment_window") or {}
    approved = set(window.get("alternative_destination_terminal_ids") or [])
    if not approved:
        return False

    in_scope = set(snapshot.get("baseline_service_ids") or [])
    return not any(
        service.get("destination_terminal_id") in approved
        for service in snapshot.get("services", [])
        if service.get("service_id") in in_scope
    )


def _template(
    permitted: list[str],
    reason_code: str | None,
    pair: bool,
) -> dict[str, Any]:
    if pair and set(permitted) >= {"CHANGE_TO_APPROVED_TERMINAL", "ADD_ORDER_APPROVED_SERVICE"}:
        return {
            "adjustment_types": ["CHANGE_TO_APPROVED_TERMINAL", "ADD_ORDER_APPROVED_SERVICE"],
            "reason": PAIRED_TEXT,
        }

    preferred = ADJUSTMENT_FOR_REASON.get(reason_code or "")
    if preferred is not None and preferred in permitted:
        return {"adjustment_types": [preferred], "reason": TEMPLATE_TEXT[preferred]}

    return {"adjustment_types": list(permitted), "reason": NEUTRAL_TEXT}


def _candidates(run: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Orders the plan left out that still have something approved to try."""
    orders = {o["order_id"]: o for o in snapshot.get("orders", [])}
    candidates: dict[str, dict[str, Any]] = {}

    for outcome in run.get("order_outcomes", []):
        # An order held at input has nothing to negotiate yet -- the missing
        # value has to be supplied before any change means anything.
        if outcome.get("input_state") != "VALID":
            continue
        if outcome.get("assignment_state") == "ASSIGNED":
            continue

        order = orders.get(outcome["order_id"])
        if order is None:
            continue

        permitted = permitted_adjustments(order)
        if not permitted:
            continue

        window = order.get("adjustment_window") or {}
        candidates[outcome["order_id"]] = {
            "order_id": outcome["order_id"],
            "primary_reason_code": outcome.get("primary_reason_code"),
            "eligibility_state": outcome.get("eligibility_state"),
            "assignment_state": outcome.get("assignment_state"),
            "permitted_adjustment_types": permitted,
            # Told to the model too, so it cannot propose the move that cannot
            # work on its own.
            "terminal_change_needs_a_service": _needs_pairing(order, snapshot),
            # The numbers the sentence is supposed to name. Asking for specifics
            # without supplying them is how a suggestion ends up quoting a date
            # from no scenario at all: told to be concrete and given nothing
            # concrete, the model fills the gap.
            "order_facts": {
                "ready_at": order.get("ready_at"),
                "due_at": order.get("due_at"),
                "gross_weight_kg": order.get("gross_weight_kg"),
                "dimensions_mm": order.get("dimensions_mm"),
                "compatibility_tags": order.get("compatibility_tags"),
                "priority_class": order.get("priority_class"),
            },
            "approved_services": [
                {
                    "service_id": service["service_id"],
                    "destination_terminal_id": service.get("destination_terminal_id"),
                    "planning_cutoff_at": service.get("planning_cutoff_at"),
                    "departure_at": service.get("departure_at"),
                    "arrival_at": service.get("arrival_at"),
                }
                for service in snapshot.get("services", [])
                if service["service_id"] in set(window.get("alternative_service_ids") or [])
            ],
            "approved_destination_terminal_ids": list(
                window.get("alternative_destination_terminal_ids") or []
            ),
            "baseline_services": [
                {
                    "service_id": service["service_id"],
                    "destination_terminal_id": service.get("destination_terminal_id"),
                    "planning_cutoff_at": service.get("planning_cutoff_at"),
                    "arrival_at": service.get("arrival_at"),
                }
                for service in snapshot.get("services", [])
                if service["service_id"] in set(snapshot.get("baseline_service_ids") or [])
            ],
        }

    return candidates


def build_suggestions(run: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return {suggestions: {order_id: {...}}, source, replaced_order_ids}."""
    candidates = _candidates(run, snapshot)
    if not candidates:
        return {"suggestions": {}, "source": "TEMPLATE", "replaced_order_ids": []}

    templates = {
        order_id: {
            "order_id": order_id,
            **_template(
                c["permitted_adjustment_types"],
                c["primary_reason_code"],
                c["terminal_change_needs_a_service"],
            ),
        }
        for order_id, c in candidates.items()
    }

    generated = _generate(candidates)
    if generated is None:
        return {
            "suggestions": templates,
            "source": "TEMPLATE",
            "replaced_order_ids": [],
        }

    outcomes = run.get("order_outcomes", [])
    allowed_ids = {o["order_id"] for o in outcomes}
    allowed_codes = {o["primary_reason_code"] for o in outcomes}
    entity_ids = explain._entity_ids(run, outcomes)
    # An approved change names the service or terminal it moves to, so those ids
    # are part of what a grounded sentence may mention.
    for order in snapshot.get("orders", []):
        window = order.get("adjustment_window") or {}
        entity_ids.update(window.get("alternative_service_ids") or [])
        entity_ids.update(window.get("alternative_destination_terminal_ids") or [])

    suggestions = dict(templates)
    replaced: list[str] = []

    for item in generated:
        order_id = item.get("order_id")
        candidate = candidates.get(order_id)
        if candidate is None:
            continue

        # Checked, not trusted: policy decides what may be asked for, and a
        # model that names a forbidden type is proposing a 409.
        chosen = [
            t
            for t in (item.get("adjustment_types") or [])
            if t in candidate["permitted_adjustment_types"]
        ]
        reason = str(item.get("reason", ""))

        rejected = explain.rejection_reason_for_text(
            reason, allowed_ids, allowed_codes, entity_ids, scope_order_id=order_id
        )
        if not chosen or rejected is not None:
            replaced.append(order_id)
            continue

        # Where the two changes have to travel together, the whole proposal
        # stays computed -- both the sentence and the set it describes.
        #
        # Overriding only the sentence let them contradict each other: the text
        # said terminal *and* service while `chosen` could be the terminal
        # alone, so the screen ticked one box under a sentence demanding two,
        # and pressing it returned NO_FEASIBLE_ALTERNATIVE every time. That is
        # the precise outcome the pairing rule exists to prevent.
        if candidate["terminal_change_needs_a_service"]:
            suggestions[order_id] = dict(templates[order_id])
            continue

        suggestions[order_id] = {
            "order_id": order_id,
            "adjustment_types": chosen,
            "reason": reason,
        }

    served_by_model = len(candidates) - len(replaced)
    return {
        "suggestions": suggestions,
        "source": "LLM" if served_by_model > 0 else "TEMPLATE",
        "replaced_order_ids": sorted(replaced),
    }


def _generate(candidates: dict[str, dict[str, Any]]) -> list[dict] | None:
    payload = list(candidates.values())
    user_prompt = (
        "Suggest which approved change to try first for each of these orders.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    response = client.complete_json(
        SYSTEM_PROMPT,
        user_prompt,
        schema=SUGGESTIONS_SCHEMA,
        schema_name="adjustment_suggestions",
        max_tokens=max(600, 200 * len(payload)),
    )
    if not response:
        return None

    suggestions = response.get("suggestions")
    if not isinstance(suggestions, list):
        return None
    return [item for item in suggestions if isinstance(item, dict)]
