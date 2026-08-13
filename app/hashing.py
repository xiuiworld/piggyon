"""Reproducibility hashes (07 §8).

Normalisation: sort `order_outcomes` by order_id and `assignments` by
(order_id, service_id, wagon_id, slot_id); drop values that differ between
identical runs (run_id, timestamps, durations); serialise as UTF-8 JSON with
sorted keys and no whitespace; SHA-256 the bytes.

The hashes in `fixtures/canonical-v1/expected-results.json` are placeholders
(09 §3), so these are computed and reported, never compared against them.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

VOLATILE_KEYS = frozenset(
    {"run_id", "created_at", "occurred_at", "solve_time_seconds", "trace_id"}
)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def strip_volatile(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: strip_volatile(value)
            for key, value in payload.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(payload, list):
        return [strip_volatile(item) for item in payload]
    return payload


def normalise_result(assignments: list[dict], order_outcomes: list[dict]) -> dict:
    return {
        "assignments": sorted(
            (strip_volatile(a) for a in assignments),
            key=lambda a: (a["order_id"], a["service_id"], a["wagon_id"], a["slot_id"]),
        ),
        "order_outcomes": sorted(
            (strip_volatile(o) for o in order_outcomes),
            key=lambda o: o["order_id"],
        ),
    }


def result_sha256(assignments: list[dict], order_outcomes: list[dict]) -> str:
    return sha256_of(normalise_result(assignments, order_outcomes))
