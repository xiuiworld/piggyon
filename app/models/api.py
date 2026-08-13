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


class ScenarioDetail(Scenario):
    """A stored scenario together with the snapshot it was created from.

    `POST /v1/scenarios` answers with the id and state only, and until a run
    exists the snapshot is readable nowhere else -- `GET /v1/runs/{id}/export`
    carries it but needs a solved, validated run. The screens that draw the
    inputs come before any of that, so they need this.
    """

    scenario_name: str
    as_of: datetime
    baseline_service_ids: list[str]
    policy_version: str
    assumption_ids: list[str] = Field(default_factory=list)
    # Verbatim, for the same reason as ExportBundle.input_snapshot: this is the
    # document `input_snapshot_sha256` was taken over.
    input_snapshot: dict


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
    display_badges: list[str] = Field(default_factory=list)
    # The link from a baseline order to the derived scenario that can carry it.
    # Without it on the model, Pydantic drops the value the run record already
    # holds, and the UI has no resource path to the alternative.
    alternative_scenario_id: str | None = None


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
    # A FAIL is only actionable if the UI can say which rule broke and on what
    # resource, which 08 §9 requires it to show.
    validator_findings: list[dict] = Field(default_factory=list)
    reproducibility: Reproducibility
    assignments: list[AssignmentModel]
    order_outcomes: list[OrderOutcomeModel]


AdjustmentType = Literal[
    "ADD_ORDER_APPROVED_SERVICE",
    "CHANGE_TO_APPROVED_TERMINAL",
    "CHANGE_WEIGHT_LIMIT",
    "CHANGE_DIMENSION_LIMIT",
    "CHANGE_ROUTE_CLEARANCE",
    "CHANGE_DUE_AT",
]


class AlternativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    # Forbidden types are accepted by the schema on purpose: the 409
    # POLICY_VIOLATION contract only exists if the request can be expressed.
    adjustment_types: list[AdjustmentType] = Field(min_length=1)


class AssignmentDelta(BaseModel):
    order_id: str
    change_type: Literal["ADDED", "MOVED", "UNASSIGNED"]
    before_assignment: AssignmentModel | None
    after_assignment: AssignmentModel | None


class AlternativeResult(BaseModel):
    parent_run_id: str
    alternative_scenario_id: str
    alternative_run_id: str
    change_set: list[dict]
    impacted_order_ids: list[str] = Field(min_length=1)
    baseline_order_update: OrderOutcomeModel
    alternative_run_order_outcome: OrderOutcomeModel
    assignment_deltas: list[AssignmentDelta] = Field(min_length=1)
    validator_status: Literal["PASS", "FAIL"]


class AlternativeUnavailableResult(BaseModel):
    order_id: str
    alternative_state: Literal["NONE"]
    status: Literal["NO_FEASIBLE_ALTERNATIVE"]
    change_set: list[dict]
    baseline_order_update: OrderOutcomeModel
    reason_code: str


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_state: Literal["ACCEPTED", "HELD", "REJECTED"]
    actor_role: Literal["SCHEDULING_OPERATOR", "PLANNING_OWNER"]
    reason: str = Field(min_length=1)
    selected_plan: Literal["BASELINE", "ALTERNATIVE"]


class Decision(BaseModel):
    decision_id: str
    run_id: str
    decision_state: str
    created_at: datetime


class TraceEvent(BaseModel):
    event_id: str
    event_type: Literal[
        "SCENARIO_CREATED",
        "VALIDATION_COMPLETED",
        "RUN_COMPLETED",
        "ALTERNATIVE_CREATED",
        "DECISION_RECORDED",
    ]
    occurred_at: datetime
    payload: dict


class ExportBundle(BaseModel):
    scenario: Scenario
    # The stored document verbatim, not a re-serialisation of the parsed model.
    # 04 §11 calls this the original input snapshot, and it is what the run's
    # `input_snapshot_sha256` was taken over; round-tripping it through the
    # model would re-add this service's defaults and no longer hash to that.
    input_snapshot: dict
    policy: dict
    run: Run
    validation_result: ValidationResult
    decisions: list[Decision]
    trace_events: list[TraceEvent]
