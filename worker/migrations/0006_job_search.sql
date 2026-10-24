-- Server-side job search (docs/server-search-plan.md, phase 2): the slim feed rows, an FTS5 index
-- for the keyword and location facets, and the index version. Written ONLY by the diff loader
-- (scripts/load_search_index.py, run by the feed workflow); the Worker only reads these tables
-- (src/search.ts). Public job postings only: no account, install or query data lives here.
--
-- Every derived column is computed by the loader in Python with the same functions the app filters
-- with (sourcing/filters.py, sourcing/feedfile.recency_key), so the Worker's SQL compares
-- precomputed values instead of re-implementing salary parsing, date parsing or Python's lower().
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,       -- rowid, also the FTS rowid
    source_id   TEXT NOT NULL UNIQUE,
    h           TEXT NOT NULL,             -- hash of the row JSON: what the diff loader compares
    rkey        TEXT NOT NULL,             -- the feed's recency order as one string (see below)
    pay         INTEGER NOT NULL,          -- filters.salary_value(salary); 0 = no readable pay
    posted_ts   INTEGER,                   -- unix seconds of filters._parse_posted; NULL = undated
    remote_l    TEXT NOT NULL,             -- (remote or "").lower()
    entry       INTEGER NOT NULL,          -- 1 when entry_level is truthy
    sponsored   INTEGER NOT NULL,          -- 1 when the "ad offers sponsorship" facet keeps the row
    visa_any    INTEGER NOT NULL,          -- 1 when the row's visa list is non-empty ("SPONSORED")
    visa_codes  TEXT NOT NULL,             -- ",H-1B,E-3," from visa + nationality_visas codes
    title_l     TEXT NOT NULL,             -- Python-lowered title, company, location: what q, loc
    company_l   TEXT NOT NULL,             -- and titles match against, exactly as apply_facets
    location_l  TEXT NOT NULL,             -- and picker._matches_focus lower them
    xh          TEXT NOT NULL,             -- job_extras.h of the row's employer fields ("" = none)
    row         TEXT NOT NULL              -- the slim row as canonical JSON, minus those fields
);

-- The employer fields (sponsor, visa, nationality_visas) are ~1.3 KB of a ~2 KB row and the same
-- for every role at a company, so they are stored once per distinct value and joined back on read.
-- This keeps the index (and the first load's request count) about a third of the size.
CREATE TABLE IF NOT EXISTS job_extras (
    h TEXT PRIMARY KEY,                    -- hash of v
    v TEXT NOT NULL                        -- canonical JSON of the fields the row carries
) WITHOUT ROWID;

-- rkey is feedfile.recency_key folded into one string: "1" or "0" (dated or not), posting date,
-- first seen and source_id joined by \x01, which sorts below every character the fields hold. So
-- ORDER BY rkey DESC is the board's newest-first order, ties included, and rkey is unique.
CREATE INDEX IF NOT EXISTS jobs_recent ON jobs(rkey);
CREATE INDEX IF NOT EXISTS jobs_pay ON jobs(pay, rkey);
CREATE INDEX IF NOT EXISTS jobs_remote ON jobs(remote_l, rkey);
CREATE INDEX IF NOT EXISTS jobs_entry ON jobs(entry, rkey);
-- No index on posted_ts: the date facet keeps undated rows ("IS NULL OR >="), and an index made
-- the planner gather every match before the LIMIT (152k rows read at 184k); the ordered walk
-- stops at the page instead.
-- Under 1% of rows state sponsorship, so a partial index keeps that facet a short walk.
CREATE INDEX IF NOT EXISTS jobs_sponsored ON jobs(rkey) WHERE sponsored = 1;

-- The keyword (q: title or company) and location facets are SUBSTRING matches in apply_facets
-- ("eng" finds "Engineer", "york" finds "New York"), so the index uses the trigram tokenizer: a
-- quoted phrase of 3+ characters matches exactly the rows whose column contains it. A word
-- tokenizer (unicode61) would answer different rows than the app does today. case_sensitive 1
-- because the columns are already lowered the Python way. External content (content='jobs')
-- rather than contentless: the text already lives in jobs, so the index stores no second copy,
-- and the triggers below can delete a row's old terms (a contentless table needs the old values
-- passed in by the writer). columnsize=0 drops the per-row size table, which only ranking reads.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title_l, company_l, location_l,
    content='jobs', content_rowid='id',
    tokenize='trigram case_sensitive 1', columnsize=0
);

CREATE TRIGGER IF NOT EXISTS jobs_fts_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title_l, company_l, location_l)
    VALUES (new.id, new.title_l, new.company_l, new.location_l);
END;

CREATE TRIGGER IF NOT EXISTS jobs_fts_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title_l, company_l, location_l)
    VALUES ('delete', old.id, old.title_l, old.company_l, old.location_l);
END;

-- Most changed rows change their pay or flags, not their text: those skip the index rewrite.
CREATE TRIGGER IF NOT EXISTS jobs_fts_au AFTER UPDATE ON jobs
WHEN old.title_l IS NOT new.title_l OR old.company_l IS NOT new.company_l
     OR old.location_l IS NOT new.location_l BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title_l, company_l, location_l)
    VALUES ('delete', old.id, old.title_l, old.company_l, old.location_l);
    INSERT INTO jobs_fts(rowid, title_l, company_l, location_l)
    VALUES (new.id, new.title_l, new.company_l, new.location_l);
END;

-- index_version changes whenever the loader applies a diff; it keys the edge cache and makes a
-- cursor from an older index answer 409. generated_at is the feed build the index holds, and rows
-- its row count (src/search.ts sizes its plan choice by it).
CREATE TABLE IF NOT EXISTS search_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
