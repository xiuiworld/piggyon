"""`GET /v1/runs/{run_id}` — read back a solved plan."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import get_store
from app.errors import ApiError
from app.models.api import ErrorResponse, Run
from app.storage import Store

router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.get(
    "/{run_id}",
    response_model=Run,
    summary="Get a run result",
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
def get_run(run_id: str, store: Store = Depends(get_store)) -> Run:
    record = store.get_run(run_id)
    if record is None:
        raise ApiError("RUN_NOT_FOUND", f"Run {run_id} does not exist.")
    return Run.model_validate(record)
