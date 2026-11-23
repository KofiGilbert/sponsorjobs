-- Daily caps for POST /agent/sign (src/botauth.ts, docs/signed-agent.md).
--
-- `sign_daily`: signatures per UTC day, under three kinds of key: "u:<account id>", "i:<sha256 of
-- the client IP>" (the limit needs "same network", not the address) and "all" (the service's own
-- total, for the logs). `sign_sites`: the distinct application sites (registrable domains; hiring
-- systems are not counted) an account was signed for that day. Past days are swept on every call.
-- The site is kept only as a registrable domain and only for the day it was counted.
CREATE TABLE IF NOT EXISTS sign_daily (
    key TEXT NOT NULL,
    day TEXT NOT NULL,                   -- "YYYY-MM-DD", UTC
    n   INTEGER NOT NULL,
    PRIMARY KEY (key, day)
);
CREATE INDEX IF NOT EXISTS sign_daily_day ON sign_daily(day);

CREATE TABLE IF NOT EXISTS sign_sites (
    user TEXT NOT NULL,
    day  TEXT NOT NULL,
    site TEXT NOT NULL,
    PRIMARY KEY (user, day, site)
);
CREATE INDEX IF NOT EXISTS sign_sites_day ON sign_sites(day);
