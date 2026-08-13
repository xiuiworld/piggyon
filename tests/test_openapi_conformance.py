"""07 §7: live responses must validate against `docs/openapi.yaml`.

That file is the contract of record (04 §10) and the front end codes against
it, so agreement is asserted rather than assumed. The service adds fields the
spec does not list — none of the spec's objects set `additionalProperties:
false`, so that is allowed — but every `required` key and every `enum` value
has to hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

jsonschema = pytest.importorskip("jsonschema", reason="schema conformance needs jsonschema")
yaml = pytest.importorskip("yaml", reason="schema conformance needs pyyaml")

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


@pytest.fixture(scope="session")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def _validate(spec: dict, schema_name: str, payload: object) -> None:
    """Check one payload against a named component schema.

    The spec's `$ref`s are document-relative (`#/components/schemas/...`), so
    the components travel inside the schema being validated and every pointer
    resolves locally — no registry or base URI to get wrong.
    """
    schema = {
        "$ref": f"#/components/schemas/{schema_name}",
        "components": spec["components"],
    }
    jsonschema.Draft202012Validator(schema).validate(payload)


@pytest.fixture
def solved(client: TestClient, request_body: dict, solver_parameters: dict) -> dict:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    validation = client.post(f"/v1/scenarios/{scenario_id}/validate").json()
    run = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()
    return {"scenario_id": scenario_id, "validation": validation, "run": run}


def test_scenario_response_conforms(
    client: TestClient, request_body: dict, spec: dict
) -> None:
    body = client.post("/v1/scenarios", json=request_body).json()

    _validate(spec, "Scenario", body)


def test_validation_response_conforms(solved: dict, spec: dict) -> None:
    _validate(spec, "ValidationResult", solved["validation"])


def test_run_response_conforms(solved: dict, spec: dict) -> None:
    _validate(spec, "Run", solved["run"])


def test_alternative_response_conforms(
    client: TestClient, solved: dict, spec: dict, expected: dict
) -> None:
    body = client.post(
        f"/v1/runs/{solved['run']['run_id']}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    _validate(spec, "AlternativeResult", body)


def test_alternative_unavailable_response_conforms(
    client: TestClient, solved: dict, spec: dict, expected: dict
) -> None:
    body = client.post(
        f"/v1/runs/{solved['run']['run_id']}/alternatives",
        json=expected["alternatives"]["ORD-009"]["request"],
    ).json()

    _validate(spec, "AlternativeUnavailableResult", body)


def test_decision_response_conforms(client: TestClient, solved: dict, spec: dict) -> None:
    body = client.post(
        f"/v1/runs/{solved['run']['run_id']}/decisions",
        json={
            "decision_state": "HELD",
            "actor_role": "SCHEDULING_OPERATOR",
            "reason": "확인 중",
            "selected_plan": "BASELINE",
        },
    ).json()

    _validate(spec, "Decision", body)


def test_export_bundle_conforms(client: TestClient, solved: dict, spec: dict) -> None:
    client.post(
        f"/v1/runs/{solved['run']['run_id']}/decisions",
        json={
            "decision_state": "HELD",
            "actor_role": "SCHEDULING_OPERATOR",
            "reason": "확인 중",
            "selected_plan": "BASELINE",
        },
    )

    body = client.get(f"/v1/runs/{solved['run']['run_id']}/export").json()

    _validate(spec, "ExportBundle", body)


def test_error_response_conforms(client: TestClient, spec: dict) -> None:
    body = client.post("/v1/scenarios", json={"scenario_name": "broken"}).json()

    _validate(spec, "Error", body)


def test_stored_snapshot_conforms(solved: dict, client: TestClient, spec: dict) -> None:
    """The snapshot is persisted as submitted, so it must still satisfy the spec."""
    stored = client.app.state.store.get_scenario(solved["scenario_id"])

    _validate(spec, "ScenarioInputSnapshot", stored["input_snapshot"])


def test_intake_response_conforms(client: TestClient, spec: dict) -> None:
    body = client.post(
        "/v1/intake/orders",
        json={
            "text": (
                Path(__file__).resolve().parent.parent
                / "data" / "samples" / "intake-01.txt"
            ).read_text(encoding="utf-8")
        },
    ).json()

    _validate(spec, "IntakeResult", body)


def test_explanation_response_conforms(
    client: TestClient, solved: dict, spec: dict
) -> None:
    body = client.get(f"/v1/runs/{solved['run']['run_id']}/explanation").json()

    _validate(spec, "ExplanationResult", body)


def test_every_served_path_is_in_the_spec(client: TestClient, spec: dict) -> None:
    """The front end generates its client from this file, so a path missing
    from it is a path the front end cannot call."""
    served = {
        path
        for path in client.get("/openapi.json").json()["paths"]
        if not path.startswith("/docs")
    }

    assert served <= set(spec["paths"]), served - set(spec["paths"])
