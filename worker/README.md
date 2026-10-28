# SponsorJobs broker on Cloudflare Workers (phase 1)

The managed-AI broker, rewritten as a TypeScript Cloudflare Worker with D1 (Cloudflare's SQLite)
for storage, so it can run on the Workers free plan instead of a paid Render box.

It does the same job as `backend/broker.py` for the routes the app needs to use the bundled AI:

- gives each app install an anonymous account (an id plus a secret bearer token, of which only the
  hash is stored),
- meters AI use per plan (free, Job Hunt Pass, Season Pass) exactly as `backend/metering.py` does,
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
| `GET /me/usage` | plan, pass end date, interviews, packages, model |
| `GET /billing/offers`, `GET /billing/packs` | passes and packs on sale (packs only while a pass is active) and where the caller stands |
| `POST /billing/passes/<id>/checkout`, `POST /billing/packs/<id>/checkout` | a one-time Stripe Checkout Session, `{url}` (`402 pass_required` for a pack without a pass) |
| `POST /billing/webhook` | Stripe's `checkout.session.completed`: grants the pass or pack once per session (signature-checked) |
| `POST /mail/alias` `{name?}` | the caller's `first.last@sponsorjobs.ai` alias for the apply agent, `{alias, forward_to}` (idempotent; `409 sign_in_first` without a verified e-mail) |
| `GET /mail/alias`, `DELETE /mail/alias` | read or release the alias (`{alias, forward_to}`, both null when there is none) |
| `GET /mail/inbox?after=<cursor>` | codes and links mailed to the alias in the last 30 minutes, `{events: [{cursor, kind, code, link, subject, sender, at}], cursor}`; `after` acknowledges |
| `POST /agent/sign` `{method, authority, path}` | Web Bot Auth headers for one page request the apply agent makes: `{signature_input, signature, signature_agent, expires}` (60 s; 120 per account per minute, `429 rate_limited`; `503 not_configured` without `AGENT_SIGNING_KEY`) |
| `GET /.well-known/http-message-signatures-directory` | the agent's public key (a signed JWKS); routed here from sponsorjobs.ai as well, the registered Signature-Agent |

Billing is off until the Stripe secrets are set (`STRIPE_SECRET_KEY` and at least one pass price;
see `docs/stripe-setup.md`). While off, the billing routes answer exactly as the Python broker does
with billing off: nothing on sale, and checkout/webhook say `503 billing is not configured`.

Sign-in is passwordless (e-mail code or Google; `src/signin.ts`). Both end in one rule: if the
e-mail already has an account, the device logs in to it; otherwise the e-mail is attached to the
caller's device account (keeping its pass and usage), or a new account is made. Buying a pass or a
pack needs a signed-in account (`401 sign_in_required` otherwise), and the e-mail is prefilled on
Stripe Checkout. Each sign-in method stays `503 ... not configured` until its secrets are set
(`docs/sign-in-setup.md`). The Python broker's password routes (`/account/signup|login|claim`) and
Telegram answer `503 ... not configured`.

Mail aliases (`src/mail.ts`, `docs/mail-alias.md`): each student gets `first.last@sponsorjobs.ai`
to give job portals. Email Routing's catch-all delivers to this Worker's `email` handler, which
keeps only the one-time code or verification link for 30 minutes (so the apply agent can poll it)
and forwards the whole message to the student's verified e-mail through Resend.

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

Moving existing accounts from Render: the D1 tables use the same names and columns as the Python
broker's SQLite file (`users`, `usage`, `accounts`, `tokens`), so existing tokens keep working
after an export and import of those tables.

## Free plan limits

The free plan allows 100,000 requests a day and 10 ms of CPU per request. Time spent waiting on
Anthropic or D1 does not count as CPU. A completion uses roughly 1 to 3 ms of CPU (one SHA-256 of
the token, parsing and re-serialising the request, three short D1 round trips), so it fits; very
large conversation histories (hundreds of KB) are the one case to watch in the dashboard.
