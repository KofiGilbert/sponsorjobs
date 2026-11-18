# Mail aliases: first.last@sponsorjobs.ai for the apply agent

## Why

The apply agent (`docs/apply-agent.md`) creates portal accounts and submits applications while the
student is away. Portals then e-mail a verification link or a one-time code, and the run cannot
finish until someone reads it. Rather than read the student's own Gmail, each student gets an alias
on our domain, `first.last@sponsorjobs.ai`, to type into the portal's e-mail field. Mail to the
alias comes to the broker Worker, which

1. keeps the code or the link, for 30 minutes, so the apply agent can poll it,
2. keeps the whole message (subject, sender, text and HTML bodies, attachment file names) until
   the person's own app has downloaded it into the Inbox on their machine, and never longer than
   30 days, and
3. tells the person's real, verified e-mail through Resend. What it sends is a per-alias setting
   (`notify_mode`): the whole message (`forward`, the default), a short notice (`notice`), or
   nothing (`off`).

**Message bodies are stored on the server, temporarily.** This changed on 2026-11-18: before, no
body was written anywhere. Now a body sits in D1 from the moment the message arrives until the
app acknowledges the download, 30 days at most. After the download the mailbox lives only on the
person's machine (`data/mailbox.db`). Attachments are never stored, only their names. The Worker
never logs a body, a code or a link.

## Routes (`worker/src/mail.ts`)

All of them need the app's normal bearer token (`401` without one). The alias forwards to the
account's verified e-mail; the client never supplies the forward address.

- `POST /mail/alias` `{name?}` -> `{alias: "zoe.muller@sponsorjobs.ai", forward_to, notify_mode}`.
  Idempotent: an account has one alias, and asking again returns it (with `forward_to` refreshed
  to the verified e-mail). The name is folded to ASCII `first.last` (letters, digits, dots, 3 to
  40 characters); a clash gets a short number (`zoe.muller42`); a reserved local (`support`,
  `admin`, `no-reply`, `postmaster`, `kofi`, ...) or a name with nothing usable becomes
  `student<n>`. Without a name the e-mail's own local part is folded.
  `409 {"error": "sign_in_first"}` for the anonymous device account (no verified e-mail yet).
- `GET /mail/alias` -> `{alias, forward_to, notify_mode}`, all `null` until one is made.
- `DELETE /mail/alias` -> `{alias: null, forward_to: null, notify_mode: null}`. Releases the alias
  and drops its pending events and every stored message the app has not downloaded yet; mail to
  it bounces ("No such address") from then on.
- `POST /mail/alias/notify` `{mode}` -> `{alias, forward_to, notify_mode}`. `mode` is `forward`,
  `notice` or `off`. `400 {"error": "bad_mode"}` for anything else, `404 {"error": "no_alias"}`
  when the account has no alias.
- `GET /mail/inbox?after=<cursor>` -> `{events: [{cursor, kind, code, link, subject, sender, at}], cursor}`.
  This account's events from the last 30 minutes, oldest first, at most 50. Passing `after=N`
  acknowledges (deletes) every event with cursor <= N; the returned `cursor` is the last event's,
  or `after` when there are none. The same contract as the Telegram inbox (`docs/notify.md`).
  `kind` is `"code"` (with `code`, 4 to 8 digits), `"verify"` (with `link`) or `"reset"` (with
  `link`); `at` is unix seconds. Poll every 10 to 20 s while a run is waiting on mail.

- `GET /mail/messages?after=<cursor>&limit=<n>` ->
  `{messages: [{cursor, sender, sender_name, subject, text, html, at, has_attachments, attachment_names}], cursor, more}`.
  This account's stored messages, oldest first, at most 25 per call (fewer when the bodies are
  large: a page carries about 4 MiB of body at most, and always at least one message). Passing
  `after=N` acknowledges and DELETES every message with cursor <= N for this account, the same
  contract as `/mail/inbox`. The app passes only a cursor whose messages it has already written
  to disk, so a crash between a download and its write loses nothing: the page is still here and
  is sent again. `cursor` is the last returned message's (or `after` when there are none); `more`
  says another call would return further messages. `html` is `null` for a text-only message.
  `at` is unix seconds.

What counts as a code or a link is decided by the same rules on both sides: `inbox/detect.py`
(`otp_code`, `verification_link`, `password_reset_link`, `kind_of`) and their port at the bottom
of `worker/src/mail.ts`. Change them together. The same goes for the Inbox labels (received,
rejected, interview, action, other): `inbox/labels.py` and `labelOf` in `worker/src/mail.ts`,
which the Worker uses only to word the notice's subject.

## What happens to a message

Cloudflare Email Routing hands every message for the domain to the Worker's `email` handler:

- Unknown local part (after lowercasing and dropping a `+tag`): a permanent reject, so the sender
  sees "No such address". Nothing is stored or forwarded.
- Known alias: the raw MIME is parsed with `postal-mime` (the parser Cloudflare recommends).
  1. If the message carries a code or a link, one `mail_events` row is written: kind, code or
     link, subject and sender (each cut to 200 characters), time. Unchanged.
  2. One `mail_messages` row is written for the Inbox: sender address and name, subject, the
     text body (or the HTML read as text), the HTML body, the time, and the attachment file
     names. Text and HTML are each capped at 1 MiB; when the two together would not fit one D1
     row (2,000,000 bytes), the HTML is dropped and the text kept. Attachment content is never
     stored.
  3. The person's verified e-mail is told, according to the alias's `notify_mode`:
     - `forward` (the default): the whole message, as before.
       From `"<sender's display name> via SponsorJobs" <no-reply@sponsorjobs.ai>`, Reply-To the
       original sender, the original subject, text and HTML bodies (each capped at 1 MiB),
       attachments dropped, and a first line that says
       `Forwarded by SponsorJobs from <alias>; attachments are not forwarded`.
     - `notice`: a short plain-text note. From `"SponsorJobs" <no-reply@sponsorjobs.ai>`,
       Reply-To the original sender. The subject is `<Company> received your application` for a
       confirmation, `An update on your application to <Company>` for a rejection, an interview
       request or a request for action, and `<Company or sender name> sent you a message`
       otherwise (the company is the sender's display name without words like "Recruiting"; when
       that is only the mail system's name, the subject leaves it out). The body is one or two
       sentences, the first 300 characters of the message as a quote, and
       `Open SponsorJobs and go to Inbox to read it.` with the app link `sponsorjobs://inbox`.
     - `off`: nothing is sent.
  A message that could not be stored (step 2 failed) is always forwarded whole, whatever the
  mode, so a storage problem never loses mail. A send that fails is logged and the event and
  the stored message are kept: the agent must get the code even when the e-mail does not go out.

## In the app

- `inbox/relay.py` `sync` downloads the stored messages into `data/mailbox.db`
  (`inbox/store.py`), one row per broker cursor. It runs when the Inbox is opened (at most once
  a minute, in the background), on `POST /api/inbox/sync`, and every 5 minutes from the app's
  existing background loop while the app is open.
- Each new message gets a label by rule (`inbox/labels.py`, no model call) and is matched to an
  application with the Tracker's matcher (`inbox/matching.py`). A matched message goes to the
  Tracker's existing suggestion path: a plain confirmation moves the card to Applied (with Undo);
  a rejection, an interview request or an assessment becomes a suggestion the person taps.
- The reader shows HTML only after `inbox/sanitize.py` has cleaned it: no script, no forms, no
  remote images (tracking pixels), links limited to http, https and mailto.
- The local routes are `/api/inbox/messages`, `/api/inbox/messages/<id>`,
  `/api/inbox/messages/read-all`, `/api/inbox/sync` and `/api/inbox/notify` in `ui/app.py`.
- The Inbox screen (`#view-inbox`; `ui/static/inbox_view.js` for its pure parts, the rest in
  `ui/static/app.js`) is a list and a reader, read only. The sanitised HTML is shown only inside
  a frame with `sandbox="allow-same-origin"` (no scripts, forms or popups; same-origin so the page
  can fit the frame's height and catch link clicks), whose own page carries
  `default-src 'none'; style-src 'unsafe-inline'`, so nothing loads. A web link opens through
  `/api/open`; a mail address is copied. `GET /api/inbox/messages?job_id=<source_id>` lists one
  application's mail; the Tracker's job panel uses it for its "N messages" line.
- Sample mail without the broker, in a dev session only (`RESUME_AGENT_DEV=1` and
  `RESUME_AGENT_FAKE_MAILBOX=1`, 404 otherwise): `POST /api/inbox/dev/mail`
  `{from, name, subject, text, html?, attachment_names?, at?}` writes one message into
  `data/mailbox.db`, labelled and matched like a downloaded one.
- Erase (Settings) deletes `data/mailbox.db`; Export includes the stored messages.

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
5. For the in-app Inbox (2026-11): apply migration `0013_mail_messages.sql` and deploy, in this
   order, from `worker/`:
   `npx wrangler d1 migrations apply DB --remote` then `npx wrangler deploy`.
   The migration adds the `mail_messages` table and the `notify_mode` column on `mail_aliases`
   (default `forward`, so nobody's e-mail changes). Apply it BEFORE the deploy: the new Worker
   reads `notify_mode` for every incoming message. Nothing else to set up; there is no new
   secret and no new binding.

## Limits and what is stored

- Resend's free plan sends 100 e-mails a day and 3,000 a month, shared between sign-in codes and
  forwarded mail. The admin dashboard's "E-mails sent" card counts both. Past the cap the forward
  fails (logged), the code still reaches the agent, and the student does not get that copy. Resend
  Pro ($20/month) removes the daily cap if that ever bites.
- A notice counts against the same Resend figures as a forward (one e-mail per incoming
  message); `off` sends none.
- `mail_aliases`: one row per account (local part, account id, forward address, `notify_mode`,
  created). Until the person deletes the alias.
- `mail_events`: kind, code or link, subject, sender, time. Deleted when acknowledged, and after 30
  minutes whatever happens (every inbox read drops the expired ones).
- `mail_messages`: sender, sender name, subject, text body, HTML body, time, size, attachment
  names. **Held until the app downloads it, 30 days at most.** A row is deleted when the app
  acknowledges it (`GET /mail/messages?after=<cursor>`), when the alias is deleted, and when it
  is older than 30 days. The 30-day sweep runs on every `/mail/messages` read, every
  `/mail/inbox` read and every incoming message, for all accounts at once; the Worker has no
  cron trigger, so with no traffic at all an expired row waits for the next request.
- Attachment content and message headers other than From and Subject are not written anywhere,
  and no body is ever logged. Cloudflare's Email Routing itself keeps no copy of a message handed
  to a Worker.
- What this costs on Cloudflare is D1 storage and D1 writes: one `mail_messages` row per incoming
  message (up to about 1.8 MB, usually a few tens of KB), one delete when it is downloaded.
