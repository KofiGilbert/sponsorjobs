> Historical note: the product was renamed from "Tailor" to **SponsorJobs** (2026-10). "Tailor" below refers to the app as it was then; it now names only the résumé-tailoring feature.

# Bundled Managed-AI Backend: Design and Plan

Status: APPROVED direction (Kofi, 2026-07-29). Not yet built. This doc is the reviewable
plan; the user-facing privacy copy and the infra choices below still need Kofi's sign-off on
specifics (marked "DECISION NEEDED").

## 1. The decision

Both paid AI features are **bundled and company-paid**, not bring-your-own-key:

- **LLM (Anthropic)** for resume tailoring, cover letters, coaching.
- **Avatar (Tavus)** for the Round-2 mock interview.

Users never obtain, paste, or see an API key. Free gets a small monthly allowance; for more
they subscribe to a plan that renews until cancelled (Student or Standard, see section 5) and
everything "just works." Their own AI key still works for unlimited tailoring. This removes the biggest friction wall for the
non-technical international-student audience.

## 2. What this does NOT change (the privacy promise holds)

Tailor stays a **local app**. The user's real data (profile, work history, saved answers,
resumes, job list, applications, all their files) continues to live on **their own machine**.
We do not upload it, store it, or keep a copy. We become a **hybrid**: a local app plus a
thin cloud broker that exists only to run the paid AI features.

### The evolved privacy promise (final wording TBD with Kofi)

The original v1 line "nothing ever leaves your computer" was never fully literal: tailoring
already sends the resume text to the AI provider to do the work. The honest, still-strong
promise:

> Your data lives on your computer. We do not upload or store your resumes, your history, or
> your applications. To run the AI features, only the text needed for that task is sent to the
> AI provider to do the work, then discarded. We never keep it, mine it, or sell it.

## 3. Architecture

```
User's machine (unchanged)                     Company backend (NEW, thin broker)
--------------------------                     ---------------------------------
Local app + local SQLite/files   <-- HTTPS -->  - holds the company API keys (server-side only)
(all personal data stays here)                  - mints per-session tokens / forwards requests
                                                - meters usage per user, enforces plan quotas
                                                - stores ONLY usage counts + billing state
                                                - never persists resumes / JD / interview media
                                                       |                         |
                                                  Anthropic API             Tavus API
```

The company keys live ONLY on the broker (in the host's secrets store), never in the
downloaded app (or they would be extractable and abusable). This is the whole reason a
backend is required.

### Privacy design principle: stateless pass-through

The broker forwards a request to the provider, returns the result, and logs only the
**meter** (tokens used, avatar minutes used) for billing. It does not write resume text, job
descriptions, or interview media to any store. That is what keeps promise-A ("we do not store
or access your data") true even though a backend now exists.

## 4. Data flow per feature (what actually touches the backend)

| Feature | Path | What the broker sees | Stored by us? |
|---|---|---|---|
| Tailoring (LLM) | app -> broker -> Anthropic -> back | the prompt (resume + JD) in transit | No, transient only |
| Avatar (Tavus) | broker mints a session token; the user's browser then streams video **peer-to-peer with Tavus** | session start/stop + minutes | No (video never flows through us) |
| Billing | broker <-> payment provider | pass end date + balances, usage counters | Yes: counts + billing state, no personal content |

## 5. Metering and quota model (protects the margin)

Corrected model pricing (per million tokens): Opus 4.8 $5/$25, Sonnet 4.6 $3/$15 (default),
Haiku 4.5 $1/$5. Avatar (Tavus) ~ $0.32-0.37 per minute; a 15-minute live interview ~ $5 raw.

- **LLM:** bundled on the cost-efficient model (Sonnet 4.6 default; Haiku for bulk/simple
  tasks later). Track tokens per user.
- **Avatar:** the one feature that can lose money, so it is metered hard:
  - per-second billing against the user's allowance/credits
  - explicit "Start interview" click (avoid paying Tavus's 30-second minimum on accidental opens)
  - idle + max-length timeout
  - one concurrent session per user
  - hard stop at zero balance -> "buy credits" prompt (never silent overage)
- **Cost-per-user tracking from day one** so real prices are set on real data.

### Pricing (decided by the owner 2026-11-25): monthly plans only, stated free limits

Plans renew automatically (the industry standard), as Stripe subscriptions sold by the Worker
broker (`worker/src/billing.ts`, `worker/src/subscriptions.ts`). **On sale at launch:**

| What | Price | Renews | Live interviews (20 min each) | Tailored applications | Recorded practice interviews, scored |
|---|---|---|---|---|---|
| Free (no card) | $0 | calendar month | 0 | 3 a month | 1 a month |
| `student_monthly` | $29 | every month | 3 a period | 40 a period | unlimited |
| `standard_monthly` | $49 | every month | 3 a period | 60 a period | unlimited |
| Extra live interview, 1 (`pack_1`) | $12 | one-time, never expires | 1 | | |
| Extra live interviews, 3 (`pack_3`) | $33 | one-time, never expires | 3 | | |

Free also keeps every job with sponsorship marks, the tracker and autofill, on the cheaper model
(`claude-haiku-4-5`) with the existing monthly token cap.

**Not sold, kept in code:**

| What | Price | Why it is still there | How to sell it again |
|---|---|---|---|
| `student_quarterly`, `standard_quarterly` | $69, $119 every 3 months (9 interviews; 100 applications on Student, 150 on Standard) | A subscription already on one keeps renewing, reading and cancelling as before | Set `SELL_QUARTERLY` to `"1"` in `worker/wrangler.jsonc` and deploy; run `scripts/stripe_setup.py --sell-quarterly` if the prices do not exist yet. The app shows its Monthly / Every 3 months switch on its own once the broker offers one |
| `pass30`, `pass90` (one-time passes) | $29, $69 | Holders keep a pass until it ends | `SELL_PASSES` = `"1"` with `STRIPE_PRICE_PASS30/90` set |
| `pack_5` (5 extra interviews) | $39 | A 5-pack already paid for is still granted | Add it back to `SOLD_PACKS` in `worker/src/metering.ts` |

The switches stop NEW sales only: offers, the plans panel and checkout. The webhook handling for a
plan or pass someone already holds is untouched.

**The free recorded interview limit** is enforced in the broker, where the free packages are
(`worker/src/metering.ts` `recordedLeft`, `worker/src/store.ts` `takeFreeRecorded`, one guarded
SQL statement). The app calls `POST /interview/recorded` once, the first time a recorded interview
is scored; Free gets 1 per calendar month (`usage.recorded_interviews`, migration 0017, the same
monthly row as the free packages, so it resets with them) and `402 recorded_limit` after. Paid
plans are unlimited and never counted. `GET /me/usage` reports `recorded_left` (null = unlimited),
which the app reads before starting another one.

**Upgrade moments.** Each is one plain line with a "See plans" button that opens the one plans
panel; no price is written anywhere else in the app:

- the 3rd free tailored resume is used: "You've used your 3 free tailored resumes this month.
  Plans include 40 or 60 a month."
- a Free user starts another recorded interview after this month's is used: "You've used this
  month's practice interview. Plans include unlimited recorded practice."
- the Round 1 report, Free only: "Practise live with an interviewer".
- a Free user opens the live interview (the Round 2 gate).

#### Margins at these prices

Conservative costs. **The Tavus rate is an assumption the owner must confirm against the real
invoice:** $0.37 a minute, so one 20-minute live interview costs **$7.40**. A tailored application
is taken at about $0.15 on the paid model (about $0.05 on the free one), the figure used since
2026-10-02; it has not been re-measured on the current models. Stripe is 2.9% + $0.30 a US card
payment, plus 0.7% Billing on a subscription payment.

| What | Price | Stripe | Tavus if every interview is used | Tailoring if every application is used | Left if everything is used | Left at half use |
|---|---|---|---|---|---|---|
| Student, monthly | $29.00 | $1.34 | 3 x $7.40 = $22.20 | 40 x $0.15 = $6.00 | **minus $0.54** | $13.56 |
| Standard, monthly | $49.00 | $2.06 | $22.20 | 60 x $0.15 = $9.00 | $15.74 | $31.34 |
| 1 extra interview | $12.00 | $0.65 | $7.40 | | $3.95 | |
| 3 extra interviews | $33.00 | $1.26 | $22.20 | | $9.54 | |
| Free | $0 | $0 | $0 | 3 x $0.05 = $0.15 | minus $0.15, plus a few cents for the one scored recorded interview | |

Reading it: the Student plan includes 40 tailored applications a month, not 60 (owner decision
2026-11-25), which takes $3.00 off its full-use cost. It now loses about $0.54 a month on a person
who uses all 3 live interviews AND all 40 applications, and earns about $13.56 on one who uses half
(1.5 interviews at $11.10, 20 applications at $3.00). Without the 0.7% Billing fee, Stripe is $1.14
and those two figures are minus $0.34 and $13.76. The live interviews are the cost that matters
(about three quarters of the Student plan's full-month cost). Scoring a recorded interview is a handful of model
calls, cents at most; on paid plans it is unlimited but still under the plan's monthly token cap.
The cost of scoring one recorded interview has not been measured.

- **Who pays the student price:** students, and graduates up to 24 months after their graduation
  month, verified ONCE while still a student with a 6-digit code to a school email
  (`worker/src/student.ts`, free and self-built; the list of academic domains lives there).
  The broker picks the price at checkout from that stored check; the app only sends the interval.
- **Entitlement:** each paid invoice (`invoice.paid`) starts a period with fresh allowances until
  that invoice's period end, once per invoice id. A failed renewal marks the plan; access ends with
  the paid period unless a retry succeeds. A deleted subscription ends access at once.
- **Cancel** in one click from the app (cancel at period end; undo while the period lasts), plus
  Stripe's billing portal for the card and invoices. **Renewal reminder:** our own e-mail through
  the mailer when Stripe sends `invoice.upcoming` (days ahead set in the Stripe dashboard).
- **Disclosure and consent** (auto-renewal laws such as California's): the price, the interval,
  that it renews until cancelled and how to cancel sit next to the button with a checkbox; the
  broker refuses a checkout without `consent: true` and records the consent time.
- Extra interview packs are one-time and sold only while a plan is active: 1 for $12, 3 for $33
  (2026-11-25). The one-time passes below are retired: holders keep them until they end, and a
  plan bought on top carries what is left into its first period.

### Pricing before plans (decided by the owner 2026-10-02, retired 2026-11-20)

Research behind it: comparable tools sit at $29 to $40 a month; a 15-minute Tavus interview
costs about $4.6 to $5; a tailored package on Claude Sonnet about $0.15 (about $0.05 on Haiku);
freemium converts about 2%; auto-renewing trials are the top complaint about resume tools. So:
**no subscription and no auto-renew.** Everything is a one-time payment.

| Tier | Price | Length | Live interviews (Round 2, 900 s each) | Tailored packages | Model |
|---|---|---|---|---|---|
| `free` (default, no card) | $0 | calendar month | 0 | 3 a month (unlimited on own key) | `claude-haiku-4-5` |
| `pass30` Job Hunt Pass | $29 once | 30 days | 3 (2,700 s) | 60 | `claude-sonnet-5-5` |
| `pass90` Season Pass | $69 once | 90 days | 9 (8,100 s) | 150 | `claude-sonnet-5-5` |
| Extra interviews (packs) | 1 for $9, 3 for $24, 5 for $35 | never expire | 1 / 3 / 5 | 0 | |

Rules as implemented (`backend/metering.py`, `backend/billing.py`):

- **A package is one tailoring run.** The app calls `POST /llm/package` once at the start of a
  run on the bundled AI (guided build, variant, job tailor, each autopilot role); the broker
  counts it against the pass balance or the free monthly 3 and answers 402
  `{reason: "package_limit"}` when none is left. With an Anthropic key saved, the app then runs
  that package on the person's own key (BYOK stays unlimited); without one it shows the passes.
  The per-calendar-month token cap (free 400k, passes 8M) stays as a safety net on every
  `/llm/complete`.
- **Pass allowances belong to the pass period**, stored on the account as `pass_until` plus a
  balance of interview seconds and packages. When `pass_until` passes, the account is free
  again; there is nothing to cancel. Leftovers of an expired pass are not revived.
- **Stacking:** buying a pass while one is active moves `pass_until` out by the new pass's days
  and adds its interviews and packages to what is left.
- **Extra interviews** are sold only while a pass is active (`POST /billing/packs/<id>/checkout`
  answers 402 `pass_required` otherwise). Once bought they never expire and are spent after
  the pass's own interviews.
- **Round 2** starts only when pass seconds + extra interview seconds cover a full 900 s.
- **Model by tier:** free on Haiku, passes on Sonnet; override with `TAILOR_FREE_MODEL` /
  `TAILOR_PASS_MODEL`.

Broker routes: `GET /billing/offers` -> `{passes: [{id, price_label, days, interviews, packages,
auto_renew: false}], packs: [...], current: {tier, pass_until, interviews_left, packages_left}}`;
`POST /billing/passes/<id>/checkout` -> `{url}`; `GET /billing/packs`;
`POST /billing/packs/<id>/checkout` -> `{url}`; `POST /billing/webhook`. A pass checkout is a
Stripe Checkout Session in `mode="payment"` with `metadata={kind: "pass", pass, days, seconds,
packages}`; the `checkout.session.completed` webhook (paid) grants it once per session id (the
processed-events table). Subscription events are handled by the Worker for the renewing plans
(above). Old accounts on the 2026 tiers `trial`/`student`/`pro`/`browse` are read as free.

### Margins sanity check (cost at Tavus $4.6 per interview, Sonnet $0.15 and Haiku $0.05 per package)

Stripe's card fee (2.9% + $0.30) is included.

| Product | Revenue | Fee | Interviews cost | Packages cost if all used | Net if everything is used | Net with all interviews + 20% of packages |
|---|---|---|---|---|---|---|
| Free (per user per month) | $0 | | $0 | 3 x $0.05 = $0.15 | -$0.15 | -$0.15 |
| Job Hunt Pass | $29 | $1.14 | 3 x $4.6 = $13.80 | 60 x $0.15 = $9.00 | **+$5.06** | +$9.56 (30 packages) |
| Season Pass | $69 | $2.30 | 9 x $4.6 = $41.40 | 150 x $0.15 = $22.50 | **+$2.80** | +$11.80 (90 packages) |
| 1 extra interview | $9 | $0.56 | $4.60 | | +$3.84 | |
| 3 extra interviews | $24 | $1.00 | $13.80 | | +$9.20 | |
| 5 extra interviews | $35 | $1.32 | $23.00 | | +$10.68 | |

Allowances were cut from 150/450 to 60/150 packages (decided 2026-10-02) so a pass is
profitable even when every interview and every package is used. A heavy job seeker tailors
30 to 60 applications a month, so 60 per 30 days is not a practical limit.

### What to create in Stripe

1. Products with **one-time** prices (not recurring): Job Hunt Pass $29, Season Pass $69,
   1 extra interview $9, 3 extra interviews $24, 5 extra interviews $35.
   `python -m scripts.stripe_setup` creates all five idempotently in the sandbox and prints
   the env lines.
2. Put the price ids in the broker env: `STRIPE_PRICE_PASS30`, `STRIPE_PRICE_PASS90`,
   `STRIPE_PRICE_PACK_1`, `STRIPE_PRICE_PACK_3`, `STRIPE_PRICE_PACK_5`. An unset one is just
   not offered; billing turns on with the secret key and at least one pass price.
3. A webhook endpoint at `https://<broker>/billing/webhook` listening to
   `checkout.session.completed` and `checkout.session.async_payment_succeeded`; its signing
   secret goes in `STRIPE_WEBHOOK_SECRET`.
4. `STRIPE_SECRET_KEY` (or `STRIPE_TEST_SECRET_KEY` for the sandbox).
5. Delete or archive the old monthly prices (Job Seeker / Pro); `STRIPE_PRICE_STUDENT` and
   `STRIPE_PRICE_PRO` are no longer read.

## 6. Build phases

- **P1 (buildable now, no keys/infra): local broker skeleton.** A small service matching the
  stack (Flask), implementing token-minting, metering, and quota enforcement against a **fake
  provider** (like the existing FakeLLM). Fully tested offline. Nothing deployed, no real key,
  no real money. Proves the pattern and is ready to wire real keys into.
- **P2: wire the real Tavus key** -> avatar end-to-end. Needs Kofi's Tavus key (free tier is fine to build/test).
- **P3: wire a company Anthropic key** -> bundle the LLM. Needs a company Anthropic account.
- **P4: billing + plan enforcement.** Needs a payment-provider business account.
- **P5: deploy the broker** to hosting. Needs a hosting decision.
- **P6: point the app at the broker** instead of a local BYO key; finalize in-product privacy copy.

## 7. DECISIONS NEEDED from Kofi (with recommendations)

1. **Hosting** for the broker. Recommendation: one small managed instance on a simple
   platform (e.g. Render / Fly.io / Railway), low monthly cost, easy deploys. Not AWS-scale
   complexity for a thin broker.
2. **Payment provider.** Decided: Stripe, one-time Checkout payments only (passes and extra
   interviews); no subscriptions.
3. **Company Anthropic account.** A business API account funded by the company, separate from
   any personal key, to serve all users' tailoring.
4. **Tavus account.** Free tier (25 min) is enough to BUILD and TEST now; becomes the
   company's funded business account (Growth = 10 concurrent streams) at launch.
5. **Security.** The broker holds the company keys in the host's secrets manager, never in the
   app or the repo. The broker is the only thing that can call the providers. A short security
   review before P5/P6 (it is money-adjacent).

## 8. What is gated on what

| To do this | We need |
|---|---|
| P1 local skeleton | nothing (build now) |
| P2 real avatar | Kofi's Tavus key |
| P3 bundled LLM | company Anthropic account |
| P4 billing | payment-provider account (Stripe) |
| P5 deploy | hosting choice + account |
| P6 go live | all of the above + final privacy copy |

## 9. Note for the other agent (VS Code Claude Code)

This supersedes the "no server in v1 / bring-your-own-key" assumption in CLAUDE.md sections 1
and 5 for the paid AI features specifically. The app stays local-first for all personal data;
only the paid AI calls move behind a company-run broker. Coordinate before touching the
intake/tailoring data model.
