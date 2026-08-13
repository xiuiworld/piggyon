-- Schema for the whole pipeline: scenarios, runs, decisions, trace events.
--
-- Each table keeps its full record in `document` (JSONB) and promotes only the
-- fields worth querying. Records gain fields every phase, so a column per field
-- would need a migration per phase and would break the moment the two drifted.
--
-- The API holds the service key and is the only writer. RLS is enabled with no
-- policy: that denies every anon/authenticated request while the service role
-- bypasses it.

create table if not exists scenarios (
    scenario_id       text primary key,
    state             text        not null
        check (state in ('VALIDATION_REQUIRED', 'READY_TO_SOLVE', 'SOLVED')),
    created_at        timestamptz not null,
    validation_result jsonb,
    document          jsonb       not null
);

create index if not exists scenarios_created_at_idx on scenarios (created_at desc);

create table if not exists runs (
    run_id           text primary key,
    scenario_id      text        not null references scenarios (scenario_id),
    solver_status    text        not null,
    validator_status text        not null,
    created_at       timestamptz,
    document         jsonb       not null
);

create index if not exists runs_scenario_idx on runs (scenario_id);

create table if not exists decisions (
    decision_id    text primary key,
    run_id         text        not null references runs (run_id),
    decision_state text        not null
        check (decision_state in ('ACCEPTED', 'HELD', 'REJECTED')),
    created_at     timestamptz not null,
    document       jsonb       not null
);

create index if not exists decisions_run_idx on decisions (run_id, created_at);

create table if not exists trace_events (
    event_id    text primary key,
    scenario_id text        not null,
    event_type  text        not null,
    occurred_at timestamptz not null,
    document    jsonb       not null
);

create index if not exists trace_events_scenario_idx on trace_events (scenario_id, occurred_at);

alter table scenarios    enable row level security;
alter table runs         enable row level security;
alter table decisions    enable row level security;
alter table trace_events enable row level security;
