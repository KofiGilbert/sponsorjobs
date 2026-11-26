-- How close the Cloudflare account is to the FREE plan's limits, for the admin dashboard's
-- "Cloudflare free plan" card and its "Time to switch to Workers Paid" banner (src/admin_cf.ts).
-- Counters only: nothing about who searched, signed in or for what.
--
-- One row a UTC day, written by the feed workflow's search loader (scripts/cf_usage_snapshot.py)
-- from Cloudflare's GraphQL Analytics API: the whole account's totals for that (finished) day, so
-- serving a request never writes here. The loader runs every few hours and upserts the same row,
-- so a later run fills in what an earlier one could not read (a NULL never replaces a number).
CREATE TABLE IF NOT EXISTS cf_usage (
    day              TEXT PRIMARY KEY,   -- "2026-11-25" (UTC): the day the numbers are for
    taken_at         TEXT NOT NULL,      -- when the loader last wrote this row (ISO, UTC)
    worker_requests  INTEGER,            -- Workers requests, every script on the account
    d1_rows_read     INTEGER,            -- D1 rows read, every database on the account
    d1_rows_written  INTEGER,            -- D1 rows written, every database on the account
    d1_storage_bytes INTEGER,            -- D1 storage, every database on the account
    d1_largest_bytes INTEGER,            -- the largest single database (the per-database cap)
    d1_largest_name  TEXT,               -- its name (or id when the name could not be read)
    d1_databases     TEXT,               -- JSON [{id, name, rows_read, rows_written, bytes}]
    load_rows        INTEGER,            -- jobs in the search index when the row was written
    load_total       INTEGER,            -- jobs in the feed the loader was loading
    load_per_day     INTEGER,            -- about how many jobs the daily write budget loads
    load_ready       INTEGER,            -- 1 once the first full load has finished
    load_written     INTEGER,            -- D1 rows the search loader itself wrote that day
                                         -- (search_usage.rows_written): planned, inside its budget
    load_budget      INTEGER,            -- the loader's daily write budget (SEARCH_D1_DAILY_WRITES;
                                         -- 0 = no limit). While the first load runs, the dashboard
                                         -- judges rows written by everyone else against what the
                                         -- budget leaves of the 100,000 (20,000 by default)
    warning          TEXT                -- what could not be read, in plain words (NULL = none)
);

-- The days search answered 503 daily_budget (src/search.ts): an isolate adds one the first time
-- it says no on a day, so this costs a write per isolate per such day, never one per request.
CREATE TABLE IF NOT EXISTS search_budget_days (
    day  TEXT PRIMARY KEY,               -- "2026-11-25" (UTC)
    hits INTEGER NOT NULL DEFAULT 0      -- isolates that turned searches away that day
);
