# Server search: work in progress (handover note)

Branch `feat/server-search` (worktree `~/sponsorjobs-wt/server-search`). Ports PR #96 (index + API)
and PR #95 (client) onto main, reworked to stay on Cloudflare's free plan. **No PR opened yet.
Nothing deployed, no Cloudflare resource created.** Delete this file when the PR is opened.

## Done (all committed and tested)

- Index schema `worker/migrations-search/0001_job_search.sql`: one row per job, `id` = the
  board's order (dated bit | posting day | label), flags `f`, visa bits `vm`, `pay`, `ts`, text
  columns, positional JSON `r`, employer fields once in `extras`, partial indexes `jobs_pay`
  (pay > 0) and `jobs_offered`, FTS5 trigram `detail=none`, `search_meta`, `search_usage`.
- Loader `scripts/load_search_index.py`: diff only, daily write budget with resume
  (`--daily-writes` 80k default, `--max-writes`, RESERVE 40 for bookkeeping), dirty-token
  recovery, ascending batches, FTS merge, `hist`/`payq` histograms in meta, `ready` flag.
- API `worker/src/search.ts`: scope facet, bounded plain walks (SCAN_CAP 2000 rows a request,
  cursor carries on), estimates instead of counting, daily budget 503 `daily_budget`
  (`SEARCH_DAILY_ROWS_READ` 3.5M, `SEARCH_DAILY_REQUESTS` 60k), CAST on FTS rowid bounds (D1 binds
  numbers as REAL). Routes wired in `index.ts`; `SEARCH_DB` optional and NOT bound in
  `wrangler.jsonc` (commented go-live block); tests bind it in `vitest.config.ts`.
- Client: `sourcing/remotesearch.py` (scope, 503 ten-minute back-off, `count_label` with
  estimates), `ui/app.py` `_jobs_search_view` (server pages through main's local layers: closed
  ids, dismissed, closed-to-visa, marks, sessions, saved, attribution; Home ranks the slice
  locally; offline slice note), picker sends scope. `feed.yml` loader step (only with
  `SEARCH_D1_TOKEN`).
- Tools: `worker/scripts/local-d1.mjs` (local D1 HTTP stand-in), `worker/scripts/measure-search.mjs`
  (rows read per query, `--trace`).
- Tests passing: worker `npm test` 542 (parity 68 facet mixes x every plan); pytest one file at a
  time: test_search_index 17, test_remote_search 27, and feed/scope/picker/home/i18n files all green.

## Measured (local workerd D1, live feed 2026-11-25, 332,937 rows)

- First load: 845,826 rows written = 2.54 a job. Storage 209.5 MB = 629 B a job.
- Churn: 2026-11-16 -> 11-25 diff (98,262 added, 69,814 removed, 6,722 changed) wrote 413,685
  rows = 46k a day = 0.144 x jobs a day (a lower bound: churn inside the 9 days is collapsed).
- Rows read, page 1, 39 typical queries: mean 672, max 2,682 (default board 62, keyword ~340).
- Free-plan thresholds: writes 80k budget passed at ~555k jobs (100k whole-account at ~690k);
  first load 333k = 16 days, 350k = 18 days, 600k+ never on free (churn > budget); storage 500 MB
  per free database at ~790k jobs; reads 3.5M/day budget / ~700 a search = ~5,000 searches a day
  (~200 daily users at 25 searches, ~400 with the edge cache). Workers Paid $5 removes all three.

## Left

1. Docs: rewrite docs/server-search-plan.md "as built" + free-plan analysis + go-live checklist
   (create D1, add the binding block, apply migrations, secrets/vars, first load, flip
   `JOBS_SEARCH=server` for dev, then everyone); short notes in docs/feed.md and worker/README.md
   (route row). Delete this WIP file.
2. Open ONE PR "Server-side job search, ready behind a switch" (supersedes #95 and #96, do not
   close them, no "Generated with Claude Code" line, do not merge).

## Exact next step

Write the docs (step 1). No processes need to be running; do not start wrangler/vitest again
unless something changes (memory is tight).
