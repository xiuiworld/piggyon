"""FastAPI application entrypoint.

Run: `uvicorn app.main:app --reload`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_error_handler,
)
from app.routers import ai, runs, scenarios
from app.storage import build_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    store = build_store(settings)
    app.state.store = store
    logger.info("storage backend: %s (ping=%s)", store.backend_name, store.ping())
    yield


app = FastAPI(
    title="Rail Slot Planning MVP API",
    version="1.0.0",
    description="All operational values in canonical-v1 are DEMO_ASSUMPTION.",
    lifespan=lifespan,
)

# The Next.js front end runs on a separate origin during the hackathon.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
# 04 §1 promises the envelope on *every* failure, including ones nobody planned.
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(scenarios.router)
app.include_router(runs.router)
app.include_router(ai.router)


def custom_openapi() -> dict:
    """Publish the contract we actually serve.

    FastAPI adds a default 422 to every operation with a body, but this API
    converts schema failures to `400 INVALID_INPUT` and reserves 422 for
    `VALIDATION_REQUIRED` (04 §9). Leaving the default in would hand the front
    end a response code this service never returns.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    declared = _declared_422_operations()
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if (path, method) in declared:
                # The route really does answer 422 (VALIDATION_REQUIRED), so
                # removing it would hide a branch the client has to handle.
                continue
            operation.get("responses", {}).pop("422", None)

    app.openapi_schema = schema
    return schema


def _declared_422_operations() -> set[tuple[str, str]]:
    """Routes that opted into 422 themselves, as opposed to FastAPI adding it.

    Included routers are nested rather than flattened into `app.routes`, so
    this walks them; scanning only the top level found nothing and stripped
    every 422, including the ones the run and export endpoints really return.
    """
    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            # An included router appears as a wrapper; its real APIRoutes hang
            # off `original_router`, already carrying their full path.
            nested = getattr(route, "routes", None)
            if nested is None:
                wrapped = getattr(route, "original_router", None)
                nested = getattr(wrapped, "routes", None) if wrapped else None
            if nested:
                walk(nested)
                continue

            responses = getattr(route, "responses", None) or {}
            if 422 in responses or "422" in responses:
                for method in getattr(route, "methods", set()) or set():
                    found.add((getattr(route, "path", ""), method.lower()))

    walk(app.routes)
    return found


app.openapi = custom_openapi


@app.get("/health", tags=["ops"], summary="Liveness and storage reachability")
def health() -> dict[str, object]:
    store = app.state.store
    return {
        "status": "ok",
        "storage_backend": store.backend_name,
        "storage_reachable": store.ping(),
    }
