"""P4 endpoints: intake structuring and result explanation."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.ai import answer, client, explain, intake, suggest
from app.dependencies import get_store
from app.errors import ApiError
from app.models.api import ErrorResponse
from app.services.planning import snapshot_of
from app.storage import Store

router = APIRouter(prefix="/v1", tags=["ai"])


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    # Relative times ("내일 오전") can only be resolved against a reference
    # instant; without one the model would have to invent a date.
    as_of: str | None = None


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=answer.MAX_QUESTION_CHARS)


@router.get("/ai/status", summary="Whether the generative layer is configured")
def ai_status() -> dict[str, object]:
    return {"llm_available": client.is_available(), "fallback": "RULE_BASED/TEMPLATE"}


@router.post("/intake/orders", summary="Structure an unstructured shipping request")
def structure_intake(payload: IntakeRequest) -> dict:
    return intake.structure_request(payload.text, as_of=payload.as_of)


@router.post(
    "/intake/order-batches",
    summary="Structure one document that asks for several orders",
)
def structure_intake_batch(payload: IntakeRequest) -> dict:
    """A request for five trailers arrives as one message, not five.

    The single-order endpoint keeps whichever order the model read first and
    drops the rest without saying so, which is the wrong answer to a document
    that plainly asks for more.
    """
    return intake.structure_requests(payload.text, as_of=payload.as_of)


@router.get(
    "/runs/{run_id}/explanation",
    summary="Operator-facing cards for a solved run",
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
def run_explanation(run_id: str, store: Store = Depends(get_store)) -> dict:
    """Generated once, then read.

    A run is immutable once solved, so its explanation is too. Generating on
    every read put two model calls and six seconds in front of every render --
    and, worse, the sentences drifted: the same plan described itself
    differently on each visit, and the downloaded bundle did not say what the
    operator had been reading when they decided.

    The alternative search is the one thing that changes a solved run, and it
    clears this so the next read regenerates.
    """
    run = _require_run(store, run_id)

    stored = store.get_explanation(run_id)
    if stored is not None:
        return stored

    cards = explain.build_cards(run)

    # Which approved change to try first, for the orders the plan left out.
    # Folded into the same response because it is read at the same moment, by
    # the same screen, about the same orders.
    scenario = store.get_scenario(run["scenario_id"])
    if scenario is not None:
        proposed = suggest.build_suggestions(run, scenario["input_snapshot"])
        by_order = proposed["suggestions"]
        for card in cards["cards"]:
            entry = by_order.get(card["order_id"])
            if entry is None:
                continue
            card["suggested_adjustment_types"] = entry["adjustment_types"]
            card["suggestion"] = entry["reason"]
        cards["suggestion_source"] = proposed["source"]

    store.save_explanation(run_id, cards)
    return cards


@router.post(
    "/runs/{run_id}/questions",
    summary="Answer one question about a solved run",
    responses={404: {"model": ErrorResponse, "description": "Run not found"}},
)
def run_question(
    run_id: str,
    payload: QuestionRequest,
    store: Store = Depends(get_store),
) -> dict:
    """Answered from the run, or refused.

    There is no template to fall back to the way an explanation card has one,
    and a plausible wrong answer to "왜 밀렸나요?" is worse than no answer: the
    operator cannot tell it from a right one. So the guard's verdict is served
    as the result rather than swallowed.
    """
    run = _require_run(store, run_id)
    scenario = store.get_scenario(run["scenario_id"])
    if scenario is None:
        raise ApiError("SCENARIO_NOT_FOUND", f"Scenario {run['scenario_id']} does not exist.")

    return answer.answer_question(
        payload.question,
        run,
        snapshot_of(scenario).model_dump(mode="json"),
    )


def _require_run(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise ApiError("RUN_NOT_FOUND", f"Run {run_id} does not exist.")
    return run
