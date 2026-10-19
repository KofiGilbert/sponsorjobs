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
breaks. Every file gets GitHub's fixed `Cache-Control: max-age=600` (the list parts too: Pages
cannot mark them immutable). A Pages deploy replaces the whole site, so the previous version's
parts vanish at once; a client mid-download then gets a 404 and re-reads the manifest. On R2 the
previous version's parts are kept for one more build.

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
                          list parts before the list, manifest last, then deletes parts named by
                          neither the new manifest nor the one it replaced
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
| `manifest.json` | tiny, uncompressed: `{generated_at, count, jobs_url, jd_shard_count, jd_url_template, parts: {version, count, total_bytes, attribution, files: [{url, sha256, raw_sha256, bytes, count}]}}` | `no-cache` |
| `sponsors/{a..z,0..9,_}.json.gz` | the browser extension's sponsor index (`scripts/build_sponsor_index.py`, built in the same workflow run): `{index_version, version, shard, records: {norm_name: [display_name, h1b_approvals, h1b_last_fy, h1b_first_fy, perm_certs, e_verify, cap_exempt, naics2, state, h1b_5y, denial_pct, flags]}}` (the last three are the 2026-10 compact facts: approvals over the newest five fiscal years, denial rate in whole percent, bit set 1 = H-1B dependent / 2 = willful violator; trailing zeros dropped), ~8 MB in all. Lets the extension badge jobs with no app installed, without sending a company name anywhere | `max-age=86400` |
| `sponsors/manifest.json` | `{index_version, version (content hash), generated_at, employers, stats, shards: {key: {file, bytes, records}}, total_bytes}`; the extension re-checks it at most daily | `no-cache` |

The list holds up to **50,000** rows (`JOBS_FEED_LIMIT`, default raised from 10,000), dated rows older
than **45 days** are dropped (`JOBS_FRESH_DAYS`), and rows unseen by a crawl for 21 days are pruned.
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
| **freehire.me** | [Terms](https://freehire.me/terms): "no scraping beyond our documented API, no attempting to bypass rate limits or authentication". The terms do not require attribution and are silent on caching and redistribution; the [API docs](https://freehire.me/docs/api) describe it as free and open-source (MIT, [strelov1/freehire](https://github.com/strelov1/freehire)). We use only the documented API, within its published rate limits | **yes.** No credit is shown in the UI or the feed file's `attribution` block (none is required); rows link only to the employer's own posting. The search-by-sponsor pass switches off with `FREEHIRE_SPONSOR_PASS_REQUESTS=0` |
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
(`WORKDAY_MAX_JOBS`, default 1000 rows; `WORKDAY_MAX_DETAIL`, default 200 descriptions per board
on a desktop refresh, `WORKDAY_MAX_DETAIL_FEED`, default 2000, for the central build). A
board is `company,workday,{tenant}.wd{n}/{site}` in the watchlist, e.g.
`nvidia.wd5/NVIDIAExternalCareerSite`; discovery adds one only after the site's own hiring
organization or site name confirms the sponsor's brand word and the list returns at least one job.
Applying on Workday is **ASSISTED** only: there is no applicant-usable submission API, so the
allowlist (`submit/allowlist.py`) is unchanged and the person clicks submit.

### Growing the watchlist (`scripts/grow_watchlist.py`, `sourcing/discover.py`)

Every board on `seed/watchlist_seed.csv` is a proven H-1B sponsor with a live public feed. The list
grows by probing the sponsors in `seed/sponsors_seed.csv.gz`, biggest recent H-1B filers first, on
the GREEN-lane ATS APIs, and keeping a board only when its own name (or a Workday posting's hiring
organization, or failing both its own job descriptions) confirms it is that employer and it lists
at least one job:

    python scripts/grow_watchlist.py --ipv4 --limit 4000 --max-minutes 180 --workers 16 \
        --ats greenhouse,lever,ashby,smartrecruiters,workday

It checkpoints every employer to `data/grow_watchlist.jsonl`, so a stopped run continues where it
left off; `--ipv4` is for networks whose IPv6 is broken (each SmartRecruiters / Ashby call otherwise
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
the saved state read-only, crawls its half from its own cursor (`crawl_cursor.s0of2.json`,
`crawl_cursor.s1of2.json`; the first sharded run starts from the old `crawl_cursor.json`), and
uploads only the rows it touched (`--export-delta`). Shard 0 alone runs the career-site lane and the
aggregator APIs, so nothing is fetched twice; Amazon is a board and lands in one shard. The
`build-and-upload` job folds the deltas into the saved DB (`--merge-dir`: newer `last_seen` wins,
earliest `first_seen` kept), prunes, writes the feed (cross-feed dedup and `JOBS_FEED_LIMIT` as
before), saves the state and publishes once. If one shard fails, the merge still runs: that
shard's boards keep the rows already saved, and its cursor does not move, so the next run retries
them. If both fail, the merge refuses to publish and the last feed stays up. Locally nothing
changes: `FEED_SHARD_INDEX` / `FEED_SHARD_COUNT` default to 0 / 1, the old single build.

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
this run on a 401 / 403 / 429, three server errors in a row, or a bot-challenge page. There is no
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
