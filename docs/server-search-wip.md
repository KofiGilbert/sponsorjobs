# Server search: work in progress (handover note)

Branch `feat/server-search` (worktree `~/sponsorjobs-wt/server-search`), started 2026-11-25 from
origin/main. It ports PR #96 (index + API) and PR #95 (client) onto today's main and reworks the
index so it can stay on Cloudflare's free plan. **Not finished. No PR opened. Nothing deployed, no
Cloudflare resource created.** Delete this file when the work is done.

## Done

- Both PRs' diffs applied onto main, conflicts resolved (`feed.yml`, `worker/README.md`,
  `worker/src/index.ts` kept as main's; `ui/app.py`, `ui/static/app.js`, `docs/feed.md` merged).
- `scripts/load_search_index.py` REWRITTEN for the free plan: one table row per job, `id` is the
  board's order (dated bit | posting day | label, 41 bits, `assign_ids` / `_place`), flags in `f`,
  visa codes as bits in `vm`, other fields as a positional JSON array `r` (lists kept in
  `search_meta`: `fields`, `tags`, `visa`), employer fields once in `extras`, a daily write budget
  with resume (`--daily-writes`, `--max-writes`, the `search_usage` table), a `load_token` /
  "dirty" marker so a dead run rebuilds its state from D1, `ready` flag after the first full load.
- `worker/migrations-search/0001_job_search.sql` REWRITTEN to that schema (jobs, extras,
  `jobs_pay` and `jobs_offered` partial indexes, FTS5 trigram `detail=none` with triggers,
  `search_meta`, `search_usage`).
- `worker/scripts/local-d1.mjs` + `local-d1.wrangler.jsonc`: a local stand-in for the D1 HTTP API
  (real workerd D1), so the Python loader runs with `SEARCH_D1_API_URL=http://127.0.0.1:8791/query`
  and reports D1's own rows_read / rows_written.
- A 30,267-row sample of the real feed loaded through it once (with the earlier 62-bit ids).

## Half-done (these files do NOT work together yet)

- `scripts/load_search_index.py`: the id change to day + label and the new `ts` column were just
  made and have NOT been run. Still to do in it: send upserts in ASCENDING id order (descending
  left table pages half empty: 908 against 456 bytes a row); after the first load, pick the
  newest rows that fit the budget and send that batch ascending; run
  `INSERT INTO jobs_fts(jobs_fts, rank) VALUES('merge', 2000)` after a run that wrote.
- `worker/src/search.ts`, `worker/test/search.test.ts`, `worker/test/search-parity.test.ts`,
  `worker/test/fixtures/search-parity.json`, `scripts/gen_search_parity.py`,
  `tests/test_search_index.py`: still PR #96's code for the OLD schema (rkey, job_extras,
  title_l...). They must be rewritten for the new schema; vitest fails until then.
- `worker/wrangler.jsonc` still has PR #96's `SEARCH_DB` entry with a PLACEHOLDER id and the
  `ratelimits` block: a deploy would fail. Must move both into a commented block, make
  `env.SEARCH_DB` optional (503 `no_index` without it) and bind SEARCH_DB for tests in
  `vitest.config.ts` (miniflare `d1Databases`) instead. `worker/src/index.ts` still lacks the two
  routes and the `exec` (waitUntil) pass-through; `worker/README.md` lacks the route row.
- `sourcing/remotesearch.py`, `sourcing/feedfile.py` (`write_slice`), `sourcing/feedclient.py`
  (`slice_only`), `ui/app.py` (`_jobs_search_view`, `_aa_server_board`), `ui/static/app.js`,
  `tests/test_remote_search.py`: PR #95's code, applied but not yet adapted to main.
- `.github/workflows/feed.yml`: the loader step is not re-added (add it before "Save the crawl
  state to R2", only when the `SEARCH_D1_TOKEN` secret is set, `continue-on-error`).

## Decisions made (and why)

- **D1 counts, measured in local workerd**: 1 row written per table row, +1 per index entry
  inserted, +1 per FTS5 row inserted or deleted (so the triggers cost 1 each); index entries
  removed by a DELETE are not counted; FTS5's own internal reads and writes are not counted.
  So: a new job = 2.51 rows written (table, FTS, pay index for the 51% with pay), a removed job
  = 2, a changed job = 1 to 2. Production should count the same (same engine) but that is
  UNVERIFIED until a real database exists.
- **Real churn** (live feed 2026-11-16 against 2026-11-25, 304,489 -> 332,937 rows): 98,262
  added, 69,814 removed in 9 days, so about 2.4% of the list added and 2.3% removed a day;
  2,824 rows changed their title/company/location. Every kept row changed its hash because new
  fields were added to the feed (a field added to every row means a slow full rewrite: the
  budget handles it, removals and new rows go first).
  Daily writes are therefore about 0.11 x jobs: 39k at 350k, 67k at 600k, 111k at 1M. Free
  limit 100k/day for the whole account: crossed near 900k jobs (near 720k with the 80k budget).
  First load at 2.51 writes a job and 80k a day: 350k = 11 days, 600k = 19, 1M = 32.
- **Storage**: free D1 is 500 MB a database. Measured on the sample: jobs 456 B/row loaded
  ascending; FTS 317 B/row with sparse 62-bit ids (88 with dense ids), which is why ids became
  day + label with a stride of 64. Re-measure; expect about 550 to 650 B/row, so about 750k to
  900k jobs in one free database.
- **Reads**: no counting to 200. Page 1 stops at limit+1 matches; every request scans at most a
  fixed number of rows (planned SCAN_CAP 3000) and returns a partial page with a cursor when it
  runs out; the count is exact when the scan reached the end, else an estimate ("about N").
  Sort by pay walks `jobs_pay`, then rows with no pay. Keyword search: FTS trigram AND as a
  prefilter in rowid (= newest first) order, then the real substring check on the row.
- **Account-wide free limits**: the broker's database shares D1's daily allowance, so the Worker
  is to stop search at its own daily budget (`search_usage`, planned vars
  `SEARCH_DAILY_ROWS_READ` 3,500,000 and `SEARCH_DAILY_REQUESTS` 60,000; 503 `daily_budget`) and
  the app falls back to its slice.
- **Slice** (client): must become "the top N rows of the board" plus recent first-seen rows,
  with the horizon in the manifest, so the app answers a page locally only when the filtered
  slice provably holds it (a filter of a prefix is a prefix of the filtered board).
- New facet `scope=sponsors|all` (flag bit 1). The personal layers stay on the device: hide
  roles closed to a visa holder, dismissed and closed ids, company marks, saved, Home's fit rank
  (Home ranks the slice only; nothing from the profile is sent).
- Workers Paid tip to document: one paid month ($5) does the whole first load in a day.

## Left to do, in order

1. Finish the loader (ascending order, budget waves, FTS merge), reload the sample, re-measure
   bytes a row and writes a job. Then load the full live feed (332,937 rows) for the doc.
2. Rewrite `worker/src/search.ts` for the new schema; wire `index.ts`, `env.ts`,
   `wrangler.jsonc` (commented block), `vitest.config.ts`; rewrite the two test files and
   regenerate the parity fixture (`python scripts/gen_search_parity.py`, add scope cases).
3. Rewrite `tests/test_search_index.py` for the new loader.
4. Adapt the client (PR #95 files above) to main: scope, slice prefix rule, main's `/api/jobs`
   response shape (`scope`, `fit_ranked`, `attribution`, `_jobs_with_sessions`, closed ids),
   picker, count label, longer back-off on 503 daily_budget.
5. Re-add the feed.yml step; update docs/server-search-plan.md (free-plan analysis, go-live
   checklist), docs/feed.md, worker/README.md.
6. Tests: `npm test` in worker/; pytest one file at a time
   (`B=$(mktemp -d); ~/sponsorjobs/.venv/bin/python -m pytest -q -p no:cacheprovider <file> --basetemp=$B; rm -rf $B`).
7. Open ONE PR "Server-side job search, ready behind a switch" (supersedes #95 and #96; do not
   close them; no "Generated with Claude Code" line; do not merge).

## Exact next step

```
cd ~/sponsorjobs-wt/server-search/worker
node scripts/local-d1.mjs --dir /tmp/search-d1 --port 8791 &
cd .. && SEARCH_D1_API_URL=http://127.0.0.1:8791/query ~/sponsorjobs/.venv/bin/python \
  scripts/load_search_index.py --feed <a jobs.json.gz> --state /tmp/search-state.json.gz --daily-writes 0
```
(a real feed: `StaticFeed("https://feed.sponsorjobs.ai/feed", <dir>).refresh(force=True)` writes
`<dir>/jobs.json.gz`). Fix what breaks in the loader, then start on `worker/src/search.ts`.
