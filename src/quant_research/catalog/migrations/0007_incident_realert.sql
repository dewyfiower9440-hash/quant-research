-- OPS-009 (v2.2 A10): an open critical incident is notified again every few hours; the time of the last
-- notification is kept per incident (null: rows from before this migration -> treated as notified at opening).
alter table incident add column last_notified_ns integer;
