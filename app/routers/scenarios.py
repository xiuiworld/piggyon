"""`POST /v1/scenarios` — create the immutable input snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.errors import ApiError
from app.models.api import (
    ErrorResponse,
    Run,
    RunRequest,
    Scenario,
    ScenarioCreateRequest,
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
def create_scenario(
    payload: ScenarioCreateRequest,
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
        # Stored in JSON mode so the snapshot round-trips through Postgres
        # unchanged; later phases hash exactly these bytes for reproducibility.
        "input_snapshot": payload.input_snapshot.model_dump(mode="json"),
    }
    store.save_scenario(record)

    response.headers["Location"] = f"/v1/scenarios/{scenario_id}"
    _trace(store, scenario_id, "SCENARIO_CREATED", {"scenario_name": payload.scenario_name})
    return Scenario(
        scenario_id=scenario_id, state="VALIDATION_REQUIRED", created_at=created_at
    )


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
    if evaluation.has_any_candidate:
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
