-- P0: the scenario input snapshot store.
-- Later phases add runs, decisions and trace events in their own migrations.

create table if not exists scenarios (
    scenario_id          text primary key,
    scenario_name        text        not null,
    state                text        not null
        check (state in ('VALIDATION_REQUIRED', 'READY_TO_SOLVE', 'SOLVED')),
    created_at           timestamptz not null,
    as_of                timestamptz not null,
    baseline_service_ids jsonb       not null,
    policy_version       text        not null,
    assumption_ids       jsonb       not null,
    -- The snapshot is immutable once written; later phases hash exactly these
    -- bytes for the reproducibility contract.
    input_snapshot       jsonb       not null
);

create index if not exists scenarios_created_at_idx on scenarios (created_at desc);

-- The API holds the service key and is the only writer, so no anon policy is
-- granted. RLS on with zero policies denies every anon/authenticated request
-- while the service role bypasses it.
alter table scenarios enable row level security;
