# Turning on sign-in (e-mail code and Google)

The broker at `https://api.sponsorjobs.ai` can now sign people in without passwords, the way
claude.ai does:

- **Continue with Google**, or
- **e-mail + a 6-digit code** we send them (valid 10 minutes).

Once someone is signed in, "Get the pass" opens Stripe Checkout with their e-mail already filled in.
Someone who is not signed in is asked to sign in first.

Until you finish the steps below, both sign-in buttons simply say "not configured", and nothing
breaks. You can do Google and e-mail separately, in any order.

**What it costs**

| Piece | Cost |
| --- | --- |
| Google sign-in | $0. Google does not charge for "Sign in with Google". |
| E-mail codes (Resend, free plan) | $0 for up to 3,000 e-mails a month, **at most 100 a day**. If we ever need more, the next plan (Pro) is $20/month for 50,000 a month with no daily limit. Nothing is charged unless you choose to upgrade. |
| The broker itself | Unchanged: still the Cloudflare Workers free plan. |

**Why Resend and not Cloudflare's own e-mail?** Cloudflare's Email Service can send these e-mails
too, but sending to any address is still in beta there and needs the Workers Paid plan
($5/month). Resend is free at our size and sets itself up on Cloudflare with one click. If we move
to Workers Paid later, the code already supports Cloudflare's sender, so switching is a settings
change.

> **Secrets go in the terminal, never in a chat.** Every key below is pasted only into the
> `npx wrangler secret put ...` prompt in your own terminal. Do not paste them into Claude, Slack,
> an e-mail or a file in the repo.

---

## Part 1: before anything, put the database update live

The sign-in feature needs three new tables. In a terminal:

1. `cd` into the repo, then `cd worker`
2. Run: `npx wrangler d1 migrations apply DB --remote`
3. When it asks "Ok to proceed?", type `y` and press Enter. It should list `0004_signin.sql` as applied.

(Do this after the pull request is merged, from the `main` branch.)

---

## Part 2: e-mail codes (Resend)

### 2a. Create the Resend account and verify the domain

1. Go to <https://resend.com> and click **Get Started**. Sign up (Google sign-up is fine).
2. In the left menu click **Domains**, then **Add Domain**.
3. Type `sponsorjobs.ai`. Pick the region closest to most users (for example **North Virginia
   (us-east-1)**). Click **Add**.
4. On the next screen click **Sign in to Cloudflare** (automatic setup). Log in to the Cloudflare
   account that holds `sponsorjobs.ai` and click **Authorize**. Resend adds its DNS records for you
   (an MX and a TXT record on `send.sponsorjobs.ai`, and a TXT record called
   `resend._domainkey`). These do not touch your existing e-mail.
5. Back in Resend, wait until the domain shows **Verified** (usually a few minutes; refresh the
   page). If it says "Pending" for more than an hour, open Cloudflare, **DNS**, **Records**, and
   check those three records exist.

### 2b. Create an API key

1. In Resend's left menu click **API Keys**, then **Create API Key**.
2. Name: `sponsorjobs-broker`. Permission: **Sending access**. Domain: `sponsorjobs.ai`.
3. Click **Add**. Copy the key (it starts with `re_`). You only see it once.

### 2c. Give the key to the broker

In the terminal, from the `worker` folder:

```sh
npx wrangler secret put RESEND_API_KEY
```

When it asks "Enter a secret value", paste the `re_...` key and press Enter.

The codes are sent from `SponsorJobs <no-reply@sponsorjobs.ai>` and read:
"Your SponsorJobs code is 123456. It expires in 10 minutes."

---

## Part 3: Continue with Google

### 3a. Create the Google Cloud project

1. Go to <https://console.cloud.google.com> and sign in with the Google account that should own this.
2. At the top, click the project picker, then **New Project**. Name: `SponsorJobs`. Click **Create**,
   then make sure the new project is selected at the top.

### 3b. Set up the consent screen (what people see when they click "Continue with Google")

1. In the search bar type **Google Auth Platform** and open it. Click **Get started**.
2. **App information**: App name `SponsorJobs`, User support email: your e-mail. **Next**.
3. **Audience**: choose **External**. **Next**.
4. **Contact information**: your e-mail. **Next**. Tick the agreement, **Continue**, **Create**.
5. Left menu **Branding**: under **Authorized domains** add `sponsorjobs.ai`. Fill in the
   homepage link (`https://sponsorjobs.ai`) and, if you have them, privacy policy and terms links.
   **Save**.
6. Left menu **Data Access**: click **Add or remove scopes**, tick `openid` and
   `.../auth/userinfo.email`, **Update**, then **Save**. (These are basic scopes: Google does not
   need to review the app for them.)
7. Left menu **Audience**: under **Publishing status** click **Publish app**, then **Confirm**.
   (While it says "Testing", only the test users you list can sign in.)

### 3c. Create the OAuth client (the ID and secret)

1. Left menu **Clients**, then **Create client**.
2. Application type: **Web application**. Name: `SponsorJobs broker`.
3. Under **Authorized redirect URIs** click **Add URI** and enter exactly:
   `https://api.sponsorjobs.ai/account/google/callback`
4. Click **Create**. A box shows the **Client ID** (ends in `.apps.googleusercontent.com`) and the
   **Client secret**. Keep this box open (or download the JSON) for the next step.

### 3d. Give them to the broker

In the terminal, from the `worker` folder, run each line and paste the matching value when asked:

```sh
npx wrangler secret put GOOGLE_CLIENT_ID
npx wrangler secret put GOOGLE_CLIENT_SECRET
```

---

## Part 4: deploy and check

1. From the `worker` folder: `npx wrangler deploy`
2. E-mail check: in a terminal, with your own address,

   ```sh
   curl -s -X POST https://api.sponsorjobs.ai/account/email/start \
     -H 'content-type: application/json' -d '{"email":"you@example.com"}'
   ```

   It should answer `{"sent":true}` and the code should arrive within a minute (check spam the
   first time). If it says "not configured", the `RESEND_API_KEY` secret is missing.
3. Google check: open
   <https://api.sponsorjobs.ai/account/google/start?redirect=http://127.0.0.1:57000/> in a browser.
   You should see Google's account chooser saying "to continue to SponsorJobs". (After choosing an
   account it returns to `127.0.0.1:57000`, which only works if the app is running; that is fine
   for this check.)

## How the rules work (for reference)

- A code is 6 random digits, valid 10 minutes, and only a scrambled (hashed) copy is stored.
- 5 wrong tries and the code is dead; the person asks for a new one.
- At most 5 code e-mails per address per hour, and 5 per network per hour.
- If the e-mail already has an account, signing in switches this device to that account (so a
  pass bought on the laptop shows up on the desktop). If it is a new e-mail, it is added to the
  account the device was already using, so anything already on it (a pass, usage) stays.
- Google sign-in only ever sends people back to the app on their own computer
  (`http://127.0.0.1:...` or `http://localhost:...`), never to another website.
