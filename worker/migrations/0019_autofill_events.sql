-- Where Autofill and the apply agent stop (2026-11-26): one row per event the app sends to
-- POST /v1/autofill/events. Where and why, never who: the hiring system and host, the kind of page,
-- the event type, the PUBLIC question and option texts (shortened), the agent's one-line reason,
-- steps, cost so far and the app version. No answers, names, emails, phones or typed values.
-- Kept 90 days (pruned on write); at most 300 rows per account per UTC day.
CREATE TABLE IF NOT EXISTS autofill_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  account   TEXT NOT NULL,
  at        INTEGER NOT NULL,          -- unix seconds, when the broker took it
  day       TEXT NOT NULL,             -- UTC day, for the per-account daily cap
  type      TEXT NOT NULL,
  ats       TEXT NOT NULL DEFAULT '',
  host      TEXT NOT NULL DEFAULT '',
  page_kind TEXT NOT NULL DEFAULT '',
  question  TEXT NOT NULL DEFAULT '',
  qnorm     TEXT NOT NULL DEFAULT '',  -- the question, lowercased, digits and punctuation folded: the admin page groups by it
  options   TEXT NOT NULL DEFAULT '[]',
  reason    TEXT NOT NULL DEFAULT '',
  steps     INTEGER NOT NULL DEFAULT 0,
  cost      REAL NOT NULL DEFAULT 0,
  version   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS autofill_events_account_day ON autofill_events(account, day);
CREATE INDEX IF NOT EXISTS autofill_events_at ON autofill_events(at);
