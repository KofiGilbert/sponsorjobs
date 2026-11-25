-- Server-side job search (docs/server-search-plan.md): the slim feed rows, a keyword index and the
-- index's own bookkeeping. Written ONLY by the diff loader (scripts/load_search_index.py, run by
-- the feed workflow); the Worker reads these tables (src/search.ts) and adds to search_usage.
-- Public job postings only: no account, install or query data lives here.
--
-- Built to stay inside Cloudflare's FREE plan for as long as it can: 100,000 D1 rows written a
-- day, 5 million rows read a day, 500 MB a database. So a job is ONE table row with as few index
-- rows beside it as the product allows, and every derived value is computed by the loader in
-- Python with the app's own functions, so the Worker's SQL only compares numbers.
CREATE TABLE IF NOT EXISTS jobs (
    -- The board's order as one number: newest first is ORDER BY id DESC, so the table is its own
    -- recency index and no second index is written per job. id = dated bit (2^40) | posting day
    -- (16 bits) | a label that keeps feedfile.recency_key's order inside one day
    -- (load_search_index.assign_ids). 41 bits: an exact number in JavaScript.
    id        INTEGER PRIMARY KEY,
    title     TEXT NOT NULL,             -- as the feed has them: searched, and returned
    company   TEXT NOT NULL,
    location  TEXT NOT NULL,
    tl        TEXT,                      -- Python's lower() of each, ONLY when SQLite's ASCII
    cl        TEXT,                      -- lower() would differ ("Ångström"); else NULL and the
    ll        TEXT,                      -- Worker matches coalesce(tl, lower(title))
    f         INTEGER NOT NULL,          -- flags: 1 sponsor board, 2 entry level, 4 ad offers
                                         -- sponsorship, 8 has a visa record, 16/32 work type
                                         -- (1 remote, 2 hybrid, 3 onsite)
    vm        INTEGER NOT NULL,          -- visa codes as bits (search_meta 'visa' lists the codes)
    pay       INTEGER NOT NULL,          -- filters.salary_value(salary); 0 = no readable pay
    ts        INTEGER,                   -- unix seconds of the posting date; NULL = none readable
    x         INTEGER NOT NULL,          -- extras.x of the employer fields (0 = none)
    h         TEXT NOT NULL,             -- hash of the feed row: what the diff loader compares
    r         TEXT NOT NULL              -- the row's other fields as a JSON array, in the order
                                         -- search_meta 'fields' names them ($[0] is source_id)
);

-- The employer fields (sponsor, visa, nationality_visas) are about 1.3 KB of a 2 KB row and the
-- same for every role at a company, so they are stored once and joined back on read.
CREATE TABLE IF NOT EXISTS extras (
    x INTEGER PRIMARY KEY,
    v TEXT NOT NULL                      -- canonical JSON object of the fields the row carries
);

-- Highest pay first (sort=pay). Only rows that state pay (about half), so it costs half an index
-- row per job; rows with no pay come after them in id order, straight from the table.
CREATE INDEX IF NOT EXISTS jobs_pay ON jobs(pay) WHERE pay > 0;
-- "Ad offers sponsorship": under 1% of rows, so the facet walks this instead of the whole table.
CREATE INDEX IF NOT EXISTS jobs_offered ON jobs(id) WHERE (f & 4) != 0;

-- Keywords and places are SUBSTRING matches in apply_facets ("eng" finds "Engineer", "york" finds
-- "New York"), so the index is trigrams. detail=none keeps it about a quarter of the size (88
-- bytes a job against 343, measured): it then only says WHICH rows hold every trigram of the
-- text, and the Worker checks the real substring on those rows. External content: the text
-- already lives in jobs. Its rowid is jobs.id, so matches come out newest first with no sort.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, location,
    content='jobs', content_rowid='id',
    tokenize='trigram', detail=none, columnsize=0
);

CREATE TRIGGER IF NOT EXISTS jobs_fts_ai AFTER INSERT ON jobs BEGIN
    INSERT INTO jobs_fts(rowid, title, company, location)
    VALUES (new.id, new.title, new.company, new.location);
END;

CREATE TRIGGER IF NOT EXISTS jobs_fts_ad AFTER DELETE ON jobs BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, location)
    VALUES ('delete', old.id, old.title, old.company, old.location);
END;

-- Most changed rows change their pay or flags, not their text: those skip the index rewrite.
CREATE TRIGGER IF NOT EXISTS jobs_fts_au AFTER UPDATE ON jobs
WHEN old.title IS NOT new.title OR old.company IS NOT new.company
     OR old.location IS NOT new.location BEGIN
    INSERT INTO jobs_fts(jobs_fts, rowid, title, company, location)
    VALUES ('delete', old.id, old.title, old.company, old.location);
    INSERT INTO jobs_fts(rowid, title, company, location)
    VALUES (new.id, new.title, new.company, new.location);
END;

-- index_version changes whenever a load wrote rows: it keys the edge cache and makes a cursor
-- from an older index answer 409. ready = '1' once the first full load has finished (503 until
-- then). generated_at is the feed build the index holds; rows / sponsor_rows its sizes; pending
-- what the last load left for the next one; fields / tags / visa the lists the rows are encoded
-- with; load_token what the loader's saved state must match.
CREATE TABLE IF NOT EXISTS search_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- One row a UTC day: the D1 rows search has read and the loader has written, and the requests
-- served. The Worker stops answering (503 daily_budget) before search can use up the account's
-- free daily allowance, which the broker's own database shares; the loader stops writing the
-- same way. Counters only: nothing about who searched or for what.
CREATE TABLE IF NOT EXISTS search_usage (
    day          TEXT PRIMARY KEY,       -- "2026-11-25" (UTC)
    rows_read    INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    requests     INTEGER NOT NULL DEFAULT 0
);
