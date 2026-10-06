# The static job feed

**Status: the supported path (2026-09-30).** It replaces the hosted "kitchen" (`backend/feed.py` on
Render, now suspended), and is hosted on GitHub Pages for now (see "Where the feed is hosted"). Nothing is served by a process we run: a scheduled GitHub Action crawls the
GREEN-lane sources, writes three kinds of file, and publishes them behind our domain. Every install
downloads the slim list at most once an hour and filters it locally. There is no server to pay for
or keep alive, and the local crawl remains the fallback, so the app works offline.

## Where the feed is hosted

**Now: GitHub Pages** (decided 2026-10-06), at `https://feed.sponsorjobs.ai/feed`. Cloudflare would
not activate R2 on our account (a billing hold that support could not clear), and Pages costs
nothing and needs no card on a public repo. The `feed` workflow builds the files and publishes them
as a Pages site; the domain stays on Cloudflare as a DNS record pointing at GitHub.

**Move back to R2 when either of these happens:**
1. Cloudflare billing is sorted out on the account that owns the domain, or
2. paid plans launch. GitHub's terms say Pages is not free hosting for a commercial business or
   SaaS, so the feed should leave Pages before SponsorJobs charges money.

**How to move back** (the app does not change; it only knows the address): do the R2 set-up
below (steps 1 to 4), set the repository variable `FEED_HOST` to `r2`, run the workflow once,
then in Cloudflare DNS replace the `feed` CNAME record with the R2 custom domain, and turn Pages
off in the repo (Settings -> Pages).

Pages limits (soft): 1 GB site (the feed is ~10 MB) and 100 GB of downloads a month. GitHub serves
the `.json.gz` files as plain gzip without `Content-Encoding`; the app checks the gzip magic bytes
and unpacks either form (`sourcing/feedfile.loads_maybe_gz`, `sourcing/feedclient.py`), so nothing
breaks. Every file gets GitHub's fixed `Cache-Control: max-age=600`.

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
  scripts/upload_feed.py  put_object to R2 (S3 API) with Content-Type / Content-Encoding / Cache-Control
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
| `manifest.json` | tiny, uncompressed: `{generated_at, count, jobs_url, jd_shard_count, jd_url_template}` | `no-cache` |

The list holds up to **50,000** rows (`JOBS_FEED_LIMIT`, default raised from 10,000), dated rows older
than **45 days** are dropped (`JOBS_FRESH_DAYS`), and rows unseen by a crawl for 21 days are pruned.
At ~500 bytes a row the full list is ~25 MB uncompressed, ~4-6 MB gzipped.

### The app side (`ui/app.py`, `sourcing/feedclient.py`)

- `JOBS_FEED_URL` is the **base URL** of the feed (e.g. `https://tailor.example/feed`).
- `jobs.json.gz` is downloaded into `<data dir>/feed_cache/` **at most once an hour**, at a
  per-install random minute (drawn once, persisted in `feed_state.json`) so a fleet of installs
  spreads its downloads across the hour. The request carries `If-None-Match`; an unchanged file costs
  one 304. The first run with no cache downloads immediately.
- `/api/jobs` filters and paginates the cached list with `sourcing/filters.py` -- the same facets the
  kitchen applied (`q, loc, days, remote, visa, level, pay, sort, sponsored, page, per_page`) -- and
  overlays the person's saved bookmarks. It reports `"source": "central"` and `refreshed_at` =
  the file's `generated_at`.
- `/api/jobs/detail` reads the role's JD from its shard (cached locally for a day). If the shard lacks
  it (a Workday or SmartRecruiters row stored list-only before the crawl checked descriptions), it
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

1. **The live feed.** With a real `JOBS_FEED_URL`, the first request downloads the live list at once;
   the client shows its "connecting" state meanwhile and re-polls until the list arrives. From then on
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
   **10-minute** wall-clock cap (`JOBS_FIRST_CRAWL_MINUTES`), and **20** descriptions per Workday /
   SmartRecruiters board (`JOBS_FIRST_CRAWL_DETAIL`). It runs once per process (retried after a
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
  free minutes/month covers runs of up to ~8 minutes each. The crawl state (jobs DB + sponsor
  overlay) is kept between runs with `actions/cache` (10 GB limit per repo).
- **Aggregator keys**: JSearch (RapidAPI) is used once per run centrally, never per user.

## Aggregator terms (what may go in the public file)

Checked 2026-09-30. `FEED_INCLUDE_AGGREGATORS` (a repository *variable*, comma list; default
`jsearch`) governs the **keyed** aggregators. Keyless feeds whose notices invite sharing are always in.

| source | terms | in the public file? |
|---|---|---|
| Company boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Workday) | public, keyless board APIs published for exactly this (CLAUDE.md §6) | **yes** |
| **freehire.me** | [Terms](https://freehire.me/terms): "no scraping beyond our documented API, no attempting to bypass rate limits or authentication"; nothing on caching/redistribution; the [API docs](https://freehire.me/docs/api) describe it as free and open-source | **yes** |
| **Remotive** | the API response's own `0-legal-notice`: "API documentation and access is granted so that developers can share our jobs further. Please do not submit Remotive jobs to third Party websites [job aggregators] ... Please link back to the URL found on Remotive AND mention Remotive as a source" (https://remotive.com/api/remote-jobs) | **yes**, with attribution; the file's `attribution` block carries the credit |
| **Remote OK** | the API's first array element: "Please link back (with follow, and without nofollow!) to the URL on Remote OK and mention Remote OK as a source ... Please don't use the Remote OK logo" (https://remoteok.com/api) | **yes**, with attribution |
| **JSearch** (RapidAPI, publisher OpenWeb Ninja) | [OpenWeb Ninja Terms](https://www.openwebninja.com/terms): "we additionally grant you a non-exclusive, non-transferable license to use, reproduce, and commercially exploit such API Data in your own products, services, applications, or business, including for resale or redistribution as part of a broader product offering, provided that you do not resell, sublicense, or redistribute API Data as a standalone data or API product that substantially replicates the Services themselves." RapidAPI's platform policy defers to the provider's terms. | **yes by default** (`jsearch`); our feed is part of the app, not a standalone data product |
| **Adzuna** | [Developer Terms of Service](https://developer.adzuna.com/docs/terms_of_service): beyond a 14-day trial, use by "a commercial, government or academic organisation ... may not be used in its original format or in aggregation ... to deliver any ongoing work or research ... without written consent"; also requires a "Jobs by Adzuna" label on every displayed advert, and the API returns only a ~500-char preview | **no.** `build_feed.py` refuses `adzuna` even if listed (and `sourcing/service.py` already prunes it). Needs written consent from Adzuna first. |

Remotive's notice also mentions a daily request budget (~4/day) and a 24-hour listing delay; the crawl
makes one Remotive request per title term per run (4 terms, 8 runs a day), which is above that
budget. If Remotive objects, `FEED_INCLUDE_AGGREGATORS` is not the lever (Remotive is keyless): drop
it from `PUBLIC_OK_AGGREGATORS` in `scripts/build_feed.py`, or run the crawl less often.

Attribution: `jobs.json.gz` carries an `attribution` map for every aggregator present, and the
Jobs page prints a muted "via Remotive" / "via Remote OK" on each such row (a fact on the card, a
plain `rel="noopener"` link to the listing in the detail view, never `nofollow`; `VIA_SOURCES` in
`ui/static/app.js`).

### Workday boards (`sourcing/workday.py`)

The biggest H-1B sponsors (NVIDIA, Intel, Cisco, Target, Adobe, Visa, US Bank...) hire through
Workday, and every Workday career site exposes the same public, keyless JSON its own page reads:
`POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` for the list (20 a
page, by offset) and `GET .../wday/cxs/{tenant}/{site}{externalPath}` for a posting's description.
No login, no key, no proxy, no browser pretence: the crawl identifies as `resume-agent/1.0`, reads
the tenant's `robots.txt` (which names the career site and is how discovery finds it) and leaves a
site alone when that file disallows it. Requests to one tenant are spaced, 429/5xx back off and
honour `Retry-After`, every request has a hard timeout, and each board is capped per refresh
(`WORKDAY_MAX_JOBS`, default 1000 rows; `WORKDAY_MAX_DETAIL`, default 200 descriptions per board
on a desktop refresh, `WORKDAY_MAX_DETAIL_FEED`, default 2000, for the central build). A
board is `company,workday,{tenant}.wd{n}/{site}` in the watchlist, e.g.
`nvidia.wd5/NVIDIAExternalCareerSite`; discovery adds one only after the site's own hiring
organization or site name confirms the sponsor's brand word and the list returns at least one job.
Applying on Workday is **ASSISTED** only: there is no applicant-usable submission API, so the
allowlist (`submit/allowlist.py`) is unchanged and the person clicks submit.

### List-only boards are checked before they are kept (Workday, SmartRecruiters)

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
| `JOBS_MAX_PER_COMPANY` | rows kept per board per crawl; the workflow defaults to 500 (the desktop app keeps 60) |

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
