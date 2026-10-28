-- Mail aliases for the apply agent (src/mail.ts, docs/mail-alias.md).
--
-- A student applying through the agent gets first.last@sponsorjobs.ai. Cloudflare Email Routing
-- hands every message for the domain to the Worker, which keeps ONLY the one-time code or the
-- verification link for 30 minutes (so the app can poll it) and forwards the whole message to the
-- student's real inbox. No body, no header beyond subject and sender, is ever written here.

-- One alias per account. `local` is the part before the @; `forward_to` is the account's verified
-- e-mail at the time the alias was made (refreshed whenever the app asks for the alias again).
CREATE TABLE IF NOT EXISTS mail_aliases (
    local      TEXT PRIMARY KEY,         -- "zoe.muller", "zoe.muller42"
    user       TEXT NOT NULL UNIQUE,     -- the account id
    forward_to TEXT NOT NULL,            -- the verified e-mail the mail is forwarded to
    created_at INTEGER NOT NULL          -- unix seconds
);

-- What the agent is waiting for: a code, a verification link or a password-reset link. The row id
-- is the cursor the app polls with (GET /mail/inbox?after=<cursor>); rows are deleted once
-- acknowledged and after 30 minutes whatever happens.
CREATE TABLE IF NOT EXISTS mail_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user    TEXT NOT NULL,
    kind    TEXT NOT NULL,               -- "code" | "verify" | "reset"
    code    TEXT,                        -- the 4 to 8 digit code, kind "code" only
    link    TEXT,                        -- the link to visit, kinds "verify" and "reset"
    subject TEXT,                        -- at most 200 characters
    sender  TEXT,                        -- the From header, at most 200 characters
    at      INTEGER NOT NULL             -- unix seconds the message arrived
);
CREATE INDEX IF NOT EXISTS mail_events_user_id ON mail_events(user, id);
