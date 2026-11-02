-- The health of the company Anthropic key (src/outage.ts).
--
-- One row, key "ai": whether model calls are going through, since when, and when the owner was
-- last alerted, so an outage is announced ONCE an hour across every isolate, not once per
-- request. Nothing about a person or a prompt is written here.
CREATE TABLE IF NOT EXISTS ai_status (
    key           TEXT PRIMARY KEY,
    state         TEXT NOT NULL,         -- "ok" | "company_outage" | "transient"
    since         INTEGER NOT NULL,      -- unix seconds the current state began
    last_seen     INTEGER NOT NULL,      -- unix seconds of the last failure (or recovery)
    last_alert_at INTEGER,               -- unix seconds the owner was last messaged, NULL = never
    note          TEXT NOT NULL DEFAULT ''  -- the short cause shown on /health and to the owner
);
