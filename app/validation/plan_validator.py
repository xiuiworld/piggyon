"""Independent re-check of a solved plan (05 §3, 07 §4).

Deliberately does not import the eligibility engine. Reusing the module that
produced the plan would only prove the solver is self-consistent; the point of
this pass is to re-derive every hard constraint straight from the snapshot so a
bug in the gate shows up as a disagreement instead of being confirmed twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.models.snapshot import ScenarioInputSnapshot
from app.solver.baseline import Assignment, OrderOutcome


@dataclass
class ValidationFinding:
    check: str
    order_id: str | None
    message: str

    def as_dict(self) -> dict:
        return {"check": self.check, "order_id": self.order_id, "message": self.message}


@dataclass
class PlanValidation:
    status: str
    findings: list[ValidationFinding] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"status": self.status, "findings": [f.as_dict() for f in self.findings]}


def validate_plan(
    snapshot: ScenarioInputSnapshot,
    assignments: list[Assignment],
    order_outcomes: list[OrderOutcome],
    allowed_service_ids: list[str] | None = None,
) -> PlanValidation:
    findings: list[ValidationFinding] = []

    orders = {o.order_id: o for o in snapshot.orders}
    terminals = {t.terminal_id: t for t in snapshot.terminals}
    routes = {r.route_constraint_id: r for r in snapshot.route_constraints}
    services = {s.service_id: s for s in snapshot.services}
    wagons = {w.wagon_id: w for w in snapshot.wagons}
    slots = {s.slot_id: s for s in snapshot.slots}
    permitted = set(allowed_service_ids or snapshot.baseline_service_ids)

    _check_uniqueness(findings, assignments)

    for a in assignments:
        order = orders.get(a.order_id)
        service = services.get(a.service_id)
        wagon = wagons.get(a.wagon_id)
        slot = slots.get(a.slot_id)

        if order is None or service is None or wagon is None or slot is None:
            findings.append(
                ValidationFinding("reference", a.order_id, "assignment references unknown entity")
            )
            continue

        # TC-14: a baseline plan may not reach outside the baseline services.
        if a.service_id not in permitted:
            findings.append(
                ValidationFinding(
                    "service_scope",
                    a.order_id,
                    f"service {a.service_id} is outside the permitted set",
                )
            )

        if slot.wagon_id != wagon.wagon_id or wagon.service_id != service.service_id:
            findings.append(
                ValidationFinding(
                    "topology", a.order_id, "slot, wagon and service are not connected"
                )
            )

        if not (service.available and wagon.available and slot.available):
            findings.append(
                ValidationFinding("availability", a.order_id, "resource is not available")
            )

        if order.gross_weight_kg is None:
            findings.append(
                ValidationFinding(
                    "input", a.order_id, "assigned order is missing gross_weight_kg"
                )
            )

        _check_terminals(findings, a, order, service, terminals)
        _check_times(findings, a, order, service, terminals)
        _check_dimensions(findings, a, order, service, slot, routes)

    _check_wagon_capacity(findings, assignments, orders, wagons)
    _check_outcome_consistency(findings, assignments, order_outcomes)

    return PlanValidation(
        status="PASS" if not findings else "FAIL", findings=findings
    )


def _check_uniqueness(findings: list[ValidationFinding], assignments: list[Assignment]) -> None:
    seen_orders: set[str] = set()
    seen_slots: set[str] = set()
    for a in assignments:
        if a.order_id in seen_orders:
            findings.append(
                ValidationFinding("duplicate_order", a.order_id, "order assigned more than once")
            )
        if a.slot_id in seen_slots:
            findings.append(
                ValidationFinding("duplicate_slot", a.order_id, f"slot {a.slot_id} reused")
            )
        seen_orders.add(a.order_id)
        seen_slots.add(a.slot_id)


def _check_terminals(findings, a, order, service, terminals) -> None:
    if service.origin_terminal_id not in order.origin_terminal_ids:
        findings.append(
            ValidationFinding("terminal", a.order_id, "service origin is not an order origin")
        )
    if service.destination_terminal_id not in order.destination_terminal_ids:
        findings.append(
            ValidationFinding(
                "terminal", a.order_id, "service destination is not an order destination"
            )
        )

    tags = set(order.compatibility_tags)
    for terminal_id in (service.origin_terminal_id, service.destination_terminal_id):
        terminal = terminals.get(terminal_id)
        if terminal is None or not tags.issubset(set(terminal.supported_tags)):
            findings.append(
                ValidationFinding(
                    "terminal", a.order_id, f"terminal {terminal_id} cannot handle the order tags"
                )
            )


def _check_times(findings, a, order, service, terminals) -> None:
    if order.ready_at > service.planning_cutoff_at:
        findings.append(
            ValidationFinding("time", a.order_id, "ready_at is after the planning cutoff")
        )

    destination = terminals.get(service.destination_terminal_id)
    handling = destination.minimum_handling_minutes if destination else 0
    if service.arrival_at + timedelta(minutes=handling) > order.due_at:
        findings.append(
            ValidationFinding("time", a.order_id, "arrival plus handling is after due_at")
        )


def _check_dimensions(findings, a, order, service, slot, routes) -> None:
    dims = order.dimensions_mm
    weight = order.gross_weight_kg or 0

    if dims.height > slot.max_dimensions_mm.height:
        findings.append(ValidationFinding("dimension", a.order_id, "height exceeds the slot"))
    if dims.width > slot.max_dimensions_mm.width:
        findings.append(ValidationFinding("dimension", a.order_id, "width exceeds the slot"))
    if dims.length > slot.max_dimensions_mm.length:
        findings.append(ValidationFinding("dimension", a.order_id, "length exceeds the slot"))
    if weight > slot.max_weight_kg:
        findings.append(ValidationFinding("weight", a.order_id, "weight exceeds the slot"))
    if not set(order.compatibility_tags).issubset(set(slot.supported_tags)):
        findings.append(
            ValidationFinding("tag", a.order_id, "slot does not support the order tags")
        )

    route = routes.get(service.route_constraint_id)
    if route is None:
        findings.append(
            ValidationFinding("route", a.order_id, "service has no route constraint")
        )
        return
    if dims.height > route.max_height_mm:
        findings.append(ValidationFinding("route", a.order_id, "height exceeds route clearance"))
    if dims.width > route.max_width_mm:
        findings.append(ValidationFinding("route", a.order_id, "width exceeds route clearance"))
    if weight > route.max_weight_kg:
        findings.append(ValidationFinding("route", a.order_id, "weight exceeds route limit"))


def _check_wagon_capacity(findings, assignments, orders, wagons) -> None:
    loaded: dict[str, int] = {}
    for a in assignments:
        order = orders.get(a.order_id)
        if order is None:
            continue
        loaded[a.wagon_id] = loaded.get(a.wagon_id, 0) + (order.gross_weight_kg or 0)

    for wagon_id, total in loaded.items():
        wagon = wagons.get(wagon_id)
        if wagon is not None and total > wagon.max_total_weight_kg:
            findings.append(
                ValidationFinding(
                    "wagon_capacity",
                    None,
                    f"wagon {wagon_id} carries {total}kg over its {wagon.max_total_weight_kg}kg limit",
                )
            )


def _check_outcome_consistency(findings, assignments, order_outcomes) -> None:
    """07 §4: the reported state axes must match what actually happened."""
    assigned_ids = {a.order_id for a in assignments}

    for outcome in order_outcomes:
        is_assigned = outcome.order_id in assigned_ids

        if is_assigned and outcome.assignment_state != "ASSIGNED":
            findings.append(
                ValidationFinding(
                    "outcome", outcome.order_id, "order is assigned but not reported as ASSIGNED"
                )
            )
        if not is_assigned and outcome.assignment_state == "ASSIGNED":
            findings.append(
                ValidationFinding(
                    "outcome", outcome.order_id, "reported ASSIGNED without an assignment"
                )
            )
        if is_assigned and outcome.eligibility_state != "ELIGIBLE":
            findings.append(
                ValidationFinding(
                    "outcome", outcome.order_id, "assigned order is not marked ELIGIBLE"
                )
            )
        if outcome.input_state == "REVIEW_REQUIRED" and is_assigned:
            findings.append(
                ValidationFinding(
                    "outcome", outcome.order_id, "REVIEW_REQUIRED order was assigned"
                )
            )

    reported = {o.order_id for o in order_outcomes}
    for order_id in sorted(assigned_ids - reported):
        findings.append(
            ValidationFinding("outcome", order_id, "assignment has no order_outcome")
        )
