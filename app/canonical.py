"""Loader for the in-repo canonical scenario.

`data/canonical-v1/scenario.json` is the single valid baseline input; the
04 §3 request envelope around it is fixed too, so tests, the smoke script and
the front end all send byte-identical requests.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CANONICAL_DIR = Path(__file__).resolve().parent.parent / "data" / "canonical-v1"
SCENARIO_PATH = CANONICAL_DIR / "scenario.json"

SCENARIO_NAME = "canonical-v1-baseline"


@lru_cache
def load_canonical_snapshot() -> dict[str, Any]:
    return json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))


def canonical_create_request() -> dict[str, Any]:
    """The exact `POST /v1/scenarios` body from 04 §3."""
    snapshot = load_canonical_snapshot()
    return {
        "scenario_name": SCENARIO_NAME,
        "as_of": snapshot["as_of"],
        "baseline_service_ids": list(snapshot["baseline_service_ids"]),
        "policy_version": snapshot["policy"]["policy_version"],
        "assumption_ids": [a["assumption_id"] for a in snapshot["assumptions"]],
        "input_snapshot": snapshot,
    }
