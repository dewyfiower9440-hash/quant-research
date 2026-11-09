-- Receive-gap scan of 1 Hz sources (plan step 10): one row per scanned raw segment, with its first and
-- last receive time so a silence spanning segments (host suspend, network outage) can be measured.
create table gapscan_segment (
    stem text primary key,
    dataset_id text not null,
    first_recv_ns integer,                  -- null: the segment held no records
    last_recv_ns integer,
    records integer not null check (records >= 0),
    scanned_ns integer not null,
    check (first_recv_ns is null or first_recv_ns <= last_recv_ns)
);
create index gapscan_segment_last on gapscan_segment(dataset_id, last_recv_ns);
create trigger gapscan_segment_no_update before update on gapscan_segment
begin select raise(abort, 'gapscan_segment rows are immutable'); end;
