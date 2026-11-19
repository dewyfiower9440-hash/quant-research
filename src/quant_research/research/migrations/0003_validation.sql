-- research.db outbox for the validator (Phase 5 step 7; design §3.1 outbox / ack). Research writes requests
-- here; the validator reads them (mode=ro) and answers through its own `ack` table in validator.db.
create table validation_request (
    request_id integer primary key,
    trial_id integer not null references trial(trial_id),   -- the S3 trial being submitted
    spec_hash text not null,
    experiment_key text not null,
    in_sample_sr real not null,                             -- period-unit SR on the research splits
    rule_hash text not null,                                -- pre-registered decision rule
    created_ns integer not null
);
create index validation_request_spec on validation_request(spec_hash);
