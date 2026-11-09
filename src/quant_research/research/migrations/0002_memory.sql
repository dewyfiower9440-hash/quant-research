-- Phase 3 (docs/plans/2026-11-10-phase3-director-memory.md): research memory, director state machine log,
-- LLM call accounting, data requests, anonymization aliases, and the Phase 4 feature_eval schema (ADR-023).
create table cycle (
    cycle_id integer primary key,
    run_id text not null,                   -- one director run (config + seed); cycles are numbered within it
    seq integer not null check (seq >= 1),
    state text not null,
    status text not null check (status in ('running', 'done', 'paused', 'failed')),
    config_json text not null,
    started_ns integer not null,
    updated_ns integer not null,
    unique (run_id, seq)
);

-- Every state transition with its reason and evidence (design §4 A: "모든 전이에 근거 기록"). Append-only.
create table transition (
    transition_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    from_state text,
    to_state text not null,
    reason text not null check (length(reason) > 0),
    evidence_json text not null check (json_valid(evidence_json)),
    at_ns integer not null
);
create index transition_cycle on transition(cycle_id);
create trigger transition_no_update before update on transition
begin select raise(abort, 'transitions are immutable'); end;
create trigger transition_no_delete before delete on transition
begin select raise(abort, 'transitions are never deleted'); end;

create table llm_call (
    llm_call_id integer primary key,
    cycle_id integer references cycle(cycle_id),
    purpose text not null check (purpose in ('hypothesize', 'plan', 'analyze')),
    attempt integer not null check (attempt >= 1),
    model_id text not null,
    llm_cutoff text,                        -- training cutoff of the model (design §8)
    prompt_sha256 text not null,
    request_json text not null,             -- the anonymized payload actually sent
    response_text text,                     -- raw response (untrusted data)
    status text not null check (status in ('ok', 'schema_invalid', 'refusal', 'error', 'blocked')),
    detail text,
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    cache_read_tokens integer not null default 0,
    cache_write_tokens integer not null default 0,
    estimate_usd real not null default 0,   -- pre-call estimate (budget check)
    usage_cost_usd real not null default 0, -- from the response usage x list price (billable, not an invoice)
    billed integer not null check (billed in (0, 1)),  -- 0: fake / replay LLM, nothing is charged
    latency_ms real not null default 0,
    created_ns integer not null
);
create index llm_call_cycle on llm_call(cycle_id);

create table hypothesis (
    hypothesis_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    family text not null,
    statement text not null,
    falsification_test text not null,
    required_data_json text not null check (json_valid(required_data_json)),
    required_features_json text not null check (json_valid(required_features_json)),  -- Phase 4 link (ADR-023)
    prior real not null check (prior >= 0 and prior <= 1),
    anonymized integer not null default 1 check (anonymized in (0, 1)),
    struct_key text not null,               -- hash of the structured fields: duplicate hypotheses (§10)
    llm_call_id integer references llm_call(llm_call_id),
    created_ns integer not null,
    unique (cycle_id)
);
create index hypothesis_struct on hypothesis(struct_key);

create table experiment_plan (
    plan_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    hypothesis_id integer not null references hypothesis(hypothesis_id),
    template text not null,
    variants_json text not null check (json_valid(variants_json)),
    rationale text not null,
    llm_call_id integer references llm_call(llm_call_id),
    created_ns integer not null,
    unique (cycle_id)
);

-- Findings need evidence (design §4 I: evidence_refs NOT NULL); a newer finding may supersede an old one.
create table finding (
    finding_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    hypothesis_id integer references hypothesis(hypothesis_id),
    family text not null,
    statement text not null check (length(statement) > 0),
    evidence_refs_json text not null check (json_valid(evidence_refs_json) and json_array_length(evidence_refs_json) > 0),
    superseded_by integer references finding(finding_id),
    created_ns integer not null
);
create trigger finding_only_superseded before update on finding
when old.superseded_by is not null or new.statement <> old.statement or new.evidence_refs_json <> old.evidence_refs_json
begin select raise(abort, 'findings are immutable except for a first superseded_by link'); end;

-- Failure analysis (design §6 cause codes).
create table failure (
    failure_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    hypothesis_id integer references hypothesis(hypothesis_id),
    family text not null,
    cause_code text not null check (cause_code in (
        'weak_signal', 'cost_erosion', 'regime_dependent', 'capacity', 'missing_data', 'overfit',
        'infeasible', 'prior_knowledge')),
    detail text not null,
    evidence_json text not null check (json_valid(evidence_json)),
    created_ns integer not null
);
create index failure_family on failure(family, cause_code);

-- ACQUIRE_DATA only records the need (data discovery module B is after Phase 5).
create table data_request (
    request_id integer primary key,
    cycle_id integer not null references cycle(cycle_id),
    need_json text not null check (json_valid(need_json)),
    status text not null default 'recorded' check (status in ('recorded', 'approved', 'rejected', 'ingested')),
    created_ns integer not null
);

-- Anonymization map: real names never leave research.db (prompts carry the alias only).
create table alias (
    kind text not null,
    real text not null,
    alias text not null,
    primary key (kind, real),
    unique (kind, alias)
);

-- Phase 4 S1 feature evaluation table (ADR-023); schema only in Phase 3.
create table feature_eval (
    feature_eval_id integer primary key,
    trial_id integer not null references trial(trial_id),
    feature_json text not null check (json_valid(feature_json)),
    horizon_bars integer not null check (horizon_bars >= 1),
    lag_versions_json text not null,
    ic real,
    ic_hac_t real,
    samples integer not null check (samples >= 0),
    data_version text not null,
    created_ns integer not null
);

-- Design §8 recording fields on every trial.
alter table trial add column model_id text;
alter table trial add column llm_cutoff text;
alter table trial add column hypothesis_id integer;
alter table trial add column cycle_id integer;
