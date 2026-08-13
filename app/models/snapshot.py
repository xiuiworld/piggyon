"""`ScenarioInputSnapshot` and its members.

Typed mirror of `docs/openapi.yaml#/components/schemas/ScenarioInputSnapshot`.
The OpenAPI file is the contract of record; this module must not loosen it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

CompatibilityTag = Literal["TRAILER_STANDARD", "TRAILER_TALL"]
CompatibilityTags = Annotated[list[CompatibilityTag], Field(min_length=1)]

# 02 §10 vocabulary. Only these three priority classes exist.
PriorityClass = Literal["P1", "P2", "P3"]

AllowedAdjustment = Literal["ADD_ORDER_APPROVED_SERVICE", "CHANGE_TO_APPROVED_TERMINAL"]
ForbiddenAdjustment = Literal[
    "CHANGE_WEIGHT_LIMIT",
    "CHANGE_DIMENSION_LIMIT",
    "CHANGE_ROUTE_CLEARANCE",
    "CHANGE_DUE_AT",
]

OBJECTIVE_ORDER: tuple[str, ...] = (
    "maximize_assigned_order_count",
    "maximize_priority_score",
    "minimize_due_at",
    "canonical_order_slot",
)


class StrictModel(BaseModel):
    """Reject unknown keys so a typo in the snapshot is a 400, not a silent drop.

    Timestamps are `AwareDatetime` throughout. The snapshot pins `timezone`, so
    a value without an offset is ambiguous; accepting one let it pass creation
    and validation and then fail deep inside the solve, where subtracting a
    naive from an aware datetime raises and the caller sees a bare 500.
    """

    model_config = ConfigDict(extra="forbid")


class Geo(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class OperatingWindow(StrictModel):
    open: str = Field(pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    close: str = Field(pattern=r"^[0-2][0-9]:[0-5][0-9]$")


class Dimensions(StrictModel):
    length: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class DisplayPosition(StrictModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class Assumption(StrictModel):
    assumption_id: str
    source_type: Literal[
        "DEMO_ASSUMPTION", "PUBLIC_CONFIRMED", "INSTITUTION_CONFIRMATION_REQUIRED"
    ]
    description: str = Field(min_length=1)
    impact_scope: str = Field(min_length=1)


class Shipper(StrictModel):
    shipper_id: str
    display_name: str
    # Map decoration only: never an input to terminal choice or travel time (03 §8).
    pickup_geo: Geo
    delivery_geo: Geo


class Terminal(StrictModel):
    terminal_id: str
    display_name: str
    geo: Geo
    operating_window: OperatingWindow
    intake_cutoff_minutes: int | None = Field(default=None, ge=0)
    minimum_handling_minutes: int = Field(ge=0)
    supported_tags: CompatibilityTags


class RouteConstraint(StrictModel):
    route_constraint_id: str
    max_height_mm: int = Field(ge=1)
    max_width_mm: int = Field(ge=1)
    max_weight_kg: int = Field(ge=1)


class Service(StrictModel):
    service_id: str
    origin_terminal_id: str
    destination_terminal_id: str
    departure_at: AwareDatetime
    arrival_at: AwareDatetime
    planning_cutoff_at: AwareDatetime
    route_constraint_id: str
    available: bool


class Wagon(StrictModel):
    wagon_id: str
    service_id: str
    max_total_weight_kg: int = Field(ge=1)
    available: bool
    display_order: int = Field(ge=1)


class Slot(StrictModel):
    slot_id: str
    wagon_id: str
    position: int = Field(ge=1)
    display: DisplayPosition
    max_weight_kg: int = Field(ge=1)
    max_dimensions_mm: Dimensions
    supported_tags: CompatibilityTags
    available: bool


class AdjustmentWindow(StrictModel):
    """Pre-approved alternatives only. Never a place to relax a safety limit."""

    alternative_service_ids: list[str] = Field(default_factory=list)
    alternative_destination_terminal_ids: list[str] = Field(default_factory=list)


class Order(StrictModel):
    order_id: str
    shipper_id: str
    origin_terminal_ids: list[str] = Field(min_length=1)
    destination_terminal_ids: list[str] = Field(min_length=1)
    ready_at: AwareDatetime
    due_at: AwareDatetime
    # The one permitted null in the snapshot: it reproduces ORD-006's
    # REVIEW_REQUIRED during validation (04 §11). A *missing* key is still a 400.
    gross_weight_kg: int | None = Field(ge=1)
    dimensions_mm: Dimensions
    compatibility_tags: CompatibilityTags
    priority_class: PriorityClass
    adjustment_window: AdjustmentWindow | None


class PriorityScores(StrictModel):
    P1: int
    P2: int
    P3: int


class Policy(StrictModel):
    policy_id: str
    policy_version: str
    priority_scores: PriorityScores
    objective_order: list[str]
    allowed_adjustments: list[AllowedAdjustment]
    forbidden_adjustments: list[ForbiddenAdjustment]

    @model_validator(mode="after")
    def _objective_order_is_fixed(self) -> "Policy":
        if tuple(self.objective_order) != OBJECTIVE_ORDER:
            raise ValueError(
                f"objective_order must be exactly {list(OBJECTIVE_ORDER)}"
            )
        return self


class ScenarioInputSnapshot(StrictModel):
    """The immutable snapshot of every input, policy and assumption."""

    schema_version: Literal["1.0.0"]
    scenario_id: str = Field(min_length=1)
    scenario_type: Literal["BASELINE", "ALTERNATIVE"]
    as_of: AwareDatetime
    timezone: Literal["Asia/Seoul"]
    baseline_service_ids: list[str] = Field(min_length=1)
    assumptions: list[Assumption] = Field(min_length=1)
    shippers: list[Shipper] = Field(min_length=1)
    terminals: list[Terminal] = Field(min_length=1)
    route_constraints: list[RouteConstraint] = Field(min_length=1)
    services: list[Service] = Field(min_length=1)
    wagons: list[Wagon] = Field(min_length=1)
    slots: list[Slot] = Field(min_length=1)
    orders: list[Order] = Field(min_length=1)
    policy: Policy

    @model_validator(mode="after")
    def _references_resolve(self) -> "ScenarioInputSnapshot":
        """Cross-entity referential integrity.

        P1 owns *order-level* validation. This is narrower: a snapshot whose
        internal IDs do not resolve is malformed input, not a reviewable order.
        """
        errors: list[str] = []

        terminal_ids = {t.terminal_id for t in self.terminals}
        route_ids = {r.route_constraint_id for r in self.route_constraints}
        service_ids = {s.service_id for s in self.services}
        wagon_ids = {w.wagon_id for w in self.wagons}
        shipper_ids = {s.shipper_id for s in self.shippers}

        _reject_duplicates(errors, "terminals", [t.terminal_id for t in self.terminals])
        _reject_duplicates(errors, "services", [s.service_id for s in self.services])
        _reject_duplicates(errors, "wagons", [w.wagon_id for w in self.wagons])
        _reject_duplicates(errors, "slots", [s.slot_id for s in self.slots])
        _reject_duplicates(errors, "orders", [o.order_id for o in self.orders])
        _reject_duplicates(errors, "shippers", [s.shipper_id for s in self.shippers])
        _reject_duplicates(
            errors, "route_constraints", [r.route_constraint_id for r in self.route_constraints]
        )

        for service_id in self.baseline_service_ids:
            if service_id not in service_ids:
                errors.append(f"baseline_service_ids: unknown service {service_id}")

        for service in self.services:
            for field, value in (
                ("origin_terminal_id", service.origin_terminal_id),
                ("destination_terminal_id", service.destination_terminal_id),
            ):
                if value not in terminal_ids:
                    errors.append(
                        f"services[{service.service_id}].{field}: unknown terminal {value}"
                    )
            if service.route_constraint_id not in route_ids:
                errors.append(
                    f"services[{service.service_id}].route_constraint_id: "
                    f"unknown route constraint {service.route_constraint_id}"
                )
            if service.arrival_at <= service.departure_at:
                errors.append(
                    f"services[{service.service_id}]: arrival_at must be after departure_at"
                )
            if service.planning_cutoff_at > service.departure_at:
                errors.append(
                    f"services[{service.service_id}]: planning_cutoff_at must not be "
                    "after departure_at"
                )

        for wagon in self.wagons:
            if wagon.service_id not in service_ids:
                errors.append(
                    f"wagons[{wagon.wagon_id}].service_id: unknown service {wagon.service_id}"
                )

        for slot in self.slots:
            if slot.wagon_id not in wagon_ids:
                errors.append(
                    f"slots[{slot.slot_id}].wagon_id: unknown wagon {slot.wagon_id}"
                )

        for order in self.orders:
            if order.shipper_id not in shipper_ids:
                errors.append(
                    f"orders[{order.order_id}].shipper_id: unknown shipper {order.shipper_id}"
                )
            for field, values in (
                ("origin_terminal_ids", order.origin_terminal_ids),
                ("destination_terminal_ids", order.destination_terminal_ids),
            ):
                for value in values:
                    if value not in terminal_ids:
                        errors.append(
                            f"orders[{order.order_id}].{field}: unknown terminal {value}"
                        )
            window = order.adjustment_window
            if window is not None:
                for value in window.alternative_service_ids:
                    if value not in service_ids:
                        errors.append(
                            f"orders[{order.order_id}].adjustment_window."
                            f"alternative_service_ids: unknown service {value}"
                        )
                for value in window.alternative_destination_terminal_ids:
                    if value not in terminal_ids:
                        errors.append(
                            f"orders[{order.order_id}].adjustment_window."
                            f"alternative_destination_terminal_ids: unknown terminal {value}"
                        )

        if errors:
            raise ValueError("; ".join(errors))
        return self


def _reject_duplicates(errors: list[str], collection: str, ids: list[str]) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            errors.append(f"{collection}: duplicate id {value}")
        seen.add(value)
