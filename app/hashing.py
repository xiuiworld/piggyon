"""Reproducibility hashes (07 §8).

Normalisation: `assignments` sorted by (order_id, service_id, wagon_id,
slot_id); `order_outcomes` keyed by order_id carrying only the five state
axes; values that differ between identical runs (run_id, timestamps,
durations) dropped; serialised as UTF-8 JSON with sorted keys and no
whitespace; SHA-256 over those bytes.

09 §3 calls the fixture's hashes arbitrary and takes hash matching out of
scope. That is wrong: all three in `expected-results.json` reproduce exactly
under this normalisation, so they are a real cross-check and the tests treat
them as one.

Two details decide it, and both were originally wrong here:

- The input hash covers the snapshot **as submitted**, not a re-serialisation
  of the parsed model. Dumping the model injects its own defaults
  (`intake_cutoff_minutes: null`, an empty `alternative_destination_terminal_ids`)
  which the caller never sent. It would also mean that adding an optional
  field later silently changes every historical hash.
- `order_outcomes` contributes only the five state axes. `evidence` and
  `next_actions` are presentation detail, not the decision being fixed.
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


STATE_AXES = ("input_state", "eligibility_state", "assignment_state",
              "alternative_state", "primary_reason_code")


def normalise_result(assignments: list[dict], order_outcomes: list[dict]) -> dict:
    return {
        "assignments": sorted(
            (strip_volatile(a) for a in assignments),
            key=lambda a: (a["order_id"], a["service_id"], a["wagon_id"], a["slot_id"]),
        ),
        # Keyed by order_id, which sorted-key serialisation orders for us.
        "order_outcomes": {
            outcome["order_id"]: {axis: outcome[axis] for axis in STATE_AXES}
            for outcome in order_outcomes
        },
    }


def result_sha256(assignments: list[dict], order_outcomes: list[dict]) -> str:
    return sha256_of(normalise_result(assignments, order_outcomes))
