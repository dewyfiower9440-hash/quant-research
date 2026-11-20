-- migrate: foreign_keys=off
-- v2.2 B4 tier T7: key-less external sources (Alternative.me, Coin Metrics Community, Deribit) need a venue
-- outside the three of 0001; the CHECK lives in the table definition, so the table is rebuilt (SQLite's
-- documented procedure: foreign keys off around the transaction, `foreign_key_check` before commit, done by
-- adapters/sqlite.py `migrate` for a file whose first line is the marker above). Rows are copied unchanged.
create table dataset_new (
    dataset_id text primary key,
    venue text not null check (venue in ('binance', 'upbit', 'collector', 'external')),
    -- how the source timestamp relates to the interval (v2.1 §5.4: interval data is normalised to its end)
    time_semantics text not null check (time_semantics in ('event', 'interval_start', 'interval_end', 'snapshot', 'unknown_interval')),
    interval_ms integer check (interval_ms is null or interval_ms > 0),
    description text,
    registered_ns integer not null
);
insert into dataset_new (dataset_id, venue, time_semantics, interval_ms, description, registered_ns)
    select dataset_id, venue, time_semantics, interval_ms, description, registered_ns from dataset;
drop table dataset;
alter table dataset_new rename to dataset;
