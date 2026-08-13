"""An explanation belongs to its run, and stops changing once written.

Generating on read cost two model calls and several seconds per render, and it
made the same immutable plan describe itself differently on each visit -- so a
downloaded bundle did not say what the operator had been reading when they
decided. These pin the three things that has to mean.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai import explain, suggest


@pytest.fixture
def solved(client: TestClient, request_body: dict, solver_parameters: dict) -> str:
    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    return client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]


def test_the_second_read_builds_nothing(
    client: TestClient, solved: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = {"cards": 0, "suggestions": 0}
    real_cards, real_suggestions = explain.build_cards, suggest.build_suggestions

    def counting_cards(run):
        built["cards"] += 1
        return real_cards(run)

    def counting_suggestions(run, snapshot):
        built["suggestions"] += 1
        return real_suggestions(run, snapshot)

    monkeypatch.setattr(explain, "build_cards", counting_cards)
    monkeypatch.setattr(suggest, "build_suggestions", counting_suggestions)

    first = client.get(f"/v1/runs/{solved}/explanation").json()
    for _ in range(3):
        again = client.get(f"/v1/runs/{solved}/explanation").json()
        # Byte-identical, not merely equivalent: the wording is what drifted.
        assert again == first

    assert built == {"cards": 1, "suggestions": 1}


def test_searching_an_alternative_rebuilds_it(client: TestClient, solved: str) -> None:
    """The one thing that changes a solved run has to invalidate its wording."""
    before = client.get(f"/v1/runs/{solved}/explanation").json()
    assert _card(before, "ORD-005")["display_label"] == "기본안 불가·대안 미검토"

    client.post(
        f"/v1/runs/{solved}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )

    after = client.get(f"/v1/runs/{solved}/explanation").json()
    assert _card(after, "ORD-005")["display_label"] == "기본안 불가"
    assert _card(after, "ORD-005")["display_badges"] == ["조건부 대안 있음"]


def test_the_bundle_carries_what_the_screen_showed(client: TestClient, solved: str) -> None:
    empty = client.get(f"/v1/runs/{solved}/export").json()
    # Nothing was shown, so nothing is recorded as shown.
    assert empty["explanation"] is None

    shown = client.get(f"/v1/runs/{solved}/explanation").json()
    bundled = client.get(f"/v1/runs/{solved}/export").json()["explanation"]

    assert bundled == shown


def test_deleting_the_scenario_takes_the_explanation_with_it(
    client: TestClient, solved: str
) -> None:
    scenario_id = client.get(f"/v1/runs/{solved}").json()["scenario_id"]
    client.get(f"/v1/runs/{solved}/explanation")

    client.delete(f"/v1/scenarios/{scenario_id}")

    assert client.get(f"/v1/runs/{solved}/explanation").status_code == 404


def _card(body: dict, order_id: str) -> dict:
    return next(c for c in body["cards"] if c["order_id"] == order_id)
