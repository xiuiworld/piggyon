"""Scenario / run / decision store.

Two backends behind one protocol so routes never learn which one is live:

- `MemoryStore`  — always available, used by tests and by the demo fallback.
- `SupabaseStore` — Postgres via Supabase, used when credentials are present.

09 §8 permits the in-memory downgrade, so a missing Supabase credential is a
startup log line, not a crash.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

SCENARIOS_TABLE = "scenarios"
RUNS_TABLE = "runs"


class ScenarioRecord(dict):
    """Stored scenario row. A plain dict keeps both backends symmetric."""


class Store(Protocol):
    backend_name: str

    def ping(self) -> bool:
        """True when the backing store answers. Never raises."""

    def next_scenario_id(self) -> str: ...

    def save_scenario(self, record: dict[str, Any]) -> None: ...

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...

    def update_scenario_state(self, scenario_id: str, state: str) -> None: ...

    def save_validation(self, scenario_id: str, result: dict[str, Any]) -> None: ...

    def get_validation(self, scenario_id: str) -> dict[str, Any] | None: ...

    def next_run_id(self) -> str: ...

    def save_run(self, record: dict[str, Any]) -> None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...


class MemoryStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scenarios: dict[str, dict[str, Any]] = {}
        self._validations: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._scenario_seq = 0
        self._run_seq = 0

    def ping(self) -> bool:
        return True

    def next_scenario_id(self) -> str:
        with self._lock:
            self._scenario_seq += 1
            return f"SCN-{self._scenario_seq:03d}"

    def save_scenario(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._scenarios[record["scenario_id"]] = record

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._scenarios.get(scenario_id)

    def update_scenario_state(self, scenario_id: str, state: str) -> None:
        with self._lock:
            scenario = self._scenarios.get(scenario_id)
            if scenario is not None:
                scenario["state"] = state

    def save_validation(self, scenario_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._validations[scenario_id] = result

    def get_validation(self, scenario_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._validations.get(scenario_id)

    def next_run_id(self) -> str:
        with self._lock:
            self._run_seq += 1
            return f"RUN-{self._run_seq:03d}"

    def save_run(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._runs[record["run_id"]] = record

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._runs.get(run_id)


class SupabaseStore:
    """Supabase-backed store.

    Scenario IDs stay monotonic per process; the row itself is the source of
    truth, so a restart continues from the highest stored ID.
    """

    backend_name = "supabase"

    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client

        self._client = create_client(url, key)
        self._lock = threading.Lock()
        self._scenario_seq: int | None = None
        self._run_seq: int | None = None

    def ping(self) -> bool:
        try:
            self._client.table(SCENARIOS_TABLE).select("scenario_id").limit(1).execute()
            return True
        except Exception as exc:  # noqa: BLE001 - ping must never raise
            logger.warning("supabase ping failed: %s", exc)
            return False

    def next_scenario_id(self) -> str:
        with self._lock:
            if self._scenario_seq is None:
                self._scenario_seq = self._highest_stored_sequence()
            self._scenario_seq += 1
            return f"SCN-{self._scenario_seq:03d}"

    def _highest_stored_sequence(self) -> int:
        try:
            response = (
                self._client.table(SCENARIOS_TABLE)
                .select("scenario_id")
                .order("scenario_id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read last scenario_id, starting at 0: %s", exc)
            return 0
        rows = response.data or []
        if not rows:
            return 0
        last = str(rows[0].get("scenario_id", ""))
        _, _, digits = last.rpartition("-")
        return int(digits) if digits.isdigit() else 0

    def save_scenario(self, record: dict[str, Any]) -> None:
        self._client.table(SCENARIOS_TABLE).insert(record).execute()

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table(SCENARIOS_TABLE)
            .select("*")
            .eq("scenario_id", scenario_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def update_scenario_state(self, scenario_id: str, state: str) -> None:
        self._client.table(SCENARIOS_TABLE).update({"state": state}).eq(
            "scenario_id", scenario_id
        ).execute()

    def save_validation(self, scenario_id: str, result: dict[str, Any]) -> None:
        self._client.table(SCENARIOS_TABLE).update({"validation_result": result}).eq(
            "scenario_id", scenario_id
        ).execute()

    def get_validation(self, scenario_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table(SCENARIOS_TABLE)
            .select("validation_result")
            .eq("scenario_id", scenario_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0].get("validation_result") if rows else None

    def next_run_id(self) -> str:
        with self._lock:
            if self._run_seq is None:
                self._run_seq = self._highest_run_sequence()
            self._run_seq += 1
            return f"RUN-{self._run_seq:03d}"

    def _highest_run_sequence(self) -> int:
        try:
            response = (
                self._client.table(RUNS_TABLE)
                .select("run_id")
                .order("run_id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read last run_id, starting at 0: %s", exc)
            return 0
        rows = response.data or []
        if not rows:
            return 0
        _, _, digits = str(rows[0].get("run_id", "")).rpartition("-")
        return int(digits) if digits.isdigit() else 0

    def save_run(self, record: dict[str, Any]) -> None:
        self._client.table(RUNS_TABLE).insert(record).execute()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table(RUNS_TABLE)
            .select("*")
            .eq("run_id", run_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None


def build_store(settings: Settings | None = None) -> Store:
    settings = settings or get_settings()

    if settings.storage_backend != "supabase":
        return MemoryStore()

    if not settings.supabase_configured:
        logger.warning(
            "STORAGE_BACKEND=supabase but SUPABASE_URL/SUPABASE_KEY are unset; "
            "falling back to in-memory store"
        )
        return MemoryStore()

    try:
        store = SupabaseStore(settings.supabase_url, settings.supabase_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("supabase client init failed (%s); using in-memory store", exc)
        return MemoryStore()

    if not store.ping():
        logger.warning("supabase unreachable at startup; using in-memory store")
        return MemoryStore()

    return store


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
