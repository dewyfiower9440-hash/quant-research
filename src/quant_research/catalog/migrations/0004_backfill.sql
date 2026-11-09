-- REST backfills (plan step 9, Upbit 1m candles): one row per written chunk of pages. Chunks of one
-- (dataset, market, run) are contiguous going backwards from the run's fixed end; the oldest chunk's
-- start is the resume cursor. A market whose history is exhausted gets a backfill_done row.
create table backfill_chunk (
    dataset_id text not null,
    market text not null,
    run_end_ns integer not null,          -- fixed upper bound of the run (exclusive)
    start_ns integer not null,            -- oldest candle open in the chunk
    end_ns integer not null,              -- exclusive upper bound the chunk covers
    rows_written integer not null check (rows_written > 0),
    lake_path text not null,
    fetched_ns integer not null,
    primary key (dataset_id, market, run_end_ns, start_ns),
    check (end_ns > start_ns)
);
create trigger backfill_chunk_no_update before update on backfill_chunk
begin select raise(abort, 'backfill_chunk rows are immutable'); end;

create table backfill_done (
    dataset_id text not null,
    market text not null,
    run_end_ns integer not null,
    first_ns integer,                     -- first candle ever returned (null: market had no candles)
    finished_ns integer not null,
    primary key (dataset_id, market, run_end_ns)
);
