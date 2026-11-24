-- v2.5 step 2 (N02): the bulk downloads ask "which files of this (dataset, symbol, month) do I hold" once per
-- month group on every resume. With only the primary key (url, last_modified) that was a full scan of bulk_file
-- (0.26 s at 220,000 rows): a resume spent its whole tick re-checking months it already had.
-- `if not exists`: two processes that open a version-9 catalog at the same moment both try (the runner reads the
-- version before it takes the write lock); the second must not die on "already exists".
create index if not exists bulk_file_symbol_month on bulk_file(dataset_id, symbol, month);
