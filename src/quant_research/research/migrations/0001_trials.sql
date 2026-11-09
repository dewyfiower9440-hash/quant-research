-- research.db (Phase 2 step 1; design v2.1 §8 trial accounting, §10 experiment key). Owned and written by
-- the research process only (single writer, design §3.1); catalog.db is never written from research.
create table experiment (
    experiment_key text primary key,        -- sha256(spec hash + data/lag/seal/engine/cost/seed config)
    spec_hash text not null,
    spec_json text not null,
    config_json text not null,
    created_ns integer not null
);

-- One row per fitness evaluation, written BEFORE the evaluation starts (status running) and closed once.
create table trial (
    trial_id integer primary key,
    experiment_key text not null references experiment(experiment_key),
    spec_hash text not null,
    stage text not null check (stage in ('S0', 'S1', 'S2', 'S3', 'reference', 'smoke')),
    fold text,
    status text not null check (status in ('running', 'done', 'failed', 'rejected', 'aborted')),
    reason text,
    metrics_json text,
    cost_s real check (cost_s is null or cost_s >= 0),
    env_id text not null,
    code_sha text not null,
    started_ns integer not null,
    finished_ns integer,
    check ((status = 'running') = (finished_ns is null))
);
create index trial_experiment on trial(experiment_key);
create trigger trial_finished_is_final before update on trial when old.status <> 'running'
begin select raise(abort, 'finished trials are immutable'); end;
create trigger trial_no_delete before delete on trial
begin select raise(abort, 'trials are never deleted'); end;

-- Backtest details of a trial (§9 fidelity; daily pnl vector kept for the Phase 4-5 multiple-testing step).
create table backtest_run (
    trial_id integer primary key references trial(trial_id),
    engine text not null check (engine in ('vector', 'event')),
    fidelity text not null check (fidelity in ('L0', 'L1', 'L2')),
    data_version text not null,             -- sha256 over the lake files read (path + size)
    lag_versions_json text not null,
    seal_epochs_json text not null,
    cost_json text not null,
    window_from_ns integer not null,
    window_to_ns integer not null,
    pnl_daily blob not null,                -- zlib(float64 little-endian), one value per UTC day
    worst_case_pnl_daily blob,              -- same, missing-data worst-case variant (§9)
    created_ns integer not null
);
create trigger backtest_run_no_update before update on backtest_run
begin select raise(abort, 'backtest_run rows are immutable'); end;
create trigger backtest_run_no_delete before delete on backtest_run
begin select raise(abort, 'backtest_run rows are never deleted'); end;
