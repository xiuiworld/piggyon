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

Return a JSON object with `order_draft`, `field_evidence` and
`assumptions_flagged`. `order_draft` holds the order fields, every one of
which may be null. Naming JSON here is also what makes the schema-less
retry legal: that mode is refused outright unless the word appears.

Rules:
- Use null for anything the text does not state. Never guess, never average,
  never carry a default. A null is a correct answer and is preferred over a
  plausible one.
- Choose ids only from the lists given in the input. Never invent an id.
- Resolve relative dates and times against `as_of`, and emit ISO 8601 with the
  +09:00 offset.
- Weight is integer kg, dimensions are integer mm. Convert stated units
  (t -> kg, m/cm -> mm) but do not invent a unit that is not written.
- For every field you fill, quote the source phrase in `field_evidence` so an
  operator can check it against the original.
- Record any interpretation that could reasonably go another way in
  `assumptions_flagged` (for example mapping a city name to a terminal).
- Do not write prose outside these fields.
"""

# Strict Structured Outputs: every property is required and nullable, which is
# what the strict mode allows, and the "unknown stays null" rule needs anyway.
_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_ID_LIST = {"type": ["array", "null"], "items": {"type": "string"}}

INTAKE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "order_draft",
        "field_evidence",
        "assumptions_flagged",
    ],
    "properties": {
        "order_draft": {
            "type": "object",
            "additionalProperties": False,
            "required": DRAFT_FIELDS,
            "properties": {
                "order_id": _NULLABLE_STRING,
                "shipper_id": _NULLABLE_STRING,
                "origin_terminal_ids": _NULLABLE_ID_LIST,
                "destination_terminal_ids": _NULLABLE_ID_LIST,
                "ready_at": _NULLABLE_STRING,
                "due_at": _NULLABLE_STRING,
                "gross_weight_kg": {"type": ["integer", "null"]},
                "dimensions_mm": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["length", "width", "height"],
                    "properties": {
                        "length": {"type": ["integer", "null"]},
                        "width": {"type": ["integer", "null"]},
                        "height": {"type": ["integer", "null"]},
                    },
                },
                "compatibility_tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "enum": ["TRAILER_STANDARD", "TRAILER_TALL"]},
                },
                "priority_class": {"type": ["string", "null"], "enum": ["P1", "P2", "P3", None]},
            },
        },
        "field_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "source_text"],
                "properties": {
                    "field": {"type": "string"},
                    "source_text": {"type": "string"},
                },
            },
        },
        "assumptions_flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "note"],
                "properties": {
                    "field": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
    },
}


def _empty_draft() -> dict[str, Any]:
    return {field: None for field in DRAFT_FIELDS}


def structure_request(
    text: str,
    as_of: str | None = None,
    vocabulary: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Structure one request document.

    `as_of` and `vocabulary` are what make "내일" and "부산" resolvable at all:
    without a reference instant and the closed id lists, the model has nothing
    to resolve them against and would have to invent.
    """
    vocabulary = vocabulary or default_vocabulary()
    response = client.complete_json(
        SYSTEM_PROMPT,
        _user_prompt(text, as_of, vocabulary),
        schema=INTAKE_SCHEMA,
        schema_name="order_intake",
    )

    source = "LLM" if response else "RULE_BASED"
    raw_draft = (response or {}).get("order_draft") or {}
    evidence = (response or {}).get("field_evidence") or []
    assumptions = (response or {}).get("assumptions_flagged") or []

    if not response:
        raw_draft = _extract_with_rules(text)

    draft = _sanitise(raw_draft, vocabulary)
    missing = _missing_fields(draft)
    # Keep evidence only for fields that survived sanitising, so the operator
    # is never shown a justification for a value that was discarded.
    evidence = [e for e in evidence if draft.get(str(e.get("field", "")).split(".")[0])]

    return {
        "order_draft": draft,
        "missing_fields": missing,
        "review_reasons": [
            {"field": field, "reason_code": "MISSING_REQUIRED_FIELD"} for field in missing
        ],
        "field_evidence": evidence,
        "assumptions_flagged": assumptions,
        # Same vocabulary as the solver path, so the UI renders one badge type.
        "input_state": "REVIEW_REQUIRED" if missing else "VALID",
        "reason_codes": ["MISSING_REQUIRED_FIELD"] if missing else [],
        "source": source,
        "assumption_note": "DEMO_ASSUMPTION",
    }


def _missing_fields(draft: dict[str, Any]) -> list[str]:
    """Required values still absent, named precisely enough to chase up.

    A half-filled dimensions block reports `dimensions_mm.width` rather than
    `dimensions_mm`, so the operator knows which measurement to ask for.
    """
    missing: list[str] = []

    for field in REQUIRED_FIELDS:
        value = draft.get(field)
        if value in (None, [], {}):
            missing.append(field)
        elif field == "dimensions_mm" and isinstance(value, dict):
            missing.extend(
                f"dimensions_mm.{axis}"
                for axis in ("length", "width", "height")
                if value.get(axis) is None
            )

    return missing


def default_vocabulary() -> dict[str, Any]:
    """The closed id lists, read from the canonical scenario.

    Ids and names are separate fields rather than one "TRM-A (합류 터미널 A)"
    string: given the combined form the model copies it back whole, and
    "TRM-A (합류 터미널 A)" is not an id.
    """
    from app.canonical import load_canonical_snapshot

    snapshot = load_canonical_snapshot()
    return {
        "terminal_ids": [
            {"id": t["terminal_id"], "name": t["display_name"]} for t in snapshot["terminals"]
        ],
        "shipper_ids": [
            {"id": s["shipper_id"], "name": s["display_name"]} for s in snapshot["shippers"]
        ],
        "compatibility_tags": ["TRAILER_STANDARD", "TRAILER_TALL"],
        "priority_class": ["P1", "P2", "P3"],
    }


def _user_prompt(text: str, as_of: str | None, vocabulary: dict[str, list[str]]) -> str:
    import json

    header = {"as_of": as_of or "unknown", **vocabulary}
    return (
        f"Reference values:\n{json.dumps(header, ensure_ascii=False, indent=2)}\n\n"
        f"Request document:\n{text}"
    )


def _sanitise(
    draft: dict[str, Any], vocabulary: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Drop anything outside the contract and coerce the types we accept.

    Structured Outputs constrains the shape, not the truth of it, so this still
    runs: an unknown tag or a string weight becomes null (i.e. 확인 필요)
    rather than reaching the solver.
    """
    clean = _empty_draft()
    known_terminals = _ids_from(vocabulary, "terminal_ids")

    for field in DRAFT_FIELDS:
        value = draft.get(field)
        if value is None:
            continue

        if field in {"origin_terminal_ids", "destination_terminal_ids"}:
            if isinstance(value, list):
                # An id outside the closed list is an invention, not a value.
                ids = [
                    bare
                    for bare in (_bare_id(v) for v in value)
                    if bare and (not known_terminals or bare in known_terminals)
                ]
                clean[field] = ids or None
        elif field == "shipper_id":
            bare = _bare_id(value)
            known_shippers = _ids_from(vocabulary, "shipper_ids")
            clean[field] = (
                bare if bare and (not known_shippers or bare in known_shippers) else None
            )
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
            clean[field] = _sanitise_dimensions(value)
        else:
            clean[field] = value if isinstance(value, str) else None

    return clean


def _sanitise_dimensions(value: Any) -> dict[str, int | None] | None:
    """Keep the dimensions that were stated and null the ones that were not.

    Discarding all three because one is missing loses information the operator
    needs: a request that gives length and height but not width should show
    exactly that gap, not a blank set.
    """
    if not isinstance(value, dict):
        return None

    kept: dict[str, int | None] = {}
    for axis in ("length", "width", "height"):
        entry = value.get(axis)
        kept[axis] = entry if isinstance(entry, int) and entry > 0 else None

    return kept if any(v is not None for v in kept.values()) else None


def _ids_from(vocabulary: dict[str, Any] | None, key: str) -> set[str]:
    if not vocabulary:
        return set()
    return {
        entry["id"] if isinstance(entry, dict) else str(entry)
        for entry in vocabulary.get(key, [])
    }


def _bare_id(value: Any) -> str | None:
    """Recover the id when a name was appended to it.

    Belt and braces alongside the structured vocabulary: a model that answers
    "TRM-A (합류 터미널 A)" means TRM-A, and silently nulling the field would
    lose a value the operator did supply.
    """
    if not isinstance(value, str):
        return None
    return value.split(" ", 1)[0].strip() or None


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
