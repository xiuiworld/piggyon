"""End-to-end gate check against a running server.

    uvicorn app.main:app --port 8000
    python scripts/smoke.py                      # local
    python scripts/smoke.py https://your.app     # deployed

Walks the five demo scenes (08 §4) and compares against the canonical expected
results. Exits non-zero if any gate does not hold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The checks print Korean labels; a cp949 console would raise on the first one.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from app.canonical import canonical_create_request  # noqa: E402

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
EXPECTED = json.loads(
    (REPO_ROOT / "fixtures" / "canonical-v1" / "expected-results.json").read_text(
        encoding="utf-8"
    )
)
SAMPLE = (REPO_ROOT / "data" / "samples" / "intake-01.txt").read_text(encoding="utf-8")

SOLVER_PARAMETERS = {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10}

EXPECTED_PRIMARY = {
    "ORD-005": "READY_AFTER_CUTOFF",
    "ORD-006": "MISSING_REQUIRED_FIELD",
    "ORD-007": "TUNNEL_HEIGHT_EXCEEDED",
    "ORD-008": "TERMINAL_NOT_COMPATIBLE",
    "ORD-009": "DUE_TIME_EXCEEDED",
}

failures: list[str] = []

# Every scenario this run brings into being, parents last.
#
# The gate writes to whatever it is pointed at, which for a deployment check is
# production. Each run left a baseline and the two scenarios its alternatives
# derive, so verifying a deploy quietly cost three rows that nobody would ever
# open again -- the store reached 187 scenarios of which five were meant to be
# there, and the demo's own list became unreadable.
created_scenarios: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {label}{'' if condition else f' -> {detail}'}")
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as client:
        # try/finally, because the gate returns early on any failed check. Those
        # are the runs that matter most: a broken deploy gets the gate re-run
        # while it is debugged, and every attempt used to leave its scenario
        # behind -- the store filled fastest exactly when this was meant to
        # protect it.
        try:
            section("P0  skeleton and storage")
            health = client.get("/health")
            check("health is ok", health.status_code == 200, health.text)
            if health.status_code != 200:
                return _finish()
            backend = health.json().get("storage_backend")
            print(f"       storage backend: {backend}")
            check("storage is reachable", health.json().get("storage_reachable") is True)
            if backend != "supabase":
                print("       NOTE: not on Supabase — this is the in-memory fallback")

            created = client.post("/v1/scenarios", json=canonical_create_request())
            check("canonical fixture accepted", created.status_code == 201, created.text)
            if created.status_code != 201:
                return _finish()
            scenario_id = created.json()["scenario_id"]
            created_scenarios.append(scenario_id)
            print(f"       scenario_id: {scenario_id}")

            section("Scene 1-2  input validation and hard constraints (P1)")
            validated = client.post(f"/v1/scenarios/{scenario_id}/validate")
            check("validate returns 200", validated.status_code == 200, validated.text)
            if validated.status_code != 200:
                return _finish()
            orders = {o["order_id"]: o for o in validated.json()["orders"]}
            check(
                "ORD-006 is REVIEW_REQUIRED with the field named",
                orders["ORD-006"]["input_state"] == "REVIEW_REQUIRED"
                and orders["ORD-006"]["missing_fields"] == ["gross_weight_kg"],
                str(orders["ORD-006"]),
            )
            for order_id, want in EXPECTED_PRIMARY.items():
                got = orders[order_id]["primary_reason_code"]
                check(f"{order_id} primary is {want}", got == want, f"got {got}")
            for order_id in ("ORD-001", "ORD-002", "ORD-003", "ORD-004"):
                check(
                    f"{order_id} has candidate slots",
                    len(orders[order_id]["eligible_slot_ids"]) == 3,
                    str(orders[order_id]["eligible_slot_ids"]),
                )

            section("Scene 3  baseline plan (P2)")
            run = client.post(
                f"/v1/scenarios/{scenario_id}/runs",
                json={"solver_parameters": SOLVER_PARAMETERS},
            )
            check("run returns 201", run.status_code == 201, run.text)
            if run.status_code != 201:
                return _finish()
            body = run.json()
            run_id = body["run_id"]
            check("solver_status is OPTIMAL", body["solver_status"] == "OPTIMAL", body["solver_status"])
            check("validator_status is PASS", body["validator_status"] == "PASS", body["validator_status"])
            check(
                "assignments match the fixture",
                body["assignments"] == EXPECTED["baseline"]["assignments"],
                json.dumps(body["assignments"], ensure_ascii=False),
            )
            for assignment in body["assignments"]:
                print(f"       {assignment['order_id']} -> {assignment['slot_id']}")
            outcomes = {o["order_id"]: o for o in body["order_outcomes"]}
            check(
                "ORD-004 is ELIGIBLE but unassigned on capacity",
                outcomes["ORD-004"]["assignment_state"] == "UNASSIGNED"
                and outcomes["ORD-004"]["primary_reason_code"] == "CAPACITY_CONFLICT",
                str(outcomes["ORD-004"]),
            )
            check(
                "multi-worker run is rejected",
                client.post(
                    f"/v1/scenarios/{scenario_id}/runs",
                    json={"solver_parameters": {**SOLVER_PARAMETERS, "num_search_workers": 4}},
                ).status_code == 400,
            )

            section("Scene 4  conditional alternatives (P3)")
            for order_id in ("ORD-005", "ORD-008"):
                want = EXPECTED["alternatives"][order_id]
                response = client.post(f"/v1/runs/{run_id}/alternatives", json=want["request"])
                ok = response.status_code == 201
                check(f"{order_id} alternative is 201", ok, response.text)
                if not ok:
                    continue
                alt = response.json()
                # Before the parent, so the delete order below is child-first.
                created_scenarios.insert(0, alt["alternative_scenario_id"])
                check(f"{order_id} change_set matches", alt["change_set"] == want["change_set"],
                      json.dumps(alt["change_set"], ensure_ascii=False))
                check(f"{order_id} impacts only itself",
                      alt["impacted_order_ids"] == [order_id], str(alt["impacted_order_ids"]))
                check(f"{order_id} deltas match", alt["assignment_deltas"] == want["assignment_deltas"])
                print(f"       {order_id} -> {alt['assignment_deltas'][0]['after_assignment']['slot_id']}"
                      f" via {alt['alternative_run_id']}")

            for order_id in ("ORD-007", "ORD-009"):
                want = EXPECTED["alternatives"][order_id]
                response = client.post(f"/v1/runs/{run_id}/alternatives", json=want["request"])
                check(
                    f"{order_id} has no feasible alternative",
                    response.status_code == 200
                    and response.json()["status"] == "NO_FEASIBLE_ALTERNATIVE"
                    and response.json()["reason_code"] == want["reason_code"],
                    response.text,
                )

            tc10 = EXPECTED["negative_api_cases"]["TC-10"]
            refused = client.post(f"/v1/runs/{run_id}/alternatives", json=tc10["request"])
            check(
                "forbidden change is 409 POLICY_VIOLATION",
                refused.status_code == 409 and refused.json()["code"] == "POLICY_VIOLATION",
                refused.text,
            )

            baseline_after = client.get(f"/v1/runs/{run_id}").json()
            check(
                "baseline never uses an alternative-only service",
                all(a["service_id"] == "SVC-AM-01" for a in baseline_after["assignments"]),
                str(baseline_after["assignments"]),
            )
            badges = {o["order_id"]: o["alternative_state"] for o in baseline_after["order_outcomes"]}
            check("ORD-005 baseline badge is AVAILABLE", badges["ORD-005"] == "AVAILABLE", badges["ORD-005"])
            check("ORD-007 baseline badge is NONE", badges["ORD-007"] == "NONE", badges["ORD-007"])

            section("P4  generative layer")
            status = client.get("/v1/ai/status").json()
            print(f"       llm_available: {status['llm_available']}")
            intake = client.post("/v1/intake/orders", json={"text": SAMPLE})
            check("intake structures the request", intake.status_code == 200, intake.text)
            if intake.status_code == 200:
                payload = intake.json()
                check(
                    "intake flags the missing field instead of guessing",
                    payload["input_state"] == "REVIEW_REQUIRED" and payload["missing_fields"],
                    str(payload["missing_fields"]),
                )
                print(f"       source: {payload['source']} | missing: {payload['missing_fields']}")
            cards = client.get(f"/v1/runs/{run_id}/explanation")
            check("explanation covers every order",
                  cards.status_code == 200 and len(cards.json()["cards"]) == 9, cards.text)
            if cards.status_code == 200:
                labels = {c["order_id"]: c["display_label"] for c in cards.json()["cards"]}
                check("labels follow 02 §4",
                      labels["ORD-001"] == "편성 가능"
                      and labels["ORD-004"] == "편성 가능·미배정"
                      and labels["ORD-006"] == "확인 필요",
                      str(labels))

            section("Scene 5  decision and export (P5)")
            held = client.post(
                f"/v1/runs/{run_id}/decisions",
                json={
                    "decision_state": "HELD",
                    "actor_role": "SCHEDULING_OPERATOR",
                    "reason": "ORD-005 대안의 실제 반입 가능 여부를 확인한다.",
                    "selected_plan": "BASELINE",
                },
            )
            check("decision is recorded", held.status_code == 201, held.text)

            bundle = client.get(f"/v1/runs/{run_id}/export")
            check("export returns a bundle", bundle.status_code == 200, bundle.text)
            if bundle.status_code == 200:
                data = bundle.json()
                check(
                    "bundle carries input, policy, run, validation, decisions, trace",
                    len(data["input_snapshot"]["orders"]) == 9
                    and data["policy"]["policy_version"] == "1.0.0"
                    and data["run"]["reproducibility"]["result_sha256"]
                    and data["validation_result"]["validation_status"] == "COMPLETED"
                    and len(data["decisions"]) >= 1
                    and len(data["trace_events"]) >= 4,
                    f"decisions={len(data['decisions'])} trace={len(data['trace_events'])}",
                )


        finally:
            _clean_up(client)

    return _finish()


def _clean_up(client: httpx.Client) -> None:
    """Remove everything this run created, children before parents.

    Deleting a scenario that something was derived from is refused with a 409,
    which is the right rule and the reason for the ordering here rather than a
    reason to skip the step.

    Reported as a check like everything else. A gate that leaves rows behind and
    says nothing is how the store filled up unnoticed; a cleanup that silently
    fails would hide the same thing one layer down.
    """
    section("Cleanup")
    if not created_scenarios:
        print("  [ok ] nothing to remove")
        return

    left: list[str] = []
    for scenario_id in created_scenarios:
        response = client.delete(f"/v1/scenarios/{scenario_id}")
        if response.status_code not in (204, 404):
            left.append(f"{scenario_id}:{response.status_code}")

    check(
        f"removed {len(created_scenarios)} scenario(s) this run created",
        not left,
        ", ".join(left),
    )


def _finish() -> int:
    print()
    if failures:
        print(f"GATE FAIL ({len(failures)}): {', '.join(failures)}")
        return 1
    print("ALL GATES PASS (P0-P5, demo scenes 1-5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
