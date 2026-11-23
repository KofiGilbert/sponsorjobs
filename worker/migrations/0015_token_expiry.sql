-- Bearer tokens expire (src/accounts.ts TOKEN_TTL_SECONDS): 180 days from their last use, rolling.
--
-- `last_used_at` is the unix second a token last authenticated a call (written at most once a
-- day). Every token that exists when this runs is treated as used NOW, so nobody is signed out by
-- the migration: an existing token keeps working for another 180 days, and from then on every use
-- extends it. A row written later without the column (an import from the Python broker) gets 0,
-- which the Worker also reads as "used now".
ALTER TABLE tokens ADD COLUMN last_used_at INTEGER NOT NULL DEFAULT 0;
UPDATE tokens SET last_used_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE last_used_at = 0;
