-- validator.db (Phase 5 step 7; design v2.1 §9-2, §3.1). Owned and written by the validator process only.
-- research.db is never written from here: research polls the `ack` table read-only (outbox / ack pattern).

-- A pre-registered submission: spec hash + decision-rule hash are fixed BEFORE any holdout query (§9-2).
create table submission (
    submission_id integer primary key,
    request_id integer not null unique,          -- research.db validation_request.request_id (outbox)
    spec_hash text not null,
    rule_hash text not null,                     -- sha256 of the decision rule parameters (alpha, k, c, ...)
    experiment_key text not null,
    in_sample_sr real not null,                  -- period-unit SR on the research splits (the shrinkage base)
    dataset_ids_json text not null check (json_valid(dataset_ids_json)),
    epoch integer not null,
    registered_ns integer not null,
    status text not null default 'registered' check (status in ('registered', 'judged', 'refused')),
    refusal text
);
create index submission_spec on submission(spec_hash);

-- Global query budget per seal epoch (k, ADR-022: 20). Every judgement consumes one query, re-queries included.
create table query_budget (
    epoch integer primary key,
    k integer not null check (k > 0),
    used integer not null default 0 check (used >= 0 and used <= k)
);

-- The one bit returned to research, plus an internal report that never leaves the validator.
create table verdict (
    verdict_id integer primary key,
    submission_id integer not null unique references submission(submission_id),
    epoch integer not null,
    query_index integer not null,                -- 1..k within the epoch
    passed integer not null check (passed in (0, 1)),
    created_ns integer not null
);
create table verdict_detail (
    verdict_id integer primary key references verdict(verdict_id),
    detail_json text not null check (json_valid(detail_json))  -- internal: SR, SE, p, stress results
);
create trigger verdict_immutable before update on verdict
begin select raise(abort, 'verdicts are immutable'); end;
create trigger verdict_no_delete before delete on verdict
begin select raise(abort, 'verdicts are never deleted'); end;

-- Acks read by research (mode=ro): request id -> one bit.
create table ack (
    request_id integer primary key,
    submission_id integer not null references submission(submission_id),
    passed integer not null check (passed in (0, 1)),
    at_ns integer not null
);

-- Hash-chained holdout access log (§9 "질의 로그는 해시 체인"): append only.
create table holdout_access_log (
    seq integer primary key,
    submission_id integer references submission(submission_id),
    dataset_id text not null,
    window_from_ns integer not null,
    window_to_ns integer not null,
    purpose text not null,
    at_ns integer not null,
    prev_hash text not null,
    hash text not null unique
);
create trigger access_log_append_only before update on holdout_access_log
begin select raise(abort, 'the access log is append only'); end;
create trigger access_log_no_delete before delete on holdout_access_log
begin select raise(abort, 'the access log is append only'); end;
