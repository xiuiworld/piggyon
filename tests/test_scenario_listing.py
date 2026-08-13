"""`GET /v1/scenarios` and the lineage fields the detail response now carries.

A scenario used to be reachable only by an id the caller kept from the response
that created it. The store held the record either way, so closing a tab lost
work that had not gone anywhere.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create(client: TestClient, request_body: dict) -> str:
    return client.post("/v1/scenarios", json=request_body).json()["scenario_id"]


def test_empty_store_lists_nothing(client: TestClient) -> None:
    response = client.get("/v1/scenarios")

    assert response.status_code == 200
    assert response.json() == []


def test_created_scenario_appears_in_the_list(
    client: TestClient, request_body: dict
) -> None:
    scenario_id = _create(client, request_body)

    body = client.get("/v1/scenarios").json()

    assert [row["scenario_id"] for row in body] == [scenario_id]
    row = body[0]
    assert row["scenario_name"] == request_body["scenario_name"]
    assert row["state"] == "VALIDATION_REQUIRED"
    assert row["order_count"] == 9
    # Created but never solved. The screen offers to solve rather than link.
    assert row["latest_run_id"] is None
    assert row["parent_scenario_id"] is None
    assert row["change_set"] == []


def test_newest_first(client: TestClient, request_body: dict) -> None:
    first = _create(client, request_body)
    second = _create(client, request_body)

    ids = [row["scenario_id"] for row in client.get("/v1/scenarios").json()]

    assert ids.index(second) < ids.index(first)


def test_limit_caps_the_page(client: TestClient, request_body: dict) -> None:
    for _ in range(3):
        _create(client, request_body)

    assert len(client.get("/v1/scenarios", params={"limit": 2}).json()) == 2

    # 04 §1: request validation answers in the same error envelope as every
    # other failure, so an out-of-range limit is a 400 INVALID_INPUT rather
    # than FastAPI's bare 422.
    for out_of_range in (0, 101):
        refused = client.get("/v1/scenarios", params={"limit": out_of_range})
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "INVALID_INPUT"


def test_a_solved_scenario_links_to_its_run(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id = _create(client, request_body)
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    run_id = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]

    row = next(
        r for r in client.get("/v1/scenarios").json() if r["scenario_id"] == scenario_id
    )

    assert row["latest_run_id"] == run_id
    assert row["state"] == "SOLVED"


def test_an_alternative_is_listed_with_its_parent_and_change(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id = _create(client, request_body)
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    run_id = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]

    alternative = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )
    assert alternative.status_code == 201, alternative.text
    derived_id = alternative.json()["alternative_scenario_id"]

    row = next(
        r for r in client.get("/v1/scenarios").json() if r["scenario_id"] == derived_id
    )

    # The lineage is what tells a screen this row is a derived plan rather than
    # another scenario someone started from scratch.
    assert row["parent_scenario_id"] == scenario_id
    assert row["change_set"] == [
        {"type": "ADD_ORDER_APPROVED_SERVICE", "service_id": "SVC-NEXT-01"}
    ]
    assert row["latest_run_id"] == alternative.json()["alternative_run_id"]


def test_detail_carries_the_lineage_too(
    client: TestClient, request_body: dict, solver_parameters: dict
) -> None:
    scenario_id = _create(client, request_body)
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    run_id = client.post(
        f"/v1/scenarios/{scenario_id}/runs",
        json={"solver_parameters": solver_parameters},
    ).json()["run_id"]
    derived_id = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    ).json()["alternative_scenario_id"]

    derived = client.get(f"/v1/scenarios/{derived_id}").json()
    parent = client.get(f"/v1/scenarios/{scenario_id}").json()

    assert derived["parent_scenario_id"] == scenario_id
    assert derived["change_set"][0]["service_id"] == "SVC-NEXT-01"
    # A link into a scenario should not need a run id beside it.
    assert derived["latest_run_id"].startswith("RUN-ALT-")
    assert parent["latest_run_id"] == run_id
    # A scenario created directly has no lineage, and says so rather than
    # omitting the fields.
    assert parent["parent_scenario_id"] is None
    assert parent["change_set"] == []
