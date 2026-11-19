-- ledger.db (Phase 6 step 13; design v2.1 §4 K, §11). Single writer: the paper process.
-- `journal` is the hash-chained source of truth; order_ / fill / position are projections kept in step.
create table journal (
    seq integer primary key,
    kind text not null check (kind in ('order', 'ack', 'fill', 'cancel', 'reject', 'uncertain', 'position', 'note')),
    ref text not null,                           -- client_id or symbol
    payload_json text not null check (json_valid(payload_json)),
    at_ns integer not null,
    prev_hash text not null,
    hash text not null unique
);
create trigger journal_append_only before update on journal
begin select raise(abort, 'the journal is append only'); end;
create trigger journal_no_delete before delete on journal
begin select raise(abort, 'the journal is append only'); end;

-- client_id is deterministic and unique (idempotent submission, §11): a retry can never create a second order
create table order_ (
    client_id text primary key,
    run_id text not null,
    spec_hash text not null,
    venue text not null,
    symbol text not null,
    side text not null check (side in ('buy', 'sell')),
    qty real not null check (qty > 0),
    limit_price real,                             -- null: taker (market) order
    reduce_only integer not null default 0 check (reduce_only in (0, 1)),
    state text not null check (state in ('PENDING_SUBMIT', 'ACKED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED',
                                         'REJECTED', 'UNCERTAIN')),
    filled_qty real not null default 0 check (filled_qty >= 0),
    venue_order_id text,
    decision_ns integer not null,
    created_ns integer not null,
    updated_ns integer not null
);
create index order_symbol_state on order_(symbol, state);

create table fill (
    fill_id integer primary key,
    client_id text not null references order_(client_id),
    qty real not null check (qty > 0),
    price real not null check (price > 0),
    fee real not null default 0,
    at_ns integer not null,
    liquidity text not null check (liquidity in ('taker', 'maker'))
);
create trigger fill_immutable before update on fill
begin select raise(abort, 'fills are immutable'); end;
create trigger fill_no_delete before delete on fill
begin select raise(abort, 'fills are never deleted'); end;

create table position (
    symbol text primary key,
    qty real not null,                            -- signed
    avg_price real not null,
    realized_pnl real not null default 0,
    fees real not null default 0,
    updated_ns integer not null
);
