-- The admin dashboard's actions (src/admin_actions.ts) and their audit log.
--
-- Every change made from admin.sponsorjobs.ai (a pass given, interview credits added, an account
-- suspended or restored, a test alert sent) is one row here: who did it (the Cloudflare Access
-- e-mail), what, to which account, and the reason typed in. Rows are never updated or deleted by
-- the app. Nothing a person wrote or uploaded is stored.
CREATE TABLE IF NOT EXISTS admin_actions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,              -- unix seconds
    admin   TEXT NOT NULL,                 -- the Access e-mail that pressed the button
    action  TEXT NOT NULL,                 -- "give_pass" | "add_interviews" | "suspend" | "unsuspend" | "test_alert"
    target  TEXT NOT NULL DEFAULT '',      -- the account id ('' for test_alert)
    detail  TEXT NOT NULL DEFAULT '',      -- e.g. "pass30", "2 interviews", the result of a test alert
    reason  TEXT NOT NULL DEFAULT ''       -- what the admin typed, at most 300 characters
);
CREATE INDEX IF NOT EXISTS admin_actions_target ON admin_actions(target, at);
CREATE INDEX IF NOT EXISTS admin_actions_at ON admin_actions(at);

-- A suspended account's tokens stop working (src/accounts.ts resolve) until it is restored; the
-- tokens are kept, so restoring it signs the person straight back in.
ALTER TABLE accounts ADD COLUMN suspended_at INTEGER;   -- unix seconds; NULL = not suspended

-- Runtime switches the dashboard can flip without a deploy. One today:
--   free_pool_cap: the free tier's shared daily token budget, overriding FREE_POOL_DAILY_TOKENS
--                  ("0" pauses the free AI for everyone; no row = the deploy's value).
CREATE TABLE IF NOT EXISTS admin_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,          -- unix seconds
    updated_by TEXT NOT NULL              -- the Access e-mail
);
