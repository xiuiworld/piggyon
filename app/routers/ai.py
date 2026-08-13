"""P4 endpoints: intake structuring and result explanation."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.ai import client, explain, intake
from app.dependencies import get_store
from app.errors import ApiError
from app.models.api import ErrorResponse
from app.storage import Store

router = APIRouter(prefix="/v1", tags=["ai"])


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


@router.get("/ai/status", summary="Whether the generative layer is configured")
def ai_status() -> dict[str, object]:
    return {"llm_available": client.is_available(), "fallback": "RULE_BASED/TEMPLATE"}


@router.post("/intake/orders", summary="Structure an unstructured shipping request")
def structure_intake(payload: IntakeRequest) -> dict:
    return intake.structure_request(payload.text)


@router.get(
    "/runs/{run_id}/explanation",
    summary="Operator-facing cards for a solved run",
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
def run_explanation(run_id: str, store: Store = Depends(get_store)) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise ApiError("RUN_NOT_FOUND", f"Run {run_id} does not exist.")
    return explain.build_cards(run)
