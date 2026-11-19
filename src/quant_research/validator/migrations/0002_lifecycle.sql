-- Strategy lifecycle (design v2.1 §9-10; spec in Phase 5 step 11, transitions executed in Phase 6).
-- No separate module: the validator owns the state table and the append-only transition log.
create table strategy_state (
    spec_hash text primary key,
    state text not null check (state in ('Candidate', 'Validated', 'Paper', 'Approved', 'Active', 'Degraded',
                                         'Suspended', 'Retired')),
    updated_ns integer not null
);
create table strategy_transition (
    transition_id integer primary key,
    spec_hash text not null,
    from_state text,
    to_state text not null,
    reason text not null,
    evidence_ref text not null,              -- trial:<id>, verdict:<id>, paper:<run>, incident:<id>
    signer text not null,                    -- 'validator' | 'paper' | 'user:<id>' | 'risk'
    at_ns integer not null
);
create trigger strategy_transition_append_only before update on strategy_transition
begin select raise(abort, 'transitions are append only'); end;
create trigger strategy_transition_no_delete before delete on strategy_transition
begin select raise(abort, 'transitions are never deleted'); end;
