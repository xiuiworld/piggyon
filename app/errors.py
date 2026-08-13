"""Error envelope shared by every failure response (04 §1, §9)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.models.api import ErrorCode

logger = logging.getLogger(__name__)

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


async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Starlette's own 404/405 answer `{"detail": ...}`, which is not the envelope.

    A client that parses every failure the documented way would hit a decode
    error on a mistyped URL.
    """
    code: ErrorCode = "SCENARIO_NOT_FOUND" if exc.status_code == 404 else "INVALID_INPUT"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": code,
            "message": str(exc.detail),
            "details": [],
            "trace_id": new_trace_id(),
        },
    )


async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    """Last resort so a bug still answers in the contract's shape (04 §1).

    Without this the response is a plain-text `Internal Server Error`: no code,
    no trace_id, nothing for the operator to quote in a fault report. The
    message stays generic because the exception text is not for the client;
    the stack trace goes to the log instead.
    """
    trace_id = new_trace_id()
    logger.exception("unhandled error [%s]", trace_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": "SOLVER_UNAVAILABLE",
            "message": "The request could not be completed.",
            "details": [],
            "trace_id": trace_id,
        },
    )


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
