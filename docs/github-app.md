# Connect GitHub (the SponsorJobs GitHub App)

The one-click connect: the person clicks **Connect GitHub**, their own browser opens GitHub's
page for the SponsorJobs GitHub App (Authorize, and the first time Install, where they pick
repositories), and the browser hands them back to the app, already connected.

- Desktop side: `ui/github_app.py`, routes in `ui/app.py` (`/api/github/app/*`, `/github/app/done`).
- Worker side: `worker/src/github.ts`, table in `worker/migrations/0012_github_connect.sql`.
- Until the Worker has all three settings below, every `/github/*` route answers 503 and the
  app falls back to the device flow (`ui/github_device.py`), then the token box.

## How the hand-back works

1. The app makes a random verifier, keeps it in memory, and opens
   `https://api.sponsorjobs.ai/github/connect?state=<base64url(sha256(verifier))>&redirect=<the app's loopback page>`.
2. The Worker sends the browser to GitHub's authorize URL (its own PKCE challenge, the state).
3. `/github/callback` exchanges the code with the client secret. If the App is not installed on
   the person's account yet, the browser goes to the App's install page; after Install, GitHub
   authorizes again and comes back. The tokens are sealed (AES-GCM) with a one-time key and kept
   at most 5 minutes; the browser goes to the app's loopback page with that key in the URL
   fragment.
4. The loopback page gives the key to the app, which calls `POST /github/claim {verifier, nonce}`.
   Both must match and the row is deleted as it is read, so the tokens come out once. The page
   then opens `sponsorjobs://github-connected`, which brings the app forward.
5. Tokens last 8 hours. The app calls `POST /github/refresh` 5 minutes before expiry, and once on
   a 401.

## Register the GitHub App (once)

Open **github.com/settings/apps/new** (or, to own it as the company, the BlackOrigin
organization: Settings, Developer settings, GitHub Apps, New GitHub App). Fill in:

| Field | Value |
|---|---|
| GitHub App name | `SponsorJobs` (if taken: `SponsorJobs Desktop`) |
| Description | `Start and publish your project repos from SponsorJobs.` |
| Homepage URL | `https://sponsorjobs.ai` |
| Callback URL | `https://api.sponsorjobs.ai/github/callback` (only this one) |
| Expire user authorization tokens | ticked (the default) |
| Request user authorization (OAuth) during installation | ticked |
| Enable Device Flow | not ticked |
| Setup URL | leave empty (GitHub greys it out once the box above is ticked) |
| Redirect on update | not ticked |
| Webhook, Active | untick it (no webhook URL, no secret) |

Permissions, **Repository permissions** (everything else stays No access):

| Permission | Level | Why |
|---|---|---|
| Administration | Read and write | create a repo on their account, make it public later |
| Contents | Read and write | read READMEs and files, write the README, push files |
| Issues | Read and write | one issue per build milestone on a new project repo |
| Metadata | Read-only | mandatory; repo names, languages |

No Organization or Account permissions. **Where can this GitHub App be installed?**: **Any account**.

Click **Create GitHub App**. On the App's **General** page:

- **Client ID**: shown near the top (starts with `Iv`). Copy it.
- **Client secrets**: click **Generate a new client secret** and copy it right away (GitHub
  shows it once).
- The **slug** is the last part of the App's public page, `https://github.com/apps/<slug>`.

## Give the Worker the settings

In `worker/wrangler.jsonc`, set the two plain vars (not secrets):

```jsonc
"GITHUB_APP_CLIENT_ID": "Iv23li...",
"GITHUB_APP_SLUG": "sponsorjobs",
```

Then, from `~/sponsorjobs/worker`:

```sh
npx wrangler secret put GITHUB_APP_CLIENT_SECRET     # paste the client secret when asked
npx wrangler d1 migrations apply sponsorjobs-broker --remote
npx wrangler deploy
```

Check: `curl https://api.sponsorjobs.ai/github/available` answers `{"configured":true,...}`.
