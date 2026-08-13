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
DECISIONS_TABLE = "decisions"
TRACE_TABLE = "trace_events"

# Enough history for a demo instance; ids are only scanned to resume numbering.
_SEQUENCE_SCAN_LIMIT = 200

# Ids share a namespace by prefix: every `RUN-ALT-001` also starts with `RUN-`.
# A scan for the baseline counter has to exclude the longer sibling.
_SIBLING_PREFIXES: dict[str, tuple[str, ...]] = {
    "RUN-": ("RUN-ALT-",),
    "SCN-": ("SCN-ALT-",),
}


class ScenarioRecord(dict):
    """Stored scenario row. A plain dict keeps both backends symmetric."""


class Store(Protocol):
    backend_name: str

    def ping(self) -> bool:
        """True when the backing store answers. Never raises."""

    def next_scenario_id(self) -> str: ...

    def save_scenario(self, record: dict[str, Any]) -> None: ...

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None: ...

    def list_scenarios(self, limit: int) -> list[dict[str, Any]]: ...

    def delete_scenario(self, scenario_id: str) -> None: ...

    def update_scenario_state(self, scenario_id: str, state: str) -> None: ...

    def save_validation(self, scenario_id: str, result: dict[str, Any]) -> None: ...

    def get_validation(self, scenario_id: str) -> dict[str, Any] | None: ...

    def next_run_id(self) -> str: ...

    def save_run(self, record: dict[str, Any]) -> None: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def latest_run_id(self, scenario_id: str) -> str | None: ...

    def list_run_ids(self, scenario_id: str) -> list[str]: ...

    def save_explanation(self, run_id: str, result: dict[str, Any]) -> None: ...

    def get_explanation(self, run_id: str) -> dict[str, Any] | None: ...

    def clear_explanation(self, run_id: str) -> None: ...

    def update_order_outcome(
        self, run_id: str, order_id: str, outcome: dict[str, Any]
    ) -> None:
        """Replace one order's outcome inside a run, atomically.

        Recording an alternative used to read the whole run, edit one order and
        write the document back. Two operators working on different orders both
        read before either wrote, and the later write reverted the earlier
        order's alternative — while its derived scenario stayed in the store.
        """

    def next_alternative_sequence(self) -> int: ...

    def save_decision(self, record: dict[str, Any]) -> None: ...

    def list_decisions(self, run_id: str) -> list[dict[str, Any]]: ...

    def append_trace(self, scenario_id: str, event: dict[str, Any]) -> None: ...

    def list_trace(self, scenario_id: str) -> list[dict[str, Any]]: ...


class MemoryStore:
    backend_name = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scenarios: dict[str, dict[str, Any]] = {}
        self._validations: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._explanations: dict[str, dict[str, Any]] = {}
        self._decisions: dict[str, list[dict[str, Any]]] = {}
        self._trace: dict[str, list[dict[str, Any]]] = {}
        self._scenario_seq = 0
        self._run_seq = 0
        self._alternative_seq = 0

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

    def list_scenarios(self, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._scenarios.values())
        # Newest first, with the id as a tiebreak: two scenarios created inside
        # the same clock tick would otherwise swap places between calls and the
        # list would reorder under the reader for no reason.
        records.sort(key=lambda r: (str(r.get("created_at", "")), r["scenario_id"]), reverse=True)
        return records[:limit]

    def delete_scenario(self, scenario_id: str) -> None:
        with self._lock:
            self._scenarios.pop(scenario_id, None)
            self._validations.pop(scenario_id, None)
            self._trace.pop(scenario_id, None)
            for run_id in [
                rid for rid, run in self._runs.items() if run.get("scenario_id") == scenario_id
            ]:
                self._runs.pop(run_id, None)
                self._explanations.pop(run_id, None)
                self._decisions.pop(run_id, None)

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

    def latest_run_id(self, scenario_id: str) -> str | None:
        with self._lock:
            runs = [r for r in self._runs.values() if r.get("scenario_id") == scenario_id]
        if not runs:
            return None
        return max(runs, key=lambda r: (str(r.get("created_at") or ""), r["run_id"]))["run_id"]

    def list_run_ids(self, scenario_id: str) -> list[str]:
        with self._lock:
            return sorted(
                rid for rid, run in self._runs.items() if run.get("scenario_id") == scenario_id
            )

    def save_explanation(self, run_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._explanations[run_id] = result

    def get_explanation(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._explanations.get(run_id)

    def clear_explanation(self, run_id: str) -> None:
        with self._lock:
            self._explanations.pop(run_id, None)

    def update_order_outcome(
        self, run_id: str, order_id: str, outcome: dict[str, Any]
    ) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            for index, existing in enumerate(run.get("order_outcomes", [])):
                if existing["order_id"] == order_id:
                    run["order_outcomes"][index] = outcome
                    return

    def next_alternative_sequence(self) -> int:
        with self._lock:
            self._alternative_seq += 1
            return self._alternative_seq

    def save_decision(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._decisions.setdefault(record["run_id"], []).append(record)

    def list_decisions(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._decisions.get(run_id, []))

    def append_trace(self, scenario_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._trace.setdefault(scenario_id, []).append(event)

    def list_trace(self, scenario_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._trace.get(scenario_id, []))


class SupabaseStore:
    """Supabase-backed store.

    Each row keeps its full record in a `document` JSONB column, with only the
    few fields worth querying promoted to real columns. Records grow every
    phase; a column per field would mean a migration per phase and a runtime
    error whenever the two drifted apart.

    Writes are upserts, because a record is legitimately re-saved: recording an
    alternative writes the baseline run back with its `alternative_state`
    updated.
    """

    backend_name = "supabase"

    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client

        self._client = create_client(url, key)
        self._lock = threading.Lock()
        self._scenario_seq: int | None = None
        self._run_seq: int | None = None
        self._alternative_seq: int | None = None

    def ping(self) -> bool:
        try:
            self._client.table(SCENARIOS_TABLE).select("scenario_id").limit(1).execute()
            return True
        except Exception as exc:  # noqa: BLE001 - ping must never raise
            logger.warning("supabase ping failed: %s", exc)
            return False

    # --- scenarios ---------------------------------------------------------

    def next_scenario_id(self) -> str:
        with self._lock:
            if self._scenario_seq is None:
                self._scenario_seq = self._highest_sequence(SCENARIOS_TABLE, "scenario_id", "SCN-")
            self._scenario_seq += 1
            return f"SCN-{self._scenario_seq:03d}"

    def save_scenario(self, record: dict[str, Any]) -> None:
        self._client.table(SCENARIOS_TABLE).upsert(
            {
                "scenario_id": record["scenario_id"],
                "state": record["state"],
                "created_at": record["created_at"],
                "document": record,
            }
        ).execute()

    def get_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        row = self._one(SCENARIOS_TABLE, "scenario_id", scenario_id)
        return row.get("document") if row else None

    def list_scenarios(self, limit: int) -> list[dict[str, Any]]:
        response = (
            self._client.table(SCENARIOS_TABLE)
            .select("document")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [row["document"] for row in (response.data or []) if row.get("document")]

    def delete_scenario(self, scenario_id: str) -> None:
        # Children first: `runs` and `decisions` carry foreign keys, so deleting
        # the scenario row on its own is refused by the database rather than
        # cascading. Trace rows are keyed by scenario id without a constraint.
        for run_id in self.list_run_ids(scenario_id):
            self._client.table(DECISIONS_TABLE).delete().eq("run_id", run_id).execute()
        self._client.table(RUNS_TABLE).delete().eq("scenario_id", scenario_id).execute()
        self._client.table(TRACE_TABLE).delete().eq("scenario_id", scenario_id).execute()
        self._client.table(SCENARIOS_TABLE).delete().eq("scenario_id", scenario_id).execute()

    def update_scenario_state(self, scenario_id: str, state: str) -> None:
        record = self.get_scenario(scenario_id)
        if record is None:
            return
        record["state"] = state
        self.save_scenario(record)

    def save_validation(self, scenario_id: str, result: dict[str, Any]) -> None:
        self._client.table(SCENARIOS_TABLE).update({"validation_result": result}).eq(
            "scenario_id", scenario_id
        ).execute()

    def get_validation(self, scenario_id: str) -> dict[str, Any] | None:
        row = self._one(SCENARIOS_TABLE, "scenario_id", scenario_id, "validation_result")
        return row.get("validation_result") if row else None

    # --- runs --------------------------------------------------------------

    def next_run_id(self) -> str:
        with self._lock:
            if self._run_seq is None:
                self._run_seq = self._highest_sequence(RUNS_TABLE, "run_id", "RUN-")
            self._run_seq += 1
            return f"RUN-{self._run_seq:03d}"

    def save_run(self, record: dict[str, Any]) -> None:
        self._client.table(RUNS_TABLE).upsert(
            {
                "run_id": record["run_id"],
                "scenario_id": record["scenario_id"],
                "solver_status": record["solver_status"],
                "validator_status": record["validator_status"],
                "created_at": record.get("created_at"),
                "document": record,
            }
        ).execute()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one(RUNS_TABLE, "run_id", run_id)
        return row.get("document") if row else None

    def latest_run_id(self, scenario_id: str) -> str | None:
        # Ordered here rather than in the query: `runs.created_at` is nullable,
        # and where a NULL sorts differs between PostgREST versions. A scenario
        # holds a handful of runs, so picking in Python is both cheap and the
        # same rule the in-memory store applies.
        response = (
            self._client.table(RUNS_TABLE)
            .select("run_id, created_at")
            .eq("scenario_id", scenario_id)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        return max(rows, key=lambda r: (str(r.get("created_at") or ""), r["run_id"]))["run_id"]

    def list_run_ids(self, scenario_id: str) -> list[str]:
        response = (
            self._client.table(RUNS_TABLE)
            .select("run_id")
            .eq("scenario_id", scenario_id)
            .execute()
        )
        return sorted(row["run_id"] for row in (response.data or []))

    def save_explanation(self, run_id: str, result: dict[str, Any]) -> None:
        self._client.table(RUNS_TABLE).update({"explanation": result}).eq(
            "run_id", run_id
        ).execute()

    def get_explanation(self, run_id: str) -> dict[str, Any] | None:
        row = self._one(RUNS_TABLE, "run_id", run_id, "explanation")
        return row.get("explanation") if row else None

    def clear_explanation(self, run_id: str) -> None:
        self._client.table(RUNS_TABLE).update({"explanation": None}).eq(
            "run_id", run_id
        ).execute()

    def update_order_outcome(
        self, run_id: str, order_id: str, outcome: dict[str, Any]
    ) -> None:
        # Narrow the window by re-reading here rather than reusing a document
        # the caller fetched before it did its own work.
        with self._lock:
            record = self.get_run(run_id)
            if record is None:
                return
            for index, existing in enumerate(record.get("order_outcomes", [])):
                if existing["order_id"] == order_id:
                    record["order_outcomes"][index] = outcome
                    break
            else:
                return
            self.save_run(record)

    def next_alternative_sequence(self) -> int:
        with self._lock:
            if self._alternative_seq is None:
                self._alternative_seq = self._highest_sequence(RUNS_TABLE, "run_id", "RUN-ALT-")
            self._alternative_seq += 1
            return self._alternative_seq

    # --- decisions and trace ------------------------------------------------

    def save_decision(self, record: dict[str, Any]) -> None:
        self._client.table(DECISIONS_TABLE).upsert(
            {
                "decision_id": record["decision_id"],
                "run_id": record["run_id"],
                "decision_state": record["decision_state"],
                "created_at": record["created_at"],
                "document": record,
            }
        ).execute()

    def list_decisions(self, run_id: str) -> list[dict[str, Any]]:
        response = (
            self._client.table(DECISIONS_TABLE)
            .select("document")
            .eq("run_id", run_id)
            .order("created_at")
            .execute()
        )
        return [row["document"] for row in (response.data or [])]

    def append_trace(self, scenario_id: str, event: dict[str, Any]) -> None:
        self._client.table(TRACE_TABLE).insert(
            {
                "event_id": event["event_id"],
                "scenario_id": scenario_id,
                "event_type": event["event_type"],
                "occurred_at": event["occurred_at"],
                "document": event,
            }
        ).execute()

    def list_trace(self, scenario_id: str) -> list[dict[str, Any]]:
        response = (
            self._client.table(TRACE_TABLE)
            .select("document")
            .eq("scenario_id", scenario_id)
            .order("occurred_at")
            .execute()
        )
        return [row["document"] for row in (response.data or [])]

    # --- helpers -----------------------------------------------------------

    def _one(
        self, table: str, key: str, value: str, columns: str = "*"
    ) -> dict[str, Any] | None:
        response = (
            self._client.table(table).select(columns).eq(key, value).limit(1).execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def _highest_sequence(self, table: str, column: str, prefix: str) -> int:
        """Continue numbering after a restart instead of colliding with old ids.

        Filtering happens here rather than in the query because the ids share
        a namespace: `RUN-ALT-003` sorts above `RUN-001`, so asking the
        database for the largest `RUN-%` would hand baseline numbering an
        alternative's counter. Only an exact `prefix + digits` match counts.
        """
        try:
            # Filter in the query, not after the limit. Ordering is textual, so
            # `RUN-ALT-001` sorts above `RUN-999` and a descending page could be
            # entirely alternative ids; every one fails the digit check, the
            # sequence restarts at 1, and the upsert overwrites a live run.
            query = self._client.table(table).select(column).like(column, f"{prefix}%")
            for other in _SIBLING_PREFIXES.get(prefix, ()):  # exclude longer ids
                query = query.not_.like(column, f"{other}%")
            response = query.order(column, desc=True).limit(_SEQUENCE_SCAN_LIMIT).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read last %s, starting at 0: %s", column, exc)
            return 0

        highest = 0
        for row in response.data or []:
            value = str(row.get(column, ""))
            if not value.startswith(prefix):
                continue
            suffix = value[len(prefix):]
            # Compare numerically: text ordering also puts `RUN-999` above
            # `RUN-1000`, so the largest string is not the largest number.
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return highest


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
