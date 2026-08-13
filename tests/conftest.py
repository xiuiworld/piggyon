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
from app.config import get_settings
from app.main import app
from app.models.snapshot import ScenarioInputSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PATH = REPO_ROOT / "fixtures" / "canonical-v1" / "expected-results.json"


@pytest.fixture(autouse=True)
def offline_generative_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches OpenAI unless it says so.

    `is_available()` reads the configured key, so a developer with one in `.env`
    ran a different suite from one without: the fallback tests exercised the
    live path instead of the fallback, the run took a minute and a half instead
    of seconds, and every full run spent money. Worse, the tests that passed
    said nothing about the behaviour they were named for.

    Cleared at the settings layer rather than by stubbing the client, so the
    code under test takes exactly the branch it takes in an unconfigured
    deployment. A test that wants the live layer patches `client.complete_json`
    with a canned reply, which is what the guard tests do.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
