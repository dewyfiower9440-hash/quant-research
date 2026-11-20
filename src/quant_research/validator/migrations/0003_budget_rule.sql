-- The epoch's query budget is bound to ONE decision rule (review finding 8, 2026-11-20): a submission made under
-- another rule (e.g. k = 1, i.e. alpha 0.05 per query) cannot spend this epoch's k = 20 queries, and a verdict whose
-- detail was computed under another rule is refused. Rows created before this migration have no rule and are
-- unusable until re-registered (there were none in the operational validator.db: it did not exist).
alter table query_budget add column rule_hash text;
