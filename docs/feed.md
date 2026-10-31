# The static job feed

**Status: the supported path (2026-09-30).** It replaces the hosted "kitchen" (`backend/feed.py` on
Render, now suspended), and is hosted on Cloudflare R2 (see "Where the feed is hosted"). Nothing is served by a process we run: a scheduled GitHub Action crawls the
GREEN-lane sources, writes three kinds of file, and publishes them behind our domain. Every install
downloads the slim list at most once an hour and filters it locally. There is no server to pay for
or keep alive, and the local crawl remains the fallback, so the app works offline.

## Where the feed is hosted

**Now: Cloudflare R2** (since 2026-10-10), at `https://feed.sponsorjobs.ai/feed`. The repository
variable `FEED_HOST` is `r2` and `R2_PREFIX` is `feed`: the `feed` workflow uploads the built files
with `scripts/upload_feed.py` into the `sponsorjobs-feed` bucket under `feed/`, and the bucket's
custom domain `feed.sponsorjobs.ai` serves them through Cloudflare's CDN. An R2 custom domain
serves **every key in the bucket**, not just `feed/`, so nothing private may be stored in that
bucket: the crawl state has its own private bucket (see "The crawl state" below).

**GitHub Pages** (2026-10-06 to 2026-10-10, while Cloudflare would not activate R2 on the account)
is still wired as the fallback host: unset `FEED_HOST` (or set it to anything but `r2`) and the
workflow publishes the same files as a Pages site instead. GitHub's terms say Pages is not free
hosting for a commercial business or SaaS, so stay on R2 once paid plans launch. To fall back:
do the Pages set-up below, clear `FEED_HOST`, run the workflow once, then point the `feed` DNS
record at GitHub. The app does not change; it only knows the address.

Pages limits (soft): 1 GB site (the feed is ~10 MB) and 100 GB of downloads a month. GitHub serves
the `.json.gz` files as plain gzip without `Content-Encoding`; the app checks the gzip magic bytes
and unpacks either form (`sourcing/feedfile.loads_maybe_gz`, `sourcing/feedclient.py`), so nothing
breaks. Every file gets GitHub's fixed `Cache-Control: max-age=600` (the list parts too: Pages
cannot mark them immutable). A Pages deploy replaces the whole site, so the previous version's
parts vanish at once; a client mid-download then gets a 404 and re-reads the manifest (stable parts
that did not change survive the deploy, so this is rarer). On R2 the parts named by the last three
manifests are kept.

### Pages set-up, step by step

1. Cloudflare -> the `sponsorjobs.ai` domain -> **DNS** -> **Add record**: type `CNAME`, name
   `feed`, target `kofigilbert.github.io`, proxy status **DNS only** (grey cloud; GitHub has to
   reach the name directly to issue its HTTPS certificate).
2. Repo -> **Settings** -> **Pages**: Source **GitHub Actions**. Custom domain
   `feed.sponsorjobs.ai`, Save. When the check passes, tick **Enforce HTTPS**.
3. Repo -> Settings -> Secrets and variables -> Actions -> **Variables**: `TAILOR_FEED_URL` =
   `https://feed.sponsorjobs.ai/feed`. (`FEED_HOST` unset means Pages.)
4. **Actions -> feed -> Run workflow**. Smoke test:
   `curl -s https://feed.sponsorjobs.ai/feed/manifest.json` shows `generated_at` and `count`.

## What the list contains

Only sponsor-relevant rows (`sourcing/quality.is_sponsor_relevant`): every job from a board on the
watchlist (those companies are there because they have a sponsorship record), plus aggregator rows
only when the employer carries a sponsor badge or the posting states sponsorship. The general
aggregator dump of all recent US postings is off (`FREEHIRE_GENERAL_BULK=1` re-enables it).
Decided 2026-10-01 so the landing page's promise holds on every row.

## How it works

```
GitHub Action (every 3h, .github/workflows/feed.yml)
  scripts/build_feed.py   crawl (sourcing.service.refresh_watchlist) -> sponsor-tag + shape
                          (backend.feed.feed_jobs) -> dedupe board vs aggregator (sourcing/dedup.py)
                          -> sourcing/feedfile.write_feed
  scripts/upload_feed.py  put_object to R2 (S3 API) with Content-Type / Content-Encoding / Cache-Control;
                          list parts before the list (skipping parts the bucket already has),
                          manifest last, then deletes parts none of the last 3 manifests name
R2 bucket, custom domain  https://<domain>/feed/jobs.json.gz, /feed/jd/{xx}.json.gz, /feed/manifest.json
Desktop app               JOBS_FEED_URL=https://<domain>/feed  (shell-electron/main.js sets it)
  sourcing/feedclient.py  download jobs.json.gz hourly (ETag), cache under data/feed_cache/,
                          filter + paginate locally with sourcing/filters.py (ui/app.py /api/jobs),
                          fetch one JD shard when a role is opened (cached a day)
```

### The files (`sourcing/feedfile.py`)

| object | contents | cache |
|---|---|---|
| `jobs.json.gz` | `{feed_version: 1, generated_at, count, attribution, jobs: [...]}`; each row is a board row **without** `jd_text` (~500 bytes instead of ~9 KB): `source_id, source, company, title, location, remote, url, posted_at, first_seen, salary, sponsorship_stated, us, entry_level, visa, nationality_visas, sponsor` | `max-age=600` |
| `jd/{xx}.json.gz` | `{source_id: jd_text}` for every job whose `sha1(source_id)` starts with `xx` -- 256 fixed shards, so a crawl uploads ~257 objects, not 50,000 | `max-age=86400` |
| `parts/jobs-{version}-{nnn}.json.gz` | the SAME list split into ~1 MB gzipped parts (~10 for 96k roles), each `{version, part, parts, jobs: [...]}`; joined in order they are exactly `jobs.json.gz`'s rows. `version` is a hash of the rows, so the names are content-addressed and a part never changes | `max-age=31536000, immutable` (R2) |
| `parts/s-{sha}.json.gz` | the SAME list again in the **stable layout** (2026-10-23): rows grouped by the ISO week they were first seen (oldest first; inside a week by `first_seen`, then `source_id`), cut into ~1 MB parts, each `{bucket, jobs: [...]}` with rows as sorted-key JSON. The name is the hash of the part's own bytes, so a part whose rows did not change keeps its name across rebuilds | `max-age=31536000, immutable` (R2) |
| `parts/d-{sha}.json.gz` | the stable layout's removed rows: `{removed: {part url: [row indexes]}}`, the rows of kept parts that left the list or changed since that part was written | same |
| `manifest.json` | tiny, uncompressed: `{generated_at, count, jobs_url, jd_shard_count, jd_url_template, parts: {version, count, total_bytes, attribution, files: [{url, sha256, raw_sha256, bytes, count}]}, stable: {format: 1, version, count, order: "recent", total_bytes, attribution, files: [{url, sha256, raw_sha256, bytes, count, bucket}], removed: {url, sha256, raw_sha256, bytes, count}}}` (`removed` only when rows were removed) | `no-cache` |
| `manifest-history.json` | R2 only, written by the uploader: the part lists of the last 3 manifests, `{manifests: [[url, ...], ...]}`; parts none of them name are deleted | `no-cache` |
| `sponsors/{a..z,0..9,_}.json.gz` | the browser extension's sponsor index (`scripts/build_sponsor_index.py`, built in the same workflow run): `{index_version, version, shard, records: {norm_name: [display_name, h1b_approvals, h1b_last_fy, h1b_first_fy, perm_certs, e_verify, cap_exempt, naics2, state, h1b_5y, denial_pct, flags]}}` (the last three are the 2026-10 compact facts: approvals over the newest five fiscal years, denial rate in whole percent, bit set 1 = H-1B dependent / 2 = willful violator; trailing zeros dropped), ~8 MB in all. Lets the extension badge jobs with no app installed, without sending a company name anywhere | `max-age=86400` |
| `sponsors/manifest.json` | `{index_version, version (content hash), generated_at, employers, stats, shards: {key: {file, bytes, records}}, total_bytes}`; the extension re-checks it at most daily | `no-cache` |

The list holds every row that passes the checks (`JOBS_FEED_LIMIT`, 1,000,000 since 2026-10-23), dated rows older
than **45 days** are dropped (`JOBS_FRESH_DAYS`, unless a crawl saw them in the last 7 days,
`JOBS_SEEN_DAYS`), and rows a crawl no longer sees are pruned: after 7 days unseen when their board
was read in full since, after 21 days unseen otherwise (see "When a row leaves the database" below).
At ~500 bytes a row the full list is ~25 MB uncompressed, ~4-6 MB gzipped.

### The app side (`ui/app.py`, `sourcing/feedclient.py`)

- `JOBS_FEED_URL` is the **base URL** of the feed (e.g. `https://tailor.example/feed`).
- `jobs.json.gz` is downloaded into `<data dir>/feed_cache/` **at most once an hour**, at a
  per-install random minute (drawn once, persisted in `feed_state.json`) so a fleet of installs
  spreads its downloads across the hour. The request carries `If-None-Match`; an unchanged file costs
  one 304. The first run with no cache downloads immediately.
- **The download never runs inside a request** (fixed 2026-10-14). It runs on one background thread
  (started at launch and whenever a request finds the list due), streams to a `.part` file in 64 KB
  reads, and lands by atomic rename; an older cached list keeps serving meanwhile. A link that sends
  nothing for **30 s** (`STALL_TIMEOUT`) or a transfer past **45 min** (`DOWNLOAD_CAP`) is abandoned;
  each download gets **3 attempts** (5 s, 10 s apart) before it counts as one failure for the back-off.
  Every request sends `Accept-Encoding: gzip`: without it Cloudflare inflates the stored gzip on the
  fly and sends the raw JSON (about 18x the bytes, ~180 MB for 96k roles, chunked with no
  Content-Length). The origin ignores `Range`, so a cut-off download restarts from zero.
- **The list downloads in parts** (2026-10-14). The origin ignores `Range` and a flaky link dropped
  the ~10 MB file every 1-3 MB, so it restarted from zero and never landed. Each download now reads
  `manifest.json` first; when it lists `parts`, the client fetches them two at a time into
  `feed_cache/parts/<version>/`, checks each against its `sha256` (or, if a CDN re-encoded it, the
  `raw_sha256` of the JSON inside), and retries a dropped part up to 4 times. A drop costs only the
  part in flight: parts already on disk are never fetched again, across attempts and app restarts.
  When every part is there they are joined into `jobs.json.gz` and swapped in atomically (then the
  parts folder is deleted). A manifest whose version matches the cached list downloads nothing
  (only `generated_at` moves). A new version deletes the old version's parts. A part that 404s
  means the feed was rebuilt mid-download: the next try re-reads the manifest. No `parts` in the
  manifest (or no manifest) falls back to the single file above, so an older feed still works, and
  `jobs.json.gz` is still published for app versions that predate the parts. `feed-status` reports
  `parts_done` / `parts_total`, and `bytes` / `total` count across all parts.
- **Only the parts that changed are downloaded** (2026-10-23). The per-version parts above carry a
  hash of the whole list in their names and are cut by recency, so every 3-hourly rebuild renamed
  all of them and an open app fetched the whole list again (~15 MB at 150k roles, ~60 MB a day).
  When `manifest.json` has a `stable` block the app uses it instead: it keeps the stable parts in
  `feed_cache/parts/store/` after joining them (they are not deleted), fetches only the files the
  manifest names that it does not have, joins the parts minus the rows the `removed` file lists,
  sorts them newest first (`feedfile.recency_key`: posting date, then first seen, then
  `source_id`, undated last; the order `jobs.json.gz` has), checks the total against `count`, and
  deletes the store files the manifest no longer names. Resume, sha256 checks, the 404 re-read and
  the same-version short cut work as for the per-version parts (the state key is
  `stable_version`). Apps from before this read the `parts` block or `jobs.json.gz`, which are
  still written, so nothing changes for them.
- **How the builder keeps parts stable** (`feedfile.write_stable`). The build starts from the
  previous build's stable parts, saved in the crawl's data dir (`.feed-data/feed-prev/`, part
  of the full crawl state the workflow saves in R2 between runs). A previous part is kept as it is (same name, copied into
  the output, since a Pages deploy replaces the whole site) when at most 20% of its rows are dead
  and it is at least half the target size. Its rows that left the list or changed are listed in the
  `removed` file. Changed rows, new rows and the rows of parts that were not kept are laid out by
  first-seen week into new parts. Without the `removed` file, the ~1% of roles that close between
  two builds, spread over every week, would touch every part. With no saved state (the first run,
  a state that held no `feed-prev/`, a file failing its sha256) the build lays everything out afresh: correct, just a
  full download once. The uploader does not resend a part the bucket already has.
- **Measured** (2026-10-23, the live 150k-role list, a rebuild simulated with 1% of rows added,
  1% removed and 1% changed): the per-version parts make a client fetch the whole
  list on every rebuild (13.1 MB for the 130k rows of the 13 parts that downloaded intact); the
  stable layout fetched **0.39 MB** on the first rebuild (2 of 16 files: one new part and the
  removed file). Over 32 simulated rebuilds in a row (4 days) it averaged **1.25 MB** a rebuild
  against 14.4 MB (8.7%), including the rebuilds that rewrite parts past 20% dead (up to 5.7 MB
  once). The removals were spread at random, the worst case; real closures lean to older weeks.
  The stable files also compress smaller (10.2 MB against 13.0 MB fresh) and grow to ~16 MB with
  dead rows before compaction. For an app open 12 h (4 rebuilds) that is ~5 MB a day instead of
  ~60 MB.
  Proven 2026-10-14 against the live 96k-role list on a local origin that cut the connection after
  every 1-3 MB: the parts landed after 6 drops (12.5 MB sent for 9.4 MB of parts); the single file
  failed all 3 attempts.
- While the FIRST download runs (nothing cached yet), `/api/jobs` answers at once with
  `"loading": true` and `"feed": {downloading, bytes, total, ...}`; Home, Jobs and Auto-apply show
  "Downloading today's jobs... 4.2 MB" and poll `GET /api/jobs/feed-status` (memory only, no network)
  until it lands. A failed first download shows one line with a Retry button
  (`POST /api/jobs/feed-retry`, which starts a new download past the back-off).
- `/api/jobs` filters and paginates the cached list with `sourcing/filters.py` -- the same facets the
  kitchen applied (`q, loc, days, remote, visa, level, pay, sort, sponsored, page, per_page`) -- and
  overlays the person's saved bookmarks. It reports `"source": "central"` and `refreshed_at` =
  the file's `generated_at`.
- `/api/jobs/detail` reads the role's JD from its shard (cached locally for a day). If the shard lacks
  it (a Workday row stored list-only before the crawl checked descriptions), it
  asks the company's **own public board endpoint** for that one posting (`fetch_board_jd`), the same
  GREEN-lane API the crawl uses, and runs the accessibility check on it before showing the role.
- **Fallback.** If the download fails and nothing is cached, `/api/jobs` serves the local crawl
  unchanged with `"source": "local"` and `"degraded": true` (the client keeps its last good board and
  retries). If a cached copy exists, it is served even when the origin is down. With `JOBS_FEED_URL`
  unset -- or still the `https://tailor.example/feed` placeholder (`.example` is an RFC 2606 reserved
  TLD that cannot resolve) -- the app is a plain local install and is **not** flagged degraded.
- Profile, CV and application data never touch any of this. The feed is public job postings only.

### What a fresh install sees

A fresh install has an empty local store and no cached list. Two things keep its Jobs page from
sitting on "0 live roles" -- or from crawling 1,105 boards from the person's laptop (one brisk run of
that got an IP barred by Workable for 19 hours, and during a feed outage every new install would have
done it at once):

1. **The live feed.** With a real `JOBS_FEED_URL`, the live list starts downloading in the background
   at launch; the board shows the download's progress meanwhile and reloads when the list arrives. From then on
   the hourly, ETag-conditional download keeps it fresh, and if the origin is down the last download
   keeps serving.
2. **The polite fallback crawl**, only when the feed is genuinely absent: `JOBS_FEED_URL` unset or the
   placeholder (nothing will ever fill the board otherwise), or a configured feed whose downloads have
   **failed for 30 minutes** (`StaticFeed.failing_for`, persisted in `feed_state.json`) with **nothing
   cached at all**, or one that has served an empty list for that long. One failed download never
   starts a crawl. The crawl itself is `sourcing.service.first_open_refresh`: the top **300** boards
   (`JOBS_FIRST_CRAWL_BOARDS`) ranked by the company's H-1B approvals with a few of every ATS, one
   request at a time with per-host spacing from `sourcing/growth.PoliteFetch` (Workable on its slow
   lane, all Workday tenants on one lane, a 429 backs the host off and a cooling host is skipped), a
   **10-minute** wall-clock cap (`JOBS_FIRST_CRAWL_MINUTES`), and **20** descriptions per Workday
   board (`JOBS_FIRST_CRAWL_DETAIL`). It runs once per process (retried after a
   back-off if it fails); `JOBS_FIRST_CRAWL=0` switches it off. The in-app auto-updater
   (`RESUME_AGENT_AUTOUPDATE`) is unchanged.

The `/api/jobs` response says `"crawling": true` while the fallback crawl runs, and the client
re-polls until rows arrive.

## Costs

- **Cloudflare R2 free tier**: 10 GB storage, 1M Class A (writes) and 10M Class B (reads) operations
  a month, **zero egress fees**. One crawl every 3 hours uploads ~257 objects = ~62k writes/month.
  Reads: each install makes at most 24 conditional list requests a day plus one shard per role opened;
  10,000 daily users is roughly 7-8M Class B operations a month, still inside the free tier. Serving
  through a Cloudflare custom domain puts the CDN cache in front, which cuts R2 reads further.
- **GitHub Actions**: ~8 runs a day. Public repos get unlimited minutes; a private repo's 2,000
  free minutes/month covers runs of up to ~8 minutes each. Since 2026-10-30 the workflow downloads
  **nothing** from the Actions cache (see "The crawl state"): the account's cache egress limit is
  what cut the 2026-10-30 18:06 run off from its state.
- **The crawl state in R2**: the newest 5 saved states, each ~0.4 GB full + ~0.05 GB slim, so about
  2.3 GB of the 10 GB free storage; ~6 writes and ~12 reads a run (Class A / B), and R2 has no
  egress fees.
- **Aggregator keys**: JSearch (RapidAPI) is used once per run centrally, never per user.

## Aggregator terms (what may go in the public file)

Checked 2026-09-30. `FEED_INCLUDE_AGGREGATORS` (a repository *variable*, comma list; default
`jsearch`) governs the **keyed** aggregators. Keyless feeds whose notices invite sharing are always in.

| source | terms | in the public file? |
|---|---|---|
| Company boards (Greenhouse, Lever, Ashby, Workable, Recruitee, Workday) | public, keyless board APIs published for exactly this (CLAUDE.md §6) | **yes** |
| **SmartRecruiters** | [`api.smartrecruiters.com/robots.txt`](https://api.smartrecruiters.com/robots.txt) reads `User-agent: LinkedInBot / Allow: /v1/companies/ / User-agent: * / Disallow: /`: every robot but LinkedInBot is disallowed from the whole API (checked 2026-10-19) | **not used.** No code path requests that API (`sourcing.ats.DISABLED_ATS`; pinned by `tests/test_smartrecruiters_disabled.py`). See "SmartRecruiters is switched off" below |
| **freehire.me** | [Terms](https://freehire.me/terms): "no scraping beyond our documented API, no attempting to bypass rate limits or authentication". The terms do not require attribution and are silent on caching and redistribution; the [API docs](https://freehire.me/docs/api) describe it as free and open-source (MIT, [strelov1/freehire](https://github.com/strelov1/freehire)). We use only the documented API, within its published rate limits | **yes.** No credit is shown in the UI or the feed file's `attribution` block (none is required); rows link only to the employer's own posting. The search-by-sponsor pass switches off with `FREEHIRE_SPONSOR_PASS_REQUESTS=0` |
| **Remotive** | the API response's own `0-legal-notice`: "API documentation and access is granted so that developers can share our jobs further. Please do not submit Remotive jobs to third Party websites [job aggregators] ... Please link back to the URL found on Remotive AND mention Remotive as a source" (https://remotive.com/api/remote-jobs) | **yes**, with attribution; the file's `attribution` block carries the credit |
| **Remote OK** | the API's first array element: "Please link back (with follow, and without nofollow!) to the URL on Remote OK and mention Remote OK as a source ... Please don't use the Remote OK logo" (https://remoteok.com/api) | **yes**, with attribution |
| **JSearch** (RapidAPI, publisher OpenWeb Ninja) | [OpenWeb Ninja Terms](https://www.openwebninja.com/terms): "we additionally grant you a non-exclusive, non-transferable license to use, reproduce, and commercially exploit such API Data in your own products, services, applications, or business, including for resale or redistribution as part of a broader product offering, provided that you do not resell, sublicense, or redistribute API Data as a standalone data or API product that substantially replicates the Services themselves." RapidAPI's platform policy defers to the provider's terms. | **yes by default** (`jsearch`); our feed is part of the app, not a standalone data product |
| **Hacker News "Who is hiring?"** (2026-10-30) | Read through the HN search API Hacker News links to (`hn.algolia.com/api/v1`, keyless; its robots.txt answers 404 = no rules). [Y Combinator's site terms](https://www.ycombinator.com/legal/) reserve HN content: "you agree not to display, distribute, license, perform, publish, reproduce, duplicate, copy ... for any commercial purposes, any portion of the Site" unless authorized; the official [HN API](https://github.com/HackerNews/API) makes the data available but grants no redistribution licence | **no, off by default** (2026-10-30). Built and tested, but not crawled unless `HN_WHOISHIRING=1`: the terms restrict commercial use and it adds only ~15 sponsor rows a month. When on, a row carries only facts from the post's first line (employer, role, location, work mode, stated pay, sponsorship signal), a description WE write from them and a link to the post (its `url`); the poster's text is never copied. Its "via Hacker News" credit is sent with its rows (the feed file's `attribution` block, `/api/jobs` `attribution`), so nothing names Hacker News while it is off |
| **Adzuna** | [Developer Terms of Service](https://developer.adzuna.com/docs/terms_of_service): beyond a 14-day trial, use by "a commercial, government or academic organisation ... may not be used in its original format or in aggregation ... to deliver any ongoing work or research ... without written consent"; also requires a "Jobs by Adzuna" label on every displayed advert, and the API returns only a ~500-char preview | **no.** `build_feed.py` refuses `adzuna` even if listed (and `sourcing/service.py` already prunes it). Needs written consent from Adzuna first. |

Remotive's notice also mentions a daily request budget (~4/day) and a 24-hour listing delay; the crawl
makes one Remotive request per title term per run (4 terms, 8 runs a day), which is above that
budget. If Remotive objects, `FEED_INCLUDE_AGGREGATORS` is not the lever (Remotive is keyless): drop
it from `PUBLIC_OK_AGGREGATORS` in `scripts/build_feed.py`, or run the crawl less often.

Attribution: `jobs.json.gz` carries an `attribution` map for every aggregator present, and the
Jobs page prints a muted "via Remotive" / "via Remote OK" on each such row (and "via Hacker News"
when that opt-in source is on; its credit comes with its rows in `/api/jobs`'s `attribution`) (a fact on the card, a
plain `rel="noopener"` link to the listing in the detail view, never `nofollow`; `VIA_SOURCES` in
`ui/static/app.js`).

### Hacker News "Who is hiring?" (`sourcing/hackernews.py`, 2026-10-30)

**Off by default.** Nothing below runs unless `HN_WHOISHIRING=1` in the environment of the feed
build or the app (`.github/workflows/feed.yml` does not pass it today, so the public feed cannot
turn it on by accident). Why: Y Combinator's site terms restrict commercial use of
HN content (see the terms row above), and the measured yield is small (~15 sponsor rows a month).
While it is off: the crawl makes no HN request (`refresh_watchlist` step 3c), `build_feed`
does not list it among the feed's aggregators, `backend.feed.feed_jobs` leaves out any HN rows a
run with it on stored, the feed file's `attribution` names only sources whose rows are present,
and the Jobs page has no built-in HN credit (it takes credits for opt-in sources from
`/api/jobs`'s `attribution`, which `ui/app.py` fills from the rows on the page). Turning it on
needs a decision on the terms first (written permission from Y Combinator would settle it).

Each month the `whoishiring` account posts "Ask HN: Who is hiring? (Month Year)" and employers
answer with one top-level comment each (the October 2026 thread: 261 posts). Many are US startups
that do not use one of our ATS boards, and the thread's convention is a first line like
`Acme (YC S24) | Senior Backend Engineer | San Francisco, CA | ONSITE | VISA | https://acme.com`.

* **Requests.** Two, through the HN search API: `search_by_date?tags=story,author_whoishiring`
  finds the newest "Who is hiring?" story (not "Who wants to be hired?" or "Freelancer?"; none
  older than 45 days), then `items/{id}` returns the whole thread in one answer (~0.5 MB). The
  answer is cached beside the database (`hn_whoishiring.json`, in the feed build's saved crawl
  state) and reused for `HN_CACHE_HOURS` (6), so the 3-hourly build asks at most 4 times a day. A
  failed refresh serves the older cache. The Firebase HN API is not used: it needs one request per
  comment. The step runs once per refresh (`service.refresh_watchlist` step 3c, like the freehire
  bulk pull, not once per title term), on the first crawl shard only, and only with
  `HN_WHOISHIRING=1`.
* **Parsing.** The header is split on `|` (else an em dash or " - "): the first part is the
  company (parentheses such as "(YC S24)" dropped, or read as the location when they name one),
  then a role, a location, a work mode (REMOTE / ONSITE / HYBRID), pay and a VISA tag are picked
  out by their words. A post that names no role in its first line gets the first role phrase in
  its body ("We're hiring Senior Platform Engineers"); a comment with neither is not a job post
  and is skipped (complaints, replies, deleted comments). Pay is kept only as a yearly USD range.
* **Sponsorship signal** (`sponsorship_stated`). True for a VISA tag, "visa sponsorship", "we
  sponsor visas", "H-1B sponsorship"; False for "no visa", "not able to sponsor", "no
  sponsorship" (a no beats a yes: "H-1B transfers welcome; no new visa sponsorship" is False);
  None when silent. A post requiring US citizenship or a clearance is dropped.
* **Facts only.** See the terms row above: the description is our own sentences ("Acme is hiring:
  Senior Backend Engineer. Location: ... The post says visa sponsorship is available. ... Read the
  full post and how to apply on Hacker News: <link>"), and the row's `url` is the post. The
  sponsorship sentence uses words `sourcing/adstance.py` reads, so the feed's existing gate
  applies unchanged: an HN row is published only with a sponsor badge (the employer is in the
  H-1B / PERM data and the role is in the US) or a stated yes, and never with a stated no.
* **Dedupe.** A post from an employer whose own board is on the watchlist is dropped (the board
  has the same roles with full descriptions; matched on the normalized name, "Anthropic" =
  "Anthropic Pbc"), and the usual cross-feed dedup (`sourcing/dedup.py`) runs on the rest, board
  rows first.
* **Measured on the October 2026 thread** (offline, from one recorded read): 261 posts -> 222 rows
  -> 115 placed in the US -> 5 from employers we already watch -> about 15 pass the sponsor gate
  (8 posts state sponsorship, 11 state they will not). With it switched on, expect roughly 15-30 new feed rows a month,
  mostly US startups with a stated VISA or an H-1B record, each linking back to its post.

### freehire: search by sponsor (`sourcing/freehire_sponsors.py`)

freehire reaches the feed three ways, all through its documented, keyless API
(`https://freehire.me/api/v1`, [docs](https://freehire.me/docs/api),
[OpenAPI](https://freehire.me/openapi.yaml)), US only and posted within `FREEHIRE_MAX_DAYS` (14):

1. **Sponsor slice** (`service.refresh_watchlist` step 3b): the newest `FREEHIRE_SPONSOR_ROWS`
   (2,000) rows whose posting states sponsorship (`visa_sponsorship=true`).
2. **General bulk**: off (`FREEHIRE_GENERAL_BULK=1`).
3. **Search by sponsor** (since 2026-10-18), shard 0 only. Any one freehire query reaches only its
   newest ~10,000 rows (`offset + limit` may not pass 10,000), and most postings never say whether
   they sponsor, so slice 1 misses the bulk of roles at real H-1B employers (about 9% of fresh US
   freehire rows). This pass asks for them by employer:
   - **Which employers.** The committed sponsor seed, summed per employer, ranked by H-1B approvals,
     still filing within 2 years of the newest year, top `FREEHIRE_SPONSOR_PASS_EMPLOYERS` (4,000;
     roughly 50+ approvals). Employers our own crawl covers (a watched board, active or not, or a
     career site) are skipped: their rows come from the employer directly.
   - **Matching names.** One company search (`GET /companies?q=`) per employer finds its freehire
     company slugs; a slug is kept only when its name is the same employer (`names_match`: Inc /
     LLC / Corp / punctuation / "&" ignored, "JP Morgan" = "JPMorgan", a longer name only extends a
     shorter one of 2+ words, and a curated brand such as Google or Deloitte spans its entities). A
     lone generic word never stretches ("Nowhere" is not "Nowhere Partners"). Answers are cached
     for 30 days in `freehire_sponsor_slugs.json`, so later laps spend requests on jobs.
   - **Jobs.** `GET /agent/jobs/search` with up to 10 `company_slug` values ORed, `countries=US`,
     newest first, paged by 100.
   - **Kept only when** the row is US, has no citizenship / clearance bar
     (`is_intl_student_accessible`), its ad does not say no (`adstance`), and it carries an
     employer sponsor badge after `SponsorDB.tag_jobs` (`is_sponsor_relevant`). Then cross-feed
     dedup (`sourcing/dedup.py`) against everything stored, so a board's own copy always wins.
   - **Budget and politeness.** At most `FREEHIRE_SPONSOR_PASS_REQUESTS` (200; 0 = off) requests
     per run, company searches included. The API publishes its limits (300/min for the agent
     search, 600/min for the rest): the pass waits for `X-RateLimit-Reset` when
     `X-RateLimit-Remaining` drops to 10, honours `Retry-After` on a 429 and stops for the run on
     a second 429 or 3 failures in a row. It also stops if the API reports it ignored a parameter
     (`meta.ignored_params`), since an unknown filter is dropped, which would turn a company query
     into the whole catalogue. Same identifying User-Agent as the rest of the crawl, 15 s timeouts.
   - **Rotation.** `freehire_sponsor_cursor.json` remembers the next employer by name; a run
     resumes there, and an employer whose search did not finish is retried first. Both files travel
     in shard 0's delta, so the merge job keeps them.
   - **Links and source.** A row links only to the ORIGINAL employer posting (freehire's `url`
     field); a row whose URL is missing or points at freehire.me is dropped (this applies to every
     freehire path, `ats.freehire_employer_url_ok`). Rows keep `source: "freehire"` internally for
     dedup and debugging; no freehire credit is shown in the UI (its terms do not require one).
   - **Switching it off.** `FREEHIRE_SPONSOR_PASS_REQUESTS=0` (a repository variable).

   **Short proof run (2026-10-18, 20 requests, against a local copy of the 113,361-row public
   feed):** 6 employers looked up (Cognizant, TCS, Google, Deloitte, Apple, Capgemini), 1,200 rows
   fetched, 77 dropped by the sponsor / ad / clearance rules, 22 already held (5 by the same
   freehire id, 17 cross-feed duplicates), **1,101 new sponsor rows** (Google 673, Capgemini 264,
   Apple 163). The top of the list is the densest, so later requests find fewer rows each.
   Expect the first lap of 4,000 employers to take about 23 runs (~3 days at 8 runs a day: one
   company search per employer plus ~600 job pages), adding an estimated 20,000-40,000 sponsor
   roles; after that the slug cache leaves the whole budget for job pages, a lap takes about 4 runs,
   and each run mostly refreshes known rows and adds that day's new postings.

### Workday boards (`sourcing/workday.py`)

The biggest H-1B sponsors (NVIDIA, Intel, Cisco, Target, Adobe, Visa, US Bank...) hire through
Workday, and every Workday career site exposes the same public, keyless JSON its own page reads:
`POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` for the list (20 a
page, by offset) and `GET .../wday/cxs/{tenant}/{site}{externalPath}` for a posting's description.
No login, no key, no proxy, no browser pretence: the crawl identifies as `resume-agent/1.0`, reads
the tenant's `robots.txt` (which names the career site and is how discovery finds it) and leaves a
site alone when that file disallows it. Requests to one tenant are spaced, 429/5xx back off and
honour `Retry-After`, every request has a hard timeout, and each board is capped per refresh
(`WORKDAY_MAX_JOBS`, default 1000 rows, 25,000 in the feed, where a board past Workday's 2,000-row
list ceiling is split by its own facets, see "Giant Workday boards" below; `WORKDAY_MAX_DETAIL`,
default 200 descriptions per board on a desktop refresh, `WORKDAY_MAX_DETAIL_FEED`, default 2000,
600 in the feed workflow, for the central build). A
board is `company,workday,{tenant}.wd{n}/{site}` in the watchlist, e.g.
`nvidia.wd5/NVIDIAExternalCareerSite`; discovery adds one only after the site's own hiring
organization or site name confirms the sponsor's brand word and the list returns at least one job.
Applying on Workday is **ASSISTED** only: there is no applicant-usable submission API, so the
allowlist (`submit/allowlist.py`) is unchanged and the person clicks submit.

### Growing the watchlist (`scripts/grow_watchlist.py`, `sourcing/discover.py`)

Every board on `seed/watchlist_seed.csv` is a proven H-1B (or PERM) sponsor with a live public feed. The list
grows by probing the sponsors in `seed/sponsors_seed.csv.gz`, biggest recent H-1B filers first, on
the GREEN-lane ATS APIs, and keeping a board only when its own name (or a Workday posting's hiring
organization, or failing both its own job descriptions) confirms it is that employer and it lists
at least one job:

    python scripts/grow_watchlist.py --ipv4 --limit 4000 --max-minutes 180 --workers 16 \
        --ats greenhouse,lever,ashby,workday

It checkpoints every employer to `data/grow_watchlist.jsonl`, so a stopped run continues where it
left off; `--ipv4` is for networks whose IPv6 is broken (each Ashby call otherwise
waits out two timeouts). Workable is left out of the big runs: its edge answers a whole day's
`Retry-After` once an address has used its budget, and the list holds one Workable board.

How a guess is made and checked (2026-10-12; the 2026-10-11 dry run accepted 2 of 185):

* **Slugs**: the leading word, a curated abbreviation (`ALIASES`: IBM, HCL, PwC, AMD, TCS, EY,
  Capital One, AT&T...), the brand words joined without filler (`thermofisher`), the name with its
  trailing filler cut, the first two words joined (`goldmansachs`), the whole name joined, and a
  brand word that is not first (`schwab`). Ashby and Lever show no employer name, so only joins that
  cover every brand word are tried there.
* **Identity**: descriptive words (design, companies, consultancy...) need not appear on the board
  ("Cadence" confirms Cadence Design Systems), but a brand qualified as another employer is turned
  down ("Cadence Bank", "Apple Bank", "Ford Foundation", "Hitachi Energy", "Lowes Foods"), and a
  truncated multi-brand name still needs two brand words ("Archer" is not Archer Daniels Midland).
  A Workday site whose postings name no organization is confirmed by at least two capitalized,
  unqualified mentions in its own JDs; a posting that names a different legal employer is never
  overridden that way (Google Operations Center's site is not Google LLC's board).
* **Workday**: instances are tried most-used first (wd1, wd5, wd3, wd12, wd103, wd10), the newer
  ones (wd102...wd504) only for the most likely slug; the site names come from `robots.txt`, and a
  tenant whose robots.txt names none but forbids none is asked a few common site names. A site
  robots.txt disallows is never read. Each Workday instance is its own pacing lane (one request per
  0.25 s per instance across all threads); every lane honours `Retry-After` and backs off on 429/5xx.

**Results of the 2026-10-12 runs** (top 4,036 sponsors by recent H-1B approvals, minus those already
watched; about 3 hours of crawling in all, ~40 employers a minute with 16-24 threads, 0 to 3 HTTP 429s
per run):

| | before (old code) | after |
|---|---|---|
| same top 50 sponsors | 1 board found, 0 new | 6 found, 4 new (all Workday) |
| 2026-10-11 dry run / 2026-10-12 full run | 2 of 185 (1.1%) | 317 of 3,872 probed (8.2%) |

Every hit was re-checked against the final identity rules before it was written, which dropped 25
wrong ones the first rules let through (Tower Research Capital for American Tower, Paper Education
for International Paper, Fusion Medical Staffing, Lowell Group, boards named Space, Foundry, Branch,
Nexus...), and four bare one-posting boards were turned down by hand (Allegro, Athene, Southwest x2,
Raven). Confirmed hits by ATS: Workday 274, SmartRecruiters 25, Greenhouse 18, Lever and Ashby 0
(only whole-name joins are trusted blind there). New boards on the list: **268** (Workday 237,
SmartRecruiters 18, Greenhouse 13; the rest of the hits were boards already watched under another
legal entity), taking the watchlist from 1,107 to **1,375** boards and Workday from 118 to 355
tenants. 195 employers still owe a probe on one ATS (the checkpoint says which); the next run picks
them up. The big sponsors without a public board stay out (Cognizant, TCS, Google, Microsoft, Apple,
Wipro, Infosys, Deloitte, Meta...): they use their own career sites, which the career-site lane
covers. Reaching ~3,000 boards needs the sponsors past rank 4,000, where the hit rate keeps falling
(12% in the top 50, ~5% around rank 3,000).

### Growing toward sponsors: PERM, cap-exempt institutions, blocks and robots (2026-10-18)

The same script now grows the list in bounded batches (default `--limit 300`) with four changes:

* **Ranking.** Score = H-1B approvals (recent years weighted, as before) + certified PERM
  applications (`PERM_WEIGHT` 1.0; PERM-only employers are now candidates too), times
  `CAP_EXEMPT_BOOST` (3.0) for a likely cap-exempt employer. `--no-cap-boost` turns the boost
  off; `--cap-exempt-only` probes only those employers.
* **Cap-exempt employers.** `growth.cap_exempt_likely`: the name reads as a university, college,
  hospital, medical center or research institute (the app chip's own rule,
  `sponsors.looks_cap_exempt`), or the seed flags it AND the name has no for-profit legal form AND
  it is NAICS 61/62 or calls itself an institute, clinic, foundation or laboratory. The seed flag
  alone is not trusted: it is the MAX over every name variant, so it is set on Microsoft, Meta
  and Snap. K-12 school districts get no boost (they are not cap-exempt). Universities used to be
  skipped outright: "University Of Michigan" is a generic word plus a place, so it had no brand
  word to check a board against. Now `discover.institution_alias` makes the WHOLE institution
  phrase the brand ("university of michigan", filing wrappers like "Regents Of The" and
  abbreviations like "Univ" cleaned off), with slug guesses from a curated short-form table
  (`INSTITUTION_SHORT`: umich, upenn, uchicago, psu, usc, wustl...), the acronym, and "u" + a
  one-word place. The phrase is never reordered, so "University of Washington" never passes for
  Washington University in St Louis, nor "Loyola University Chicago" for the University of
  Chicago. A name made only of descriptors ("Childrens Hospital") names no one and is skipped. A
  university's student-jobs Workday site is not a board for outside applicants and is skipped.
* **Blocked hosts.** A host that answers 401 / 403, a bot-challenge page, or non-JSON where JSON
  was expected is marked blocked and never asked again: a Workday tenant at once and for good
  (its host is written to the employer's checkpoint line as `blocked_hosts`, and the next run
  starts with every blocked host from the checkpoint), a shared ATS API after two refusals and
  for the rest of the run (its ATS is owed by the employer, never recorded as a miss). 429s keep
  the existing back-off and cooldown. Nothing tries to get past a block.
* **robots.txt.** Each ATS API's robots.txt is read once per run and obeyed, Crawl-delay
  included (Lever asks for 1 s). **SmartRecruiters' `api.smartrecruiters.com/robots.txt`
  disallows everything to everyone but LinkedInBot, so discovery no longer probes
  SmartRecruiters at all**, and since 2026-10-19 neither does the feed or the app (see
  "SmartRecruiters is switched off" below).
  Greenhouse (only `/embed/` disallowed), Lever, Workable allow it; Ashby's API answers 401 for
  robots.txt, which RFC 9309 reads as "no robots file". A robots.txt that cannot be read (429,
  5xx, timeout) owes the request rather than guessing (see "robots.txt status codes" below). Workday tenants' own robots.txt was already
  read and obeyed per tenant.
* **Evidence.** Every verified board gets a line in `seed/watchlist_evidence.csv` (`ats,
  board_id, company, url, jobs, verified, cap_exempt, h1b_approvals, perm_certs, source`), merged
  by board (`source`, added 2026-10-30, names where a hand-added board came from, e.g.
  `remoteintech`; blank for boards discovery found). The seed itself keeps its three columns. The app needs no new field for the cap-exempt
  chip: it already derives "Cap-exempt (likely)" per employer at serve time
  (`SponsorDB.lookup` -> `CAP-EXEMPT` badge, `ui/static/app.js`), so the evidence flag is a
  record for maintainers, not a second source of truth.

`--count-us` reads each NEW board's list once after the run and counts its US jobs with the
feed's own `looks_us` rule (a Workday row listed as "3 Locations" is not counted, so it undercounts).

Universities list a building or campus as the location ("Franklin Building", "Penn State University
Park"), which `looks_us` cannot place, so their roles were dropped as "not US". `workday_detail` now
reads the posting's own `country` and appends ", United States" when it is the US, and
`fill_details` lets that replace a location the US rule could not read.

**Sample run, 2026-10-18** (`--limit 50 --cap-exempt-only --count-us`, all ATSs but Workable; 6.2
minutes, 1,427 requests, 0 x 429): 48 employers probed, 2 skipped (Battelle: clearance-heavy;
Childrens Hospital: names no one), **11 boards verified, all Workday, all universities**: Maryland,
Penn, Chicago, Penn State, USC, Virginia, Texas A&M, UT Austin, Washington, Ohio State, WashU. The
Virginia hit was its student-jobs site, so the rule now skips student sites and it was dropped,
leaving **10 new boards with 6,275 listed jobs** (3,104 placed in the US from the list alone; most of
the rest are campus buildings the country fix above places). Turned down correctly: Indiana
University's tenant "iu" (IU Dortmund GmbH), boards named "Maryland", "Virginia", "Virtual
Preparatory Academy of Oklahoma", "Lincoln Property Company". Yale and Arizona State run Workday but
their robots.txt disallows the career sites, so they were left alone. One tenant answered 401
(nyu.wd503) and is now blocked. SmartRecruiters was left out by its robots.txt.

### The crawl state (R2, 2026-10-30)

The crawl's persistent state (the jobs DB with every description, the sponsor overlay, the
cursors, the last build's stable parts) is what makes `first_seen`, the cross-run dedup and the
prune work. Until 2026-10-30 it lived in the GitHub Actions cache, and **each of the five jobs**
(four crawl shards + the merge) downloaded the whole ~377 MB entry, plus a ~243 MB pip cache: about
**3.1 GB of Actions-cache downloads a run**, ~25 GB a day. On 2026-10-30 the account's cache egress
limit refused the merge job's restore ("Egress is over the account limit"), the merge built from an
empty DB and published 158,535 jobs instead of ~233,000, and saved that as the next base. PR #149
made a missing state fail the run; this change removes the dependency.

**Where it lives.** A **private** R2 bucket named by the repository variable `R2_STATE_BUCKET`,
under `R2_STATE_PREFIX` (default `state`). Not the feed bucket: an R2 custom domain serves every key
of its bucket, so `state/` in `sponsorjobs-feed` would be public at `feed.sponsorjobs.ai/state/...`
(`scripts/feed_state.py` refuses `R2_STATE_BUCKET` = `R2_BUCKET`). The same `R2_*` secrets are used,
so the API token must cover both buckets. Layout (`scripts/feed_state.py`):

| key | what |
|---|---|
| `state/full/<id>.tar.zst` | the merge job's whole data dir after a good build |
| `state/slim/<id>.tar.zst` | the **shard state**: the jobs DB with each stored description replaced by a marker (`JD_HELD`), plus the cursor / slug JSON files; no sponsor overlay (shard 0 rebuilds it from the committed seed, which is all the saved copy ever held) and no `feed-prev/` |
| `state/runs/<id>.json` | that state's manifest: `{format, id, created_at, rows, feed_count, full: {key, bytes, sha256}, slim: {...}}` |
| `state/latest.json` | a copy of the newest good manifest: what a run starts from |

`<id>` is the merge run's `github.run_id` (`seed-<cache key>` for a bootstrap copy).

**Each run.** Each crawl shard downloads only the **slim** state, crawls, and hands the merge its
delta plus the id of the state it started from (`base-<n>.txt`). The merge job downloads the
**full** state with that same id (the latest if the shards disagree), folds the deltas in, builds
the feed, runs the publish guard, then saves the next state: both archives, the run manifest, and
`latest.json` **last**, so `latest.json` never names an archive that is not there. It then deletes
all but the newest **5** saved states, never the one `latest.json` names. A shard never reads a
stored description's text (it only asks whether a row has one), so the slim marker changes nothing
it decides; the merge keeps the full state's description for every delta row that carries the
marker (`build_feed.merge_delta`) and re-runs the salary backfill for those rows, which a shard on
the full state would have done from the text. A test (`tests/test_feed_state.py`) runs the same
shards on the full and on the slim state and checks the two merges publish the same feed and
store the same rows.

**What a run downloads** (389,256 rows in the DB, 2026-10-30):

| | before | now |
|---|---|---|
| Actions cache | 5 x (377 MB state + 243 MB pip) = **~3.1 GB** | **0** |
| R2 (no egress fees, no account limit) | 0 | full state ~380 MB (merge) + 4 x slim ~40 MB = **~540 MB** |

The slim size is an estimate (the live rows of the published list, ~85 bytes a row compressed,
scaled to the DB's 389k rows: ~185 MB as a file, ~35-45 MB compressed); each run logs the real
sizes (`[feed_state] uploaded slim state ...`).

`pip` now downloads its wheels from PyPI (no `cache: pip`), which costs a little install time and
no cache egress. Nothing is saved to the Actions cache any more either, so the ~10 GB of old
`feed-data-*` entries stop growing and age out by themselves (GitHub evicts entries unused for 7
days); nothing deletes them.

**A missing state stops the run** (as with PR #149's `fail-on-cache-miss`): with nothing in R2, or
an archive failing its sha256 or its row count, the shard or the merge fails and nothing is
published or saved. The live feed and the saved state stay as they were.

**The publish guard.** After the build, `feed_state.py guard` compares the new feed's count with
the live `manifest.json` (`TAILOR_FEED_URL/manifest.json`; if that cannot be read, the `feed_count`
the restored state recorded). Under **80%** of it, the run fails before saving the state or
uploading anything. The workflow input `allow_drop` publishes anyway (a real drop, e.g. a source
switched off on purpose).

**Bootstrap and recovery** (Actions -> feed -> Run workflow):

- **First R2 run (once, after this change):** `seed_from_cache` = the last good Actions-cache key,
  e.g. `feed-data-41039418699` (the 2026-10-30 10:15 run, 233,496 jobs published). A `seed-state`
  job restores that one entry (one ~377 MB cache download), saves it to R2 as the latest state
  (full + slim), and the run then crawls and merges from it as usual. The key must still be in the
  cache (Settings -> Actions -> Caches lists them; entries unused for 7 days are evicted). Do not
  pick `feed-data-41103949680`: that is the 158,535-job state the failed restore saved.
- **First-ever run, nothing saved anywhere:** `allow_empty_state` = true lets the shards and the
  merge start from an empty DB; add `allow_drop` if a feed is already live (the guard would refuse
  the smaller first build).
- **Roll back to an earlier state:** `state_id` = one of the ids under `state/runs/` (the newest 5
  are kept). The shards and the merge start from it; the run then saves a new latest as usual.
- **By hand** (a machine with the R2 keys and `pip install boto3`; `R2_STATE_BUCKET` and the
  `R2_*` keys in the environment):

  ```sh
  python scripts/feed_state.py pull --kind full --dest .feed-data           # the latest state
  python scripts/feed_state.py pull --kind full --dest .feed-data --id 41039418699
  python scripts/feed_state.py push --data .feed-data --id manual-2026-10-31   # becomes the latest
  ```

**Set-up (once, before the first run of this workflow version):**

1. Cloudflare -> R2 -> **Create bucket** `sponsorjobs-state`. Leave **Public access** off: no
   custom domain, no r2.dev URL.
2. R2 -> **Manage API tokens** -> the feed workflow's token -> **Edit** -> add `sponsorjobs-state`
   to its buckets (Object Read & Write). Same keys, nothing to change in GitHub secrets.
3. GitHub -> Settings -> Secrets and variables -> Actions -> **Variables** -> `R2_STATE_BUCKET` =
   `sponsorjobs-state`.
4. Actions -> feed -> Run workflow with `seed_from_cache` = `feed-data-41039418699`.

Until step 3 is done, every run stops at its first step with "set R2_STATE_BUCKET" and the live
feed stays as it is.

### How often each board is crawled

The build crawls boards in watchlist order from a cursor saved under `--data`, stopping at
`FEED_CRAWL_MINUTES` (140 by default since 2026-10-12) and resuming there next run. The cursor
remembers the next board itself, so a seed sync that inserts boards does not shift it. The
2026-10-12 run covered 376 boards in 70 minutes, so a 140-minute run covers roughly 750; at 8 runs a
day that is about 6,000 board visits. The 1,375-board list is walked end to end in 2 runs (every
board about 4 times a day), and even a 3,000-board list within 4 runs (about 12 hours, twice a day),
so no priority ordering is needed yet; if the list outgrows ~6,000 boards, the high-volume boards
should be visited every run and the long tail rotated.

**Two crawl shards (2026-10-13).** Real runs reached far fewer boards than that estimate (about
150 of 1,375 per run), so the workflow now crawls in two parallel jobs. `board_shard` in
`scripts/build_feed.py` puts each board in shard 0 or 1 by a stable hash of `(ats, board_id)`
(so a board never moves between shards, and new seed boards split evenly). Each shard job restores
the saved state read-only (since 2026-10-30 the slim shard state from R2, see "The crawl state"), crawls its half from its own cursor (`crawl_cursor.s0of2.json`,
`crawl_cursor.s1of2.json`; the first sharded run starts from the old `crawl_cursor.json`), and
uploads only the rows it touched (`--export-delta`). Shard 0 alone runs the career-site lane and the
aggregator APIs, so nothing is fetched twice; Amazon is a board and lands in one shard. The
`build-and-upload` job folds the deltas into the saved DB (`--merge-dir`: newer `last_seen` wins,
earliest `first_seen` kept), prunes, writes the feed (cross-feed dedup and `JOBS_FEED_LIMIT` as
before), saves the state and publishes once. If one shard fails, the merge still runs: that
shard's boards keep the rows already saved, and its cursor does not move, so the next run retries
them. If both fail, the merge refuses to publish and the last feed stays up. Locally nothing
changes: `FEED_SHARD_INDEX` / `FEED_SHARD_COUNT` default to 0 / 1, the old single build.

**Three crawl shards (2026-10-18).** The 2026-10-18 16:00 run (the last one whose merge finished)
reached 475 of 698 boards on shard 0 and 354 of 677 on shard 1 in the 140-minute cap: about 830
boards a run, so the 1,375-board list took ~1.7 runs (~5 hours) per lap. Shard 1 is slower per board
because Workday detail reads dominate; shard 0 also carries the career-site lane. To absorb the
discovery growth the matrix is now `[0, 1, 2]` with `FEED_SHARD_COUNT: 3` (a test keeps the two in
step): ~460 boards a shard, so one run covers about the whole list today, and a list of ~2,500
boards still laps in ~2 runs. Changing the count moves boards between shards (the hash is taken
mod the count); each new shard's first run starts from the old single-job cursor
(`crawl_cursor.json`), and the `s*of2` cursor files are simply no longer read. The merge folds three
deltas instead of two (shard 0's delta is the big one, ~110k rows, because of the career-site
lane), well inside its 60 minutes. If the list passes ~3,500 boards, the next step is a fourth
shard or visiting the high-volume boards every run and rotating the long tail; nothing in the data
needs priority ordering yet (every watched board is a verified sponsor).

**Four crawl shards (2026-10-23).** The 2026-10-23 11:51 run finished every board on all three
shards, but close to the 140-minute cap: shard 0 422 boards in 7,671 s (128 min, with the
career-site lane and the freehire pass), shard 1 401 in 6,669 s, shard 2 409 in 7,220 s; the merge
took 12 minutes. The split Workday listing below adds up to 10 minutes per giant board, so the
matrix is now `[0, 1, 2, 3]` with `FEED_SHARD_COUNT: 4` (~310 boards a shard, about 95 minutes of
board pass at today's pace). The career-site lane and the freehire pass stay on shard 0 only (the
lane has its own 130-minute cap and runs beside the board pass, so the shortened board pass does
not make shard 0 longer than today). As at the last change, each shard's first run starts from
`crawl_cursor.json`; the `s*of3` cursor files are no longer read.

### When a row leaves the database (pruning, 2026-10-24)

Three rules decide when a stored row stops being published or is deleted. They are applied in the
merge job after the shard deltas are folded in (`scripts/build_feed.py` `prune_rows`), and the
build log shows how many rows each removed (`pruned unlisted=... stale=...`). The first rule is a
publish filter, the other two delete rows:

1. **Posting age** (`backend/feed.py`, `JOBS_FRESH_DAYS` = 45, `JOBS_SEEN_DAYS` = 7): a dated row
   posted over 45 days ago is left out of the feed unless a crawl saw it listed in the last 7 days.
   The row stays in the DB and comes back if a crawl sees it again. Unchanged by the rules below.
2. **Unlisted** (`Watchlist.prune_unlisted`, `scheduler.FAST_PRUNE_DAYS`, env `JOBS_FAST_PRUNE_DAYS`,
   default 7): a row is deleted once it has been unseen for 7 days AND its board had a **full,
   successful read after the row was last seen**. That read answered and did not list the job, so
   the employer took it down. The read is recorded per board in the `board_crawl` table
   (`ats, board_id, last_ok, last_partial, last_error, error, consecutive_errors, updated_at`),
   stamped by `refresh_watchlist` for every board it attempts, after the board's rows are
   upserted: `last_ok` when the read succeeded and listed the whole board, `last_partial` when it
   succeeded but stopped at a cap, `last_error` when it raised (its message in `error`, the streak
   in `consecutive_errors`; a success clears the streak). Each shard exports the rows it stamped
   in its delta and the merge keeps the newer timestamp per column, so the rule works across the
   four shards. Only `last_ok` counts. A board that raised, that the time cap did not reach, or
   that was read partially stamps no `last_ok`, so its rows are untouched by this rule: a live job
   is never dropped because its board was down. "Partial" (`service.listing_complete`) means the
   adapter's own cap was reached: Amazon returns the newest `AMAZON_MAX_JOBS`, a Workday board that
   listed `WORKDAY_MAX_JOBS` or Workday's 2,000-row ceiling (so a giant, split Workday board stays
   on the 21-day rule), or `JOBS_MAX_PER_COMPANY` rows were kept. A **collapsed listing** is
   partial too (`service.listing_collapsed`): a 200 that lists no rows at all, or fewer than a
   fifth of what the board's last full read listed when that was 20 or more (`board_crawl.rows`,
   or the stored rows before any read was recorded), is a broken or changed API answer more
   often than an emptied board, so it is stamped `last_partial` with `error` = "listing
   collapsed: 0 of 480" and the rows keep the 21-day rule. A normal shrink (480 to 410) is a
   full read. The trade-off: a board an employer genuinely emptied keeps its rows the full 21
   days, as before this rule; the fast rule only ever speeds up rows on boards that still list
   something. Rows whose `source_id` does not start with a watched board's `ats:board_id:` (the
   aggregators, the career-site lane) have no board row and are left to rule 3.
3. **Stale** (`Watchlist.prune_stale`, `scheduler.PRUNE_DAYS`, env `JOBS_PRUNE_DAYS`, default 21):
   every row unseen for 21 days is deleted, whatever its board did. The safety net, as before.

Neither delete rule ever touches a saved, dismissed or tracked row (the person's own records).
Before rule 2 (2026-10-24) a job the employer quietly removed could linger in the feed for up to
21 days; with every board crawled every run, it now leaves about 7 days after it was taken down,
and only when we know the board was read fine.

### SmartRecruiters is switched off (2026-10-19)

`https://api.smartrecruiters.com/robots.txt` allows only LinkedInBot (on `/v1/companies/`) and
disallows the whole API to every other user agent, so every automated request we made to it -- the
feed's board crawl, the per-posting descriptions, the app's detail-view fill (the JD read when a role is opened),
discovery -- was one the site asks robots not to make. All of them are off:

* `sourcing/ats.py` `DISABLED_ATS` records the ATS, the host, the reason and the date checked;
  `ats_disabled(ats)` is the one check. The adapter (`smartrecruiters_jobs`), the per-posting detail
  (`smartrecruiters_detail` / `smartrecruiters_fill`) and `normalize_jobs` return nothing without a
  request; `refresh_watchlist` skips the board (`boards_disabled` in its summary);
  `feedclient.fetch_board_jd` / `fill_list_only_detail` return nothing; discovery leaves it out of
  `DISCOVER_ATS` and its probe / name check make no request; the app's add-a-board endpoint refuses
  it with the reason.
* The **158 SmartRecruiters boards stay on the watchlist** (`seed/watchlist_seed.csv`), stored
  **inactive** (`watchlist.active = 0`; an existing database is switched on open), so nothing crawls
  them and nothing is lost. To reverse it (if their robots.txt ever allows us), delete the entry from
  `DISABLED_ATS`: the feed build's `sync_seed` re-activates the boards and the dormant adapter, still
  tested against a fake (`tests/test_smartrecruiters.py`), takes over.
* SmartRecruiters rows already in the published feed are not purged; they age out under the normal
  rules, since no crawl sees them again: a row posted over 45 days ago leaves once it is 7 days unseen
  (`JOBS_SEEN_DAYS`), and every row is pruned at 21 days unseen (`scheduler.PRUNE_DAYS`; the 7-day
  fast prune below never applies, because an inactive board is never read). On 2026-10-18 the live feed held **2,430 SmartRecruiters rows of 119,616 (2.0%)**, from
  136 boards (Domino's 487, AbbVie 294, Turner & Townsend 163, Intuitive Surgical 138, ServiceNow 125,
  ...). None of those 158 employers has another source in the feed today (no career-site entry in
  `config/careersites.json` and no board on another ATS), so their roles leave the feed until one is
  found by other means. `tests/test_smartrecruiters_disabled.py` fails if any new file under
  `sourcing/`, `ui/`, `backend/`, `scripts/`, `extension/` (and the other app folders) names
  `api.smartrecruiters.com`.
* Not affected: assisted apply on the public posting page (`jobs.smartrecruiters.com`) in the
  person's own browser or the extension, which is the person filling a form, not a robot reading the
  API.

### List-only boards are checked before they are kept (Workday; SmartRecruiters while it was on)

Two of the ATSs deliver their job list WITHOUT the description: Workday's `cxs` list and
SmartRecruiters' `GET https://api.smartrecruiters.com/v1/companies/{board}/postings` (the body and
the posting's real public page live on `.../postings/{id}`). The accessibility check (no citizenship /
clearance / no-sponsorship bar) reads the description, so a row from either is kept only once its
description has been fetched and checked, or is already stored with one
(`sourcing/service.py select_checked_rows`). Details are read newest posting first so the budget goes
to fresh roles, under a per-board budget: `WORKDAY_MAX_DETAIL` / `SMARTRECRUITERS_MAX_DETAIL`, default
200 on a desktop refresh, and `WORKDAY_MAX_DETAIL_FEED` / `SMARTRECRUITERS_MAX_DETAIL_FEED`, default
2000, for the central build (a 25,000-posting franchise board is never read whole). Rows the budget did
not reach are left for the next refresh rather than stored or published unchecked, and a row that
still lacks a description (stored before this rule) is hidden from the board and the feed
(`sourcing.quality.is_jd_checked`) until it is opened, when the detail is fetched, the check runs, and
a barred role is dismissed with a clear message instead of shown. SmartRecruiters publishes a limit of
10 requests a second on its Posting API; the crawl spaces one board's detail reads 0.2 s apart and
backs off on a 429 / 5xx before one retry.

### Boards from the remoteintech company list (2026-10-30)

[remoteintech/remote-jobs](https://github.com/remoteintech/remote-jobs) (ISC) lists ~880 remote-
friendly tech companies with their careers pages. Only facts were used (company names and careers
URLs); none of its prose was copied. Of its companies, 144 not already covered run a supported ATS
(Greenhouse, Lever, Ashby, Workable, Workday, Recruitee; SmartRecruiters is off). Each board was
checked with one request (robots.txt read first, one request a second, `SponsorJobsBot` user
agent): 99 answered with jobs, 16 were live but empty, 25 were dead (404 / 422) and one refused
(403). Following the watchlist's own rules, **42 were added**: the live boards with jobs whose
employer has an H-1B or PERM record (the seed's company is the employer's filing name, so the
sponsor badge matches). Not added: the 57 live boards with no sponsorship record (the watchlist
promises every board's jobs are from a sponsor; e.g. Calendly, Zapier, OpenRouter, Sigma Defense),
empty boards (`jobs` must be at least 1), and Marco Polo (its H-1B match is a different company).
The largest: The New York Times (159 jobs, 152 US), Grafana Labs (120), Red Hat (Workday, 127),
1Password (87), Kraken (87), Instructure (62), Supabase (64), Mapbox (35), Render (35), Replit (34),
Grubhub (Workday, 34). In all ~1,200 listed jobs, ~590 placed in the US from the list alone.
Evidence rows carry `source=remoteintech`.

Big sponsors whose own sites are JavaScript apps were probed the same way (a direct guess per
likely ATS, then `discover.discover_for_company`, the logic `scripts/discover_boards.py` runs):
PNC (Workday `pnc.wd5/External`, 2,000+ jobs), HubSpot (Greenhouse `hubspotjobs`, 84), Plaid
(Ashby `plaid`, 132), DigitalOcean (Greenhouse `digitalocean98`, 167) and Quora (Ashby `quora`, 9)
were added. Wells Fargo and Dow Jones were already watched. No public board on a supported ATS was
found for Akamai, Atlassian, Nagarro (its jobs are on SmartRecruiters, which is off), Shopify,
HashiCorp (its Greenhouse board is gone since IBM bought it) or NerdWallet. Oracle and IBM were not asked
(known to block); Cognizant, Wipro and Capgemini were not either (no readable board).

### robots.txt status codes (RFC 9309, 2026-10-30)

[RFC 9309](https://www.rfc-editor.org/rfc/rfc9309) section 2.3.1: a robots.txt answered with a
4xx other than 429 is "unavailable", and the crawler MAY access the site (there are no rules); a
429 or 5xx is "unreachable", and the crawler must assume a complete disallow. One helper,
`careersite.robots_unavailable(code)`, decides this everywhere:

* **ATS discovery** (`growth.PoliteFetch`) already read a 4xx as "no rules" (Ashby's API answers
  401); a 429 was wrongly counted with the other 4xx and is now owed like a 5xx.
* **The career-site lane** (`careersite.Host.load_robots`) treated only a 404 as "no rules": a 401
  or 403 stopped the host as if it had blocked us, and a 410 or 451 was an error. Now any 4xx but
  429 means no rules; a 429 still stops the host and a 5xx still reads nothing.
* **Career-site discovery** (`scripts/discover_careersites.py`) did the same (only 404 / 410 were
  "no rules"); now it follows the same rule.
* A 401 / 403 whose body is a bot-challenge page is still a block in both career-site paths: that
  is a WAF saying no, not a missing file. Any later 401 / 403 / 429 on a real page still stops the
  host, so nothing gets past a refusal.

What it unlocks today: no board on the watchlist (the board crawl does not gate on ATS robots.txt,
and discovery already let Ashby through) and none of the configured career sites (all serve a
readable robots.txt). It changes what career-site discovery can verify from now on: hosts whose
robots.txt answers a plain 401 / 403 / 410 used to be rejected unseen. The 35 Ashby boards from the
remoteintech list were verified under this rule. (Python's `urllib.robotparser` reads a 401 / 403
robots.txt as "disallow all", so a quick script built on it would wrongly skip every Ashby board.)

### Career sites (`sourcing/careersite.py`)

Most of the biggest H-1B sponsors (Amazon, Apple, JPMorgan, IBM, Oracle, Capital One...) hire
through their OWN career site, not through one of the ATS feeds above, so an ATS-only crawl never
saw them. Since 2026-10-11 (CLAUDE.md §6) an employer's own career site is GREEN where the site
permits it, and the lane reads it the way Google for Jobs does:

1. `robots.txt` first, and obeyed with our own user agent (`SponsorJobsBot/1.0 (+https://sponsorjobs.ai; ...)`):
   a disallowed path is never requested, a `Crawl-delay` raises the gap between requests, and the
   lane follows only the job sitemap(s) `robots.txt` (or the site table) names.
2. The site's job sitemap: the list of job-page URLs with `lastmod`, read newest first.
3. Each job page's schema.org `JobPosting` JSON-LD, the block every such site embeds for search
   engines: title, description, hiring organization, location, `datePosted`, `validThrough`. A page
   without one is not a job and is skipped; a posting whose `validThrough` has passed is closed.

Politeness is not optional: one request at a time per host with at least a 1 s gap, hard timeouts,
honest identification, `CAREERSITE_MAX_DETAIL` job pages per site per run (default 400; pages already
stored are stamped "still listed" from the sitemap without a request), and a hard STOP for the host
this run on a 401 / 403 / 429, three server errors in a row, or a bot-challenge page (robots.txt
itself follows RFC 9309: a plain 4xx other than 429 there means no rules, see above). There is no
retry loop, no second identity and no proxy: a host that answers like that has said no for now, and
the next run asks again politely. The lane runs beside the board pass in the feed build in its own
threads and SQLite connections (`CAREERSITE_WORKERS` hosts at once, default 4, each still one request
at a time), under its own wall-clock cap (`CAREERSITE_MINUTES`, default 60). `CAREERSITE_LANE=0`
switches it off.

With dozens of sites the lane also has a **page budget** across all of them,
`CAREERSITE_PAGE_BUDGET` (default 12,000 job pages a run; `0` = no cap), shared by the worker
threads. Only job pages count: a site reached after the budget is spent still reads its robots.txt
and sitemap and stamps its stored rows "still listed", so nothing is pruned just because the budget
ran out. The order **rotates**: sites are sorted by host and the run starts at a cursor kept in the
feed data dir (`careersite_cursor.json`, next to the board pass's `crawl_cursor.json`); the next run
starts at the first site this one did not finish (cut short by the budget or the deadline, or never
started), so a capped run never starves the end of the table. Within a site, pages are read newest
`lastmod` first, as before.

The site table is the allowlist: only a host in it is ever fetched. It lives in
**`config/careersites.json`** (`{"sites": {host: {...}}}`), loaded by `sourcing/careersite.py` into
the same `SITES` dict; each entry has `company`, `sitemaps`, `job_url` (anchored to its own host),
`platform`, `verified` (date) and `jobs_seen`, plus the optional `reader` / `keep` / `id_order` /
`max_urls` below. An entry whose `job_url` or sitemaps leave its own host is dropped at load time,
and the original four are built in as the fallback if the file is missing or unreadable. A site is
added only after the checks below (by `scripts/discover_careersites.py` or by hand). Submission on
these sites stays ASSISTED (`submit/allowlist.py`).

Verified 2026-10-11 and in the table: Microsoft (2,296 jobs) and Qualcomm (2,068), both Eightfold
(the sitemap and every job URL carry `?domain=`, and the robots file is "Disallow: / then Allow:
/careers", which only a longest-match robots reader gets right); Capital One (1,820, `jobs_sitemap.xml`;
many of its ads state no sponsorship and are excluded by the ad-stance rule); Cisco (about 1,500
across three sitemap files). Checked and left OUT: JPMorgan and Oracle (client-rendered Oracle
Recruiting Cloud, dead sitemaps, a WAF on robots.txt), IBM and Deloitte (Avature answers crawlers an
empty body), Infosys and Tesla (Akamai 403 on robots.txt itself), Meta (its robots.txt header forbids
automated collection without written permission), Apple and Google (no sitemap, no JobPosting block,
and Google disallows paging its results). **Amazon**, the largest sponsor, has no sitemap or JSON-LD
either, but the public keyless JSON its own search page reads is open to crawlers (robots.txt
disallows only `/internal`): `sourcing/amazonjobs.py` reads it as the `amazonjobs` adapter (board id
= country code, `AMAZON_MAX_JOBS` newest rows per run, default 1,000, full text in every row; the newest 500 are stored per run, and a row stays while it is re-seen).

#### Discovering more sites (`scripts/discover_careersites.py`)

A maintainer script finds and verifies career sites for the biggest sponsors. It ranks employers by
H-1B approvals (recent years weighted, `seed/sponsors_seed.csv.gz`), skips those already on the
watchlist, already in the table, clearance-heavy, or recorded above as blocked (JPMorgan, Oracle,
IBM, Deloitte, Infosys, Tesla, Meta, Apple, Google; Amazon and Walmart have their own paths), and
for each of the rest tries a few hosts guessed from the brand: `careers.<brand>.com`,
`jobs.<brand>.com`, `<brand>.eightfold.ai`, `apply.careers.<brand>.com`, `www.<brand>careers.com`
(a robots.txt redirect to another careers host is followed once). Per host:

1. `robots.txt` first, with our user agent. A 404 means no rules; an HTML page, a 5xx, or a file
   whose comments forbid automated collection means leave it; 401 / 403 / 429 or a challenge page
   stops that host at once.
2. The platform is named from robots.txt and the sitemap: Eightfold (`/api/pcsx`, `careerhub`,
   `?domain=` sitemaps), Phenom (`phenomtrack`, `px-widgets`, `/<country>/<lang>/job/` URLs), Radancy
   (`/search-jobs/`, `/job/<city>/<title>/<id>/<id>`), Workday, iCIMS, SuccessFactors, Oracle, Avature.
3. The sitemap(s) robots.txt names (or the platform's usual one), only if robots.txt allows them;
   job-page URLs are counted (at least 10 needed). A multi-locale site keeps one locale (English when
   it lists a fair share), and a title slug before `/job/` becomes a wildcard segment in `job_url`.
4. One robots-allowed job page must carry a schema.org `JobPosting` with a title.

Politeness as in the lane: one request at a time per registrable domain (every `*.eightfold.ai`
tenant shares one gate) at least 1 s apart, raised by `Crawl-delay`; short timeouts; no retry.

    python scripts/discover_careersites.py --limit 600 --minutes 60 [--workers 8] [--write] [--ipv4]
    python scripts/discover_careersites.py --report        # what the checkpoint holds

Progress is checkpointed in `data/careersite_discovery.json` (git-ignored), so a rerun continues and
never probes a host twice. `--write` adds verified hosts to `config/careersites.json` (never
overwriting an entry); review the diff before committing. Workday tenants are reported, not added
(Workday boards belong on the watchlist, read by `sourcing/workday.py`). Platforms checked for
sitemap + JSON-LD: Eightfold, Phenom and Radancy qualify; Workday also has a per-tenant `siteMap.xml`
and JSON-LD but is already covered through its JSON API; SuccessFactors / SAP pages tried carried no
JSON-LD; iCIMS hosts guessed answered "gone"; Oracle and Avature are client-rendered. Each one is
judged on the four checks above, not on its platform name. `--ipv4` is for a network whose IPv6
route is broken (Python waits about 10 s per v6 address before falling back).

First run, 2026-10-12: the top 600 uncovered sponsors, 4,147 hosts probed (3,694 did not exist),
**53 sites verified and added, 67,162 job URLs in their sitemaps**:

- **Other (own sitemap + JSON-LD)** (15): Costco (`careers.costco.com`, 20,381), Marriott International (`careers.marriott.com`, 13,614), Hitachi (`careers.hitachi.com`, 2,513), Henry Ford Health System (`careers.henryford.com`, 1,539), EPAM Systems (`careers.epam.com`, 1,293), Nike (`careers.nike.com`, 1,289), Gartner (`jobs.gartner.com`, 763), Garmin (`careers.garmin.com`, 420), Equifax (`careers.equifax.com`, 220), Infor (`careers.infor.com`, 220), SAP (`jobs.sap.com`, 161), Teradata (`careers.teradata.com`, 146), Equinix (`careers.equinix.com`, 50), Gap Inc. (`www.gapinc.com`, 24), Ancestry (`careers.ancestry.com`, 13).
- **Phenom** (17): NTT DATA (`careers.nttdata.com`, 2,910), Thermo Fisher Scientific (`jobs.thermofisher.com`, 2,392), State Street (`careers.statestreet.com`, 1,098), GE HealthCare (`careers.gehealthcare.com`, 889), RBC (`jobs.rbc.com`, 799), The Cigna Group (`jobs.thecignagroup.com`, 528), Arrow Electronics (`careers.arrow.com`, 513), Xoriant (`careers.xoriant.com`, 481), Merck (`jobs.merck.com`, 390), Snowflake (`careers.snowflake.com`, 364), LPL Financial (`career.lpl.com`, 296), Chewy (`careers.chewy.com`, 255), Blue Yonder (`careers.blueyonder.com`, 139), Freedom Mortgage (`careers.freedommortgage.com`, 26), Yelp (`www.yelp.careers`, 19), Southwest Airlines (`careers.southwestair.com`, 18), Howard Hughes Medical Institute (`careers.howardhughes.com`, 14).
- **Radancy (TalentBrew)** (11): Quest Diagnostics (`careers.questdiagnostics.com`, 2,197), Ford (`www.careers.ford.com`, 901), Comcast (`jobs.comcast.com`, 702), Synopsys (`careers.synopsys.com`, 620), Intuit (`jobs.intuit.com`, 615), Citizens (`jobs.citizensbank.com`, 467), Arm (`careers.arm.com`, 414), NetApp (`careers.netapp.com`, 308), Santander Bank (`www.santandercareers.com`, 176), Audible (`www.audiblecareers.com`, 54), Synchrony Bank (`www.synchronycareers.com`, 45).
- **Eightfold** (10): Applied Materials (`careers.appliedmaterials.com`, 2,012), Lam Research (`careers.lamresearch.com`, 1,365), Arcadis (`jobs.arcadis.com`, 1,316), Ericsson (`jobs.ericsson.com`, 523), Boston Scientific (`bostonscientific.eightfold.ai`, 475), DPR Construction (`careers.dpr.com`, 446), John Deere (`careers.deere.com`, 300), Whirlpool (`jobs.whirlpool.com`, 228), TriNet (`jobs.trinet.com`, 196), Amdocs (`jobs.amdocs.com`, 25).

Rejected (297 hosts): the sitemap listed no job pages (206, mostly corporate sites and wildcard
`*.eightfold.ai` tenants with an empty sitemap), robots.txt disallows the sitemap (41), job pages
without JobPosting JSON-LD (21: Capgemini, Cognizant, Wipro, PwC, Caterpillar, Nutanix, DocuSign,
eBay, Keysight, ADP, HSBC, Rivian, Paramount, SiriusXM, TEKsystems...), robots.txt an HTML page (11),
robots.txt answered a 5xx (11) or a 202 / 406 (3), too few job URLs (4). Blocked on contact (403 / 401, left alone): T-Mobile,
DoorDash, Verizon, Akamai, Lowe's, UBS, Change Healthcare, Box, Hallmark, Cohesity, Shutterfly,
Cummins (403 on its sitemap), credit.com (401). One Workday tenant (Northern Trust) reported for the watchlist.

Live capped run after adding them (fresh database, `CAREERSITE_MAX_DETAIL` 20): Lam Research
(Eightfold) 20 pages, 20 kept; Thermo Fisher (Phenom) 20 pages, 16 kept, 4 without a posting;
Intuit (Radancy) 20 of 20. Then 10 pages each from Marriott, Hitachi and EPAM (own sitemaps,
wildcard job URLs): 30 of 30. No block, no challenge, no errors; the cursor moved on as designed.

#### Page readers and the keep rule (`sourcing/careersite_readers.py`)

Some sponsors' sites have a clean `robots.txt` and job sitemap but put the job data in the page's
framework JSON instead of JSON-LD. A site entry may name a **reader** (`"reader": "nextdata"`):
JSON-LD is still tried first, and only a page without it is handed to the reader, which returns a
JobPosting-shaped dict so the rest of the lane is unchanged. `nextdata` reads the Next.js
`__NEXT_DATA__` script: a known shape first (Walmart's `props.pageProps.jobDetails`, a closed
requisition is skipped), then a generic walk for the object with a title-like key and the longest
description-like string (at least 200 characters, so an SEO blurb never passes for an ad).

A site whose sitemap is mostly store, warehouse and driver hourly roles gets a **keep** rule
(`"keep": {...}`), because those roles are not what our users (international students looking for
sponsored professional jobs) can use. Hourly words drop first (cashier, stocker, driver, cart
attendant, personal shopper, technician, part time, hourly...), then professional words keep
(software, engineer, data, analyst, product manager, finance, risk, supply chain, director...),
then the site's optional `department` pattern keeps, and `else` (default `drop`) decides the rest;
`drop` / `keep` add site-specific words. It is applied to the sitemap URL's **slug first**, before
any request, when the slug spells the title; when the URL is only an id the page's title (and the
department the reader found) decide, and a page dropped that way, or one with no posting (a closed
requisition), is remembered for 30 days in a `careersite_skip` table in the same SQLite file so the
next run spends its page budget on pages it has not read. `id_order` reads a sitemap that has no
`lastmod` newest requisition first; `max_urls` lifts the per-site sitemap cap (6,000).

Added 2026-10-12 (robots.txt read by hand, sitemap counted, pages parsed, no challenge):

| Site | Job URLs | How | Keep rule effect |
|---|---|---|---|
| Walmart (`careers.walmart.com`) | 15,839 (`/us/en/jobs/R-<id>`, no `lastmod`, no title in the URL) | `nextdata` reader; robots disallows only `/api`, `/results`, `/cms`, `/debug`, `/docs` | 200-page random sample: 3 closed, 12 of 197 kept (6%): Staff / Principal Software Engineer, Technical Program Manager, Principal Data Analyst, Senior Director Technology Operations, Realty Project Coach. Dropped: store and club roles, pharmacists and optometrists (store-based), distribution-center floor managers, facility technicians. Department rule: `corporate / Home Office` |
| The Home Depot (`careers.homedepot.com`) | 25,333 (`/jobsitemap`, which redirects to its sitemap vendor's file on `feeds.hodesdigital.com`) | JSON-LD | Slug rule, no request spent: 354 of 25,333 kept (1.4%; 0 of a random 200, all store roles); kept are Atlanta HQ product, data science, analyst, engineering, marketing analytics roles |
| Bank of America (`careers.bankofamerica.com`) | 2,113 (`sitemap_main.xml`) | JSON-LD (`datePosted` as MM/DD/YYYY, now read) | none |
| Accenture US (`www.accenture.com/us-en/jobpostings-sitemap.xml`) | 952 | JSON-LD (a description holding `&quot;` inside a JSON string: the block is parsed raw before unescaping) | none |

Live capped run 2026-10-12 (fresh database): Walmart, two runs of 30 pages each, 0 kept, 51 dropped by
the keep rule, 9 pages with no posting (closed requisitions); the newest requisitions are all store
hiring, and the second run did not re-read the 26 pages the first run dropped. At a 6% keep rate the default 400 pages a
run yields about 24 Walmart roles a run and covers the whole sitemap in about 40 runs. Home Depot
12 pages, 12 kept (Senior Data Scientist, Senior Product Manager, Controls Engineer, Manager
Marketing Analytics...). Bank of America 6 of 6, Accenture 6 of 6. No block, no challenge.

Checked and left OUT on 2026-10-12: Goldman Sachs (`higher.gs.com` robots.txt disallows
everything), Wells Fargo and Cognizant (robots.txt allows and the sitemaps list 1,402 and 2,126
jobs, but every job page answers a Cloudflare challenge), Target (a Workday tenant, `sourcing/workday.py`);
Salesforce (`careers.salesforce.com` answers a redirect; not followed further). None of the candidates needed the `nextdata` reader besides Walmart.

### Growing toward 500k jobs (2026-10-23)

Starting point, the 2026-10-23 11:51 build: `in_db=312,510`, `not_us=124,568`, `ad_says_no=13,939`,
`kept=167,472`, 5,088 cross-feed duplicates, **150,000 written**. Same quality rules throughout (US
only, fresh or still listed, not "no sponsorship", sponsor-relevant); only the reach changed.

**1. The publish cap was cutting checked rows.** 162,384 rows survived every filter and the dedup,
and `JOBS_FEED_LIMIT` (150,000) cut 12,384 of them. PR #86 (same day) lifted the cap to 1,000,000
and the scan to 2,000,000. Gain: **+12,384 at once.**

**2. `too_old` is not a drop.** The funnel's `too_old_still_seen_7d` (66,097) counts rows posted over
45 days ago that a board still listed in the last 7 days; those are KEPT (`fresh_recent` = 305,983
of 312,510, so only 6,527 rows left for age). Every board is crawled every run (3 shards, 422 / 401
/ 409 boards in ~7,000 s of the 8,400 s cap), so the 7-day rule does not drop live jobs. Nothing to fix.

**3. `not_us` is mostly right.** A sample of 1,367 rows from 37 random Greenhouse / Lever / Ashby
boards: 304 not US, of which ~280 are plainly abroad (Budapest, Toronto, Pune, London...) and ~24
are ambiguous ("Remote", "Hybrid", "Global", "SFHQ"; left alone: a bare "Remote" on a global board
is not a US promise). The real misreads were on **Workday**: campus, plant and city names with no
state ("Springfield", "MN-Champlin", "New Bedford", "East Hanover", "101 Corsair Hall"), stored
before the 2026-10-18 country fix and never re-read, because a stored row's description is not
fetched again. Fixed below, through the board's own country field.

**4. Workday: list US roles only, and up to the ceiling** (`sourcing/workday.py`).

* `us_country_facet` finds the board's US country value in the first list response
  (`locationCountry`, `Location_Country`, `LocationCountry`, `Country`, a custom
  `CF_..._Country_...`, NVIDIA's `locationHierarchy1`; nested groups too; a `locations` facet
  holding one office called "United States" is not a country filter). When it covers fewer rows
  than the whole list, page one is read again with it applied, so the per-board cap and the
  description budget go to US roles. `WORKDAY_US_ONLY=0` switches it off.
* A row the board's US filter returned gets ", United States" when its location reads as nothing
  (`place_in_us`); a foreign-named one ("Tokyo, Japan", matched through another office) and
  "N Locations" (the description read fills it) are left alone. `Watchlist.upsert_jobs` lets such
  a list location replace a stored one the US rule cannot place, so the old rows are mended too.
* Boards without the facet: `select_checked_rows` reads descriptions for US-readable rows before
  clearly foreign ones (`sponsors.names_foreign`), so a short budget is not spent on Pune.
* `WORKDAY_MAX_JOBS` is **2000** in the feed (Workday's own list ceiling; the code default 1000
  stays for desktop refreshes).
* Discovery's fallback site names gained `External_Career_Site` and `ExternalCareers` (the 3rd and
  4th most used of the 369 watched sites).

Probe 2026-10-23 (one list request per board, 50 random watched boards): 27 offer a US country
facet; the boards were already listing ~7,800 US rows and would list ~9,800 (**+2,000 per 50
boards, about +14,800 across 369**, before the ~8% that say "no sponsorship"). Measured end to end on
five boards (list only, US-readable rows before -> after): Salesforce 105 -> 166 (529 US roles; the
rest are "N Locations" until their description is read), Amgen 406 -> 489, Cardinal Health 357 ->
635, Santander 86 -> 193, Ochsner 979 -> 1,741 (no facet; the 1,000 cap). **+1,291 on five boards.**

**5. Phenom pages with an empty JSON-LD block** (`careersite_readers.read_phenom`). Many Phenom job
pages ship `<script type="application/ld+json"></script>` and keep the posting in the page's own
`phApp.ddo` object, so the lane logged ~1,200 "no posting" pages a run on the Phenom sites (State
Street 234, Thermo Fisher 171, GE HealthCare 140, Xoriant 129, Merck 110...) and re-read them every
run. Every `platform: phenom` entry now gets the reader (`reader_for`); an explicit `reader` wins.
Capped probe, 12 pages each: State Street 12 of 12 kept (was 1 of 5), Merck 12 of 12 (8 US).

**6. New career sites.** `discover_careersites.py --start 500 --limit 300` (sponsor ranks 500-800;
`--start` added so a fresh checkpoint does not re-probe the first run's hosts; a cut-off chunked
body no longer kills the run). 300 employers, 2,351 hosts, 35 minutes: 6 new sites verified and
added (sitemap + JobPosting JSON-LD, robots allows):

| Site | Job URLs | 12-page probe |
|---|---|---|
| IQVIA (`jobs.iqvia.com`) | 3,681 | 12 kept; global (first 8 all Europe) |
| PruittHealth (`careers.pruitthealth.com`, Phenom; PERM filer) | 1,385 | 12 kept, all US |
| Veeva Systems (`careers.veeva.com`) | 898 | 12 kept, about half US |
| Eliassen Group (`careers.eliassen.com`) | 273 | 12 kept, 7 of 8 US |
| City National Bank (`careers.citynational.com`, Radancy) | 78 | not probed |
| HERE Technologies (`herecareers.com`) | 10 | not probed |

Walmart was already in the table (2026-10-12, `nextdata` reader): 15,551 job URLs but a 6% keep
rate by design (store, club and DC roles drop), so it adds ~1,000 roles in all, not 15,000.

Blocked on contact 2026-10-23 (403; left alone, do not re-probe): Verizon (`mycareer.verizon.com`
job pages), Progressive (`careers.progressive.com`), Kearney (`careers.atkearney.com`), Hallmark,
Cohesity, Shutterfly (again). In the 2026-10-23 feed run the lane itself was refused by Gartner
(`jobs.gartner.com`), SAP (`jobs.sap.com`) and Equifax (`careers.equifax.com`), all 403: the lane
stops that host for the run, as designed; if they keep refusing, take them out of the table.

**Projection.**

| Source | Rows gained (published) | How sure |
|---|---|---|
| Publish cap lifted (PR #86) | +12,400 | certain (next build) |
| Workday US facet + 2000 list cap + location mending | +12,000 to +15,000 | probe of 50 + 5 boards; fills over ~3 runs (600 descriptions per board per run) |
| New career sites (6) | +2,500 to +3,500 | IQVIA's US share unknown |
| Phenom reader | +400 to +800 | 12-page probes |
| **Total** | **~+27k to +32k: ~190k to 195k published** | |

Reaching 500k honestly needs about 2.5x the sources, not looser filters: the watchlist at ~3,000
boards (the daily discovery adds ~100-300 a week at a falling hit rate past rank 4,000), Workday
boards over 2,000 US roles split by `jobFamilyGroup` facet (CVS lists 19,562; today
`JOBS_MAX_PER_COMPANY` 2,000 caps every employer), ~100 more career sites, and a fourth crawl
shard once the list passes ~3,500 boards. **Client size:** the app holds ~1 KB per row in memory
(140 MB for 149k) and downloads ~1 MB gzipped per 10k rows, so 250k is ~25 MB to download and
~240 MB in memory. Past ~250-300k rows the Jobs page should move to server-side search (the
client asks a search endpoint for one page; see `docs/server-search-plan.md`).

### Giant Workday boards, more career sites, a fourth shard (2026-10-23, part 2)

**Giant Workday boards** (`sourcing/workday.py`: `split_plan`, `list_postings`). One Workday list
query never pages past 2,000 rows, and `JOBS_MAX_PER_COMPANY` (2,000) cut every employer there too.
A one-request size probe of all 370 watched Workday boards (8 did not answer) found **15 boards
over 2,000 US-or-unfiltered rows, 98,078 rows in all, 68,078 past the old cap**: CVS Health 19,545,
Walmart 15,463 (US facet), Target 14,117 (US), TJX 11,363, Trinity Health 6,717, Sanford Health
3,980 (US), Petco 3,976 (US), PwC 3,899, Dick's 3,810, Micron 3,120, Bridgestone 3,088 (US),
Mass General Brigham 2,455, GE Vernova 2,256, Genpact 2,172, Tapestry 2,117.

* The board's own facets partition its list: counts are conditional on the facets applied, and
  several ids of one facet are OR-ed. `split_plan` picks the facet that leaves the fewest rows in
  values still over 2,000 (then the fewest queries), packs the small values into OR-groups of at
  most 2,000 rows and 100 ids, and splits an oversized value once more by a second facet (CVS:
  "Retail Store & Pharmacy" 12,720 -> part time 7,206 / full time 5,512). Past two facets a query
  reads its first 2,000 rows. The US country filter stays applied; country facets are never keys.
* Facets with more than 60 values are never used: CVS's `locations` has 5,145 values, 400 OR-ed
  ids answer HTTP 500, and every page of a location query still carries the whole 450 KB facet
  list (two minutes a page, live).
* Some tenants cap `total` itself at 2,000 (Bridgestone: total 2000, US value 3,088), so a board
  at the ceiling is sized by its largest facet count.
* Split queries run for at most `WORKDAY_SPLIT_SECONDS` (600) per board per run and start at a
  query chosen by the 3-hour tick, so a board too big for one run is covered over the next ones.
  A timeout or 5xx mid-split keeps the rows already listed. Rows are deduplicated by posting.
* The feed workflow raises `WORKDAY_MAX_JOBS` and `JOBS_MAX_PER_COMPANY` to 25,000. Unchanged: a
  row is stored only once its description is read and checked (`WORKDAY_MAX_DETAIL_FEED`, 600 per
  board per run, newest first); stored rows cost nothing to keep. So a giant board fills by ~600
  checked rows a run (CVS: about 4 days), and the desktop refresh (1,000 rows) never splits.

Capped live probes (split budget 300 s; list rows before -> after): **Micron 2,000 -> 3,120** (all;
256 requests, 355 s), **GE Vernova 2,000 -> 2,255** (all; 213 requests, 384 s), **Sanford Health
2,000 -> 3,019** of 3,975 US rows (251 requests, 441 s; the rest on the next runs). CVS, TJX and
Trinity Health timed out from this Mac (CVS pages took 6-50 s each); in the crawl a failed split
keeps the base 2,000.

**Career sites.** `discover_careersites.py --start 800 --limit 900` (uncovered sponsor ranks
800-1,700): 909 employers, 7,454 requests, 45 minutes, 15 hosts verified. Robots.txt read by hand
for each; 3 dropped for identity (careers.achieve.com is Achieve the fintech, not the sponsor
Achieve Beyond; jobs.empower.com is Empower retirement, not Empower Professionals; EBSCO Industries'
iCIMS site for the sponsor EBSCO Publishing, 11 jobs). **12 added** (job URLs in the sitemap):
Eaton (`eaton.eightfold.ai`, 2,348), Corning (Phenom, 988), Novartis (`www.novartis.com/careers`,
895), Thumbtack (514), ICF (Phenom, 424), Molina Healthcare (Radancy, 391), Thornton Tomasetti
(Phenom, 171), Alexion (Radancy, 141), Incyte (134), MSCI (114), Dolby (Eightfold, 89), Medallia
(68): 6,277 job URLs. The hit rate past rank 800 is ~1.7%, so ~100 more sites would need ~6,000
more employers probed; most large hospitals, universities and retailers run Workday and belong on
the watchlist instead.

Removed from the table: Gartner (`jobs.gartner.com`), SAP (`jobs.sap.com`) and Equifax
(`careers.equifax.com`) answered the lane 403 in consecutive runs (2026-10-22 and 10-23; Applied
Materials once). They and the 2026-10-23 refusals are in the script's `SKIP_BRANDS`.

Blocked on contact 2026-10-23 (403 / 401 on robots.txt, a sitemap or a job page; left alone, do
not re-probe): Blizzard, UBS, Better, Rocket, National Grid, Fidelity (`jobs.fidelity.com` job
pages), ADP (`jobs.adp.com` job pages), Cox Enterprises (`jobs.coxenterprises.com` job pages),
Prudential (`jobs.prudential.com`), McDonald's, Ampere Computing, Credit One Bank, Egencia,
United Wholesale Mortgage, American Eagle (sitemap), Bentley (`www.bentleycareers.com`), Saint
Francis (`www.saintfranciscareers.com`).

**Projection.**

| Source | Rows gained (published) | How sure |
|---|---|---|
| Part 1 (publish cap, US facet, 6 sites, Phenom) | ~+27k to +32k -> ~190k to 195k | see above |
| Giant Workday boards split, 25,000 cap | +35,000 to +50,000 | 68k more US-or-unfiltered rows listed; less abroad (Micron, PwC, Genpact are global), "no sponsorship" ads (~8%), and fills at 600 checked rows per board per run over ~4 days |
| 12 new career sites | +2,500 to +3,500 | 6,277 job URLs, Corning / Novartis / Eaton global |
| Gartner, SAP, Equifax out | ~0 | already refused every run |
| **Total** | **~230k to 250k published** | |

500k still needs about twice the sources: the watchlist growing past ~3,000 boards, and the
career-site lane reaching the retail and hospital giants that are not on Workday.

## Set-up, step by step

### 1. Create the bucket (Cloudflare dashboard)

1. Cloudflare dashboard -> **R2 Object Storage** -> **Create bucket**. Name it e.g. `tailor-feed`,
   location **Automatic**. Leave the storage class Standard.
2. Note your **Account ID** (right-hand column of the R2 overview page, also in the URL). That is
   `R2_ACCOUNT_ID`.

### 2. Create an API token for the Action

1. R2 Object Storage -> **Manage R2 API Tokens** (top right, "{} API" / "Manage API tokens") ->
   **Create API token**.
2. Permissions: **Object Read & Write**. Scope: **Apply to specific buckets only** -> pick
   `tailor-feed`. TTL: forever (or rotate yearly).
3. Create. Copy the **Access Key ID** -> `R2_ACCESS_KEY_ID` and **Secret Access Key** ->
   `R2_SECRET_ACCESS_KEY`. The secret is shown once.

### 3. Add the GitHub secrets and variables

Repository -> **Settings** -> **Secrets and variables** -> **Actions**.

Secrets (**New repository secret**):

| name | value |
|---|---|
| `R2_ACCOUNT_ID` | from step 1 |
| `R2_ACCESS_KEY_ID` | from step 2 |
| `R2_SECRET_ACCESS_KEY` | from step 2 |
| `R2_BUCKET` | `tailor-feed` |
| `RAPIDAPI_KEY` | JSearch key (optional; without it JSearch is skipped) |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | optional; **unused** for the public file (see the terms table) |

Variables (**Variables** tab -> **New repository variable**), all optional:

| name | value |
|---|---|
| `TAILOR_FEED_URL` | the public base URL, e.g. `https://tailor.example/feed` (recorded in `manifest.json`) |
| `R2_PREFIX` | key prefix inside the bucket, e.g. `feed`, so objects land at `/feed/jobs.json.gz`. Leave empty to publish at the bucket root. |
| `FEED_INCLUDE_AGGREGATORS` | default `jsearch`; `off` for boards + keyless feeds only |
| `JOBS_SYNC_SEED` | `1` loads the bundled H-1B seed on the calling thread (tests, CLI); unset, a background thread loads it while the app serves |
| `JOBS_FIRST_CRAWL` | `0` disables the first-open crawl; a failed crawl retries after 10 min, doubling to an hour |
| `JOBS_MAX_PER_COMPANY` | rows kept per board per crawl; the workflow defaults to 25,000 (the desktop app keeps 60) |

Then **Actions -> feed -> Run workflow** once by hand and check the run log ends with
`[upload_feed] uploaded 258 objects`.

### 4. Point a custom domain at the bucket

1. The domain must be on Cloudflare (add the zone, move the nameservers).
2. R2 -> the bucket -> **Settings** -> **Public access** -> **Custom Domains** -> **Connect Domain**.
   Enter the hostname, e.g. `feed.tailor.example` (a subdomain is simplest; R2 custom domains are
   whole hostnames, so the `/feed` path is just the `R2_PREFIX` above). Cloudflare adds the DNS
   record and issues the certificate; wait for **Active**.
3. **Settings -> CORS policy -> Add**: the landing page (`site/`) reads `manifest.json` from the
   browser to show the live job count, so the bucket must allow that origin. Paste:

   ```json
   [{"AllowedOrigins": ["https://tailor.example", "https://www.tailor.example"],
     "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 86400}]
   ```

   Replace the two origins with the site's real domain. The desktop app needs no CORS (it is not a
   browser page), so this rule only ever serves the website.
4. Optional but recommended: **Caching -> Cache Rules** for the hostname, "Eligible for cache",
   respect origin TTL -- the `Cache-Control` headers set by the uploader then drive the edge cache.
   The list parts (`/feed/parts/*`) are content-addressed and sent `immutable`, so a rule that
   caches just that path is safe even if the rest stays uncached.
5. Set `TAILOR_FEED_URL` (variable, step 3) to `https://feed.tailor.example` (plus `/feed` if you
   used a prefix), and replace the placeholder in `shell-electron/main.js`:
   `const FEED_URL = process.env.TAILOR_FEED_URL || "https://tailor.example/feed"; // TODO`.
   Until then the shipped app runs on its local crawl.

Smoke test: `curl -sI https://feed.tailor.example/feed/manifest.json` should show
`cache-control: no-cache`; `curl -s .../manifest.json` shows `generated_at` and `count`.

## Running it locally

```sh
python scripts/build_feed.py --data .feed-data --out .feed-out --no-crawl   # publish what the DB holds
python scripts/build_feed.py --data .feed-data --out .feed-out              # full crawl (minutes)
pip install boto3 && R2_... python scripts/upload_feed.py --dir .feed-out --prefix feed
# point a dev app at a local copy: python -m http.server -d .feed-out 8765; JOBS_FEED_URL=http://127.0.0.1:8765 python -m ui.app
```

## The legacy kitchen

`backend/feed.py` still works (the builder reuses its `feed_jobs` shaping, and its `/feed` routes
serve if the Render blueprint is deployed), but no shipped app points at it any more and the
Render service is suspended. See `docs/hosting-render.md`.
