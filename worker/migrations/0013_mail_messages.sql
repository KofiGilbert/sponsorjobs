-- The in-app mailbox (src/mail.ts, docs/mail-alias.md).
--
-- Every message to a known alias is kept here, body included, ONLY until the person's own app
-- has downloaded it (GET /mail/messages?after=<cursor> acknowledges and deletes), and never
-- longer than 30 days. After the download the mailbox lives on the person's machine. Attachments
-- are never stored, only their file names. The 30-minute code/link rows in mail_events are
-- unchanged.

-- The row id is the cursor the app syncs with. `text` and `html` are each capped at 1 MiB by
-- the Worker, and trimmed further when the two together would not fit one D1 row.
CREATE TABLE IF NOT EXISTS mail_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user             TEXT NOT NULL,               -- the account id
    local            TEXT NOT NULL,               -- the alias local part it was sent to
    sender           TEXT NOT NULL DEFAULT '',    -- the From address
    sender_name      TEXT NOT NULL DEFAULT '',    -- the From display name
    subject          TEXT NOT NULL DEFAULT '',
    text             TEXT NOT NULL DEFAULT '',    -- the plain body (or the HTML read as text)
    html             TEXT,                        -- the HTML body, when there is one
    received_at      INTEGER NOT NULL,            -- unix seconds
    size             INTEGER NOT NULL DEFAULT 0,  -- bytes of text + html as stored
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    attachment_names TEXT NOT NULL DEFAULT '[]'   -- JSON array of file names; no content
);
CREATE INDEX IF NOT EXISTS mail_messages_user_id ON mail_messages(user, id);
CREATE INDEX IF NOT EXISTS mail_messages_received ON mail_messages(received_at);

-- What the person's own e-mail gets for each message: 'forward' (the whole message, as before),
-- 'notice' (a short note that there is mail in the app's Inbox) or 'off'.
ALTER TABLE mail_aliases ADD COLUMN notify_mode TEXT NOT NULL DEFAULT 'forward';
