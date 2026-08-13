"""`POST /v1/scenarios` — create the immutable input snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.errors import ApiError
from app.models.api import ErrorResponse, Scenario, ScenarioCreateRequest
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
    return Scenario(
        scenario_id=scenario_id, state="VALIDATION_REQUIRED", created_at=created_at
    )


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
