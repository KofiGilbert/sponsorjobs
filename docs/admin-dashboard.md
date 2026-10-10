# Admin dashboard (admin.sponsorjobs.ai)

A private, read-only page that shows how SponsorJobs is doing: accounts, sign-ins, plans, this
month's usage and what it roughly costs, the free AI pool, and sign-in e-mails against Resend's free
plan. It lives in the same Cloudflare Worker as the broker (`worker/src/admin.ts`) and only answers
on `admin.sponsorjobs.ai`.

**Cost: $0.** Cloudflare Access (Zero Trust) is free for up to 50 users, and the Worker route uses
the plan you already have.

## How it stays private (two locks)

1. **Cloudflare Access** stands in front of `admin.sponsorjobs.ai`. Before anyone sees the page,
   Cloudflare asks them to log in, and only your e-mail is allowed.
2. **The Worker checks again.** Access adds a signed token to every request it lets through. The
   Worker verifies that token itself (signature, the application's AUD tag, the team, the expiry)
   and answers **403 Forbidden** if anything is off, or if the two settings below are still empty.
   So the page is closed by default, and a mistake in the Access policy alone cannot open it.

The page never shows tokens, token hashes or sign-in code hashes, and it cannot change anything.

## What it shows

- **Accounts:** total, how many signed in with an e-mail, new this week, and a bar chart of new
  accounts per day for the last 30 days (hover a bar for the number, or open "Show as a table").
- **Plans:** free, active Job Hunt Pass, active Season Pass, purchases (passes and interview packs
  together: Stripe's records here do not say which was which), and unused extra-interview credits.
- **This month:** active accounts, tailored packages, AI tokens with an estimated cost, live
  interviews and minutes with an estimated Tavus cost ($0.35 a minute).
- **Free AI pool today:** tokens used against the daily cap (`FREE_POOL_DAILY_TOKENS`).
- **Sign-in code e-mails:** today against Resend's free 100 a day, and the month against 3,000.
  Amber at 70 a day, red at 90.
- **Recent sign-ins:** the last 20 accounts with an e-mail, their plan and dates.
- The same numbers as JSON at `https://admin.sponsorjobs.ai/admin/api/stats`.

Cost estimates use list prices: Haiku 4.5 ($1 in / $5 out per million tokens) for the free plan,
Sonnet 5.5 ($2 / $10) for passes, assuming 80% of tokens are input. Check the Anthropic and Tavus
consoles for the real bill.

**Dates start now.** The database did not record when an account was created or when someone
signed in, or count pass packages and sign-in e-mails per month. This release starts recording
them. Older accounts show "unknown" and are left out of the chart; nothing is made up.

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

## Changing things later

- Different cost rates or the Resend thresholds: constants at the top of `worker/src/admin.ts`.
- Another admin: add their e-mail to the Access policy **and** to `ADMIN_EMAILS` (comma-separated).
