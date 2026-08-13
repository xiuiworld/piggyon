"""The fixture is the contract, so the fixture's own numbers are assertions.

09 §3 calls the reproducibility hashes arbitrary and takes matching them out of
scope. That turned out to be wrong — all three reproduce exactly — so they are
checked here rather than descoped.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.canonical import SCENARIO_PATH, load_canonical_snapshot
from app.hashing import sha256_of

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_FIXTURE = REPO_ROOT / "fixtures" / "canonical-v1" / "scenario.json"


def _run(client: TestClient, request_body: dict, solver_parameters: dict) -> dict:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    return client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()


def test_all_three_reproducibility_hashes_match_the_fixture(
    client: TestClient, request_body: dict, solver_parameters: dict, expected: dict
) -> None:
    want = expected["baseline_run"]["reproducibility"]

    got = _run(client, request_body, solver_parameters)["reproducibility"]

    assert got["input_snapshot_sha256"] == want["input_snapshot_sha256"]
    assert got["policy_sha256"] == want["policy_sha256"]
    assert got["result_sha256"] == want["result_sha256"]


def test_input_hash_covers_the_document_as_submitted(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    """Not a re-serialisation of the parsed model.

    Dumping the model adds this service's defaults — `intake_cutoff_minutes`
    on a terminal that never set it, an empty alternative-terminal list — so
    the hash would describe our output instead of the caller's input, and
    every historical hash would shift the day an optional field is added.
    """
    got = _run(client, request_body, solver_parameters)["reproducibility"]

    assert got["input_snapshot_sha256"] == sha256_of(request_body["input_snapshot"])


def test_stored_snapshot_is_byte_identical_to_the_request(
    client: TestClient, request_body: dict
) -> None:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]

    stored = client.app.state.store.get_scenario(scenario_id)["input_snapshot"]

    assert stored == request_body["input_snapshot"]


def test_result_hash_ignores_presentation_detail(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    """`evidence` and `next_actions` describe the decision, they are not it."""
    run = _run(client, request_body, solver_parameters)
    outcomes = run["order_outcomes"]

    assert any(o["evidence"] for o in outcomes)  # they are served...
    from app.hashing import normalise_result

    normalised = normalise_result(run["assignments"], outcomes)
    assert all(
        set(entry) == {
            "input_state",
            "eligibility_state",
            "assignment_state",
            "alternative_state",
            "primary_reason_code",
        }
        for entry in normalised["order_outcomes"].values()
    )  # ...but they do not reach the hash


def test_the_two_canonical_scenario_files_stay_identical() -> None:
    """The repo holds the input twice and reads only one of them.

    `fixtures/` is the reference carried in from planning; `data/` is the
    same-day rewrite the rules require and the loader actually uses. Nothing
    keeps them in step, so a silent divergence would move every hash while
    the reference still looked authoritative.
    """
    assert SCENARIO_PATH != REFERENCE_FIXTURE
    assert load_canonical_snapshot() == json.loads(
        REFERENCE_FIXTURE.read_text(encoding="utf-8")
    )


def test_baseline_order_update_carries_the_link_to_the_alternative(
    client: TestClient, request_body: dict, solver_parameters: dict, expected: dict
) -> None:
    """The fixture fixes both fields; without them the UI has no way through."""
    run = _run(client, request_body, solver_parameters)

    body = client.post(
        f"/v1/runs/{run['run_id']}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    update = body["baseline_order_update"]
    assert update["alternative_scenario_id"] == body["alternative_scenario_id"]
    assert update["display_badges"] == ["조건부 대안 있음"]

    # And it survives a plain read of the baseline run.
    served = client.get(f"/v1/runs/{run['run_id']}").json()
    outcome = next(o for o in served["order_outcomes"] if o["order_id"] == "ORD-005")
    assert outcome["alternative_scenario_id"] == body["alternative_scenario_id"]


def test_validator_findings_reach_the_client(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    """08 §9: a FAIL must name the rule and the resource, not just say FAIL."""
    run = _run(client, request_body, solver_parameters)
    assert "validator_findings" in run

    stored = client.app.state.store.get_run(run["run_id"])
    stored["validator_status"] = "FAIL"
    stored["validator_findings"] = [
        {"check": "duplicate_slot", "order_id": "ORD-002", "message": "slot reused"}
    ]
    client.app.state.store.save_run(stored)

    served = client.get(f"/v1/runs/{run['run_id']}").json()
    assert served["validator_findings"][0]["check"] == "duplicate_slot"


def test_export_never_invents_a_validation_result(
    client: TestClient, request_body: dict, solver_parameters: dict, expected: dict
) -> None:
    """A derived scenario is genuinely validated, so its bundle says so truthfully."""
    run = _run(client, request_body, solver_parameters)
    alternative = client.post(
        f"/v1/runs/{run['run_id']}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    bundle = client.get(f"/v1/runs/{alternative['alternative_run_id']}/export").json()

    validation = bundle["validation_result"]
    assert validation["scenario_id"] == alternative["alternative_scenario_id"]
    assert len(validation["orders"]) == 9  # a real pass, not an empty COMPLETED


def test_export_refuses_when_no_validation_was_recorded(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    run = _run(client, request_body, solver_parameters)
    client.app.state.store.save_validation(run["scenario_id"], None)

    response = client.get(f"/v1/runs/{run['run_id']}/export")

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_REQUIRED"
