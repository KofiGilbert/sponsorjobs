-- "Connect GitHub" through the SponsorJobs GitHub App (src/github.ts, docs/github-app.md).
--
-- One row per connect in flight. The desktop app made a random verifier and kept it; `state` is
-- base64url(sha256(verifier)), so the row can only be claimed by the app that started it. After
-- GitHub hands back a code, the Worker exchanges it (with the client secret, which only this
-- Worker has) and keeps the tokens here for at most 5 minutes, ENCRYPTED with a one-time key
-- that is sent only to the person's own machine (the browser lands on the app's loopback page
-- with it in the URL fragment). D1 never holds a usable token, only `nonce_hash` to check the
-- claim. The claim deletes the row, so a token is handed out once.
CREATE TABLE IF NOT EXISTS github_connects (
    state      TEXT PRIMARY KEY,             -- base64url(sha256(app verifier)), 43 chars
    redirect   TEXT NOT NULL,                -- the app's loopback page (allowlisted at connect)
    pkce       TEXT NOT NULL,                -- this Worker's PKCE code verifier for GitHub
    status     TEXT NOT NULL,                -- pending | installing | ready | denied
    nonce_hash TEXT,                         -- sha256 hex of the one-time key, once ready
    sealed     TEXT,                         -- AES-GCM(iv || ciphertext) of the tokens, base64url
    expires    INTEGER NOT NULL              -- unix seconds; ready rows live 5 minutes
);
CREATE INDEX IF NOT EXISTS github_connects_expires ON github_connects(expires);

-- Per client network, per route, per UTC hour: connect, claim and refresh calls. Keys are hashed
-- ("connect:" + sha256(ip)); the limit only needs "same network as before".
CREATE TABLE IF NOT EXISTS github_limits (
    key  TEXT NOT NULL,
    hour INTEGER NOT NULL,                   -- floor(unix seconds / 3600)
    n    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, hour)
);
CREATE INDEX IF NOT EXISTS github_limits_hour ON github_limits(hour);
