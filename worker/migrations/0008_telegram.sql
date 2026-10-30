-- The official SponsorJobs Telegram bot relay (src/telegram.ts, docs/notify.md).
--
-- The broker is a relay, not a store of personal data: no resume, no message text it sends, no
-- picture or PDF is ever written here. It keeps only what it needs to route button taps and
-- replies back to the right app, and send counters for the rate limit.

-- A one-time "connect" code, stored only as its sha256 hex. Valid 15 minutes, deleted on use.
CREATE TABLE IF NOT EXISTS telegram_links (
    code_hash  TEXT PRIMARY KEY,
    account    TEXT NOT NULL,
    expires_at INTEGER NOT NULL          -- unix seconds
);

-- The account to chat binding: until unlink, or until Telegram says the person blocked the bot.
-- One chat belongs to at most one account.
CREATE TABLE IF NOT EXISTS telegram_chats (
    account   TEXT PRIMARY KEY,
    chat_id   INTEGER NOT NULL UNIQUE,
    username  TEXT,
    paused    INTEGER NOT NULL DEFAULT 0,
    linked_at TEXT NOT NULL              -- ISO 8601 UTC, as the status route returns it
);

-- Button taps, typed replies and commands waiting for the app to poll. The id is the inbox
-- cursor. Deleted once acknowledged (a later `after`), or after 7 days whatever happens.
CREATE TABLE IF NOT EXISTS telegram_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    type    TEXT NOT NULL,               -- "button" | "text" | "command"
    data    TEXT NOT NULL DEFAULT '',
    text    TEXT NOT NULL DEFAULT '',
    at      INTEGER NOT NULL             -- unix seconds
);
CREATE INDEX IF NOT EXISTS telegram_events_account_id ON telegram_events(account, id);
CREATE INDEX IF NOT EXISTS telegram_events_at ON telegram_events(at);

-- Rate limit counters: one row per message sent ("send", which includes send_media) or edited
-- ("edit") through the bot, with the time only. A Worker is not one process, so the old broker's
-- in-memory counters live here instead. Rows older than 24 hours are swept on every check.
CREATE TABLE IF NOT EXISTS telegram_sends (
    account TEXT NOT NULL,
    kind    TEXT NOT NULL,               -- "send" | "edit"
    at      INTEGER NOT NULL             -- unix milliseconds
);
CREATE INDEX IF NOT EXISTS telegram_sends_account ON telegram_sends(account, kind, at);
