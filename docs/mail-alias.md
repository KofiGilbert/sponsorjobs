# Mail aliases: first.last@sponsorjobs.ai for the apply agent

## Why

The apply agent (`docs/apply-agent.md`) creates portal accounts and submits applications while the
student is away. Portals then e-mail a verification link or a one-time code, and the run cannot
finish until someone reads it. Rather than read the student's own Gmail, each student gets an alias
on our domain, `first.last@sponsorjobs.ai`, to type into the portal's e-mail field. Mail to the
alias comes to the broker Worker, which

1. keeps ONLY the code or the link, for 30 minutes, so the app can poll it, and
2. forwards the whole message to the student's real, verified e-mail through Resend, so they still
   see every mail a portal sends (and recruiters' replies go to them, not to us).

Nothing else is stored. The Worker never logs a body, a code or a link.

## Routes (`worker/src/mail.ts`)

All four need the app's normal bearer token (`401` without one). The alias forwards to the
account's verified e-mail; the client never supplies the forward address.

- `POST /mail/alias` `{name?}` -> `{alias: "zoe.muller@sponsorjobs.ai", forward_to}`.
  Idempotent: an account has one alias, and asking again returns it (with `forward_to` refreshed
  to the verified e-mail). The name is folded to ASCII `first.last` (letters, digits, dots, 3 to
  40 characters); a clash gets a short number (`zoe.muller42`); a reserved local (`support`,
  `admin`, `no-reply`, `postmaster`, `kofi`, ...) or a name with nothing usable becomes
  `student<n>`. Without a name the e-mail's own local part is folded.
  `409 {"error": "sign_in_first"}` for the anonymous device account (no verified e-mail yet).
- `GET /mail/alias` -> `{alias, forward_to}`, both `null` until one is made.
- `DELETE /mail/alias` -> `{alias: null, forward_to: null}`. Releases the alias and drops its
  pending events; mail to it bounces ("No such address") from then on.
- `GET /mail/inbox?after=<cursor>` -> `{events: [{cursor, kind, code, link, subject, sender, at}], cursor}`.
  This account's events from the last 30 minutes, oldest first, at most 50. Passing `after=N`
  acknowledges (deletes) every event with cursor <= N; the returned `cursor` is the last event's,
  or `after` when there are none. The same contract as the Telegram inbox (`docs/notify.md`).
  `kind` is `"code"` (with `code`, 4 to 8 digits), `"verify"` (with `link`) or `"reset"` (with
  `link`); `at` is unix seconds. Poll every 10 to 20 s while a run is waiting on mail.

What counts as a code or a link is decided by the same rules on both sides: `inbox/detect.py`
(`otp_code`, `verification_link`, `password_reset_link`, `kind_of`) and their port at the bottom
of `worker/src/mail.ts`. Change them together.

## What happens to a message

Cloudflare Email Routing hands every message for the domain to the Worker's `email` handler:

- Unknown local part (after lowercasing and dropping a `+tag`): a permanent reject, so the sender
  sees "No such address". Nothing is stored or forwarded.
- Known alias: the raw MIME is parsed with `postal-mime` (the parser Cloudflare recommends).
  If the message carries a code or a link, one `mail_events` row is written: kind, code or link,
  subject and sender (each cut to 200 characters), time. Then the message is forwarded to the
  verified e-mail:
  - From: `"<sender's display name> via SponsorJobs" <no-reply@sponsorjobs.ai>`
  - Reply-To: the original sender, so a reply goes to the portal or the recruiter, not to us
  - the original subject, text and HTML bodies (each capped at 1 MiB)
  - attachments are dropped; the first line of the text (and a note at the top of the HTML) says
    `Forwarded by SponsorJobs from <alias>; attachments are not forwarded`
  A forward that fails is logged and the event is still kept: the agent must get the code even
  when the forward does not go out.

## Setup (owner, once)

1. Put the tables live: `cd worker && npx wrangler d1 migrations apply DB --remote`
   (migration `0006_mail_alias.sql`).
2. Deploy the Worker: `npx wrangler deploy`.
3. Point the domain's mail at the Worker. The sponsorjobs.ai zone already uses Email Routing (MX
   `route*.mx.cloudflare.net`). In the Cloudflare dashboard: **Compute > Email Service > Email
   Routing > Routing Rules > Catch-all**, action **Send to a Worker**, destination
   **sponsorjobs-broker**, then enable the rule. Keep any existing address rules (support@ and
   so on) above it; the catch-all only takes what no rule matched.
4. `RESEND_API_KEY` must already be set (it is, for sign-in codes; `docs/sign-in-setup.md`).
   Without it mail is still classified and the code reaches the agent, but nothing is forwarded.

## Limits and what is stored

- Resend's free plan sends 100 e-mails a day and 3,000 a month, shared between sign-in codes and
  forwarded mail. The admin dashboard's "E-mails sent" card counts both. Past the cap the forward
  fails (logged), the code still reaches the agent, and the student does not get that copy. Resend
  Pro ($20/month) removes the daily cap if that ever bites.
- `mail_aliases`: one row per account (local part, account id, forward address, created). Until the
  person deletes the alias.
- `mail_events`: kind, code or link, subject, sender, time. Deleted when acknowledged, and after 30
  minutes whatever happens (every inbox read drops the expired ones).
- No message body, header or attachment is written anywhere, and none is logged. Cloudflare's
  Email Routing itself keeps no copy of a message handed to a Worker.
