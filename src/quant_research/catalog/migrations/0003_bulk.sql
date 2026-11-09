-- Bulk downloads (plan step 8): one row per (url, last_modified); a regenerated upstream file is a new
-- version, the previous one is kept (bitemporal, design v2.1 §5.4).
create table bulk_file (
    url text not null,
    last_modified text not null,
    dataset_id text not null,
    symbol text not null,
    month text not null,
    sha256 text not null,
    zip_bytes integer not null check (zip_bytes >= 0),
    rows_written integer not null check (rows_written >= 0),
    lake_path text not null,
    fetched_ns integer not null,
    primary key (url, last_modified)
);
create trigger bulk_file_no_update before update on bulk_file
begin select raise(abort, 'bulk_file rows are immutable'); end;
