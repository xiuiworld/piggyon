"""P4(a): turn an unstructured request into a structured order draft.

The model may only read values out of the text. Anything it cannot find stays
null and is reported as 확인 필요 — the same discipline the rest of the system
applies to ORD-006 (01: never invent an operational value).
"""

from __future__ import annotations

import re
from typing import Any

from app.ai import client
from app.models.snapshot import CompatibilityTag

REQUIRED_FIELDS = [
    "shipper_id",
    "origin_terminal_ids",
    "destination_terminal_ids",
    "ready_at",
    "due_at",
    "gross_weight_kg",
    "dimensions_mm",
    "compatibility_tags",
    "priority_class",
]

DRAFT_FIELDS = REQUIRED_FIELDS + ["order_id"]

SYSTEM_PROMPT = """\
You extract rail freight order fields from a Korean shipping request.

Return JSON only, with exactly this shape:
{
  "order_id": string|null,
  "shipper_id": string|null,
  "origin_terminal_ids": [string]|null,
  "destination_terminal_ids": [string]|null,
  "ready_at": "YYYY-MM-DDTHH:MM:SS+09:00"|null,
  "due_at": "YYYY-MM-DDTHH:MM:SS+09:00"|null,
  "gross_weight_kg": integer|null,
  "dimensions_mm": {"length": integer, "width": integer, "height": integer}|null,
  "compatibility_tags": ["TRAILER_STANDARD"|"TRAILER_TALL"]|null,
  "priority_class": "P1"|"P2"|"P3"|null
}

Rules:
- Use null for anything the text does not state. Never guess, never average,
  never carry a default. A null is a correct answer.
- Weight is integer kg, dimensions are integer mm. Convert stated units
  (t -> kg, m/cm -> mm) but do not invent a unit that is not written.
- Do not add fields. Do not write prose.
"""


def _empty_draft() -> dict[str, Any]:
    return {field: None for field in DRAFT_FIELDS}


def structure_request(text: str) -> dict[str, Any]:
    """Return {order_draft, missing_fields, input_state, source}."""
    draft = client.complete_json(SYSTEM_PROMPT, text) or {}
    source = "LLM" if draft else "RULE_BASED"

    if not draft:
        draft = _extract_with_rules(text)

    draft = _sanitise(draft)
    missing = [f for f in REQUIRED_FIELDS if draft.get(f) in (None, [], {})]

    return {
        "order_draft": draft,
        "missing_fields": missing,
        # Same vocabulary as the solver path, so the UI renders one badge type.
        "input_state": "REVIEW_REQUIRED" if missing else "VALID",
        "reason_codes": ["MISSING_REQUIRED_FIELD"] if missing else [],
        "source": source,
        "assumption_note": "DEMO_ASSUMPTION",
    }


def _sanitise(draft: dict[str, Any]) -> dict[str, Any]:
    """Drop anything outside the contract and coerce the types we accept.

    The model is not trusted to respect the schema; an unknown tag or a string
    weight becomes null (i.e. 확인 필요) rather than reaching the solver.
    """
    clean = _empty_draft()

    for field in DRAFT_FIELDS:
        value = draft.get(field)
        if value is None:
            continue

        if field in {"origin_terminal_ids", "destination_terminal_ids"}:
            if isinstance(value, list) and all(isinstance(v, str) for v in value) and value:
                clean[field] = value
        elif field == "compatibility_tags":
            allowed = {"TRAILER_STANDARD", "TRAILER_TALL"}
            if isinstance(value, list):
                tags = [v for v in value if v in allowed]
                clean[field] = tags or None
        elif field == "priority_class":
            clean[field] = value if value in {"P1", "P2", "P3"} else None
        elif field == "gross_weight_kg":
            clean[field] = value if isinstance(value, int) and value > 0 else None
        elif field == "dimensions_mm":
            if isinstance(value, dict) and all(
                isinstance(value.get(k), int) and value.get(k, 0) > 0
                for k in ("length", "width", "height")
            ):
                clean[field] = {k: value[k] for k in ("length", "width", "height")}
        else:
            clean[field] = value if isinstance(value, str) else None

    return clean


# `\b` is useless as a closing boundary here: Korean letters are word
# characters, so "SHP-02입니다" never matches. Assert only that the id is not
# continued by another id character.
_END = r"(?![A-Za-z0-9-])"

_PATTERNS: dict[str, re.Pattern[str]] = {
    "order_id": re.compile(rf"(ORD-\d{{3,}}){_END}"),
    "shipper_id": re.compile(rf"(SHP-\d{{2,}}){_END}"),
}
_TERMINAL = re.compile(rf"(TRM-[A-Z]){_END}")
_WEIGHT_KG = re.compile(r"(\d[\d,]*)\s*(?:kg|KG|킬로)")
_WEIGHT_TON = re.compile(r"(\d+(?:\.\d+)?)\s*(?:t\b|톤)")
_ISO_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+09:00)?)")


def _extract_with_rules(text: str) -> dict[str, Any]:
    """Deterministic fallback so the demo runs with no API key (05 §5).

    Intentionally conservative: it only lifts unambiguous tokens and leaves
    everything else null, which keeps the 확인 필요 story honest.
    """
    draft = _empty_draft()

    for field, pattern in _PATTERNS.items():
        match = pattern.search(text)
        if match:
            draft[field] = match.group(1)

    terminals = _TERMINAL.findall(text)
    if len(terminals) >= 2:
        draft["origin_terminal_ids"] = [terminals[0]]
        draft["destination_terminal_ids"] = [terminals[1]]

    if match := _WEIGHT_KG.search(text):
        draft["gross_weight_kg"] = int(match.group(1).replace(",", ""))
    elif match := _WEIGHT_TON.search(text):
        draft["gross_weight_kg"] = int(float(match.group(1)) * 1000)

    times = _ISO_TIME.findall(text)
    if len(times) >= 2:
        draft["ready_at"] = _with_offset(times[0])
        draft["due_at"] = _with_offset(times[1])

    if "TRAILER_TALL" in text:
        draft["compatibility_tags"] = ["TRAILER_TALL"]
    elif "TRAILER_STANDARD" in text:
        draft["compatibility_tags"] = ["TRAILER_STANDARD"]

    if match := re.search(rf"(P[123]){_END}", text):
        draft["priority_class"] = match.group(1)

    return draft


def _with_offset(value: str) -> str:
    return value if "+" in value else f"{value}+09:00"
