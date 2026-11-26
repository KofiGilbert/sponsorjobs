-- Applications the apply agent finishes, per calendar month (owner decision 2026-11-26).
--
-- The Autofill button fills fast first; when it leaves required fields, more pages or a job page,
-- the apply agent steps in, and that application counts once here: Free 3, Student 40, Standard 60
-- a month (metering.ts Plan.agentFinishes). Counted in the same per-month row as the tailored
-- packages, so it resets with the month. Every plan is counted. Existing rows start at 0.
ALTER TABLE usage ADD COLUMN agent_finishes INTEGER NOT NULL DEFAULT 0;
