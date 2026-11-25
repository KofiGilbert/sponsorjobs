-- Free plan: 1 scored recorded practice interview per calendar month (owner decision 2026-11-25).
--
-- Counted next to the free tailored packages, in the same per-month row, so it resets with the
-- month exactly like they do. Only free accounts are counted: every paid plan is unlimited and
-- never touches this column. Existing rows start at 0, so nobody loses this month's interview.
ALTER TABLE usage ADD COLUMN recorded_interviews INTEGER NOT NULL DEFAULT 0;
