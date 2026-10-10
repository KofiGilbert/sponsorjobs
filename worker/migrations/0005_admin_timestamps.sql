-- Timestamps for the founder's admin dashboard (src/admin.ts), counted from this migration on.
--
-- SQLite cannot add a column whose default is "now", so these start NULL and the code writes them:
-- every account created after this migration gets created_at, and email_at is set the moment an
-- e-mail is attached (sign-in). Rows from before stay NULL and the dashboard shows them as
-- "unknown"; no date is ever made up for them.
ALTER TABLE accounts ADD COLUMN created_at INTEGER;   -- unix seconds the account was created
ALTER TABLE accounts ADD COLUMN email_at   INTEGER;   -- unix seconds an e-mail was first attached
CREATE INDEX IF NOT EXISTS accounts_created_at ON accounts(created_at);

-- Tailored packages taken from a PASS this month. Free packages were already counted in
-- usage.packages; pass packages only decremented users.pass_packages, so there was no monthly
-- figure. This counter is for reporting only and never limits anything.
ALTER TABLE usage ADD COLUMN pass_packages INTEGER NOT NULL DEFAULT 0;

-- Sign-in code e-mails actually handed to the mailer, per UTC day, so the dashboard can show how
-- close we are to Resend's free plan (100 a day, 3,000 a month). send_limits cannot answer this: it
-- keeps only the current hour, per address and network. No address is stored here, only a count.
CREATE TABLE IF NOT EXISTS email_sends (
    day TEXT PRIMARY KEY,                -- "2026-10-10" (UTC)
    n   INTEGER NOT NULL DEFAULT 0
);
