> Historical note: the product was renamed from "Tailor" to **SponsorJobs** (2026-10). "Tailor" below refers to the app as it was then; it now names only the résumé-tailoring feature.

# Hosting the Tailor broker on Render (P5)

The broker is the small server that holds the company keys (Anthropic, Tavus, Stripe), meters usage
per plan, and runs billing. The desktop app stays local on each user's machine and calls the broker
over HTTPS. Only `backend/` is deployed — none of the résumé/OCR/PDF code.

## 0. Prerequisites
- A **Render account** (render.com).
- The **Starter plan ($7/mo)** for this service. The free tier won't work: it has no persistent
  disk (usage/plans would reset) and it spins down when idle (checkout would randomly stall).
- Your keys ready: `ANTHROPIC_API_KEY`, `TAVUS_API_KEY`, `TAVUS_PERSONA_ID`, the Stripe price IDs
  (two passes, three extra-interview packs; see "What to create in Stripe" in
  docs/bundled-api-backend.md), and a Stripe secret key (`sk_test_…` to trial in the sandbox first, `sk_live_…` to go live).

## 1. Deploy the blueprint
1. Render dashboard → **New → Blueprint**.
2. Connect the `KofiGilbert/sponsorjobs` repo. Render finds `render.yaml` and shows a
   **tailor-broker** web service with a 1 GB disk.
3. Click **Apply**. The first build runs `pip install -r requirements-broker.txt` and starts gunicorn.

## 2. Set the secret env vars
In the service → **Environment**, set the values for every var marked "sync:false":
`ANTHROPIC_API_KEY`, `STRIPE_SECRET_KEY`, `TAVUS_API_KEY`, `TAVUS_PERSONA_ID`, and the price
ids below. Leave `STRIPE_WEBHOOK_SECRET` blank for now (step 4). Every price is a ONE-TIME price
in Stripe; nothing is a subscription and nothing auto-renews.
- `STRIPE_PRICE_PASS30`: Job Hunt Pass, $29, 30 days (3 live interviews, 60 packages).
- `STRIPE_PRICE_PASS90`: Season Pass, $69, 90 days (9 live interviews, 150 packages).
- `STRIPE_PRICE_PACK_1`: 1 extra live interview, $9.
- `STRIPE_PRICE_PACK_3`: 3 extra live interviews, $24.
- `STRIPE_PRICE_PACK_5`: 5 extra live interviews, $35.

(A price whose var is unset is simply not offered in the app; billing turns on with the secret
key and at least one pass price. `STRIPE_PRICE_STUDENT` / `STRIPE_PRICE_PRO` are retired and no
longer read. Optional model overrides: `TAILOR_FREE_MODEL`, default `claude-haiku-4-5`, and
`TAILOR_PASS_MODEL`, default `claude-sonnet-5-5`.)

Optional, the official SponsorJobs Telegram bot (docs/notify.md): `TELEGRAM_BOT_TOKEN` (from
@BotFather), `TELEGRAM_BOT_USERNAME` (e.g. `SponsorJobsBot`), `TELEGRAM_WEBHOOK_SECRET` (any random
string) and `BROKER_PUBLIC_URL` (this service's https URL). Leave them unset and the bot routes
answer 503. Once set, run `python -m backend.telegram_setup` from the Render Shell to register the
webhook (see docs/notify.md, "Owner setup").
Save — Render redeploys.

## 3. Confirm it's up
- Copy the service URL, e.g. `https://tailor-broker.onrender.com`.
- Visit `…/health` → should return `{"ok": true}`.

## 4. Wire the Stripe webhook (this is what actually grants a pass)
1. Stripe dashboard (same **test** mode you built in) → **Developers → Webhooks → Add endpoint**.
2. Endpoint URL: `https://tailor-broker.onrender.com/billing/webhook`.
3. Events to send: `checkout.session.completed` and
   `checkout.session.async_payment_succeeded` (each grants the pass or extra interviews that
   were paid for, once per session). No `customer.subscription.*` events are needed any more.
4. Create it, then copy its **Signing secret** (`whsec_…`).
5. Back in Render → Environment → set `STRIPE_WEBHOOK_SECRET` to that value → save (redeploys).

## 5. Point the desktop app at the hosted broker
The app defaults to a local broker. Set `TAILOR_BROKER_URL=https://tailor-broker.onrender.com`
in the app's environment (for launch, this ships baked into the packaged app). With it set, the
app's checkout/avatar/LLM calls all go to the hosted broker.

## 6. Test end to end
Open the app → account menu → Get a pass → Job Hunt Pass → pay with test card
`4242 4242 4242 4242` (any future date, any CVC). Stripe fires the webhook → the broker grants
`pass30` for 30 days. Check `…/me/usage` (with your user header): `tier: "pass30"`,
`pass_until`, `interviews_left: 3`, `packages_left: 60`. The `/billing/plan` and
`/billing/credits` dev stubs answer 403 on Render (the `RENDER` env var) and whenever billing is
configured.

## Known gap before REAL customers (P6 — identity)
Right now every app calls the broker as the single user `local` (see `ui/app.py` `_BROKER_USER`).
That's fine for one person testing, but with many real users they'd all share one account/plan/usage.
**Before charging real customers, the broker needs per-user identity** — each install authenticates
as its own account (a license/account token instead of the `local` header). That's the P6 work; it
does not block deploying or sandbox-testing the flow above.

## Central job feed ("the kitchen") -- RETIRED, see docs/feed.md
**As of 2026-09-30 the supported job feed is the STATIC feed** (`docs/feed.md`): a scheduled GitHub
Action runs the same crawl and uploads `jobs.json.gz` + JD shards to Cloudflare R2; the desktop app
downloads the list hourly and filters locally. No server to pay for or keep alive. The section below
describes the legacy server-side path, kept working but no longer what `JOBS_FEED_URL` points at.

The same broker service also hosts the shared job feed, so there's no second bill. It fetches
public job listings once (with our aggregator keys), keeps them fresh with a background robot, and
serves them read-only at `GET /feed`. It holds NO personal data — only public jobs + the public
government sponsor DB. The desktop app pulls this feed instead of every user crawling on their own
(which would multiply the paid API calls). Private data (résumé, memory) still stays on each machine.

**Turn it on (one-time):**
1. In `render.yaml` the feed env vars are already declared. In the Render dashboard, set the secret
   VALUES: `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, and optionally `RAPIDAPI_KEY` (JSearch). `JOBS_AUTOUPDATE=1`
   and `JOBS_DATA_DIR=/var/data` are set for you.
2. **Upload the sponsor DB once.** The 56 MB `data/sponsors.db` is too big for git, so copy it to the
   persistent disk at `/var/data/sponsors.db` (Render Shell: `scp`/`curl` it up, or run the visa-data
   update on the box). Without it, jobs show no visa badges.
3. Redeploy. The robot starts in ONE worker (a file lock stops the other workers duplicating it),
   discovers boards, refreshes every few hours, and prunes filled roles. Check `GET /feed/health`.

**Pointing the desktop app at it no longer works as before:** `JOBS_FEED_URL` is now the base URL of
the STATIC feed (`<base>/jobs.json.gz`), not a `/feed` JSON endpoint. To serve this server's data to
the app, publish it with `scripts/build_feed.py --no-crawl --data /var/data` + `scripts/upload_feed.py`.

## Scaling note
Metering uses SQLite on the disk — fine for early volume. If write contention shows up at scale,
move the store to Postgres (Render offers a managed Postgres) and swap `SqliteUsageStore` for a
Postgres-backed `UsageStore`; nothing else changes. The job feed's crawler is the heaviest job; it
runs in one worker. Its weekly discovery pass is bounded: it probes the next `JOBS_DISCOVER_BUDGET`
(default 300) best un-probed H-1B sponsors from the ranked seed, remembers them in
`<JOBS_DATA_DIR>/discover_checkpoint.jsonl`, and adds the boards it confirms — the same crawl that
grew the committed seed (`scripts/grow_watchlist.py`), so the watchlist keeps growing after launch
without ever re-probing the same employers.
