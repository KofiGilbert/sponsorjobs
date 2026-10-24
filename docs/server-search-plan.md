# Server-side job search: when and how

**Status: phase 1 built 2026-10-23; phase 2 (index + API) built 2026-10-24 and not deployed; see
"Phase 2 as built" at the end.** Prices were read from Cloudflare's and the
vendors' pages on 2026-10-23: **verify before buying.**

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
                   &sponsored=1&limit=30&cursor=<opaque>
-> 200 {
  "jobs": [ <the same slim row the feed carries: source_id, source, company, title, location,
            remote, url, posted_at, first_seen, salary, sponsorship_stated, us, entry_level,
            visa, nationality_visas, sponsor, ad_stance> ],
  "count": 812, "count_capped": false,        // capped at 1000 -> "1,000+"
  "next_cursor": "...",                       // keyset on (sort key, source_id); null at the end
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

`/api/jobs` keeps its response shape. With no keyword, page 1 comes from the slice. With a keyword,
or deeper pages, it calls `/v1/jobs/search`, then applies the local layers it applies today:
`_jobs_personal`, dismissed, `_jobs_mark`, and the saved overlay. Because dismissed rows are removed
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

## Phase 2 as built (2026-10-24)

Not deployed. Nothing runs until the owner says yes (Workers Paid, section 4) and adds the loader's
token.

- **Schema** (`worker/migrations/0006_job_search.sql`, in the broker's D1): `jobs` (the slim row as
  JSON plus columns the loader precomputes in Python: recency key, annual pay, posting time, lowered
  title / company / location, visa codes, flags), `job_extras` (the per-employer `sponsor`, `visa`,
  `nationality_visas` fields stored once and joined back: they were ~1.3 KB of a ~2 KB row),
  `jobs_fts` (FTS5, **trigram** tokenizer, external content, over title, company and location) and
  `search_meta` (`index_version`, `generated_at`, `rows`). Trigram, not a word tokenizer, because
  `q` and `loc` are substring matches in `apply_facets` ("eng" finds "Engineer").
- **API** (`worker/src/search.ts`): the routes and response shape of section 3. 400 on a malformed
  parameter, 503 until an index is loaded, 409 `stale_cursor`, 429 `rate_limited`. The cursor is
  signed (HMAC); set `SEARCH_CURSOR_SECRET` before launch or a caller could forge one past the
  depth limit. Rate limits use the `ratelimits` binding in `wrangler.jsonc` (no resource to
  create). Plan choice: a capped probe counts the FTS matches; few matches use the index, many
  walk the ordered index (a common term in the index costs more than walking).
- **Loader** (`scripts/load_search_index.py`, `feed.yml` step after the build, only when the
  `SEARCH_D1_TOKEN` secret is set, with `SEARCH_D1_ACCOUNT_ID` / `SEARCH_D1_DATABASE_ID`
  variables): diffs the feed against the last loaded state and sends deletes and upserts as SQL
  literals (~90 KB statements; D1 allows 100 bound parameters a query). `--dry-run` prints the
  counts. A failed load deletes its state; the next run rebuilds it from D1 and finishes.
- **Parity**: `worker/test/search-parity.test.ts` runs 57 facet mixes (50 search, 7 candidates)
  through the Worker on 420 synthetic rows and gets `apply_facets`' IDs in the same order, through
  both query plans. No semantic difference was needed. Known edges, not exercised by real data:
  JS and Python can lowercase a few rare Unicode characters differently, and the loader drops NUL
  characters from text.

### Measured sizes and costs (local D1 in miniflare, the live 184,552-row list on 2026-10-24)

| | 184k rows | 500k rows (list x2.7) |
|---|---|---|
| storage | 326 MB (jobs 173, FTS 64, indexes ~80) | ~0.9-1.0 GB |
| first load | 1,848 requests, 165 MB of SQL, 1.48M rows written | 5,043 requests, 4.0M rows written |
| a 3-hourly diff (1% new, 1% changed, 1% removed) | 54 requests, ~26k rows written | ~70k rows written |
| rows written a month (8 loads a day) | ~6M (included: 50M) | ~17M |
| browse (no keyword, any of visa / days / level / sponsored / pay sort) | ~1.1k rows read | ~1.1k |
| keyword, page 1 | 2k-45k (`engineer` 19k, `nurse` 34k, `software engineer` 35k) | 2k-60k |
| keyword, later pages | 100-3k (more when the FTS plan was chosen) | similar |

**Cost warning.** Section 2(a) assumed ~3k rows read a query. Keyword searches measure ~15-25k on
average, and in the hybrid design (d) the server mostly sees keyword searches. At 10k MAU
(~2.5M uncached searches) that is ~50B rows, **~$25/month** over the 25B included. At 100k MAU
and 500k rows it is ~$450-600/month, not $20-70. The capped count drives most of it (the walk reads
until it has 1,001 matches). Options for the owner: cap the count at 200 ("200+") or show "about N"
from the probe, which cuts the common case roughly 5x; or accept the cost.
