-- Rate limit for POST /agent/sign (src/botauth.ts, docs/signed-agent.md).
--
-- One row per account per minute: how many request signatures it asked for. At most 120 a minute;
-- rows for past minutes are swept on every call. Nothing about the request itself (the site being
-- applied to) is written here.
CREATE TABLE IF NOT EXISTS sign_limits (
    user   TEXT NOT NULL,                -- the account id
    minute INTEGER NOT NULL,             -- unix seconds / 60
    n      INTEGER NOT NULL,
    PRIMARY KEY (user, minute)
);
