"""Shared fixtures.

`expected-results.json` is the contract of record for P1/P2 outcomes, so the
tests read it rather than restating its values.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.canonical import canonical_create_request, load_canonical_snapshot
from app.main import app
from app.models.snapshot import ScenarioInputSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PATH = REPO_ROOT / "fixtures" / "canonical-v1" / "expected-results.json"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def request_body() -> dict:
    return copy.deepcopy(canonical_create_request())


@pytest.fixture
def snapshot() -> ScenarioInputSnapshot:
    return ScenarioInputSnapshot.model_validate(load_canonical_snapshot())


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def solver_parameters() -> dict:
    return {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10}


@pytest.fixture
def validated_scenario_id(client: TestClient, request_body: dict) -> str:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    return scenario_id
