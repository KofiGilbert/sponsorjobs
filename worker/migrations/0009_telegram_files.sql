-- Files the person sent the official bot (src/telegram.ts, docs/notify.md): ONLY Telegram's
-- file_id, so /notify/telegram/file can check that a file_id came from THIS account's own chat
-- before passing the bytes through to the app. No file, name or content is ever stored here; the
-- bytes are fetched from Telegram and streamed to the app inside one request. Deleted on unlink,
-- and after 7 days whatever happens (the same lifetime as an inbox event).
CREATE TABLE IF NOT EXISTS telegram_files (
    account TEXT NOT NULL,
    file_id TEXT NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,    -- bytes, as Telegram reported it (0 when unknown)
    at      INTEGER NOT NULL,              -- unix seconds
    PRIMARY KEY (account, file_id)
);
CREATE INDEX IF NOT EXISTS telegram_files_at ON telegram_files(at);
