"""Regressions for defects the canonical fixture happened to hide.

Each of these survived a full green suite because the canonical scenario has
properties that mask them — every other order is filtered out by
DUE_TIME_EXCEEDED, no approved order is already assigned, nothing ever times
out. The tests here construct the conditions the fixture does not.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from ortools.sat.python import cp_model

from app.canonical import canonical_create_request, load_canonical_snapshot
from app.models.snapshot import ScenarioInputSnapshot
from app.rules.eligibility import evaluate_scenario
from app.services.alternatives import (
    AmbiguousAlternative,
    apply_to_baseline,
    build_change_set,
    derive_snapshot,
)
from app.solver.baseline import SolverParameters, solve_baseline
from app.validation.plan_validator import validate_plan

SOLVER = {"random_seed": 7, "num_search_workers": 1, "max_time_seconds": 10}


def _solved(client: TestClient, body: dict) -> str:
    scenario_id = client.post("/v1/scenarios", json=body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    return client.post(
        f"/v1/scenarios/{scenario_id}/runs", json={"solver_parameters": SOLVER}
    ).json()["run_id"]


# --- A-1: the approved service belongs to the requesting order --------------


@pytest.fixture
def body_with_window_on_assigned_order() -> dict:
    body = copy.deepcopy(canonical_create_request())
    for order in body["input_snapshot"]["orders"]:
        if order["order_id"] == "ORD-001":
            order["adjustment_window"] = {
                "alternative_service_ids": ["SVC-NEXT-01"],
                "alternative_destination_terminal_ids": [],
            }
    return body


def test_added_service_does_not_reach_other_orders(
    client: TestClient, body_with_window_on_assigned_order: dict
) -> None:
    """Requesting for ORD-001 must not hand ORD-005 the new service.

    The derived snapshot widens `baseline_service_ids` for validation scope,
    and the evaluator used to read its default from there, so every order saw
    the addition and an unrelated one took the slot.
    """
    run_id = _solved(client, body_with_window_on_assigned_order)

    response = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-001", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )

    assert "ORD-005" not in json.dumps(response.json())


def test_derived_evaluation_keeps_other_orders_on_the_baseline(snapshot) -> None:
    payload = snapshot.model_dump(mode="json")
    for order in payload["orders"]:
        if order["order_id"] == "ORD-001":
            order["adjustment_window"] = {"alternative_service_ids": ["SVC-NEXT-01"]}
    widened = ScenarioInputSnapshot.model_validate(payload)

    derived, order_services = derive_snapshot(
        widened, "SCN-X", "ORD-001",
        [{"type": "ADD_ORDER_APPROVED_SERVICE", "service_id": "SVC-NEXT-01"}],
    )
    evaluation = evaluate_scenario(
        derived,
        service_ids=list(widened.baseline_service_ids),
        service_ids_by_order={"ORD-001": order_services},
    )

    assert "SVC-NEXT-01" in evaluation.by_order_id["ORD-001"].evidence
    for order_id in ("ORD-002", "ORD-005", "ORD-009"):
        assert "SVC-NEXT-01" not in evaluation.by_order_id[order_id].evidence, order_id


def test_canonical_alternatives_still_impact_one_order(
    client: TestClient, request_body: dict, expected: dict
) -> None:
    run_id = _solved(client, request_body)

    for order_id in ("ORD-005", "ORD-008"):
        body = client.post(
            f"/v1/runs/{run_id}/alternatives",
            json=expected["alternatives"][order_id]["request"],
        ).json()
        assert body["impacted_order_ids"] == [order_id]


# --- A-2 / C-6: an unsolved run is refused, not published -------------------


def test_unsolved_run_is_refused_with_503(
    monkeypatch, client: TestClient, request_body: dict
) -> None:
    """No plan means no per-order verdict. Storing the run stamped every
    eligible order CAPACITY_CONFLICT, and the validator PASSed it."""
    import app.solver.baseline as baseline

    scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
    client.post(f"/v1/scenarios/{scenario_id}/validate")
    monkeypatch.setattr(baseline, "_optimise", lambda *a, **k: (cp_model.UNKNOWN, False))

    response = client.post(
        f"/v1/scenarios/{scenario_id}/runs", json={"solver_parameters": SOLVER}
    )

    assert response.status_code == 503
    assert response.json()["code"] == "SOLVER_UNAVAILABLE"
    assert client.app.state.store.get_run("RUN-001") is None


def test_unsolved_solve_never_blames_capacity(monkeypatch, snapshot) -> None:
    import app.solver.baseline as baseline

    monkeypatch.setattr(baseline, "_optimise", lambda *a, **k: (cp_model.UNKNOWN, False))

    result = solve_baseline(snapshot, evaluate_scenario(snapshot), SolverParameters())

    assert result.run_state == "ERROR"
    assert not [o for o in result.order_outcomes
                if o.primary_reason_code == "CAPACITY_CONFLICT"]


# --- A-3: a timestamp without an offset is malformed input ------------------


@pytest.mark.parametrize(
    ("path", "mutate"),
    [
        ("as_of", lambda s: s.__setitem__("as_of", "2026-08-17T08:00:00")),
        ("orders[0].ready_at", lambda s: s["orders"][0].__setitem__("ready_at", "2026-08-17T09:00:00")),
        ("services[0].departure_at", lambda s: s["services"][0].__setitem__("departure_at", "2026-08-17T12:00:00")),
    ],
)
def test_naive_timestamp_is_rejected_at_creation(
    client: TestClient, request_body: dict, path: str, mutate
) -> None:
    """It used to pass creation and validation, then raise inside the solve."""
    mutate(request_body["input_snapshot"])

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400, path
    assert response.json()["code"] == "INVALID_INPUT"


# --- A-4: one destination per alternative -----------------------------------


def test_two_approved_terminals_are_refused(snapshot) -> None:
    """Applying them overwrote each other while reporting both as applied."""
    payload = snapshot.model_dump(mode="json")
    for order in payload["orders"]:
        if order["order_id"] == "ORD-008":
            order["adjustment_window"]["alternative_destination_terminal_ids"] = [
                "TRM-C",
                "TRM-B",
            ]
    ambiguous = ScenarioInputSnapshot.model_validate(payload)

    with pytest.raises(AmbiguousAlternative):
        build_change_set(ambiguous, "ORD-008", ["CHANGE_TO_APPROVED_TERMINAL"])


def test_ambiguous_terminal_request_is_400(client: TestClient, request_body: dict) -> None:
    for order in request_body["input_snapshot"]["orders"]:
        if order["order_id"] == "ORD-008":
            order["adjustment_window"]["alternative_destination_terminal_ids"] = [
                "TRM-C",
                "TRM-B",
            ]
    run_id = _solved(client, request_body)

    response = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-008", "adjustment_types": ["CHANGE_TO_APPROVED_TERMINAL"]},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_every_reported_change_is_actually_applied(snapshot) -> None:
    """The invariant the overwrite broke: change_set == what happened."""
    change_set = build_change_set(snapshot, "ORD-008", ["CHANGE_TO_APPROVED_TERMINAL"])
    derived, _ = derive_snapshot(snapshot, "SCN-X", "ORD-008", change_set)

    applied = next(o.destination_terminal_ids for o in derived.orders if o.order_id == "ORD-008")
    reported = [c["destination_terminal_id"] for c in change_set
                if c["type"] == "CHANGE_TO_APPROVED_TERMINAL"]
    assert applied == reported


# --- A-5: the state and the link move together ------------------------------


def test_none_transition_clears_the_alternative_link(
    client: TestClient, request_body: dict
) -> None:
    run_id = _solved(client, request_body)
    client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )

    client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-005", "adjustment_types": ["CHANGE_TO_APPROVED_TERMINAL"]},
    )

    outcome = next(
        o for o in client.get(f"/v1/runs/{run_id}").json()["order_outcomes"]
        if o["order_id"] == "ORD-005"
    )
    assert outcome["alternative_state"] == "NONE"
    assert outcome["alternative_scenario_id"] is None
    assert outcome["display_badges"] == []


def test_apply_to_baseline_pairs_state_and_link() -> None:
    run = {"order_outcomes": [{"order_id": "ORD-005", "alternative_state": "NOT_SEARCHED"}]}

    apply_to_baseline(run, "ORD-005", "AVAILABLE", "SCN-ALT-001")
    assert run["order_outcomes"][0]["alternative_scenario_id"] == "SCN-ALT-001"

    apply_to_baseline(run, "ORD-005", "NONE")
    assert run["order_outcomes"][0]["alternative_scenario_id"] is None


# --- B-1: every failure wears the envelope ----------------------------------


def test_unknown_path_answers_in_the_envelope(client: TestClient) -> None:
    response = client.get("/v1/definitely-not-a-route")

    body = response.json()
    assert set(body) >= {"code", "message", "details", "trace_id"}
    assert body["trace_id"]


def test_unexpected_error_answers_in_the_envelope(
    monkeypatch, request_body: dict
) -> None:
    import app.routers.scenarios as scenarios
    from app.main import app

    def boom(*_args, **_kwargs):
        raise RuntimeError("solver exploded")

    # raise_server_exceptions=False so the handler's real response is returned
    # rather than the exception being re-raised into the test.
    with TestClient(app, raise_server_exceptions=False) as client:
        scenario_id = client.post("/v1/scenarios", json=request_body).json()["scenario_id"]
        client.post(f"/v1/scenarios/{scenario_id}/validate")
        monkeypatch.setattr(scenarios, "run_baseline", boom)

        response = client.post(
            f"/v1/scenarios/{scenario_id}/runs", json={"solver_parameters": SOLVER}
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) >= {"code", "message", "details", "trace_id"}
    # The exception text is for the log, not the client.
    assert "solver exploded" not in body["message"]


# --- B-2: the served schema keeps a 422 the route really returns ------------


def test_served_schema_keeps_declared_422(client: TestClient) -> None:
    served = client.get("/openapi.json").json()["paths"]

    assert "422" in served["/v1/scenarios/{scenario_id}/runs"]["post"]["responses"]
    assert "422" in served["/v1/runs/{run_id}/export"]["get"]["responses"]
    # FastAPI's automatic one is still stripped where no route declared it.
    assert "422" not in served["/v1/scenarios"]["post"]["responses"]


# --- B-4: an alternative that changes nothing -------------------------------


def test_no_op_alternative_is_reported_not_crashed(
    client: TestClient, request_body: dict
) -> None:
    """The derived plan equals the baseline, so there are no deltas.

    Reporting success built a result whose `assignment_deltas` broke its own
    minItems rule — a 500 raised *after* the derived scenario had been stored.
    """
    for order in request_body["input_snapshot"]["orders"]:
        if order["order_id"] == "ORD-001":
            order["adjustment_window"] = {
                "alternative_service_ids": ["SVC-AM-01"],
                "alternative_destination_terminal_ids": [],
            }
    run_id = _solved(client, request_body)

    response = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json={"order_id": "ORD-001", "adjustment_types": ["ADD_ORDER_APPROVED_SERVICE"]},
    )

    assert response.status_code == 200
    assert response.json()["reason_code"] == "ALTERNATIVE_NOT_REQUIRED"
    assert client.app.state.store.get_run("RUN-ALT-001") is None
    assert client.app.state.store.get_scenario("SCN-ALT-001") is None


# --- B-5: concurrent alternatives on one run --------------------------------


def test_concurrent_alternatives_do_not_overwrite_each_other(
    client: TestClient, request_body: dict
) -> None:
    """Two operators, two orders, interleaved read-modify-write.

    A real backend hands each handler an independent document, so writing the
    whole run back reverted whichever order was recorded first.
    """
    run_id = _solved(client, request_body)
    store = client.app.state.store

    def independent_read() -> dict:
        return json.loads(json.dumps(store.get_run(run_id)))

    doc_a, doc_b = independent_read(), independent_read()
    update_a = apply_to_baseline(doc_a, "ORD-005", "AVAILABLE", "SCN-ALT-001")
    update_b = apply_to_baseline(doc_b, "ORD-008", "AVAILABLE", "SCN-ALT-002")
    store.update_order_outcome(run_id, "ORD-005", update_a)
    store.update_order_outcome(run_id, "ORD-008", update_b)

    outcomes = {o["order_id"]: o for o in store.get_run(run_id)["order_outcomes"]}
    assert outcomes["ORD-005"]["alternative_state"] == "AVAILABLE"
    assert outcomes["ORD-008"]["alternative_state"] == "AVAILABLE"


# --- C-1: a time-range fault is not a missing field -------------------------


def test_invalid_time_range_does_not_ask_for_missing_fields(snapshot) -> None:
    payload = snapshot.model_dump(mode="json")
    for order in payload["orders"]:
        if order["order_id"] == "ORD-002":
            order["due_at"] = order["ready_at"]
    broken = ScenarioInputSnapshot.model_validate(payload)

    result = solve_baseline(broken, evaluate_scenario(broken), SolverParameters())
    outcome = next(o for o in result.order_outcomes if o.order_id == "ORD-002")

    assert outcome.primary_reason_code == "INVALID_TIME_RANGE"
    assert outcome.next_actions == ["CORRECT_INPUT_VALUES"]
    assert "missing_fields" not in outcome.evidence
    assert "INVALID_TIME_RANGE" in outcome.evidence["reason_codes"]


def test_missing_field_still_asks_to_complete_it(snapshot) -> None:
    result = solve_baseline(snapshot, evaluate_scenario(snapshot), SolverParameters())
    outcome = next(o for o in result.order_outcomes if o.order_id == "ORD-006")

    assert outcome.next_actions == ["COMPLETE_REQUIRED_FIELDS"]
    assert outcome.evidence["missing_fields"] == ["gross_weight_kg"]


# --- C-2: the schema-less retry has to be legal -----------------------------


def test_intake_prompt_names_json_so_the_retry_is_accepted() -> None:
    """`json_object` mode is refused unless the prompt says "json"."""
    from app.ai import intake

    assert "json" in intake.SYSTEM_PROMPT.lower()


# --- C-3: route_constraints is an id collection too -------------------------


def test_duplicate_route_constraint_is_rejected(
    client: TestClient, request_body: dict
) -> None:
    constraints = request_body["input_snapshot"]["route_constraints"]
    constraints.append({**copy.deepcopy(constraints[0]), "max_height_mm": 3000})

    response = client.post("/v1/scenarios", json=request_body)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


# --- C-4: the alternative keeps its own audit trail -------------------------


def test_alternative_export_carries_trace_events(
    client: TestClient, request_body: dict, expected: dict
) -> None:
    run_id = _solved(client, request_body)
    alternative = client.post(
        f"/v1/runs/{run_id}/alternatives",
        json=expected["alternatives"]["ORD-005"]["request"],
    ).json()

    bundle = client.get(f"/v1/runs/{alternative['alternative_run_id']}/export").json()

    assert bundle["trace_events"]
    assert any(e["event_type"] == "ALTERNATIVE_CREATED" for e in bundle["trace_events"])


# --- C-5: the lexicographic stage values reach the client -------------------


def test_objective_values_are_served(client: TestClient, request_body: dict) -> None:
    run_id = _solved(client, request_body)

    served = client.get(f"/v1/runs/{run_id}").json()

    assert served["objective_values"]["assigned_count"] == 3
    assert served["objective_values"]["priority_score"] == 8
    assert client.get(f"/v1/runs/{run_id}/export").json()["run"]["objective_values"]


# --- the canonical input must survive its callers ---------------------------


def test_canonical_loader_returns_a_fresh_copy() -> None:
    """One caller's edit used to rewrite the scenario for the whole process,
    including the hash the reproducibility contract rests on."""
    first = load_canonical_snapshot()
    original = next(o for o in first["orders"] if o["order_id"] == "ORD-002")["due_at"]

    next(o for o in first["orders"] if o["order_id"] == "ORD-002")["due_at"] = "mutated"

    second = load_canonical_snapshot()
    assert next(o for o in second["orders"] if o["order_id"] == "ORD-002")["due_at"] == original
    assert first is not second


def test_create_request_does_not_share_the_cached_snapshot() -> None:
    request = canonical_create_request()
    request["input_snapshot"]["orders"][0]["gross_weight_kg"] = 1

    assert load_canonical_snapshot()["orders"][0]["gross_weight_kg"] != 1
