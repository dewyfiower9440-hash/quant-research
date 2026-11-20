-- v2.2 B2 (ADR-040): a seal epoch may consist of several windows per role. Epoch 2 of the bulk datasets:
-- research [2020-01-01, 2023-01-01) + [2024-12-01, 2026-07-01), historical holdout [2023-01-01, 2024-12-01),
-- forward holdout [2026-07-01, ...). A dataset whose latest epoch has no windows keeps the epoch-1 meaning
-- (research = everything before research_until_ns). Windows are pre-registered and never change.
create table seal_window (
    dataset_id text not null,
    epoch integer not null,
    role text not null check (role in ('research', 'holdout_hist', 'holdout_fwd')),
    from_ns integer not null,
    to_ns integer not null,
    primary key (dataset_id, epoch, role, from_ns),
    foreign key (dataset_id, epoch) references seal_policy(dataset_id, epoch),
    check (to_ns > from_ns)
);
create trigger seal_window_no_update before update on seal_window
begin select raise(abort, 'seal windows are pre-registered and immutable'); end;
create trigger seal_window_no_delete before delete on seal_window
begin select raise(abort, 'seal windows are pre-registered and never deleted'); end;
