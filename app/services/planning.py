"""Orchestration for validate and run.

Keeps the routers thin: they own HTTP concerns, this owns the P1 -> P2 ->
validator -> hash pipeline.
"""

from __future__ import annotations

from typing import Any

from app.hashing import sha256_of, result_sha256
from app.models.snapshot import ScenarioInputSnapshot
from app.rules.eligibility import ScenarioEvaluation, evaluate_scenario
from app.solver.baseline import SolverParameters, solve_baseline
from app.validation.plan_validator import validate_plan


def snapshot_of(scenario: dict[str, Any]) -> ScenarioInputSnapshot:
    return ScenarioInputSnapshot.model_validate(scenario["input_snapshot"])


def build_validation_result(
    scenario_id: str, evaluation: ScenarioEvaluation
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "validation_status": "COMPLETED",
        "orders": [
            {
                "order_id": e.order_id,
                "input_state": e.input_state,
                "eligibility_state": e.eligibility_state,
                "reason_codes": e.reason_codes,
                "missing_fields": e.missing_fields,
                "eligible_slot_ids": e.eligible_slot_ids,
                "primary_reason_code": e.primary_reason_code,
            }
            for e in sorted(evaluation.evaluations, key=lambda e: e.order_id)
        ],
    }


def run_baseline(
    run_id: str,
    scenario_id: str,
    snapshot: ScenarioInputSnapshot,
    parameters: SolverParameters,
    raw_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Solve and package one run.

    `raw_snapshot` is the document as it was submitted. The hashes cover that,
    not the parsed model, so they identify the caller's input rather than this
    service's serialisation of it.
    """
    hashed = raw_snapshot if raw_snapshot is not None else snapshot.model_dump(mode="json")
    evaluation = evaluate_scenario(snapshot)
    result = solve_baseline(snapshot, evaluation, parameters)
    validation = validate_plan(snapshot, result.assignments, result.order_outcomes)

    assignments = [a.as_dict() for a in result.assignments]
    order_outcomes = [o.as_dict() for o in result.order_outcomes]

    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "solver_status": result.solver_status,
        "run_state": result.run_state,
        "is_optimal": result.is_optimal,
        "validator_status": validation.status,
        "validator_findings": [f.as_dict() for f in validation.findings],
        "reproducibility": {
            "solver_parameters": parameters.as_dict(),
            "input_snapshot_sha256": sha256_of(hashed),
            "policy_sha256": sha256_of(hashed["policy"]),
            "result_sha256": result_sha256(assignments, order_outcomes),
        },
        "assignments": assignments,
        "order_outcomes": order_outcomes,
        "objective_values": result.objective_values,
    }
