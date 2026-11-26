# SponsorJobs broker on Cloudflare Workers (phase 1)

The managed-AI broker, rewritten as a TypeScript Cloudflare Worker with D1 (Cloudflare's SQLite)
for storage, so it can run on the Workers free plan instead of a paid Render box.

It does the same job as `backend/broker.py` for the routes the app needs to use the bundled AI:

- gives each app install an anonymous account (an id plus a secret bearer token, of which only the
  hash is stored),
- meters AI use per plan (free, the renewing Student and Standard plans, and the retired one-time
  passes their holders keep) exactly as `backend/metering.py` does,
- holds the company Anthropic key and calls Anthropic for the app.

The Python broker stays as the reference for development and tests. This is a parallel
implementation; it does not replace or change anything in `backend/`.

## What is in phase 1

| Route | What it does |
| --- | --- |
| `GET /health` | `{ok, real_providers}`; `real_providers` is true when `ANTHROPIC_API_KEY` is set |
| `POST /account/register` | new anonymous account; 50 per network per day (`429 register_limit`) |
| `GET /account/me` | `{account_id, email}` (`email` is null until the person signs in) |
| `POST /account/email/start` | `{email}`: e-mails a 6-digit code valid 10 minutes (5 per address and 5 per network per hour, `429 rate_limited`) |
| `POST /account/email/verify` | `{email, code}` (+ the device token, if any): `{token, account_id, email}`; `400 bad_code` / `400 expired` |
| `GET /account/google/start?redirect=<app callback>` | 302 to Google (PKCE + one-time state); the redirect must be `http://127.0.0.1:*` or `http://localhost:*` |
| `GET /account/google/callback` | 302 to the app callback with `#token=...` (or `#error=cancelled` / `#error=signin_failed`) |
| `POST /llm/complete` | one metered completion (`402 token_cap`, `503 free_busy`, `502` on upstream failure) |
| `POST /llm/package` | counts one tailoring run (`402 package_limit`) |
| `POST /interview/recorded` | counts one scored recorded practice interview: Free has 1 a calendar month (`402 recorded_limit`), paid plans are unlimited and not counted |
| `GET /me/usage` | plan, pass end date, interviews, packages, `recorded_left` (null = unlimited), model |
| `GET /billing/offers`, `GET /billing/packs` | the plans ON SALE (both tiers, monthly only unless `SELL_QUARTERLY` is `"1"`), the tier this caller pays, the student check, the running plan, and packs (only while a plan is active) |
| `POST /billing/subscribe` | `{interval, consent: true}`: a renewing Stripe Checkout Session at the price the server picks (student or standard), `{url, plan}` |
| `POST /billing/subscription/cancel`, `.../resume`, `POST /billing/portal` | cancel at period end in one call, undo it, or Stripe's billing portal `{url}` |
| `GET /account/student`, `POST /account/student/start`, `.../verify` | the student price check: a 6-digit code to a school email, stored once (`src/student.ts`) |
| `POST /billing/packs/<id>/checkout`, `POST /billing/passes/<id>/checkout` | a one-time Stripe Checkout Session, `{url}` (`402 pass_required` for a pack without a plan; packs on sale are `pack_1` and `pack_3`; passes only with `SELL_PASSES` set to `"1"`) |
| `POST /billing/webhook` | Stripe events (signature-checked): packs on `checkout.session.completed`; plans on `invoice.paid`, `invoice.payment_failed`, `invoice.upcoming` (renewal reminder email) and `customer.subscription.*` (`src/subscriptions.ts`) |
| `POST /mail/alias` `{name?}` | the caller's `first.last@sponsorjobs.ai` alias for the apply agent, `{alias, forward_to}` (idempotent; `409 sign_in_first` without a verified e-mail) |
| `GET /mail/alias`, `DELETE /mail/alias` | read or release the alias (`{alias, forward_to}`, both null when there is none) |
| `GET /mail/inbox?after=<cursor>` | codes and links mailed to the alias in the last 30 minutes, `{events: [{cursor, kind, code, link, subject, sender, at}], cursor}`; `after` acknowledges |
| `POST /mail/alias/notify` `{mode}` | what the person's own e-mail gets for each message: `forward` (default), `notice` or `off` |
| `GET /mail/messages?after=<cursor>&limit=<n>` | whole messages for the in-app Inbox, oldest first, at most 25, `{messages, cursor, more}`; `after` acknowledges and deletes; kept until downloaded, 30 days at most |
| `POST /agent/sign` `{method, authority, path}` | Web Bot Auth headers for one page request the apply agent makes: `{signature_input, signature, signature_agent, expires}` (60 s; 120 per account per minute, `429 rate_limited`; `503 not_configured` without `AGENT_SIGNING_KEY`) |
| `GET /.well-known/http-message-signatures-directory` | the agent's public key (a signed JWKS); routed here from sponsorjobs.ai as well, the registered Signature-Agent |
| `GET /v1/jobs/search`, `GET /v1/jobs/candidates` | server-side job search (`src/search.ts`, docs/server-search-plan.md), for SponsorJobs accounts only (`401 no_account` without a valid bearer token; 300 a minute per network, then 60 per account): the board's facets plus `scope`, cursors, at most 50 / 500 rows a page and 1,000 deep. Its own D1 database (`SEARCH_DB`, `migrations-search/`), not bound until go-live, so it answers `503 no_index`; `503 daily_budget` once the day's free allowance for search is used. `scripts/local-d1.mjs` and `scripts/measure-search.mjs` try it locally |
| `GET /v1/feed/<file>` | the locked job list (`src/feed.ts`, docs/feed.md "The locked route"): `manifest.json`, `jobs.json.gz`, `parts/*.json.gz`, `jd/*.json.gz`, streamed from the `sponsorjobs-feed` bucket through the `FEED_BUCKET` binding (stored gzip passed through, ETag / `304`). `401 no_account` without a valid bearer token; 600 a minute per network, then 120 per account; `503 feed_not_configured` without the binding |

Billing is off until the Stripe secrets are set (`STRIPE_SECRET_KEY` and at least one plan or pass price;
see `docs/stripe-setup.md`). While off, the billing routes answer exactly as the Python broker does
with billing off: nothing on sale, and checkout/webhook say `503 billing is not configured`.

Sign-in is passwordless (e-mail code or Google; `src/signin.ts`). Both end in one rule: if the
e-mail already has an account, the device logs in to it; otherwise the e-mail is attached to the
caller's device account (keeping its pass and usage), or a new account is made. Buying a pass or a
pack needs a signed-in account (`401 sign_in_required` otherwise), and the e-mail is prefilled on
Stripe Checkout. Each sign-in method stays `503 ... not configured` until its secrets are set
(`docs/sign-in-setup.md`). The Python broker's password routes (`/account/signup|login|claim`)
answer `503 ... not configured`.

The official Telegram bot (`src/telegram.ts`, `docs/notify.md`) relays alerts and button taps
between the app and Telegram. It stays `503 telegram_not_configured` until the owner runs
`./scripts/telegram-setup.sh` once, which stores the three `TELEGRAM_*` secrets and points
Telegram's webhook here.

When the company Anthropic key fails (no credit, disabled, invalid), `src/outage.ts` answers the
app with a structured `503 ai_unavailable` (reason `company` or `transient`), records it so
`/health` says `company_outage since <time>`, and messages the owner once an hour through the
Telegram bot with the billing link. Set `OWNER_TELEGRAM_CHAT_ID` (a secret) or rely on the first
`ADMIN_EMAILS` account's linked chat; details in `docs/admin-dashboard.md`.

Mail aliases (`src/mail.ts`, `docs/mail-alias.md`): each student gets `first.last@sponsorjobs.ai`
to give job portals. Email Routing's catch-all delivers to this Worker's `email` handler, which
keeps the one-time code or verification link for 30 minutes (so the apply agent can poll it),
holds the whole message in `mail_messages` until the student's app has downloaded it for the
in-app Inbox (30 days at most; attachment names only), and tells the student's verified e-mail
through Resend: the whole message, a short notice, or nothing (`notify_mode`).

Request signing for the apply agent (`src/botauth.ts`, `docs/signed-agent.md`): the agent identifies
itself to job sites with RFC 9421 signatures in Cloudflare's Web Bot Auth shape. The Ed25519 key is
the `AGENT_SIGNING_KEY` secret, only in this Worker; its public half is published at
`https://sponsorjobs.ai/.well-known/http-message-signatures-directory` (a route on this Worker).

## Run it locally

Needs Node 20 or newer. No Cloudflare account is needed for any of this.

```sh
cd worker
npm install                       # once
npm run migrate:local             # create the local D1 tables
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .dev.vars   # optional: a real key for real completions
npx wrangler dev                  # serves on http://localhost:8787
```

Point the app at it with `TAILOR_BROKER_URL=http://localhost:8787`. Without a key, `/health` says
`real_providers: false` and the app falls back to the person's own key, as it does with the Python
broker. To test with the old `X-Tailor-User` header instead of a token, put `DEV_HEADER_AUTH=1` in
`.dev.vars` (never in production).

## Tests

```sh
npm test            # vitest, inside the real Workers runtime, against a local D1
npm run typecheck   # tsc
npm run build       # wrangler deploy --dry-run: bundles without deploying
```

The tests never touch the network: every call to Anthropic is stubbed.

## Deploy checklist (owner)

1. `npx wrangler login` and pick the SponsorJobs Cloudflare account.
2. Create the database: `npx wrangler d1 create sponsorjobs-broker`. Copy the `database_id` it
   prints into `wrangler.jsonc`, in the `d1_databases` entry.
3. Create the tables: `npx wrangler d1 migrations apply DB --remote`.
4. Add the Anthropic key: `npx wrangler secret put ANTHROPIC_API_KEY` and paste it when asked.
5. Deploy: `npx wrangler deploy`. Check `https://sponsorjobs-broker.<your-subdomain>.workers.dev/health`
   says `"real_providers": true`.
6. Custom domain: in the Cloudflare dashboard, Workers & Pages, `sponsorjobs-broker`, Settings,
   Domains & Routes, add `api.sponsorjobs.ai` (the sponsorjobs.ai zone must be on the same account).
7. Point the official app build at `https://api.sponsorjobs.ai` (`TAILOR_BROKER_URL`).
8. Request signing for the apply agent: the key, the extra route and the Cloudflare registration
   are in `docs/signed-agent.md`.
9. The locked job list: right after the deploy that adds `/v1/feed/*`, block the job files on
   `feed.sponsorjobs.ai` with the one WAF rule in docs/feed.md ("Go-live"). No secret is needed.

Moving existing accounts from Render: the D1 tables use the same names and columns as the Python
broker's SQLite file (`users`, `usage`, `accounts`, `tokens`), so existing tokens keep working
after an export and import of those tables.

## Free plan limits

The free plan allows 100,000 requests a day and 10 ms of CPU per request. Time spent waiting on
Anthropic or D1 does not count as CPU. A completion uses roughly 1 to 3 ms of CPU (one SHA-256 of
the token, parsing and re-serialising the request, three short D1 round trips), so it fits; very
large conversation histories (hundreds of KB) are the one case to watch in the dashboard. The
locked job list (`/v1/feed/*`) costs one request per file the app reads, ~25 to 80 a day per
install with the app open (docs/feed.md, "Worker requests per install"), and almost no CPU (the body
is streamed from R2 untouched).

The admin dashboard tracks how close the whole account is to these limits: the Health page's
**Cloudflare Free Plan** card (Workers requests, D1 rows read and written, D1 storage, each as used
/ limit and a percentage, latest day and 7-day peak) and a banner on every admin page, "Time to
switch to Workers Paid ($5 a month)", once any 7-day peak reaches 70% or search answered
`daily_budget` in the last 7 days ("Getting close" from 50%). The numbers are a daily snapshot the
feed workflow writes into the search database from Cloudflare's GraphQL Analytics API
(`src/admin_cf.ts`, `scripts/cf_usage_snapshot.py`, `migrations-search/0002_cf_usage.sql`), so
the Worker writes nothing per request for it. Its Cloudflare token (`SEARCH_D1_TOKEN`) needs **D1
Edit** and **Account Analytics Read**; details in docs/server-search-plan.md ("Watching it").
