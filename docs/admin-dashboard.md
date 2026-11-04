# Admin dashboard (admin.sponsorjobs.ai)

A private dashboard that shows how SponsorJobs is doing and lets the founder handle the few support
jobs that come up: revenue, users, AI and interview costs, the free AI limit, and the health of every
outside service. It lives in the same Cloudflare Worker as the broker (`worker/src/admin.ts`, with
`admin_ui.ts`, `admin_pages.ts`, `admin_data.ts`, `admin_actions.ts`) and only answers on
`admin.sponsorjobs.ai`. The look follows ByeWind's free "Dashboard UI Kit" (SnowUI) from Figma
Community, CC BY 4.0, rebuilt in our own markup and credited in the page footer.

**Cost: $0.** Cloudflare Access (Zero Trust) is free for up to 50 users, and the Worker route uses
the plan you already have.

## How it stays private (two locks)

1. **Cloudflare Access** stands in front of `admin.sponsorjobs.ai`. Before anyone sees the page,
   Cloudflare asks them to log in, and only your e-mail is allowed.
2. **The Worker checks again.** Access adds a signed token to every request it lets through. The
   Worker verifies that token itself (signature, the application's AUD tag, the team, the expiry)
   and answers **403 Forbidden** if anything is off, or if the two settings below are still empty.
   So the page is closed by default, and a mistake in the Access policy alone cannot open it.

The pages never show tokens, token hashes, sign-in code hashes, or a forwarded mail's code or link.
E-mails are masked (`ka***@gmail.com`) everywhere except one person's page after **Show full e-mail**,
and that reveal is written to the audit log. The application e-mail alias is masked the same way,
because it carries the person's name.

## What it shows

Left sidebar: **Overview, Users, Revenue, Usage & costs** (dashboards) and **Health, Audit log**
(system). The search box at the top finds a person by e-mail or account id.

- **Overview:** revenue over 30 days (after refunds, from Stripe), active passes, accounts active this
  month, new this week with the share that ever bought a pass; new accounts per day (30 days); plans
  as a donut; this month's revenue, estimated AI and Tavus cost and margin; today's free AI use
  against its limit; recent e-mail sign-ins. The right column lists **Needs attention**:
  - the company AI key failing
  - the job feed unreadable or older than 8 hours
  - a failed GitHub `feed`, `grow-boards` or `tests` run
  - the free AI paused or 75% used
  - Resend near its limit
  - a disputed payment
  - Stripe in test mode or unreadable
  - no Telegram chat for alerts
  - suspended accounts

  Below that, **Recent activity** shows sign-ups, e-mail sign-ins, paid checkouts and admin actions.
  Health gets a red count in the sidebar when something is failing.
- **Users:** every account, newest first. E-mails are masked. You can filter (Free, On a pass,
  E-mail sign-in, Suspended), sort by AI use this month, and page through 50 at a time.
- **A person** (`/users/<id>`) shows:
  - the plan and when it ends, with what is left on the pass
  - extra interviews, signed-in devices (a count), Telegram linked or not
  - the application e-mail and how many mails it got (never their content)
  - usage by month, live interviews, and every admin action on this account

  The support actions are below.
- **Revenue:** net revenue over 7, 30 and 90 days, refunds and repeat buyers. Each payment shows its
  product (worked out from the price), the masked buyer and a link to it in Stripe, and disputes are
  flagged. There is a by-product donut and a check that payments in Stripe match passes granted by
  the webhook. Passes are one-time, so there is no MRR or churn. Refunds and disputes are handled
  in Stripe, never here.
- **Usage & costs:** this month's AI and interview cost, cost per active user with the 95th
  percentile (a runaway account shows there even when the average is calm), and the free AI for the
  last 14 days against its limit. It also has the **free AI limit control**, top AI users this month
  (free users show their share of the monthly cap), monthly totals, sign-ups today per network
  (salted hash, never an address, against the per-network limit), and e-mail domains.
- **Health:** a status for each service:
  - the Anthropic key
  - the job feed (job count and age, from `feed.sponsorjobs.ai/feed/manifest.json`)
  - the sponsor index
  - the latest GitHub `feed`, `grow-boards`, `tests` and `release` runs
  - Stripe mode
  - Resend e-mails
  - the Telegram bot and owner chat
  - Tavus, Google sign-in, and the apply agent's signing key

  The **Send test alert** button is here too.
- **Audit log:** every action, newest first, with who (the Access e-mail), what, which account, the
  detail, and the reason. Rows are never edited or deleted by the app.
- The overview numbers as JSON at `https://admin.sponsorjobs.ai/admin/api/stats`.

Stripe, the feed and GitHub are fetched at most every 5 to 10 minutes per Worker instance. A page
still loads when any of them is down; it says "Unreadable" in that row. The GitHub repo is public, so
no token is needed. An optional `GITHUB_TOKEN` secret raises GitHub's rate limit.

## Actions

Every action:
- is a form on the dashboard
- is accepted only from the dashboard itself (another site's form gets 403)
- needs a typed reason
- writes an audit row in the same database transaction as the change

| Action | Where | What it does |
|---|---|---|
| Give a pass | a person | Starts a Job Hunt or Season Pass now, or adds to an active one. No charge; not counted as a Stripe purchase. |
| Add live interviews | a person | 1 to 10 interviews (20 minutes each) as credit that never expires. |
| Suspend | a person | Type `SUSPEND`. The account's tokens stop working, so the app is signed out everywhere and cannot use AI, until restored. Nothing is deleted. |
| Restore | a person | The same tokens work again. |
| Show full e-mail | a person | Shows the full e-mail and alias once, and logs it. |
| Free AI limit | Usage & costs | Set a daily token limit, go back to the deploy's `FREE_POOL_DAILY_TOKENS`, or pause the free AI (type `PAUSE`). Takes effect on the next request with no deploy; pass holders are never affected. |
| Send test alert | Health | One Telegram message to the owner, logged with the result. |

A suspended anonymous account can register a new anonymous one. Suspension is meant for e-mail
accounts and obvious abuse. The free AI limit and the per-network sign-up limit are what stop
scripts.

## Deploying a change that adds tables

Migration `0011_admin_actions.sql` adds `admin_actions`, `admin_settings` and
`accounts.suspended_at`, and the Worker reads them on every request. **Apply the migration BEFORE
deploying the code**, or every AI request and every sign-in fails until it is applied:

```
cd ~/sponsorjobs/worker
npx wrangler d1 migrations apply DB --remote
npx wrangler deploy
```

## Setup, step by step

Do these in order. You need the Cloudflare account that holds `sponsorjobs.ai` (BlackOrigin).

### 1. The address (already done in the code)

`worker/wrangler.jsonc` now has a second route:

```jsonc
"routes": [
  { "pattern": "api.sponsorjobs.ai", "custom_domain": true },
  { "pattern": "admin.sponsorjobs.ai", "custom_domain": true }
],
```

You do not need to touch DNS. When you deploy (step 4), Cloudflare creates `admin.sponsorjobs.ai`
for the Worker by itself.

### 2. Create the Cloudflare Access application

1. Go to <https://one.dash.cloudflare.com> and pick the BlackOrigin account.
   - If it is your first time, Cloudflare asks you to choose a **team name** (for example
     `sponsorjobs`) and the **Free** plan. Choose Free; it asks for a card but charges $0.
2. In the left menu click **Access** → **Applications** → **Add an application** → **Self-hosted**.
3. Fill in:
   - **Application name:** `SponsorJobs admin`
   - **Session duration:** `24 hours` (how often you log in again)
   - **Public hostname:** subdomain `admin`, domain `sponsorjobs.ai`, path empty.
4. **Policies** → **Add a policy**:
   - **Policy name:** `Only Kofi`
   - **Action:** `Allow`
   - **Include** → selector **Emails** → value: your own e-mail address.
   - Add nothing else. Save the policy.
5. **Login methods:** keep **One-time PIN** ticked. That is the simplest: Cloudflare e-mails you a
   code each time you log in. (Optional: to use "Sign in with Google" instead, first add Google
   under **Settings** → **Authentication** → **Login methods** → **Add new** → **Google** and follow
   Cloudflare's instructions, then tick it here.)
6. Click through to **Save** / **Add application**.

### 3. Copy the two values into the Worker settings

You need two things from the Cloudflare dashboard:

- **Team domain:** **Settings** → **Custom pages** (or **Settings** → **General**) shows
  `<your-team>.cloudflareaccess.com`. You only need the `<your-team>` part (the full
  `xxx.cloudflareaccess.com` also works).
- **AUD tag:** **Access** → **Applications** → `SponsorJobs admin` → **Configure** →
  **Overview** (or **Basic information**) → **Application Audience (AUD) Tag**. Click the copy icon.
  It is a long string of letters and numbers.

Neither is a secret, so they go in the settings file. Open `worker/wrangler.jsonc`, find these lines
near the bottom and fill them in:

```jsonc
"ACCESS_TEAM_DOMAIN": "your-team",
"ACCESS_AUD": "paste-the-aud-tag-here",
// Optional extra lock: put YOUR e-mail here; only it gets in, even if the Access policy is loosened.
"ADMIN_EMAILS": "you@example.com"
```

Save the file. (Settings typed into the Cloudflare dashboard are overwritten by the next deploy, so
keep them in this file.)

### 4. Update the database, then deploy

From the `worker` folder, in your own terminal:

```bash
cd worker
npx wrangler d1 migrations apply DB --remote   # FIRST: adds the new date columns and counters
npx wrangler deploy                            # THEN: the new code, and admin.sponsorjobs.ai
```

The order matters: the new code writes to the new columns, so the database must have them before
the code goes live. Type `y` if wrangler asks to confirm the migration.

### 5. Check it

1. Open <https://admin.sponsorjobs.ai> in a private window. Cloudflare's login page should appear.
   Enter your e-mail, then the code it sends you. The dashboard opens.
2. Try with any other e-mail: Cloudflare should refuse to send a code (or show "access denied").
3. If you see a plain **Forbidden** after logging in, the Worker refused the token: check that
   `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` in `wrangler.jsonc` match step 3 exactly (and
   `ADMIN_EMAILS`, if set, has your e-mail), then deploy again.
4. `https://api.sponsorjobs.ai/health` should still answer as before. The app is not affected.

## When the company AI key fails (`worker/src/outage.ts`)

On 2026-11-01 the company Anthropic organization ran out of credits: every model call failed, the
app told people "this one needs you", and nobody was told. Now:

- The broker classifies each failed model call. No credit, billing, a disabled organization or an
  invalid key is **ours** ("company"); a rate limit, an overload or a 5xx is **transient**. The app
  gets `503 {"error": "ai_unavailable", "reason": "company" | "transient", "retry_after"?: n}`, never
  Anthropic's words. People see "SponsorJobs' AI is temporarily unavailable. Your pass and packages
  are not affected." with no upgrade button; the person's own limits (packages, pass, token cap) keep
  their usual messages. A transient answer is retried by the app a few times before it says "busy".
- On the first company failure the owner gets ONE Telegram message through the official bot with
  the cause and the billing link (`https://console.anthropic.com/settings/billing`), then one an
  hour while it lasts, and one "back" message when a call goes through again. Every outage is also
  in the Worker log (`npx wrangler tail`).
- **Setup (once):** tell the Worker which chat to message. Either
  `npx wrangler secret put OWNER_TELEGRAM_CHAT_ID` (your private chat id with the bot: message
  @userinfobot on Telegram, or read `chat_id` from the `telegram_chats` row of your account), or
  leave it unset and the first address in `ADMIN_EMAILS` is used, provided that account has
  connected Telegram in the app (Settings, Notifications). With neither, only the log is written.
- **Check it works:** press **Send test alert** on the dashboard's AI card. It sends one Telegram
  message the same way an outage alert is sent and shows "Test alert sent" or the reason it was not
  (for example "telegram failed: ... chat not found" when the id is wrong or you have never pressed
  Start in a chat with the bot; a bot cannot message someone first).
- Apply the migration before deploying: `npx wrangler d1 migrations apply DB --remote`, then
  `npx wrangler deploy` (both from the `worker` folder).

## Changing things later

- Different cost rates or the Resend thresholds: constants at the top of `worker/src/admin.ts`.
- Another admin: add their e-mail to the Access policy **and** to `ADMIN_EMAILS` (comma-separated).
