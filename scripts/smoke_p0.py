"""P0 gate check against a running server.

    uvicorn app.main:app --port 8000
    python scripts/smoke_p0.py

Exits non-zero if the gate does not hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.canonical import canonical_create_request  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        health = client.get("/health")
        print(f"GET /health -> {health.status_code} {health.text}")
        if health.status_code != 200 or not health.json()["storage_reachable"]:
            print("FAIL: storage is not reachable")
            return 1

        created = client.post("/v1/scenarios", json=canonical_create_request())
        print(f"POST /v1/scenarios -> {created.status_code}")
        print(json.dumps(created.json(), indent=2, ensure_ascii=False))
        if created.status_code != 201:
            print("FAIL: canonical fixture was not accepted")
            return 1

        body = created.json()
        if not body.get("scenario_id") or body.get("state") != "VALIDATION_REQUIRED":
            print("FAIL: unexpected response body")
            return 1

        rejected = client.post("/v1/scenarios", json={"scenario_name": "broken"})
        print(f"POST /v1/scenarios (malformed) -> {rejected.status_code}")
        if rejected.status_code != 400 or rejected.json()["code"] != "INVALID_INPUT":
            print("FAIL: malformed request did not return 400 INVALID_INPUT")
            return 1

    print("\nP0 GATE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
