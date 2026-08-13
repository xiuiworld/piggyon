"""End-to-end gate check against a running server.

    uvicorn app.main:app --port 8000
    python scripts/smoke.py

Walks P0 -> P1 -> P2 and compares against the canonical expected results.
Exits non-zero if any gate does not hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.canonical import canonical_create_request  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
EXPECTED = json.loads(
    (REPO_ROOT / "fixtures" / "canonical-v1" / "expected-results.json").read_text(
        encoding="utf-8"
    )
)

SOLVER_PARAMETERS = {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10}

EXPECTED_PRIMARY = {
    "ORD-005": "READY_AFTER_CUTOFF",
    "ORD-006": "MISSING_REQUIRED_FIELD",
    "ORD-007": "TUNNEL_HEIGHT_EXCEEDED",
    "ORD-008": "TERMINAL_NOT_COMPATIBLE",
    "ORD-009": "DUE_TIME_EXCEEDED",
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {label}{'' if condition else f' -> {detail}'}")
    if not condition:
        failures.append(label)


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        print("P0 skeleton")
        health = client.get("/health")
        check("health is ok", health.status_code == 200, health.text)
        print(f"       storage: {health.json().get('storage_backend')}")

        created = client.post("/v1/scenarios", json=canonical_create_request())
        check("canonical fixture accepted", created.status_code == 201, created.text)
        if created.status_code != 201:
            return _finish()
        scenario_id = created.json()["scenario_id"]
        check(
            "state is VALIDATION_REQUIRED",
            created.json()["state"] == "VALIDATION_REQUIRED",
            created.text,
        )
        print(f"       scenario_id: {scenario_id}")

        print("\nP1 validation and eligibility gate")
        validated = client.post(f"/v1/scenarios/{scenario_id}/validate")
        check("validate returns 200", validated.status_code == 200, validated.text)
        if validated.status_code != 200:
            return _finish()

        orders = {o["order_id"]: o for o in validated.json()["orders"]}
        check(
            "ORD-006 is REVIEW_REQUIRED",
            orders["ORD-006"]["input_state"] == "REVIEW_REQUIRED",
            str(orders["ORD-006"]),
        )
        for order_id, want in EXPECTED_PRIMARY.items():
            got = orders[order_id]["primary_reason_code"]
            check(f"{order_id} primary is {want}", got == want, f"got {got}")
        for order_id in ("ORD-001", "ORD-002", "ORD-003", "ORD-004"):
            slots = orders[order_id]["eligible_slot_ids"]
            check(f"{order_id} has candidate slots", len(slots) == 3, str(slots))

        print("\nP2 baseline plan")
        run = client.post(
            f"/v1/scenarios/{scenario_id}/runs",
            json={"solver_parameters": SOLVER_PARAMETERS},
        )
        check("run returns 201", run.status_code == 201, run.text)
        if run.status_code != 201:
            return _finish()

        body = run.json()
        check("solver_status is OPTIMAL", body["solver_status"] == "OPTIMAL", body["solver_status"])
        check("validator_status is PASS", body["validator_status"] == "PASS", body["validator_status"])
        check(
            "assignments match the fixture",
            body["assignments"] == EXPECTED["baseline"]["assignments"],
            json.dumps(body["assignments"], ensure_ascii=False),
        )
        for assignment in body["assignments"]:
            print(f"       {assignment['order_id']} -> {assignment['slot_id']}")

        want_outcomes = EXPECTED["baseline"]["order_outcomes"]
        for outcome in body["order_outcomes"]:
            want = want_outcomes[outcome["order_id"]]
            same = all(
                outcome[key] == want[key]
                for key in (
                    "input_state",
                    "eligibility_state",
                    "assignment_state",
                    "alternative_state",
                    "primary_reason_code",
                )
            )
            check(f"{outcome['order_id']} outcome matches", same, str(outcome))

        rejected = client.post(
            f"/v1/scenarios/{scenario_id}/runs",
            json={"solver_parameters": {**SOLVER_PARAMETERS, "num_search_workers": 4}},
        )
        check(
            "multi-worker run is rejected",
            rejected.status_code == 400 and rejected.json()["code"] == "INVALID_INPUT",
            rejected.text,
        )

        fetched = client.get(f"/v1/runs/{body['run_id']}")
        check("run reads back", fetched.status_code == 200, fetched.text)

    return _finish()


def _finish() -> int:
    print()
    if failures:
        print(f"GATE FAIL ({len(failures)}): {', '.join(failures)}")
        return 1
    print("P0-P2 GATES PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
