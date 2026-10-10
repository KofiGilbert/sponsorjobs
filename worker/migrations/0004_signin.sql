-- Passwordless sign-in: email one-time codes and Google OAuth state.
--
-- Nothing here is a credential that outlives a few minutes. A sign-in code is stored only as a
-- salted sha256 (the 6 digits themselves are never written down), and every row expires.

-- One live code per email. Asking again replaces it (and resets the wrong-attempt count).
CREATE TABLE IF NOT EXISTS email_codes (
    email     TEXT PRIMARY KEY,          -- trimmed, lowercased
    code_hash TEXT NOT NULL,             -- sha256(salt ":" email ":" code)
    salt      TEXT NOT NULL,
    expires   INTEGER NOT NULL,          -- unix seconds
    attempts  INTEGER NOT NULL DEFAULT 0 -- verify calls against this code; 5 burns it
);

-- Code e-mails sent per email address and per client network, per UTC hour. Keys are hashed
-- ("e:" + sha256(email), "i:" + sha256(ip)): the limit needs "same as before", not the value.
CREATE TABLE IF NOT EXISTS send_limits (
    key  TEXT NOT NULL,
    hour INTEGER NOT NULL,               -- floor(unix seconds / 3600)
    n    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, hour)
);
CREATE INDEX IF NOT EXISTS send_limits_hour ON send_limits(hour);

-- "Continue with Google" in flight: one row per started sign-in, consumed by the callback.
-- `state` is stored hashed; `verifier` is the PKCE code verifier (useless without Google's code);
-- `account_id` is the device account to attach the email to when the start carried a token.
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    redirect   TEXT NOT NULL,            -- the app's loopback callback (allowlisted at start)
    account_id TEXT,
    verifier   TEXT NOT NULL,
    expires    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_states_expires ON oauth_states(expires);
