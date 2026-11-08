-- Compaction bookkeeping (plan step 7). One row per raw segment stem (independent of .jsonl / .jsonl.gz).
create table compacted_segment (
    stem text primary key,
    dataset_id text not null,
    segment_sha256 text not null,
    records integer not null check (records >= 0),
    rows_written integer not null check (rows_written >= 0),
    skipped_bytes integer not null check (skipped_bytes >= 0),
    lake_path text,                          -- null when the segment produced no rows
    compacted_ns integer not null
);
create trigger compacted_segment_no_update before update on compacted_segment
begin select raise(abort, 'compacted_segment rows are immutable'); end;
