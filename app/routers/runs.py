"""Run-scoped endpoints: read, alternatives, decisions, export."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from app.dependencies import get_store
from app.errors import ApiError
from app.models.api import (
    AlternativeRequest,
    AlternativeResult,
    AlternativeUnavailableResult,
    Decision,
    DecisionRequest,
    ErrorResponse,
    ExportBundle,
    Run,
)
from app.services.alternatives import (
    AmbiguousAlternative,
    PolicyViolation,
    apply_to_baseline,
    search_alternative,
)
from app.services.planning import snapshot_of
from app.solver.baseline import SolverParameters
from app.storage import Store, utc_now

router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.get(
    "/{run_id}",
    response_model=Run,
    summary="Get a run result",
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
def get_run(run_id: str, store: Store = Depends(get_store)) -> Run:
    return Run.model_validate(_require_run(store, run_id))


@router.post(
    "/{run_id}/alternatives",
    summary="Search the approved alternatives for one order",
    response_model=None,
    responses={
        201: {"model": AlternativeResult, "description": "Alternative found"},
        200: {
            "model": AlternativeUnavailableResult,
            "description": "Permitted alternatives were searched but none is feasible",
        },
        400: {"model": ErrorResponse, "description": "Invalid input"},
        404: {"model": ErrorResponse, "description": "Run not found"},
        409: {"model": ErrorResponse, "description": "Requested change is forbidden"},
    },
)
def create_alternative(
    run_id: str,
    payload: AlternativeRequest,
    response: Response,
    store: Store = Depends(get_store),
):
    baseline_run = _require_run(store, run_id)
    scenario = store.get_scenario(baseline_run["scenario_id"])
    if scenario is None:
        raise ApiError(
            "SCENARIO_NOT_FOUND", f"Scenario {baseline_run['scenario_id']} does not exist."
        )

    snapshot = snapshot_of(scenario)
    if not any(o.order_id == payload.order_id for o in snapshot.orders):
        raise ApiError(
            "INVALID_INPUT",
            f"Order {payload.order_id} is not part of this scenario.",
            details=[{"location": "order_id", "message": "unknown order"}],
        )

    sequence = store.next_alternative_sequence()
    parameters = SolverParameters(
        **baseline_run["reproducibility"]["solver_parameters"]
    )

    try:
        outcome = search_alternative(
            snapshot=snapshot,
            baseline_run=baseline_run,
            order_id=payload.order_id,
            adjustment_types=list(payload.adjustment_types),
            scenario_id=f"SCN-ALT-{sequence:03d}",
            run_id=f"RUN-ALT-{sequence:03d}",
            parameters=parameters,
        )
    except PolicyViolation as exc:
        # TC-10: the refusal must not disturb whatever the order already had.
        raise ApiError(
            "POLICY_VIOLATION",
            "Requested adjustment is forbidden by the scenario policy.",
            details=[{"adjustment_type": t} for t in exc.forbidden],
        ) from exc
    except AmbiguousAlternative as exc:
        raise ApiError(
            "INVALID_INPUT",
            f"Order {exc.order_id} approves more than one destination terminal, "
            "so a single alternative cannot express the change.",
            details=[{"destination_terminal_id": t} for t in exc.terminal_ids],
        ) from exc

    if not outcome.found:
        baseline_update = apply_to_baseline(baseline_run, payload.order_id, "NONE")
        store.save_run(baseline_run)
        _trace(store, baseline_run["scenario_id"], "ALTERNATIVE_CREATED", {
            "run_id": run_id,
            "order_id": payload.order_id,
            "status": "NO_FEASIBLE_ALTERNATIVE",
            "reason_code": outcome.reason_code,
        })
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=AlternativeUnavailableResult(
                order_id=payload.order_id,
                alternative_state="NONE",
                status="NO_FEASIBLE_ALTERNATIVE",
                change_set=outcome.change_set,
                baseline_order_update=baseline_update,
                reason_code=outcome.reason_code or "NO_FEASIBLE_ALTERNATIVE",
            ).model_dump(mode="json"),
        )

    baseline_update = apply_to_baseline(
        baseline_run, payload.order_id, "AVAILABLE", outcome.alternative_scenario_id
    )

    # Build and validate the response BEFORE writing anything. A response model
    # that rejects the result would otherwise leave a derived scenario and run
    # committed to the store behind a 500, with no rollback.
    body = AlternativeResult(
        parent_run_id=run_id,
        alternative_scenario_id=outcome.alternative_scenario_id,
        alternative_run_id=outcome.alternative_run_id,
        change_set=outcome.change_set,
        impacted_order_ids=outcome.impacted_order_ids,
        baseline_order_update=baseline_update,
        alternative_run_order_outcome=outcome.alternative_run_order_outcome,
        assignment_deltas=outcome.assignment_deltas,
        validator_status=outcome.validator_status,
    )

    store.save_scenario(
        {
            "scenario_id": outcome.alternative_scenario_id,
            "scenario_name": f"{scenario['scenario_name']}-alt-{payload.order_id}",
            "state": "SOLVED",
            "created_at": utc_now().isoformat(),
            "as_of": scenario["as_of"],
            "baseline_service_ids": outcome.alternative_snapshot.baseline_service_ids,
            "policy_version": scenario["policy_version"],
            "assumption_ids": scenario["assumption_ids"],
            "input_snapshot": outcome.alternative_snapshot.model_dump(mode="json"),
            "parent_scenario_id": scenario["scenario_id"],
            "change_set": outcome.change_set,
        }
    )
    store.save_run(outcome.alternative_run)
    if outcome.alternative_validation is not None:
        store.save_validation(outcome.alternative_scenario_id, outcome.alternative_validation)

    store.update_order_outcome(run_id, payload.order_id, baseline_update)
    # The order's alternative axis just moved, and its card is written from that
    # axis: 기본안 불가·대안 미검토 becomes 기본안 불가 with a badge. The stored
    # explanation now describes a state the run has left, so it goes and the
    # next read rebuilds it.
    store.clear_explanation(run_id)

    event = {
        "run_id": run_id,
        "order_id": payload.order_id,
        "alternative_run_id": outcome.alternative_run_id,
        "change_set": outcome.change_set,
    }
    _trace(store, baseline_run["scenario_id"], "ALTERNATIVE_CREATED", event)
    # Also on the derived scenario: its own export bundle reads trace by its own
    # scenario id, so recording only against the parent left the alternative's
    # audit trail empty.
    _trace(store, outcome.alternative_scenario_id, "ALTERNATIVE_CREATED", event)

    response.status_code = status.HTTP_201_CREATED
    response.headers["Location"] = f"/v1/runs/{outcome.alternative_run_id}"
    return body


@router.post(
    "/{run_id}/decisions",
    response_model=Decision,
    status_code=status.HTTP_201_CREATED,
    summary="Record the operator decision",
    responses={
        404: {"model": ErrorResponse, "description": "Run not found"},
        409: {"model": ErrorResponse, "description": "Run cannot be accepted"},
    },
)
def create_decision(
    run_id: str,
    payload: DecisionRequest,
    store: Store = Depends(get_store),
) -> Decision:
    run = _require_run(store, run_id)

    acceptable = run["solver_status"] == "OPTIMAL" and run["validator_status"] == "PASS"
    if payload.decision_state == "ACCEPTED" and not acceptable:
        # 04 §8: a FEASIBLE or FAIL run can be held or rejected, never accepted.
        raise ApiError(
            "RUN_NOT_ACCEPTABLE",
            "Only an OPTIMAL run with validator PASS can be ACCEPTED.",
            details=[
                {
                    "solver_status": run["solver_status"],
                    "validator_status": run["validator_status"],
                }
            ],
        )

    created_at = utc_now()
    record = {
        "decision_id": f"DEC-{uuid.uuid4().hex[:12]}",
        "run_id": run_id,
        "decision_state": payload.decision_state,
        "actor_role": payload.actor_role,
        "reason": payload.reason,
        "selected_plan": payload.selected_plan,
        "created_at": created_at.isoformat(),
    }
    store.save_decision(record)
    _trace(store, run["scenario_id"], "DECISION_RECORDED", {
        "run_id": run_id,
        "decision_id": record["decision_id"],
        "decision_state": payload.decision_state,
    })

    return Decision.model_validate(record)


@router.get(
    "/{run_id}/export",
    response_model=ExportBundle,
    summary="Immutable demo and verification bundle",
    responses={
        404: {"model": ErrorResponse, "description": "Run not found"},
        # Raised when the scenario has no recorded validation; declaring it here
        # is what keeps it in the served schema.
        422: {"model": ErrorResponse, "description": "Scenario was never validated"},
    },
)
def export_run(run_id: str, store: Store = Depends(get_store)) -> ExportBundle:
    run = _require_run(store, run_id)
    scenario = store.get_scenario(run["scenario_id"])
    if scenario is None:
        raise ApiError(
            "SCENARIO_NOT_FOUND", f"Scenario {run['scenario_id']} does not exist."
        )

    validation = store.get_validation(run["scenario_id"])
    if validation is None:
        # Never fabricate a COMPLETED here. Claiming a scenario was validated
        # when it was not breaks the traceability the bundle exists to provide,
        # and an empty COMPLETED is indistinguishable from a real clean pass.
        raise ApiError(
            "VALIDATION_REQUIRED",
            f"Scenario {run['scenario_id']} has no recorded validation result.",
        )

    snapshot = scenario["input_snapshot"]
    return ExportBundle.model_validate(
        {
            "scenario": {
                "scenario_id": scenario["scenario_id"],
                "state": scenario["state"],
                "created_at": scenario["created_at"],
            },
            "input_snapshot": snapshot,
            "policy": snapshot["policy"],
            "run": run,
            "validation_result": validation,
            "decisions": store.list_decisions(run_id),
            "trace_events": store.list_trace(run["scenario_id"]),
            # Read, never generated here. A bundle that produced its own
            # wording would describe the same run in different words from the
            # screen it is supposed to evidence.
            "explanation": store.get_explanation(run_id),
        }
    )


def _require_run(store: Store, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise ApiError("RUN_NOT_FOUND", f"Run {run_id} does not exist.")
    return run


def _trace(store: Store, scenario_id: str, event_type: str, payload: dict) -> None:
    store.append_trace(
        scenario_id,
        {
            "event_id": f"EVT-{uuid.uuid4().hex[:12]}",
            "event_type": event_type,
            "occurred_at": utc_now().isoformat(),
            "payload": payload,
        },
    )
