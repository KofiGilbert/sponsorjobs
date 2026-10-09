-- Live interviews minted on the company Tavus key. One row per conversation, so that only the
-- account that started a conversation can end it and read its transcript. No video, audio or
-- transcript is stored here: the person's browser streams to Tavus directly, and the transcript
-- is fetched from Tavus and handed straight back to the app that asked.
CREATE TABLE IF NOT EXISTS avatar_sessions (
    conversation_id TEXT PRIMARY KEY,
    user            TEXT NOT NULL,
    started         INTEGER NOT NULL,
    ended           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS avatar_sessions_user ON avatar_sessions(user);
