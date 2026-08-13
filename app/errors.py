"""Error envelope shared by every failure response (04 §1, §9)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.models.api import ErrorCode

HTTP_STATUS_BY_CODE: dict[ErrorCode, int] = {
    "INVALID_INPUT": 400,
    "SCENARIO_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "VALIDATION_REQUIRED": 422,
    "POLICY_VIOLATION": 409,
    "RUN_NOT_ACCEPTABLE": 409,
    "SOLVER_UNAVAILABLE": 503,
}


class ApiError(Exception):
    """Raise to return a contract-shaped error response."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


def new_trace_id() -> str:
    return f"trc-{uuid.uuid4().hex[:16]}"


def error_response(
    code: ErrorCode,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=HTTP_STATUS_BY_CODE[code],
        content={
            "code": code,
            "message": message,
            "details": jsonable_encoder(details or []),
            "trace_id": new_trace_id(),
        },
    )


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.code, exc.message, exc.details)


async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic rejects the body -> 400 INVALID_INPUT, not FastAPI's default 422.

    422 is reserved for VALIDATION_REQUIRED (scenario not yet validated).
    """
    return error_response(
        "INVALID_INPUT",
        "Request body failed schema validation.",
        details=[
            {
                "location": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ],
    )
