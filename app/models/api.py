"""Request and response models for the HTTP layer.

Mirrors `docs/openapi.yaml`. P0 only needs the scenario-creation pair and the
shared `Error` envelope; later phases extend this module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.snapshot import ScenarioInputSnapshot

ScenarioState = Literal["VALIDATION_REQUIRED", "READY_TO_SOLVE", "SOLVED"]

ErrorCode = Literal[
    "INVALID_INPUT",
    "SCENARIO_NOT_FOUND",
    "RUN_NOT_FOUND",
    "VALIDATION_REQUIRED",
    "POLICY_VIOLATION",
    "RUN_NOT_ACCEPTABLE",
    "SOLVER_UNAVAILABLE",
]


class ScenarioCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_name: str
    as_of: datetime
    baseline_service_ids: list[str] = Field(min_length=1)
    input_snapshot: ScenarioInputSnapshot
    policy_version: str
    assumption_ids: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    scenario_id: str
    state: ScenarioState
    created_at: datetime


class ErrorResponse(BaseModel):
    """Every failure response carries code, message, details and trace_id (04 §1)."""

    code: ErrorCode
    message: str
    details: list[dict] = Field(default_factory=list)
    trace_id: str
