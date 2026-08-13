"""`POST /v1/scenarios` — create the immutable input snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.errors import ApiError
from app.models.api import (
    ErrorResponse,
    Run,
    RunRequest,
    Scenario,
    ScenarioCreateRequest,
    ScenarioDetail,
    ScenarioSummary,
    ValidationResult,
)
from app.services.planning import build_validation_result, run_baseline, snapshot_of
from app.rules.eligibility import evaluate_scenario
from app.solver.baseline import SolverParameters
from app.storage import Store, utc_now
from app.dependencies import get_store

router = APIRouter(prefix="/v1/scenarios", tags=["scenarios"])


@router.post(
    "",
    response_model=Scenario,
    status_code=status.HTTP_201_CREATED,
    summary="Create an input snapshot",
    responses={400: {"model": ErrorResponse, "description": "Invalid input"}},
)
async def create_scenario(
    payload: ScenarioCreateRequest,
    request: Request,
    response: Response,
    store: Store = Depends(get_store),
) -> Scenario:
    _reject_inconsistent_request(payload)

    scenario_id = store.next_scenario_id()
    created_at = utc_now()

    record = {
        "scenario_id": scenario_id,
        "scenario_name": payload.scenario_name,
        "state": "VALIDATION_REQUIRED",
        "created_at": created_at.isoformat(),
        "as_of": payload.as_of.isoformat(),
        "baseline_service_ids": payload.baseline_service_ids,
        "policy_version": payload.policy_version,
        "assumption_ids": payload.assumption_ids,
        # The snapshot is persisted exactly as submitted. `payload` has already
        # validated it; re-dumping the model would add this service's own
        # defaults to the caller's document and change its hash, which is
        # supposed to identify what the caller actually sent.
        "input_snapshot": (await request.json())["input_snapshot"],
        "parent_scenario_id": payload.parent_scenario_id,
    }
    store.save_scenario(record)

    response.headers["Location"] = f"/v1/scenarios/{scenario_id}"
    _trace(store, scenario_id, "SCENARIO_CREATED", {"scenario_name": payload.scenario_name})
    return Scenario(
        scenario_id=scenario_id, state="VALIDATION_REQUIRED", created_at=created_at
    )


@router.get(
    "",
    response_model=list[ScenarioSummary],
    summary="List stored scenarios, newest first",
)
def list_scenarios(
    limit: int = Query(20, ge=1, le=100),
    store: Store = Depends(get_store),
) -> list[ScenarioSummary]:
    """Every scenario this store holds, newest first.

    Without it a scenario is only reachable by an id the caller happened to keep
    from the response that created it: close the tab and the work is gone, even
    though the record is still there. The demo could only ever start over.
    """
    return [
        _summarise(store, record)
        for record in store.list_scenarios(limit)
    ]


def _summarise(store: Store, record: dict) -> ScenarioSummary:
    latest_run_id = store.latest_run_id(record["scenario_id"])
    # The standing decision is the last one recorded: a run can be held and then
    # accepted, and a list that showed the first would say the opposite of where
    # the scenario ended up.
    decisions = store.list_decisions(latest_run_id) if latest_run_id else []

    return ScenarioSummary(
        scenario_id=record["scenario_id"],
        # Older records predate the field; the id is a worse name than a name
        # but a better one than nothing.
        scenario_name=record.get("scenario_name") or record["scenario_id"],
        state=record["state"],
        created_at=record["created_at"],
        parent_scenario_id=record.get("parent_scenario_id"),
        change_set=record.get("change_set") or [],
        order_count=len((record.get("input_snapshot") or {}).get("orders") or []),
        latest_run_id=latest_run_id,
        decision_state=decisions[-1]["decision_state"] if decisions else None,
    )


@router.get(
    "/{scenario_id}",
    response_model=ScenarioDetail,
    summary="Read a stored scenario and its input snapshot",
    responses={404: {"model": ErrorResponse, "description": "Scenario not found"}},
)
def get_scenario(
    scenario_id: str,
    store: Store = Depends(get_store),
) -> ScenarioDetail:
    record = _require_scenario(store, scenario_id)
    # Resolved at read time rather than stored: a scenario gains runs after it
    # is written, and a denormalised copy would be stale the moment it did.
    return ScenarioDetail.model_validate(
        {**record, "latest_run_id": store.latest_run_id(scenario_id)}
    )


@router.get(
    "/{scenario_id}/validation",
    response_model=ValidationResult,
    summary="Read the stored validation result",
    responses={
        404: {"model": ErrorResponse, "description": "Scenario not found"},
        422: {"model": ErrorResponse, "description": "Scenario has not been validated"},
    },
)
def read_validation(
    scenario_id: str,
    store: Store = Depends(get_store),
) -> ValidationResult:
    """The validation this scenario already has, without producing another.

    Validating is a POST because it computes and stores, and it also records a
    `VALIDATION_COMPLETED` event. A screen that shows candidate slots therefore
    could not read them without appending to the audit trail -- so a dashboard
    that validated on every render logged a visit as an action, and the trail
    stopped describing what anyone did.
    """
    _require_scenario(store, scenario_id)

    stored = store.get_validation(scenario_id)
    if stored is None:
        raise ApiError(
            "VALIDATION_REQUIRED",
            f"Scenario {scenario_id} has not been validated yet.",
        )
    return ValidationResult.model_validate(stored)


@router.delete(
    "/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a scenario and everything recorded against it",
    responses={
        404: {"model": ErrorResponse, "description": "Scenario not found"},
        409: {"model": ErrorResponse, "description": "Scenario has derived scenarios"},
    },
)
def delete_scenario(
    scenario_id: str,
    store: Store = Depends(get_store),
) -> Response:
    """Remove a scenario, its runs, its decisions and its trace.

    Refused while anything was derived from it. A derived scenario records the
    id it came from and a screen reads that lineage back; deleting the parent
    would leave the child pointing at nothing, and its `기본안 대비` comparison
    with no baseline to compare against.
    """
    _require_scenario(store, scenario_id)

    children = [
        s["scenario_id"]
        for s in store.list_scenarios(100)
        if s.get("parent_scenario_id") == scenario_id
    ]
    if children:
        raise ApiError(
            "POLICY_VIOLATION",
            f"Scenario {scenario_id} has derived scenarios; delete those first.",
            details=[{"derived_scenario_id": child} for child in children],
        )

    store.delete_scenario(scenario_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{scenario_id}/validate",
    response_model=ValidationResult,
    summary="Validate inputs and build slot candidates",
    responses={404: {"model": ErrorResponse, "description": "Scenario not found"}},
)
def validate_scenario(
    scenario_id: str,
    store: Store = Depends(get_store),
) -> ValidationResult:
    scenario = _require_scenario(store, scenario_id)
    snapshot = snapshot_of(scenario)

    evaluation = evaluate_scenario(snapshot)
    result = build_validation_result(scenario_id, evaluation)

    store.save_validation(scenario_id, result)
    # 02 §9: REVIEW_REQUIRED orders are dropped from the solve, they do not
    # block the scenario. Only a scenario with nothing left to compute stays put.
    #
    # Never backwards. Validation is deterministic over an immutable snapshot,
    # so running it again is a no-op -- but it used to reset a SOLVED scenario
    # to READY_TO_SOLVE, and a screen that validates whenever it renders turned
    # every visit into a downgrade. The scenario would then be listed as
    # unsolved next to the plan it had already produced.
    if evaluation.has_any_candidate and scenario["state"] == "VALIDATION_REQUIRED":
        store.update_scenario_state(scenario_id, "READY_TO_SOLVE")
    _trace(
        store,
        scenario_id,
        "VALIDATION_COMPLETED",
        {"review_required": [o.order_id for o in evaluation.evaluations
                             if o.input_state == "REVIEW_REQUIRED"]},
    )

    return ValidationResult.model_validate(result)


@router.post(
    "/{scenario_id}/runs",
    response_model=Run,
    status_code=status.HTTP_201_CREATED,
    summary="Solve the baseline plan",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input"},
        404: {"model": ErrorResponse, "description": "Scenario not found"},
        422: {"model": ErrorResponse, "description": "Scenario must be validated first"},
        503: {"model": ErrorResponse, "description": "Solver produced no plan"},
    },
)
def create_run(
    scenario_id: str,
    payload: RunRequest,
    response: Response,
    store: Store = Depends(get_store),
) -> Run:
    scenario = _require_scenario(store, scenario_id)

    if payload.solver_parameters.num_search_workers != 1:
        # 04 §10: anything else makes tie-breaking and the hashes unreproducible.
        raise ApiError(
            "INVALID_INPUT",
            "num_search_workers must be 1.",
            details=[
                {
                    "location": "solver_parameters.num_search_workers",
                    "message": f"got {payload.solver_parameters.num_search_workers}, expected 1",
                }
            ],
        )

    if store.get_validation(scenario_id) is None:
        raise ApiError(
            "VALIDATION_REQUIRED",
            f"Scenario {scenario_id} must be validated before it can be solved.",
        )

    record = run_baseline(
        run_id=store.next_run_id(),
        scenario_id=scenario_id,
        snapshot=snapshot_of(scenario),
        parameters=SolverParameters(
            random_seed=payload.solver_parameters.random_seed,
            num_search_workers=payload.solver_parameters.num_search_workers,
            max_time_seconds=payload.solver_parameters.max_time_seconds,
        ),
        raw_snapshot=scenario["input_snapshot"],
    )
    if record["run_state"] == "ERROR":
        # The solver proved nothing, so there is no plan and no per-order
        # verdict to publish. Storing the run anyway would stamp every eligible
        # order UNASSIGNED/CAPACITY_CONFLICT — "the slots were full" — when the
        # truth is that nothing was computed, and the plan validator would
        # cheerfully PASS a run with no assignments to contradict.
        raise ApiError(
            "SOLVER_UNAVAILABLE",
            "The solver did not produce a plan within the time budget.",
            details=[{"solver_status": record["solver_status"]}],
        )

    record["created_at"] = utc_now().isoformat()
    store.save_run(record)
    store.update_scenario_state(scenario_id, "SOLVED")
    _trace(
        store,
        scenario_id,
        "RUN_COMPLETED",
        {
            "run_id": record["run_id"],
            "solver_status": record["solver_status"],
            "validator_status": record["validator_status"],
        },
    )

    response.headers["Location"] = f"/v1/runs/{record['run_id']}"
    return Run.model_validate(record)


def _trace(store: Store, scenario_id: str, event_type: str, payload: dict) -> None:
    import uuid

    store.append_trace(
        scenario_id,
        {
            "event_id": f"EVT-{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "occurred_at": utc_now().isoformat(),
            "payload": payload,
        },
    )


def _require_scenario(store: Store, scenario_id: str) -> dict:
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise ApiError("SCENARIO_NOT_FOUND", f"Scenario {scenario_id} does not exist.")
    return scenario


def _reject_inconsistent_request(payload: ScenarioCreateRequest) -> None:
    """Cross-check the request envelope against the snapshot it carries.

    The snapshot validates itself; what it cannot see is whether the caller's
    top-level fields agree with it. Disagreement is malformed input.
    """
    snapshot = payload.input_snapshot
    details: list[dict[str, str]] = []

    if payload.policy_version != snapshot.policy.policy_version:
        details.append(
            {
                "location": "policy_version",
                "message": (
                    f"policy_version {payload.policy_version!r} does not match "
                    f"input_snapshot.policy.policy_version "
                    f"{snapshot.policy.policy_version!r}"
                ),
            }
        )

    known_services = {service.service_id for service in snapshot.services}
    for service_id in payload.baseline_service_ids:
        if service_id not in known_services:
            details.append(
                {
                    "location": "baseline_service_ids",
                    "message": f"unknown service {service_id} in input_snapshot.services",
                }
            )

    known_assumptions = {a.assumption_id for a in snapshot.assumptions}
    for assumption_id in payload.assumption_ids:
        if assumption_id not in known_assumptions:
            details.append(
                {
                    "location": "assumption_ids",
                    "message": (
                        f"unknown assumption {assumption_id} in input_snapshot.assumptions"
                    ),
                }
            )

    if details:
        raise ApiError(
            "INVALID_INPUT",
            "Request fields are inconsistent with input_snapshot.",
            details=details,
        )
