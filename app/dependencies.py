"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from app.storage import Store


def get_store(request: Request) -> Store:
    """The process-wide store, built once during startup."""
    return request.app.state.store
