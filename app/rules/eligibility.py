"""P1: input validation and the eligibility gate.

Produces `input_state`, `eligibility_state`, candidate slots and the reason
codes behind each. It does not choose between candidates — that is the
solver's job (05 §3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models.snapshot import (
    Order,
    RouteConstraint,
    ScenarioInputSnapshot,
    Service,
    Slot,
    Terminal,
    Wagon,
)
from app.rules import reason_codes as rc


@dataclass(frozen=True)
class Candidate:
    """A slot the order could legally occupy on a baseline service."""

    order_id: str
    service_id: str
    wagon_id: str
    slot_id: str


@dataclass
class OrderEvaluation:
    order_id: str
    input_state: str
    eligibility_state: str
    reason_codes: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def eligible_slot_ids(self) -> list[str]:
        return [c.slot_id for c in self.candidates]

    @property
    def primary_reason_code(self) -> str | None:
        return rc.select_primary(self.reason_codes)


@dataclass
class ScenarioEvaluation:
    evaluations: list[OrderEvaluation]

    @property
    def by_order_id(self) -> dict[str, OrderEvaluation]:
        return {e.order_id: e for e in self.evaluations}

    @property
    def has_any_candidate(self) -> bool:
        return any(e.candidates for e in self.evaluations)


class _Index:
    """Lookup tables over one snapshot."""

    def __init__(self, snapshot: ScenarioInputSnapshot) -> None:
        self.terminals: dict[str, Terminal] = {
            t.terminal_id: t for t in snapshot.terminals
        }
        self.routes: dict[str, RouteConstraint] = {
            r.route_constraint_id: r for r in snapshot.route_constraints
        }
        self.services: dict[str, Service] = {s.service_id: s for s in snapshot.services}
        self.wagons: dict[str, Wagon] = {w.wagon_id: w for w in snapshot.wagons}
        self.slots_by_service: dict[str, list[tuple[Wagon, Slot]]] = {}
        for slot in snapshot.slots:
            wagon = self.wagons[slot.wagon_id]
            self.slots_by_service.setdefault(wagon.service_id, []).append((wagon, slot))


def evaluate_scenario(
    snapshot: ScenarioInputSnapshot,
    service_ids: list[str] | None = None,
) -> ScenarioEvaluation:
    """Evaluate every order against the baseline services.

    `service_ids` overrides the baseline set; P3 uses it to score a derived
    scenario without mutating the snapshot's own baseline.
    """
    index = _Index(snapshot)
    target_service_ids = list(service_ids or snapshot.baseline_service_ids)

    return ScenarioEvaluation(
        evaluations=[
            _evaluate_order(order, snapshot, index, target_service_ids)
            for order in snapshot.orders
        ]
    )


def _evaluate_order(
    order: Order,
    snapshot: ScenarioInputSnapshot,
    index: _Index,
    service_ids: list[str],
) -> OrderEvaluation:
    missing_fields, input_codes = _validate_input(order)

    if input_codes:
        # 02 §9.1: a failed order is never given candidates.
        return OrderEvaluation(
            order_id=order.order_id,
            input_state="REVIEW_REQUIRED",
            eligibility_state="NOT_EVALUATED",
            reason_codes=sorted(set(input_codes)),
            missing_fields=missing_fields,
        )

    candidates: list[Candidate] = []
    violations: set[str] = set()
    evidence: dict[str, dict] = {}

    for service_id in service_ids:
        service = index.services.get(service_id)
        if service is None:
            violations.add(rc.SERVICE_UNAVAILABLE)
            continue

        service_codes = _check_service(order, service, index)
        evidence[service_id] = {"service_violations": sorted(service_codes)}

        if service_codes:
            # Stop here on purpose. Descending into slots would add slot-level
            # codes for an order already excluded at the service level, and the
            # extra code can outrank the real cause inside its own family
            # (ORD-007: SLOT_HEIGHT_EXCEEDED would beat TUNNEL_HEIGHT_EXCEEDED
            # on the alphabetical tie-break).
            violations.update(service_codes)
            continue

        slot_codes: set[str] = set()
        for wagon, slot in sorted(
            index.slots_by_service.get(service_id, []),
            key=lambda pair: (pair[0].display_order, pair[1].position, pair[1].slot_id),
        ):
            codes = _check_slot(order, wagon, slot)
            if codes:
                slot_codes.update(codes)
                continue
            candidates.append(
                Candidate(
                    order_id=order.order_id,
                    service_id=service_id,
                    wagon_id=wagon.wagon_id,
                    slot_id=slot.slot_id,
                )
            )

        evidence[service_id]["slot_violations"] = sorted(slot_codes)
        if not candidates and slot_codes:
            violations.update(slot_codes)

    if candidates:
        # A candidate exists, so nothing blocks this order; the reasons other
        # services rejected it are evidence, not a verdict.
        return OrderEvaluation(
            order_id=order.order_id,
            input_state="VALID",
            eligibility_state="ELIGIBLE",
            reason_codes=[],
            candidates=candidates,
            evidence=evidence,
        )

    if not violations:
        violations.add(rc.NO_ELIGIBLE_SLOT)

    return OrderEvaluation(
        order_id=order.order_id,
        input_state="VALID",
        eligibility_state="INELIGIBLE",
        reason_codes=sorted(violations),
        evidence=evidence,
    )


def _validate_input(order: Order) -> tuple[list[str], list[str]]:
    """Required values, units and time coherence (02 §8)."""
    missing_fields: list[str] = []
    codes: list[str] = []

    if order.gross_weight_kg is None:
        missing_fields.append("gross_weight_kg")

    if missing_fields:
        codes.append(rc.MISSING_REQUIRED_FIELD)

    if order.ready_at >= order.due_at:
        codes.append(rc.INVALID_TIME_RANGE)

    return missing_fields, codes


def _check_service(order: Order, service: Service, index: _Index) -> set[str]:
    """Hard constraints that depend on the service, not on a specific slot."""
    codes: set[str] = set()

    if not service.available:
        codes.add(rc.SERVICE_UNAVAILABLE)

    origin = index.terminals.get(service.origin_terminal_id)
    destination = index.terminals.get(service.destination_terminal_id)

    # The order has to actually travel this service's leg.
    if service.origin_terminal_id not in order.origin_terminal_ids:
        codes.add(rc.TERMINAL_NOT_ON_SERVICE_ROUTE)
    if service.destination_terminal_id not in order.destination_terminal_ids:
        codes.add(rc.TERMINAL_NOT_ON_SERVICE_ROUTE)

    # Both terminals must handle the order's trailer type.
    for terminal in (origin, destination):
        if terminal is None:
            codes.add(rc.TERMINAL_NOT_COMPATIBLE)
            continue
        if not set(order.compatibility_tags).issubset(set(terminal.supported_tags)):
            codes.add(rc.TERMINAL_NOT_COMPATIBLE)

    # Intake: ready before the service stops accepting freight.
    # `planning_cutoff_at` is the materialised deadline; on the canonical
    # fixture it equals departure_at - origin.intake_cutoff_minutes. Origin
    # handling time is deliberately not added here — doing so would push
    # ORD-008 past the cutoff and mask its real cause (TERMINAL_NOT_COMPATIBLE).
    if order.ready_at > service.planning_cutoff_at:
        codes.add(rc.READY_AFTER_CUTOFF)

    # Delivery: arrival plus destination handling must land inside the due time.
    handling = destination.minimum_handling_minutes if destination else 0
    if _add_minutes(service.arrival_at, handling) > order.due_at:
        codes.add(rc.DUE_TIME_EXCEEDED)

    route = index.routes.get(service.route_constraint_id)
    if route is not None:
        if order.dimensions_mm.height > route.max_height_mm:
            codes.add(rc.TUNNEL_HEIGHT_EXCEEDED)
        if order.dimensions_mm.width > route.max_width_mm:
            codes.add(rc.ROUTE_WIDTH_EXCEEDED)
        if order.gross_weight_kg is not None and order.gross_weight_kg > route.max_weight_kg:
            codes.add(rc.ROUTE_WEIGHT_EXCEEDED)

    return codes


def _check_slot(order: Order, wagon: Wagon, slot: Slot) -> set[str]:
    """Hard constraints of one wagon/slot pair."""
    codes: set[str] = set()

    if not wagon.available:
        codes.add(rc.WAGON_UNAVAILABLE)
    if not slot.available:
        codes.add(rc.SLOT_UNAVAILABLE)

    if not set(order.compatibility_tags).issubset(set(slot.supported_tags)):
        codes.add(rc.SLOT_TAG_NOT_SUPPORTED)

    if order.dimensions_mm.height > slot.max_dimensions_mm.height:
        codes.add(rc.SLOT_HEIGHT_EXCEEDED)
    if order.dimensions_mm.width > slot.max_dimensions_mm.width:
        codes.add(rc.SLOT_WIDTH_EXCEEDED)
    if order.dimensions_mm.length > slot.max_dimensions_mm.length:
        codes.add(rc.SLOT_LENGTH_EXCEEDED)

    if order.gross_weight_kg is not None:
        if order.gross_weight_kg > slot.max_weight_kg:
            codes.add(rc.SLOT_WEIGHT_EXCEEDED)
        # A single order heavier than the whole wagon can never fit, whatever
        # else is loaded; the shared-capacity check belongs to the solver.
        if order.gross_weight_kg > wagon.max_total_weight_kg:
            codes.add(rc.WAGON_WEIGHT_EXCEEDED)

    return codes


def _add_minutes(moment: datetime, minutes: int) -> datetime:
    from datetime import timedelta

    return moment + timedelta(minutes=minutes)
