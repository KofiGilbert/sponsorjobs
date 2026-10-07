-- The broker's D1 schema, phase 1.
--
-- The usage + accounts tables mirror backend/store_sqlite.py and backend/accounts.py column for
-- column, so the Render SQLite file can be exported and imported here without reshaping, and the
-- two brokers stay readable side by side. Only `register_limits` is new: the Python broker keeps
-- that counter in process memory, which a Worker cannot do (every request may land on a fresh
-- isolate), so it lives in the database.

-- Per-account plan, pass and purchased credits. Nothing here resets with the month.
CREATE TABLE IF NOT EXISTS users (
    user           TEXT PRIMARY KEY,
    plan           TEXT NOT NULL DEFAULT 'free',
    credit_seconds INTEGER NOT NULL DEFAULT 0,
    pass_until     INTEGER NOT NULL DEFAULT 0,
    pass_seconds   INTEGER NOT NULL DEFAULT 0,
    pass_packages  INTEGER NOT NULL DEFAULT 0
);

-- Per-period counters. `period` is the calendar month ("2026-10") for a person, and the day
-- ("2026-10-07") for the shared free-tier pool, which lives here under user '__free_pool__'.
CREATE TABLE IF NOT EXISTS usage (
    user        TEXT NOT NULL,
    period      TEXT NOT NULL,
    avatar_used INTEGER NOT NULL DEFAULT 0,
    llm_tokens  INTEGER NOT NULL DEFAULT 0,
    packages    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user, period)
);

-- Stripe webhook idempotency (phase 2 uses it; created now so the schema matches the Python one).
CREATE TABLE IF NOT EXISTS processed_events (
    key  TEXT PRIMARY KEY,
    at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Accounts: an anonymous device account has no email; phase 2 adds email/Google sign-in.
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    email         TEXT UNIQUE,
    password_hash TEXT
);

-- Bearer tokens, stored ONLY as their sha256 hex. A leaked DB yields no usable token.
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
);

-- New anonymous accounts per client network per UTC day. The IP is stored hashed: the limit
-- only needs "same network as before", not the address itself.
CREATE TABLE IF NOT EXISTS register_limits (
    ip_hash TEXT NOT NULL,
    day     TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip_hash, day)
);
CREATE INDEX IF NOT EXISTS register_limits_day ON register_limits(day);
