# Server-side job search: when and how

**Status (2026-11-25): built and tested, behind the `JOBS_SEARCH=server` switch, OFF. Not
deployed: no Cloudflare resource exists yet.** Read "As built" and "Staying on Cloudflare's free
plan" at the end first; sections 1 to 4 are the October plan, kept for its reasoning. Prices were
read from Cloudflare's pages: **verify before buying.**

Today every install downloads the whole slim list (`sourcing/feedclient.py`, ~1 MB gzipped per 10k
roles, in content-addressed parts) and filters it in the engine (`sourcing/filters.apply_facets`,
`ui/app.py /api/jobs`, `autoapply/picker.candidates`). That is simple, private and works offline,
but it grows linearly with the list. The goal is 500k+ roles.

## 1. Thresholds

Measured: the live manifest holds **150,000 rows in 15 parts, 14.9 MB** (~100 bytes a row gzipped).
Engine memory is ~0.22 GB at 150k after PR #83. A full-board filter on a 25k cached list took
8 ms for a keyword and 46 ms for a pay sort; it scales linearly.

| roles | list size | at 60 KB/s (hotspot) | at 5 MB/s | engine RAM | pay-sort pass | data/day if app open 12 h* |
|---|---|---|---|---|---|---|
| 150k | 15 MB | 4.2 min | 3 s | 0.22 GB | ~0.3 s | ~60 MB |
| 300k | 30 MB | 8.3 min | 6 s | ~0.44 GB | ~0.6 s | ~120 MB |
| 500k | 50 MB | 14 min | 10 s | ~0.73 GB | ~0.9 s | ~200 MB |
| 1M | 100 MB | 28 min | 20 s | ~1.5 GB | ~1.9 s | ~400 MB |

\* The feed rebuilds every 3 h, and a part's name is a hash of all rows, so each rebuild means a
full re-download (4 rebuilds in 12 h). Phase 1 (stable parts, 2026-10-23) cuts this to the parts that
changed; see docs/feed.md for the measured figure.

**Switch point: 250k roles** (~25 MB, ~7 min on the hotspot, ~0.37 GB). Past that the first launch on
a slow link is a long wait, a laptop with 8 GB of RAM feels it, and the daily download costs real
mobile data. **Never ship a full list above 300k.** Build phases 1 and 2 now, so the switch is a
config flag when the list reaches the switch point.

## 2. Options

Usage assumption: an active user runs ~500 searches a month (20 days x 25), so **10k MAU = ~5M
searches/month, 100k MAU = ~50M**. With an edge cache, about half of these never reach the database:
the default board and the common queries repeat.

### (a) Worker + D1 with FTS5

One D1 table of slim rows plus an FTS5 index over title and company (the fields `q` matches today),
with ordinary indexes on `posted_at`, `salary_value`, `state`, `remote`, `entry_level`. JD texts stay in
the R2 shards. 500k rows is ~0.5-1 GB, well under D1's 10 GB limit.

- Pricing ([D1](https://developers.cloudflare.com/d1/platform/pricing/),
  [Workers](https://developers.cloudflare.com/workers/platform/pricing/)): Workers Paid $5/month
  includes 10M requests (then $0.30/M) and 30M CPU-ms (then $0.02/M). D1 includes 25B rows read
  (then $0.001/M), 50M rows written (then $1.00/M), and 5 GB of storage. The free plan stops at
  100k requests/day, so 10k MAU needs Paid.
- Cost risk: rows read counts **rows scanned**. `OFFSET` paging and exact `COUNT(*)` over broad
  queries are expensive, so use keyset cursors, cap counts at "1,000+", and keep the average under
  ~3k rows a query. Load the index by diff (changed rows only): a full 500k rewrite every 3 h would be
  ~120M writes/month (~$70), and FTS index writes add to that.
- Throughput: a D1 database runs one query at a time
  ([limits](https://developers.cloudflare.com/d1/platform/limits/)). At 10 ms a query that is ~100
  queries/s, enough for 100k MAU when the edge cache is in front. Read replication is the next step
  if it is not.
- **~$5/month at 10k MAU, ~$20-70/month at 100k** (depends mostly on rows read). Effort: medium.

### (b) Worker + R2 prebuilt sharded indexes

The build writes shards by state, title word and sponsor. The client fetches the shards it needs and
intersects them. [R2](https://developers.cloudflare.com/r2/pricing/): $0.015/GB-month, Class A
$4.50/M, Class B $0.36/M, free egress, and 10M Class B and 1M Class A free each month. Cached reads
cost nothing. **~$0-5/month at 10k, ~$5-20 at 100k.** The catch: free text is poor. A word like
"engineer" is a 100k-row shard (~10 MB), which brings back the problem we are trying to fix.
Multi-word and multi-facet queries need several downloads. Good for browsing by state or sponsor,
weak for search.

### (c) Vectorize or a hosted search service

- [Vectorize](https://developers.cloudflare.com/vectorize/platform/pricing/): storage is cheap
  (500k x 384 dims ~ $0.10), but queries cost $0.01 per million queried dimensions: 50M x 384 ~
  **$190/month at 100k MAU**, plus the cost of embedding each query. It is semantic only and weak at
  literal facets. ATS matching depends on literal terms ("PnL"), so it adds to keyword search rather
  than replacing it.
- Typesense Cloud: a fixed hourly node. Third-party figures put it at ~$22/month for 0.5 GB and
  ~$45-50 for 2 GB; a 3-node HA cluster is ~4x that, so **~$50-150/month** whatever the traffic, plus
  bandwidth ([billing](https://cloud-help.typesense.org/article/billing-process); no official price
  list found).
- Meilisearch Cloud usage plan: Build is $30/month with 50k searches and 100k documents, then
  $0.40/1k searches and $0.30/1k documents
  ([usage-based](https://www.meilisearch.com/usage-based)). At 500k documents and 5M searches that
  is **~$2,100/month at 10k MAU**. Resource-based plans run ~$20-60+ a month and need sizing.
- Algolia Grow: 10k searches and 100k records free, then $0.50/1k searches and $0.40/1k records
  ([support](https://support.algolia.com/hc/en-us/articles/15745996583441-How-am-I-charged-on-the-Grow-plan)).
  **~$2,700/month at 10k MAU, ~$25k at 100k.** Ruled out.

### (d) Hybrid: a local slice plus the server for the long tail

The build publishes a second, small list with the same parts mechanism: roles first seen in the
last 7 days, capped at ~40k (~4 MB, ~70 MB RAM). The app keeps that slice, plus the person's saved
jobs from the local store, and answers from it instantly and offline. Searches that go beyond the
slice (a keyword, older roles, page 2+) go to (a). Costs are (a)'s minus whatever the slice answers:
**~$5/month at 10k MAU, ~$15-40 at 100k.**

### Comparison

| | offline | privacy | latency | abuse surface | 10k MAU | 100k MAU | effort |
|---|---|---|---|---|---|---|---|
| today (full list) | full | best | 50 ms-2 s, grows | dataset already public | $0 (R2 egress free) | $0 | done |
| (a) D1 FTS5 | none | query + filters only | 60-200 ms | new API: rate-limit it | ~$5 | ~$20-70 | M |
| (b) R2 shards | cached shards | shard names reveal the query | 1+ downloads | low | ~$0-5 | ~$5-20 | M-L |
| (c) hosted / vector | none | query goes to a third party | 50-150 ms | vendor-metered | $50-2,700 | $150-25k | S-M |
| **(d) hybrid on (a)** | **recent + saved** | **query + filters only** | **instant, then complete** | **as (a)** | **~$5** | **~$15-40** | **M** |

## 3. Recommendation: (d), with D1 FTS5 behind it

### What goes to the server, and what never does

Only the query and the facets go: `q, loc, days, remote, visa, level, pay, sort, sponsored`,
plus a cursor. **These never leave the machine:** the resume and profile, the title and skill words
taken from them (`autoapply/picker.profile_rank`), saved jobs, dismissed IDs, company exclude and
prioritize lists, and match scores. A pytest guard checks every outgoing search URL against this
allowlist of keys. The server stores counters, not query logs linked to an install.

### API sketch (`worker/`, api.sponsorjobs.ai)

```
GET /v1/jobs/search?q=&loc=&days=&remote=&visa=H1B,OPT&level=&pay=&sort=recent|pay
                   &sponsored=1&scope=sponsors|all&limit=30&cursor=<opaque>
-> 200 {
  "jobs": [ <the same slim row the feed carries: source_id, source, company, title, location,
            remote, url, posted_at, first_seen, salary, sponsorship_stated, us, entry_level,
            visa, nationality_visas, sponsor, ad_stance> ],
  "count": 112, "count_capped": false,        // exact once the scan reached the end
  "count_estimate": null,                     // else "about N" when the server could tell
  "next_cursor": "...",                       // where the scan stopped; null at the end
  "index_version": "...", "generated_at": "..."
}

GET /v1/jobs/candidates?<same facets>&titles=Data%20Analyst|BI%20Analyst&limit=500&cursor=
-> same shape, limit up to 500: the Auto-apply picker's pool
```

- `limit` is at most 50 for search and 500 for candidates. **Search depth stops at 1,000 results**
  (refine to see more), so the API is not a bulk export.
- A cursor from an older `index_version` returns 409 and the client restarts at page 1.
- JD text still comes from the `jd/{xx}` shards (256-way, so one fetch says little about which
  role was opened). No new endpoint is needed.

### Jobs page

`/api/jobs` keeps its response shape. As built, every Jobs page (keyword or not) calls
`/v1/jobs/search` (the default board costs 62 rows read and is edge-cached), then applies the local
layers it applies today: `_jobs_personal`, dismissed and closed postings, `_jobs_mark`, resume
sessions and the saved overlay. Home's best-fit strip ranks the local slice instead, so nothing
from the profile leaves the machine. The slice also answers whenever the API cannot. Because dismissed rows are removed
on the device, a page can come back short. The engine then fetches the next cursor until it has 30
rows or has made 3 calls. The total reads "about N".

### Auto-apply picker and alerts

`candidates()` gets its board from `/v1/jobs/candidates`. The titles sent are only the ones the person
typed into their saved search or today's focus, which the UI shows as "Searching for: ...". The
picker merges the pool with the slice, then ranks it **on the device** with `profile_rank` and the
JD keyword match, as it does today. Job alerts look for new roles, which are already in the slice,
so they run locally.

### Caching and failure

- Edge: the Worker caches each normalized query with the Cache API until `index_version` changes
  (at most 10 minutes).
- Engine: an in-memory cache of the last ~50 responses, so the Back button and facet toggles are free.
- Offline or server down: the slice is served with "Showing recent roles. Search all jobs when
  you're back online."

### Abuse and rate limiting

- An anonymous random install token sent in a header. It is not tied to an account and never logged
  with the query.
- The Workers [rate-limit binding](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/):
  60 requests/60 s per token and 300/60 s per IP. It is approximate and per location, so a
  Cloudflare WAF rule is the backstop.
- The 1,000-result depth limit and the 500-row candidates limit.

### Migration phases

1. **Quick win (now). Built 2026-10-23** (`feedfile.write_stable`, docs/feed.md). Hold
   `JOBS_FEED_LIMIT` at ≤250k. Make the list parts stable: bucket rows by `first_seen` week so a
   rebuild re-sends only the parts that changed, with a small removed-rows file so roles closing
   across all weeks do not touch every part. This cuts the 3-hourly full re-download. *Test:* two
   builds that share most rows share most part names (tests/test_feed_stable.py).
2. **Index + API (no client change).** Add the D1 schema and FTS5 migration in `worker/migrations`.
   Add a diff loader step to `feed.yml`, run after `build_feed.py` (D1 HTTP API, a scoped token in an
   Actions secret). Add `/v1/jobs/search` and `/v1/jobs/candidates`. *Tests:* vitest for parameter
   validation, FTS syntax escaping (quotes, `NEAR`, `*`), cursors, and the 409 on a stale cursor. A
   **parity test** runs ~50 facet combinations through `apply_facets` and the Worker (miniflare) on
   the same fixture rows and expects the same IDs in the same order.
3. **Client behind a flag.** Add `sourcing/remotesearch.py` and the slice in the `feedclient`
   manifest (`slice` next to `parts`), switched on with `JOBS_SEARCH=server`. *Tests:* pytest with a
   fake fetch covering slice-first answers, server merge, short-page refill, offline fallback, the
   stale-cursor restart, and the **privacy allowlist guard**.
4. **Switch.** Turn it on by default when the list reaches 250k. Keep publishing the full list for one
   or two releases for older apps, then stop. Raise the server index to 500k+ (and drop the 45-day
   cut-off there if wanted). The slice stays small.

## 4. Risks and open questions for the owner

1. **Workers Paid, $5/month** (if the account is not already on it), with expected overage of
   ~$15-65/month at 100k MAU. **Needs your yes** before phase 2 goes live.
2. **Offline gets smaller:** older roles and keyword search need internet. Acceptable?
3. **Search titles leave the machine:** the Auto-apply picker would send the job titles you typed
   (never your resume). OK, or should the picker use only the local slice?
4. **D1 single-writer limit:** if peak traffic outgrows it, add read replicas or a second database
   per region (engineering time, not much money).
5. **Rows-read cost** depends on query shape. Watch it in the dashboard in the first month and set a
   billing alert at $25.
6. **Scraping:** a search API is easier to harvest than a file. Today's full list is already
   public, so the risk does not rise much, but the depth limit and rate limits should ship from day one.
7. **Hosting:** Pages' 100 GB/month soft limit cannot carry today's full-list model past a few
   hundred users. Keep the feed on R2 (free egress) whatever is decided here.

## As built (2026-11-25)

PR "Server-side job search, ready behind a switch" ports #96 (index + API) and #95 (client) onto
main and rebuilds the index to fit Cloudflare's free plan. Nothing changes for anyone until the
go-live checklist below is followed.

**Index** (`worker/migrations-search/0001_job_search.sql`, its own D1 database
`sponsorjobs-search`, binding `SEARCH_DB`, so search never queues behind the broker):

- One table row per job. Its `id` IS the board's newest-first order (a dated bit, the posting
  day, a label that keeps `feedfile.recency_key`'s order inside a day; 41 bits, exact in
  JavaScript), so the table is its own date index and no second row is written per job.
- Every filter is precomputed by the loader with the app's own Python functions: flags in `f`
  (sponsor board, entry level, ad offers sponsorship, has a visa record, work type), visa codes as
  bits in `vm`, `pay` (`salary_value`), `ts` (posting time). The rest of the row is a positional
  JSON array; each company's sponsor and visa block is stored once in `extras`.
- Two small partial indexes (pay > 0, about half the rows; "ad offers sponsorship", under 1%) and
  an FTS5 trigram keyword index with `detail=none` (140 bytes a job instead of 343). The keyword
  index only says which rows hold every trigram of the text; the Worker checks the real substring.
- `search_meta` (version, sizes, the lists the rows are encoded with, per-day row counts and pay
  quantiles that bound each walk) and `search_usage` (rows read and written, requests, per UTC day).

**Loader** (`scripts/load_search_index.py`, a `feed.yml` step that runs only when the
`SEARCH_D1_TOKEN` secret exists and never blocks the feed): sends only what changed (removals
first, then new jobs newest first, then changed jobs), stops at a daily write budget
(`SEARCH_D1_DAILY_WRITES`, default 80,000, the rest of the free 100,000 left to the broker) and
carries on next run. D1 reports the rows each statement really wrote, keyword index included, and
the loader counts them. A run that dies part-way leaves a "dirty" marker; the next run rebuilds its
state from D1 and finishes. The API answers 503 `no_index` until the first full load is in.

**API** (`worker/src/search.ts`): the facets of section 3 plus `scope=sponsors`. Each request is
a plain ordered walk that SQLite stops when the page is full, bounded to about 2,000 index rows
(6,000 for the picker's pool) by the loader's histograms; a rare filter mix comes back short with
a cursor that carries on from where the scan stopped. No counting past the page: the count is
exact once the scan reached the end, else "about N" (the sponsor or whole-board size, a keyword's
match count up to 250, or the share of rows walked that matched), else "N+". 1,000 results deep at
most. Rate limits 60 a minute per install token and 300 per network. A daily budget
(`SEARCH_DAILY_ROWS_READ` 3,500,000 and `SEARCH_DAILY_REQUESTS` 60,000; "0" turns a cap off)
answers 503 `daily_budget` before search can use the free allowance the broker needs.
Two D1 details found while measuring: D1 binds a JavaScript number as REAL, and FTS5 then ignores a
rowid bound (36,892 rows read instead of 306), so those bounds are CAST to INTEGER; and with a
partial index SQLite takes the first range term it sees, so `pay >= ?` comes before `pay > 0`.

**App** (`JOBS_SEARCH=server`, `sourcing/remotesearch.py`, `ui/app.py _jobs_search_view`): with
the switch on, the app downloads only the feed's slice (roles first seen in the last 7 days, at most
40,000) and asks the API for the Jobs pages, through the same local layers as the full list:
closed postings and dismissed roles left out, "ad says no sponsorship" and citizenship roles hidden
while the person needs sponsorship, general (no-record) rows and their chips as they come, company
marks, resume sessions, saved overlay, source credits. Home ranks the slice by fit on the device.
The picker sends its saved search's filters, the scope and typed titles only. Offline, or on a 503,
the board shows the slice with "Showing recent roles. Search all jobs when you're back online." and
waits 10 minutes before asking again. Job alerts stay local on the slice.

**Tests:** worker `npm test` (the parity test runs 68 facet mixes, scope included, through every
plan and a one-row scan cap, and gets `apply_facets`' IDs in the same order), `tests/test_search_index.py`,
`tests/test_remote_search.py` (with the privacy guard over every outgoing URL and header).

**Try it locally, no Cloudflare account:** `node worker/scripts/local-d1.mjs --dir /tmp/sd1`
(the real D1 engine behind a stand-in of the D1 HTTP API), then
`SEARCH_D1_API_URL=http://127.0.0.1:8791/query python scripts/load_search_index.py --feed jobs.json.gz --state /tmp/st.json.gz --daily-writes 0`,
stop the stand-in, and `node worker/scripts/measure-search.mjs --dir /tmp/sd1` for the rows each
search reads (`--trace 'q=nurse&level=entry'` for one query's statements). Use a sample of 20k to
50k jobs on an 8 GB machine.

## Staying on Cloudflare's free plan

Free plan (Workers Free + D1 free): 100,000 Worker requests a day, 5 million D1 rows read a day,
100,000 rows written a day, 500 MB per database and 5 GB in all. These are **account-wide**: the
broker's own database and requests share them. Workers Paid, $5 a month: 10M requests, 25B rows
read and 50M rows written a month included, 10 GB per database.

Measured on 2026-11-25 with the real D1 engine (workerd) on the live feed, 332,937 jobs:

| | measured |
|---|---|
| first load | 845,826 rows written: **2.54 a job** (table, keyword index, pay index) |
| storage | 209.5 MB: **629 bytes a job** (table 459, keyword index 140, company blocks 21, pay 9) |
| daily change | the 2026-11-16 and 11-25 feeds differ by 98,262 added, 69,814 removed and 6,722 changed jobs; loading that difference wrote 413,685 rows, **about 46,000 a day, 0.144 writes a job a day**. A lower bound: a job that came and went inside the 9 days does not show |
| bookkeeping | about 20 rows a run (version, sizes, histograms), 8 runs a day |
| rows read, page 1 | 39 typical searches: **mean 672, most 2,682**. Default board 62, sponsors only 90, sort by pay 62, a keyword about 340 (250 of them the match count), keyword plus a rare filter 450 to 2,700. Page 2 is cheaper. This barely grows with the number of jobs: a walk stops at the page |

What that means at each size (80,000 writes a day for search):

| jobs | first load | daily writes to keep up | storage | free? |
|---|---|---|---|---|
| 333k (today) | 16 days | 48,000 | 210 MB | yes |
| 350k | 18 days | 50,000 | 220 MB | yes |
| 500k | about 40 days | 72,000 | 315 MB | yes, close to the write budget |
| 600k | never (change outruns the budget) | 87,000 | 377 MB | no |
| 1M | never | 144,000 | 629 MB | no (writes and storage) |

The first-load figures count the daily change of the rows already loaded, which slows the load
as it goes. One paid month ($5, `SEARCH_D1_DAILY_WRITES=0`) does any of these first loads in a day.

**Where each free limit is first passed:**

1. **Rows read, by daily users (comes first).** 3.5M rows a day for search / about 700 a search =
   about 5,000 searches a day: **about 200 daily active users** at 25 searches each, about 400
   with half of the searches answered by the edge cache. The number of jobs hardly matters.
2. **Rows written, by job count:** the 80,000 budget at **about 555,000 jobs**; the account's whole
   100,000 at about 690,000 (less whatever the broker writes).
3. **Storage:** 500 MB a database at **about 790,000 jobs**.
4. **Requests:** 100,000 a day at about 33 a daily user (pages and short-page refills) = about 3,000
   daily users, so never the first limit.

**Workers Paid ($5 a month) becomes necessary at about 200 to 400 daily active users who search,
or at about 550,000 jobs, whichever comes first.** 500k fits on free; 1M does not. On the paid
plan the same numbers cost: 1M jobs is about 4.3M writes a month (50M included), and 100k monthly
users at 500 searches each are about 35B rows read (25B included, then $0.001 a million, about
$10). Set `SEARCH_DAILY_ROWS_READ`, `SEARCH_DAILY_REQUESTS` and `SEARCH_D1_DAILY_WRITES` to "0"
after upgrading.

Not verified: that production D1 counts rows read and written exactly as the local engine does
(same engine, but check the dashboard in the first week), and the 500 MB free database limit
(read it on Cloudflare's D1 limits page before relying on it).

## Go-live checklist

1. **Create the database:** `cd worker && npx wrangler d1 create sponsorjobs-search`. Uncomment
   the `SEARCH_DB` block in `wrangler.jsonc` (inside `"d1_databases"`) and paste the id it printed.
2. **Apply its migrations:** `npx wrangler d1 migrations apply SEARCH_DB --remote`.
3. **Rate limits and cursor secret:** uncomment the `"ratelimits"` block;
   `npx wrangler secret put SEARCH_CURSOR_SECRET` (any long random string). Deploy the Worker.
   `/v1/jobs/search` answers 503 `no_index` until step 5 finishes.
4. **Loader credentials (GitHub, repo settings):** secret `SEARCH_D1_TOKEN` (a Cloudflare API
   token with D1 Edit), variables `SEARCH_D1_ACCOUNT_ID` and `SEARCH_D1_DATABASE_ID` (from step 1).
   Optional: `SEARCH_D1_DAILY_WRITES` ("0" for a paid month).
5. **First load:** the feed workflow loads after each build; watch its `[search-index]` lines
   (`left=` counts down). About 16 days on free at today's size, or a day on a paid month. Done when
   `search_meta` has `ready = 1` (`npx wrangler d1 execute SEARCH_DB --remote --command
   "SELECT * FROM search_meta WHERE k IN ('ready','rows','pending')"`).
6. **Switch on for dev:** run a dev build with `JOBS_SEARCH=server` (and `JOBS_SEARCH_URL` if the
   API is not the broker URL). Check Jobs (keyword, filters, Sponsors only / All jobs, deep pages),
   Home, and offline (the slice note).
7. **Switch on for everyone:** set `JOBS_SEARCH=server` in the shipped app (`shell-electron`), keep
   publishing the full list for one or two releases for older apps, then raise the feed's size
   past 330k. Watch D1's rows read and written in the dashboard the first week.
