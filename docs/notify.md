# Notifications: the official SponsorJobs Telegram bot

SponsorJobs runs ONE Telegram bot for everyone. A person connects by tapping a link in the app
(Settings, Notifications) instead of creating a bot of their own. The bot token lives only on the
broker; the desktop app talks to the broker with its normal account bearer token.

## Privacy

The broker is a relay. It stores no résumé. Outgoing messages carry only what the app sends
(job title, company, short text, button ids) and are not stored. A CV preview picture or PDF the
person asked for (`send_media`) is streamed through to Telegram inside the same request: it is
never written to disk or a table, and only its size is logged. Every photo and document goes out
with `protect_content: true` (Telegram blocks forwarding and saving); the app cannot turn that
off. Telegram bot chats are cloud chats, not end to end encrypted, and a bot can delete its
messages only within 48 hours, which is why previews cover the contact line by default and the
first "Send PDF" warns the person. The broker keeps exactly three tables in its SQLite DB
(`backend/store_sqlite.py`):

| table | what | lifetime |
|---|---|---|
| `telegram_links(code, account, expires_at)` | sha256 of a one-time link code | 15 minutes, deleted on use |
| `telegram_chats(account, chat_id, username, paused, linked_at)` | the account to chat binding | until unlink (or the person blocks the bot) |
| `telegram_events(id, account, type, data, text, at)` | button taps / replies waiting for the app | deleted once acknowledged, or after 7 days |

Send rate-limit counters are kept in memory only (the broker runs one worker process).

## Contract

All `/notify/telegram/*` routes authenticate with the broker's existing account auth
(`Authorization: Bearer <token>`, see `backend/accounts.py`); no account is a 401. When the bot is
not configured every route, including the webhook, answers `503 {"error": "telegram_not_configured"}`.

- `POST /notify/telegram/link` -> `{code, url: "https://t.me/<BOT_USERNAME>?start=<code>", expires_in: 900}`.
  A one-time code bound to the account, valid 15 minutes.
- `GET /notify/telegram/status` -> `{linked: bool, username?: str, linked_at?: iso, paused?: bool}`.
- `POST /notify/telegram/unlink` -> `{linked: false}`. Sends the chat "Disconnected from SponsorJobs."
  and deletes the binding and any queued events.
- `POST /notify/telegram/send` `{text, buttons?}` -> `{ok: true, message_id}`.
  - `text`: non-empty string, at most 4096 characters (Telegram's own limit; "Show changes"
    sends chunks this size).
  - `buttons`: up to 3 rows of 1 to 3 buttons, each `{id, label}`; `id` is 1 to 40 chars of
    `[A-Za-z0-9:_-]` and becomes the Telegram `callback_data`; `label` is 1 to 30 chars.
  - `400 {error}` for a bad body; `409 {"error": "not_linked"}` with no chat;
    `409 {"error": "paused"}` after the person sent /stop;
    `429 {"error": "rate_limited", retry_after}` (also a `Retry-After` header) past 1 message per
    3 seconds or 30 per rolling 24 hours per account; `502 {"error": "telegram_unavailable"}` when
    Telegram fails.
- `POST /notify/telegram/send_media` (multipart/form-data) -> `{ok: true, message_id}`.
  - fields: `kind` `"photo"` or `"document"`; `file` (a photo at most 10 MB, a document at most
    20 MB); `caption?` at most 1024 characters; `buttons?` the same rows as `send`, as a JSON
    string.
  - Calls `sendPhoto` / `sendDocument` with `protect_content: true`, always. The bytes are held
    only for the request (pass-through, nothing stored, size logged).
  - Same auth, `409 not_linked` / `409 paused`, and the same rate limit as `send` (a media
    message counts toward the 1 per 3 seconds and the 100 per day). `400` for a bad form, `413`
    for a file over its limit, `502 telegram_unavailable`, and `503 telegram_not_configured`
    when the bot or its upload transport is not configured. A bad request uses no quota.
- `POST /notify/telegram/edit` `{message_id, text? | caption?, buttons?}` -> `{ok: true}`.
  `text` -> `editMessageText`, `caption` -> `editMessageCaption` (a photo), only `buttons` ->
  `editMessageReplyMarkup` (`[]` removes them; the app uses this to clear Accept / Undo once
  tapped). Edits change a message already sent, so they do not use the 100 per day; their own
  limit is 1 per second and 200 per day per account. `409 cannot_edit` when Telegram refuses
  (message too old, deleted, or unchanged). Replacing a photo in place (`editMessageMedia`) is
  not offered: the app sends a fresh preview instead.
- `GET /notify/telegram/inbox?after=<cursor>` -> `{events: [{cursor, type, data, text, at}], cursor}`.
  That account's events only, oldest first, at most 50. Passing `after=N` acknowledges (deletes)
  every event with cursor <= N. The returned `cursor` is the last event's cursor, or `after`
  when there are no events. Poll every 20 to 60 s.
  - `type: "button"`: `data` = the button id, `text` = "".
  - `type: "text"`: `data` = "", `text` = what the person typed (cut to 1000 chars).
    `send` text may be longer (4096); inbox text stays at 1000.
  - `type: "command"`: `data` = the command name without the slash (`stop`, `resume`, or any
    other command such as `jobs`), `text` = anything after it.
- `POST /telegram/webhook` (Telegram calls this). The `X-Telegram-Bot-Api-Secret-Token` header must
  equal `TELEGRAM_WEBHOOK_SECRET` (403 otherwise). Always answers 200 for an authentic call so
  Telegram does not retry. Handles:
  - `/start <code>`: binds the chat to the code's account and replies "Connected. I'll message you
    about new jobs and applications. Send /stop to pause." An expired, used or unknown code gets
    "That link expired, open SponsorJobs and try again."
  - `/stop` pauses (send returns 409 paused), `/resume` unpauses, `/help` explains.
  - a button tap: `answerCallbackQuery` with the toast "Got it", then queues a `button` event.
  - plain text: queues a `text` event.
  - anything from a chat not bound to an account gets "Open SponsorJobs, Settings, Notifications
    to connect." and nothing is stored.

### Deviations from the original brief (additive)

- `status` also returns `paused` when linked, so the app can show "paused from Telegram".
- `/stop` and `/resume` also queue a `command` event (data `stop` / `resume`), and unknown slash
  commands queue a `command` event too, so the app can react.
- `cursor` values are integers (pass them back verbatim).
- If Telegram answers 403 to a send (the person blocked the bot or deleted the chat), the broker
  drops the binding and returns `409 not_linked`, so the app shows "connect" again.
- Group / channel chats are ignored; the bot works only in a private chat.
- The relay turns on only when all three env vars below are set; a half configuration stays off
  (503) and prints a warning at boot.

## Environment (broker host only, never in the app or git)

| var | value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token @BotFather gives you, e.g. `123456:ABC...` |
| `TELEGRAM_BOT_USERNAME` | the bot's username, e.g. `SponsorJobsBot` (a leading `@` is fine) |
| `TELEGRAM_WEBHOOK_SECRET` | a random string you make up, 1 to 256 chars of `A-Z a-z 0-9 _ -` (e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) |
| `BROKER_PUBLIC_URL` | only for the setup helper: the broker's https URL, e.g. `https://tailor-broker.onrender.com` (on Render, `RENDER_EXTERNAL_URL` is used if this is unset) |

## Owner setup (once)

1. In Telegram, open **@BotFather** and send `/newbot`. Name: `SponsorJobs`. Username: e.g.
   `SponsorJobsBot` (must end in `bot`; pick another if it is taken). Copy the token.
2. Optional, in @BotFather: `/setdescription`, `/setabouttext` and `/setuserpic` so the bot looks
   official.
3. On the broker host (Render: service, Environment) set `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_BOT_USERNAME` and `TELEGRAM_WEBHOOK_SECRET`. Save (Render redeploys).
4. Point Telegram at the broker. From the Render **Shell** tab (or any machine with the same env):

   ```
   python -m backend.telegram_setup
   ```

   It calls `setWebhook` for `<BROKER_PUBLIC_URL>/telegram/webhook` with the secret, registers the
   `/help /stop /resume /show /versions` commands, and prints `getWebhookInfo`. Check `url` is right and
   `last_error_message` is absent. Re-run it any time the URL or the secret changes.
5. Test: in the app, Settings, Notifications, Connect Telegram; tap the link; press Start. The bot
   replies "Connected." and `GET /notify/telegram/status` shows `linked: true`.

Rotating the token: `/revoke` in @BotFather, set the new `TELEGRAM_BOT_TOKEN`, redeploy, re-run the
setup helper. Existing links survive (chat ids do not change).

## Desktop side

The app tells the person on Telegram about new jobs that fit them and about what it did
while they were away, and lets them answer with buttons. All of it lives in `notify/` and
runs on the person's own machine; the broker only relays messages for the official bot.

### Channels (`notify/channel.py`)

One interface, `send(text, buttons)`, `send_photo(path, caption, buttons)`,
`send_document(path, caption, buttons)`, `edit_text(message_id, text?, buttons?, caption?)` and
`poll() -> events`, two implementations (every photo and document is sent with
`protect_content`; the official bot waits out a 429 of 6 seconds or less once, so a text and
its picture can go back to back):

| Channel | When | How |
|---|---|---|
| `OfficialBotChannel` | the broker reports `linked: true` | `POST /notify/telegram/send`, `/send_media`, `/edit`, `GET /notify/telegram/inbox?after=<cursor>` with the app's bearer token |
| `OwnBotChannel` | the person saved their own bot token and chat id | Bot API `sendMessage` with `reply_markup` (inline keyboard), `sendPhoto` / `sendDocument` (multipart, `protect_content`), `editMessageText` / `editMessageCaption` / `editMessageReplyMarkup`, `getUpdates` for messages and `callback_query` taps, `answerCallbackQuery` to ack |

`select_channel` picks official, else own bot, else none. Broker status is cached for a
minute. A `409 not_linked` from the broker (the person blocked the bot or unlinked
elsewhere) drops the cache, so the next tick falls back to the own bot or none and Settings
shows "Not connected". `429` and `502 telegram_unavailable` leave the alert queued for the
next tick. Command events from the broker (`{type: "command", data: "approve", text: "12"}`)
are rebuilt into `/approve 12` so the existing command router handles both channels the
same way. The own bot obeys only the configured chat id and ignores everything when none is
set (fail-closed).

Buttons are rows of `{id, label}`. Job buttons use `job:<action>:<short>` where `short` is a
10 character hash of the source id, mapped back locally. No source id, profile data or
resume text is ever sent to the broker; message text holds the company, title, location,
match percent and sponsor badge only.

### The tick (`notify/hub.py`)

A background thread (`start_notify_loop` in `ui/app.py`) runs one tick every 30 seconds
while the app is open. `POST /api/notify/poll` and `POST /api/review/telegram/poll` run the
same tick on demand (the second also drains rate-limited auto submissions). A tick:

1. polls the active channel and dispatches each event:
   * `job:*` buttons to the alert engine,
   * `approve:<id>`, `skip:<id>`, `approveall` to the batch review (`BatchReview.act`),
   * text and slash commands (`/list`, `/approve`, `/status`, `/stop`, `/resume`, `/skip`,
     `/handle`, free text chat) to `NotifyService.handle_command`;
2. sends queued job alerts that are due;
3. sends the away digest when it is due.

Every fifth minute it also checks whether the board changed (static feed `generated_at`
or the local crawl's `jobs_refreshed_at`) and, if so, scores new roles.

**The app must be running for buttons to act.** A tap sent while the app is closed is
handled on the next start: the official bot's inbox keeps events for 7 days, the person's
own bot (`getUpdates`) keeps them for 24 hours. Older taps are lost.

### Job-match alerts (`notify/alerts.py`)

* **New**: first seen or posted within the last 3 days, never alerted, not dismissed, not
  already scored.
* **Sponsor relevant**: a US role with an H-1B or PERM badge, or a posting that states
  sponsorship.
* **Score**: a cheap title pre-score against the profile's role titles and skills picks the
  top 5; only those get their JD fetched (feed shard, company board, or local store) and run
  through `_job_match`. The alert score is `60% must-have coverage + 40% overall coverage`.
  "Fewer like this" lowers it (15 points per company vote, 8 per title word vote, at most 60).
* **Limits**: threshold 70, at most 3 alerts a day (1, 3 or 5 in Settings), quiet hours
  22:00 to 08:00 local (alerts wait until morning; a queued alert older than 36 hours is
  dropped), never the same job twice.

Message templates (exact):

* assisted site, or autonomous submission off:
  `{Company} is hiring a {Title} in {Location}. {Strong|Good} fit for your profile ({score}% match, H-1B sponsor). Want me to tailor your résumé and queue it? I'll prepare it; you click submit.`
  Buttons: `[Tailor and queue] [Skip]` / `[Fewer like this]`.
* site on the verified AUTO allowlist (`submit/allowlist.py`) and autonomous submission ON:
  `{Company} is hiring a {Title} in {Location}. {fit} for your profile ({score}% match, H-1B sponsor). This site accepts applications from SponsorJobs. Want me to tailor your résumé and apply for you?`
  Buttons: `[Apply for me] [Skip]` / `[Fewer like this]`.

"Strong fit" is 85 and up, "Good fit" 70 to 84. The location and sponsor parts are left out
when unknown.

Button actions:

* **Tailor and queue**: tailors from the saved profile (the same unattended path as the
  autopilot, `_tailor_job_unattended`) into the review queue, then replies
  `Ready: {Title} at {Company} is in your review queue (#{id}, {n}% JD match). Open SponsorJobs to review it and click submit.`
* **Apply for me**: re-checks the allowlist and the autonomous setting at tap time; if
  either fails it queues instead. Otherwise tailors, then submits through
  `_submit_record_id` (the same per-site path, daily cap and spacing as the Submit button).
* **Skip**: dismisses the role (local list and the board's dismissed flag).
* **Fewer like this**: records the company and title words and dismisses the role.

### CV review in the chat (`notify/cvreview.py`, `notify/cvdoc.py`, `notify/redact.py`)

After every tailoring a Telegram tap started ([Tailor and queue] or [Apply for me]), the hub
sends the "Ready" text and then a **preview**: a PNG of page 1 rendered from the record's PDF
(pypdfium2, the same renderer as the thumbnails) with the contact line (address, phone, email,
profile links) covered by a solid bar. The name stays. The bar is placed on the text boxes of
the person's own contact strings; when none are found it covers the first line under the name,
and failing that a fixed band near the top (it fails closed). Settings, Notifications, "Show my
contact details in Telegram previews" (default off, pref `show_contact`) removes the bar. The
batch review's preview picture uses the same renderer. "show me" (or `/show <id>`) sends the
preview again.

Caption (from the record's coverage report and the per-bullet diff between the material before
tailoring and the page):

`Tailored for {Role} at {Company}: {n} bullets reworded, summary updated. Added the job's terms: PnL, ETL. Missing: Kafka.`

(parts with nothing to say are left out). Buttons: `[Show changes] [Send PDF]` / `[Edit] [Looks good]`
(no Edit once the application was auto-submitted).

* **Show changes**: every part of the page with its id and, where tailoring changed it,
  `Before:` / `After:`; split into messages under 4096 characters. Ids: `S` summary, `E1.2`
  employer 1 bullet 2 (numbered across that employer's roles), `E1.title` `E1.dates`
  `E1.company` `E1.location`, `E1.R2.title` for a second role at the same employer, `P3.1`
  project 3 bullet 1, `P3.name`, `ED1.school` `ED1.degree` `ED1.date`, `K2` skills line 2,
  `X1.1` extracurricular, `I` interests.
* **Send PDF**: the first time only, the warning
  `Before the PDF: chats with a bot are stored on Telegram's servers (they are not end to end encrypted), and the bot can only delete its own messages within 48 hours. I send it with forwarding and saving turned off.`
  then the PDF (`protect_content`), caption `{Role} at {Company}: your tailored CV.`
* **Edit**: `Tell me what to change, in your own words. For example: rewrite E1.2 shorter, use the word PnL in E2.1, reword the summary, change E1 dates to Jan 2020 to Mar 2023, move Projects above Experience, drop P2, add a bullet to E1: Led the card launch. Tap Show changes to see every id.`
  The next message (within 30 minutes) is the instruction. Edits are also recognised without
  the button for the CV previewed in the last 24 hours when the message names an id or a CV
  part with an edit verb ("reword the summary"), and with `/edit <id> <change>`. A question
  ("what jobs should I look at?") still goes to the regular chat.
* **Looks good**: `Great. {Role} at {Company} is ready in your review queue (#{id}). Open SponsorJobs to submit.`
  On a verified auto-allowlisted site with autonomous submission on it says
  `This site accepts applications from SponsorJobs. Tap Apply for me to send it.` with
  `[Apply for me]`, which re-checks both at tap time and submits through `_submit_record_id`.

**How an edit flows.** The model plans (`plan_cv_edit`: the instruction plus a one-line-per-id
outline in, `{edits: [{op, target, value?, instruction?, section?, before?|after?}], clarify}`
out) and the app does the rest:

1. Unknown ids get `Did you mean E1.3?` with `[Yes] [No]` (closest real id); an unclear
   instruction gets the planner's question or a hint to tap Show changes.
2. A **fact** (`E*.title`, `E*.dates`, `E*.company`, `E*.location`, `ED*.*` school / degree /
   date / location) is never reworded by the model, it needs the exact value, and always asks first:
   `This changes a fact on your CV: Stanbic Bank title 'Senior Product Manager' -> 'Product Lead'. Apply to this application only, or also update your profile?`
   with `[This CV only] [Also update my profile] [Cancel]`. "Also update my profile" writes the
   same field into the saved profile (matched by employer or school and title, not position)
   when the edit is accepted. Cancel: `Cancelled. Nothing changed.`
3. **Text edits** (`rewrite`, or `set` when the person typed the text): `edit_cv_spans` gets the
   target spans plus the rest of the entry as read-only context and returns `{id: text}`. Any id
   outside the targets with a different text rejects the whole edit (`I only change the part
   you point at, and that rewrite also touched E1.3. I left your CV as it was.`), and after
   writing, every span outside the targets is compared before and after (the over-editing guard).
4. **Honesty**: a rewrite that adds a skill its own entry never shows (`introduced_skills`, role
   scoped, the same gate as tailoring) is rejected unless the person typed that skill, in which
   case it stays with `Check: Kafka in E1.2 is from your message, not your saved material. Keep it only if it's true.`
   A new figure the model added is flagged (`Check: the figure 40 in E1.1 isn't in your material.`);
   a figure the person typed is theirs. A reworded bullet keeps the tailoring length budget
   (`_bullet_budget`, shortened by the model when over).
5. **Structural** ops are plain code: `move_section` (the template's section order for this
   record), `drop` (a bullet, a skills line, or a whole entry such as `P2`), `add_bullet` (the
   person's text verbatim).
6. **Layout**: the edited page is rendered from the template preamble and compiled; it must be
   one page, not past the bottom edge (`MAX_FILL`), and have no more overfull lines than the
   page had before. If it overflows, only the edited text is shortened (up to three tries, each
   re-checked for honesty). A value the person typed, or a structural change, is never reworded:
   `That change would push your CV past one page, so I left it as it was. Try a shorter wording.`
   A compile failure: `That change broke the layout, so I left your CV as it was.`
7. The reply is `Here's your edit.` + `E1.2` / `Before:` / `After:` for each touched part (or
   `Moved Projects above Experience.`, `Removed P2 (Fare Map).`, `Added a bullet to E1: ...`), any
   `Check:` notes, and `Nothing is saved until you tap Accept.`, as the caption of a fresh preview
   picture with `[Accept] [Undo]` (text first, then the picture, when it is over 1024 characters).

**Accept** copies the candidate PDF over `cv-<id>.pdf`, stores the page, section order and
coverage on the record (so the desktop review screen and the .docx export show the latest),
saves a version, clears the buttons on the preview, and replies
`Saved as version {n} of {Role} at {Company}. Open SponsorJobs to submit.` with `[Undo]`.
Accept never submits. **Undo** on a pending edit discards it (`Discarded. Your CV is as it was.`);
on the latest accepted edit it restores the version before it (`Undone. {Role} at {Company} is back to version {n}.`).
`/versions <id>` lists them: `v1: as tailored`, `v2 (current): rewrite E1.2 shorter, 2026-10-02 14:03`.

Stored locally only: the record's data bag holds `page_profile`, `base_profile`, `sections`,
`template` (written when a CV is accepted in the builder), `versions` (each with its page and
`cv-<id>-v<n>.pdf`), `version`, `tg_pending` and `tg_plan`; `notify_state.json` holds the
button map `cv_shorts` (`cv:<act>:<8 chars>` -> record id and pending edit id), `cv_active`,
`cv_await` and `pdf_warned`. Callback data never carries a source id, profile text or résumé text.

Records made before `page_profile` was saved are rebuilt from `render_profile` with the same
role selection; their Show changes has no "Before" column.

### Away digest

When the app tailored or submitted applications and the person did not use the app since
the work began (the UI sends a presence ping at most once a minute on real input), one
message goes out 10 minutes after the last activity, outside quiet hours:

`While you were away: tailored 3, submitted 1 (Recruitee), 2 waiting for your click. Open SponsorJobs and go to the review queue to finish them.`

If the person comes back first, the digest is skipped.

### Settings and state

Settings, Notifications: Connect Telegram (official: a t.me link button plus a QR code drawn
client-side by `ui/static/qr.js`, polled every 3 seconds until linked; when the broker
answers `telegram_not_configured` or is unreachable, the own-bot setup in three steps),
"New job matches", "Application updates", "Show my contact details in Telegram previews"
(default off), alerts a day (1, 3, 5), quiet hours, Disconnect,
and "Paused, send /resume in Telegram" when the broker reports `paused`.

Routes: `GET/POST /api/notify/settings`, `POST /api/notify/telegram/link`,
`GET /api/notify/telegram/status`, `POST /api/notify/telegram/unlink`,
`POST /api/notify/presence`, `POST /api/notify/poll`.

State is one JSON file beside the app database, `notify_state.json`: prefs, the alert queue,
sent and dismissed ids, scored ids, "fewer like this" weights, per-day counts, the short id
map, the broker inbox cursor, presence and the last digest time. The own bot's getUpdates
offset stays in `telegram_offset`.
