-- Catalog schema v1 (design v2.1 §5.3-5.4, §9-2; plan step 5). Times are UTC nanoseconds.

create table instrument (
    instrument_id integer primary key,
    venue text not null check (venue in ('binance', 'upbit')),
    venue_symbol text not null,
    base text not null,
    quote text not null,
    market_type text not null check (market_type in ('spot', 'usdm_perp', 'usdm_future')),
    contract_multiplier real not null default 1 check (contract_multiplier > 0),
    onboard_ns integer,
    delist_ns integer,
    successor_id integer references instrument(instrument_id),
    unique (venue, venue_symbol, market_type, onboard_ns),
    check (delist_ns is null or onboard_ns is null or delist_ns >= onboard_ns)
);

create table dataset (
    dataset_id text primary key,
    venue text not null check (venue in ('binance', 'upbit', 'collector')),
    -- how the source timestamp relates to the interval (v2.1 §5.4: interval data is normalised to its end)
    time_semantics text not null check (time_semantics in ('event', 'interval_start', 'interval_end', 'snapshot', 'unknown_interval')),
    interval_ms integer check (interval_ms is null or interval_ms > 0),
    description text,
    registered_ns integer not null
);

create table gap (
    gap_id integer primary key,
    source text not null,
    instrument_id integer references instrument(instrument_id),
    start_ns integer,                       -- null: start unknown (no earlier heartbeat)
    end_ns integer not null,
    cause text not null check (cause in (
        'restart', 'reconnect', 'renewal', 'api_window_exceeded', 'no_trade', 'exchange_outage',
        'clock_suspect', 'unknown')),
    detail text,
    recoverable integer not null default 0 check (recoverable in (0, 1)),
    recorded_ns integer not null,
    check (start_ns is null or start_ns <= end_ns)
);
create unique index gap_identity on gap(source, coalesce(start_ns, -1), end_ns, cause, coalesce(detail, ''));

-- Seal boundaries per dataset and epoch (v2.1 §9-2): immutable rows; a later epoch never moves back.
create table seal_policy (
    dataset_id text not null references dataset(dataset_id),
    epoch integer not null check (epoch >= 1),
    research_until_ns integer not null,     -- research may read only t < research_until_ns
    holdout_until_ns integer not null,      -- [research_until, holdout_until) is the sealed holdout
    rationale text not null,
    decided_ns integer not null,
    primary key (dataset_id, epoch),
    check (holdout_until_ns > research_until_ns)
);
create trigger seal_policy_no_update before update on seal_policy
begin select raise(abort, 'seal_policy rows are immutable'); end;
create trigger seal_policy_no_delete before delete on seal_policy
begin select raise(abort, 'seal_policy rows are immutable'); end;

create table universe_membership (
    instrument_id integer not null references instrument(instrument_id),
    rule_version text not null,
    from_ns integer not null,
    to_ns integer,                          -- exclusive; null = still a member
    primary key (instrument_id, rule_version, from_ns),
    check (to_ns is null or to_ns > from_ns)
);

-- Versioned availability lag (v2.1 §5.4): immutable, and a new version may only raise the lag.
create table lag_policy (
    dataset_id text not null references dataset(dataset_id),
    version integer not null check (version >= 1),
    lag_ns integer not null check (lag_ns >= 0),
    rationale text not null,
    decided_ns integer not null,
    primary key (dataset_id, version)
);
create trigger lag_policy_only_grows before insert on lag_policy
when new.lag_ns < coalesce((select max(lag_ns) from lag_policy where dataset_id = new.dataset_id), 0)
begin select raise(abort, 'lag policy may only grow'); end;
create trigger lag_policy_no_update before update on lag_policy
begin select raise(abort, 'lag_policy rows are immutable'); end;
create trigger lag_policy_no_delete before delete on lag_policy
begin select raise(abort, 'lag_policy rows are immutable'); end;
