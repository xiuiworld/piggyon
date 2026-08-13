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


class OrderValidation(BaseModel):
    order_id: str
    input_state: Literal["VALID", "REVIEW_REQUIRED"]
    reason_codes: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    eligible_slot_ids: list[str] = Field(default_factory=list)
    # Additive beyond the openapi.yaml required set: P1 exists to produce these
    # two, and the demo's 확인 필요 / 불가 badges read them directly.
    eligibility_state: Literal["ELIGIBLE", "INELIGIBLE", "NOT_EVALUATED"]
    primary_reason_code: str | None = None


class ValidationResult(BaseModel):
    scenario_id: str
    validation_status: Literal["COMPLETED", "FAILED"]
    orders: list[OrderValidation]


class SolverParametersModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    random_seed: int = Field(ge=0)
    # 04 §10 pins this to 1 so tie-breaking and hashes reproduce.
    num_search_workers: int
    max_time_seconds: int = Field(ge=1)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solver_parameters: SolverParametersModel


class AssignmentModel(BaseModel):
    order_id: str
    service_id: str
    wagon_id: str
    slot_id: str


class OrderOutcomeModel(BaseModel):
    order_id: str
    input_state: Literal["VALID", "REVIEW_REQUIRED"]
    eligibility_state: Literal["ELIGIBLE", "INELIGIBLE", "NOT_EVALUATED"]
    assignment_state: Literal["ASSIGNED", "UNASSIGNED", "NOT_APPLICABLE"]
    alternative_state: Literal["AVAILABLE", "NONE", "NOT_SEARCHED"]
    primary_reason_code: str
    evidence: dict = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)
    display_label: str | None = None


class Reproducibility(BaseModel):
    solver_parameters: SolverParametersModel
    input_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Run(BaseModel):
    run_id: str
    scenario_id: str
    solver_status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "ERROR"]
    run_state: Literal["SOLVED_OPTIMAL", "SOLVED_FEASIBLE", "MODEL_INFEASIBLE", "ERROR"]
    is_optimal: bool
    validator_status: Literal["PASS", "FAIL"]
    reproducibility: Reproducibility
    assignments: list[AssignmentModel]
    order_outcomes: list[OrderOutcomeModel]
