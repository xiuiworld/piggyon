"""Both store backends must satisfy the same contract.

`SupabaseStore` is what production runs on, so it cannot ship only exercised by
hand. A fake PostgREST client stands in for the network: it implements the
narrow slice of the query builder the store uses, which is enough to catch the
mistakes that actually happen here — inserting where an upsert is required,
forgetting to unwrap `document`, or reading a sequence off the wrong prefix.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.storage import MemoryStore, SupabaseStore

PRIMARY_KEYS = {
    "scenarios": "scenario_id",
    "runs": "run_id",
    "decisions": "decision_id",
    "trace_events": "event_id",
}


class FakeQuery:
    def __init__(self, table: "FakeTable") -> None:
        self._table = table
        self._rows: list[dict[str, Any]] | None = None
        self._filters: list[tuple[str, Any]] = []
        # (column, sql-like pattern, keep_matching)
        self._patterns: list[tuple[str, str, bool]] = []
        self._negate_next = False
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None
        self._pending_update: dict[str, Any] | None = None

    # writes
    def insert(self, record: dict[str, Any]) -> "FakeQuery":
        key = PRIMARY_KEYS[self._table.name]
        if any(r[key] == record[key] for r in self._table.rows):
            raise RuntimeError(f"duplicate key {record[key]} in {self._table.name}")
        self._table.rows.append(dict(record))
        return self

    def upsert(self, record: dict[str, Any]) -> "FakeQuery":
        key = PRIMARY_KEYS[self._table.name]
        for existing in self._table.rows:
            if existing[key] == record[key]:
                existing.update(record)
                return self
        self._table.rows.append(dict(record))
        return self

    def update(self, changes: dict[str, Any]) -> "FakeQuery":
        self._pending_update = changes
        return self

    # reads
    def select(self, columns: str = "*") -> "FakeQuery":
        self._rows = self._table.rows
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._filters.append((column, value))
        return self

    def like(self, column: str, pattern: str) -> "FakeQuery":
        keep = not self._negate_next
        self._negate_next = False
        self._patterns.append((column, pattern, keep))
        return self

    @property
    def not_(self) -> "FakeQuery":
        self._negate_next = True
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> "FakeQuery":
        self._limit = count
        return self

    def execute(self) -> Any:
        rows = [dict(r) for r in self._table.rows]
        for column, value in self._filters:
            rows = [r for r in rows if r.get(column) == value]
        for column, pattern, keep in self._patterns:
            prefix = pattern.rstrip("%")
            rows = [
                r for r in rows if str(r.get(column, "")).startswith(prefix) is keep
            ]

        if self._pending_update is not None:
            for row in self._table.rows:
                if all(row.get(c) == v for c, v in self._filters):
                    row.update(self._pending_update)
            return type("Result", (), {"data": rows})()

        if self._order:
            column, desc = self._order
            rows.sort(key=lambda r: str(r.get(column, "")), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return type("Result", (), {"data": rows})()


class FakeTable:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: list[dict[str, Any]] = []


class FakeClient:
    def __init__(self) -> None:
        self.tables = {name: FakeTable(name) for name in PRIMARY_KEYS}

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self.tables[name])


@pytest.fixture
def supabase_store() -> SupabaseStore:
    store = SupabaseStore.__new__(SupabaseStore)
    import threading

    store._client = FakeClient()
    store._lock = threading.Lock()
    store._scenario_seq = None
    store._run_seq = None
    store._alternative_seq = None
    return store


@pytest.fixture(params=["memory", "supabase"])
def store(request, supabase_store):
    return MemoryStore() if request.param == "memory" else supabase_store


def _scenario(scenario_id: str = "SCN-001") -> dict:
    return {
        "scenario_id": scenario_id,
        "scenario_name": "canonical-v1-baseline",
        "state": "VALIDATION_REQUIRED",
        "created_at": "2026-08-17T00:00:00+00:00",
        "input_snapshot": {"orders": []},
    }


def _run(run_id: str = "RUN-001", scenario_id: str = "SCN-001") -> dict:
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "solver_status": "OPTIMAL",
        "validator_status": "PASS",
        "created_at": "2026-08-17T00:00:00+00:00",
        "order_outcomes": [{"order_id": "ORD-005", "alternative_state": "NOT_SEARCHED"}],
    }


def test_scenario_round_trip(store) -> None:
    store.save_scenario(_scenario())

    assert store.get_scenario("SCN-001")["scenario_name"] == "canonical-v1-baseline"
    assert store.get_scenario("SCN-404") is None


def test_scenario_state_transition(store) -> None:
    store.save_scenario(_scenario())

    store.update_scenario_state("SCN-001", "READY_TO_SOLVE")

    assert store.get_scenario("SCN-001")["state"] == "READY_TO_SOLVE"


def test_validation_round_trip(store) -> None:
    store.save_scenario(_scenario())
    assert store.get_validation("SCN-001") is None

    store.save_validation("SCN-001", {"validation_status": "COMPLETED"})

    assert store.get_validation("SCN-001")["validation_status"] == "COMPLETED"


def test_run_is_resaved_not_duplicated(store) -> None:
    """Recording an alternative writes the baseline run back with a new state."""
    store.save_scenario(_scenario())
    record = _run()
    store.save_run(record)

    record["order_outcomes"][0]["alternative_state"] = "AVAILABLE"
    store.save_run(record)

    stored = store.get_run("RUN-001")
    assert stored["order_outcomes"][0]["alternative_state"] == "AVAILABLE"


def test_decisions_are_listed_per_run(store) -> None:
    store.save_scenario(_scenario())
    store.save_run(_run())
    for index in (1, 2):
        store.save_decision(
            {
                "decision_id": f"DEC-{index}",
                "run_id": "RUN-001",
                "decision_state": "HELD",
                "created_at": f"2026-08-17T0{index}:00:00+00:00",
                "reason": "확인 중",
            }
        )

    decisions = store.list_decisions("RUN-001")

    assert [d["decision_id"] for d in decisions] == ["DEC-1", "DEC-2"]
    assert store.list_decisions("RUN-999") == []


def test_trace_is_listed_per_scenario(store) -> None:
    store.save_scenario(_scenario())
    store.append_trace(
        "SCN-001",
        {
            "event_id": "EVT-1",
            "event_type": "SCENARIO_CREATED",
            "occurred_at": "2026-08-17T00:00:00+00:00",
            "payload": {},
        },
    )

    events = store.list_trace("SCN-001")

    assert [e["event_type"] for e in events] == ["SCENARIO_CREATED"]
    assert store.list_trace("SCN-999") == []


def test_ids_are_unique(store) -> None:
    assert store.next_scenario_id() != store.next_scenario_id()
    assert store.next_run_id() != store.next_run_id()
    assert store.next_alternative_sequence() != store.next_alternative_sequence()


def test_supabase_numbering_resumes_past_existing_rows(supabase_store) -> None:
    """A restart must not hand out an id that is already taken."""
    supabase_store.save_scenario(_scenario("SCN-007"))
    supabase_store.save_run(_run("RUN-004", "SCN-007"))

    assert supabase_store.next_scenario_id() == "SCN-008"
    assert supabase_store.next_run_id() == "RUN-005"


def test_alternative_ids_do_not_steal_the_baseline_counter(supabase_store) -> None:
    """RUN-ALT-009 sorts above RUN-001, so the two counters must not share a scan."""
    supabase_store.save_scenario(_scenario())
    supabase_store.save_run(_run("RUN-001"))
    supabase_store.save_run(_run("RUN-ALT-009"))

    assert supabase_store.next_run_id() == "RUN-002"
    assert supabase_store.next_alternative_sequence() == 10
