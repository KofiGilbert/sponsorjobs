# The apply agent: "Assisted apply"

> **Switched off for launch (2026-11-24).** The product launches with assisted apply only: the
> person opens the application, signs in, presses Fill this page, checks it and clicks Next and
> Submit themselves; nothing is ever submitted by the app. Everything in this document stays in
> the repo behind ONE switch, `SJ_APPLY_AGENT=1` in the engine's environment
> (`ui/app.py _apply_agent_enabled`, written on `<html data-apply-agent>` for the page). Off, the
> engine refuses `/api/autoapply/agent/*`, `/api/autoapply/auto/plan` and `/auto/release` (403,
> `apply_agent_off`), the mode reads Assisted, the Autopilot never runs, `/api/submit/decision`
> never allows a click on Submit, and the UI hides every agent control. To work on the agent, start
> the app with `SJ_APPLY_AGENT=1 ./shell-electron/dev-start.sh`; its tests turn it on with the
> `apply_agent_on` fixture (tests/conftest.py), and `e2e-agent.mjs` / `e2e-apply-step.mjs` set it.

> The spec for the in-app agent that applies to a job the way a person would: opens the job,
> clicks Apply, signs in (or creates the portal account), works through every page, uploads the
> resume, and stops at Submit. Read it before touching `extension/agent_view.js`,
> `shell-electron/agent.js`, `autoapply/brain.py` or the `/api/autoapply/agent/*` routes.
> Design informed by a read-only study of browser-use 0.13.14 (MIT); what we copied and what we
> deliberately left out is in section 9.

## 1. Why

"Fill application" asked the person to open the job page, click Apply, sign in, and come back.
Nobody wants that. The person clicks ONE button (or none, in a batch run) and the agent does the
tedious part. The person's only jobs are: approve what gets sent, and clear anything only a human
may clear (a captcha, a phone code).

Hard lines that do not move (CLAUDE.md section 7):
- The MODEL never presses Submit. It reports "ready to submit"; the shell decides (section 6).
- A captcha or verification challenge stops the run. We never solve, intercept or bypass one.
- No automation-masking flags, no proxies, no "human-like" randomized delays, no user-agent
  tricks. The pane presents itself as what it is (`sponsorjobs-shell/<version>` in the UA).
- LinkedIn, Indeed, Glassdoor and the other bot-prohibited boards (`submit/allowlist.py
  PROHIBITED_HOSTS`) are never driven. `fill_permitted(url)` is checked before the first step.
- The model never sees the person's data (section 5).

## 2. The loop, in one page

```
runApplyAgent(recordId)                                   shell-electron/agent.js
  open the apply URL in the pane, wait for it to settle
  for step in 1..MAX_STEPS (60, SJ_AGENT_MAX_STEPS) while under the dollar cap (section 15):
    1. settle(): URL changed OR DOM quiet for 600 ms OR 8 s cap (never a flat nap)
    2. guard(): blocker (captcha -> park "captcha"; auth -> section 4), host off the
                allowlist -> stop "left-site", success page -> stop "submitted"
    3. fill(): the deterministic engine fills what it recognizes on this page
                (recipes -> labels -> widgets -> /api/autoapply/plan), uploads the resume
    4. view():  serialize the page for the model (section 3), main frame + cross-origin frames;
                hang detection (section 13): a page with nothing on it for 45 s goes to the ladder
    5. brain(): POST /api/autoapply/agent/step {view, history, plan, observations, ...}
                -> {memory, plan, actions, done, cost}
    6. act():   run the actions in order; the read-only tools (section 12) return observations;
                a page check holds a Next whose required fields are empty (section 14);
                stop the queue when the page changes
    7. record(): one history line {memory, evaluation, actions, results}; loop detector
    done.outcome == "ready_to_submit" -> ready gate, critic, then hand over (sections 14, 6)
    done.outcome == "blocked"         -> captcha / phone code park; needs-person -> the ladder
  every call into the pane is capped (20 s) and Stop preempts it (section 13)
  nothing "stuck" ever stops the run: the recovery ladder (section 13), then a park that says
  what was tried; a batch job also parks after 12 minutes ("took too long", section 16)
```

The deterministic engine goes first on every page so the model is only ever asked WHAT TO
CLICK NEXT (Apply, Apply manually, Sign in, Next, Save and continue, dismiss a popup, pick a
dropdown the engine could not). Fields are filled without a model call; this is what keeps a
Workday application to roughly 10 to 25 steps and a few cents each.

## 3. What the model sees: the page view

`window.TailorAgent.view()` (extension/agent_view.js, injected into the pane like
autofill_core.js, and into every cross-origin frame through shell-electron/frames.js) returns
`{ text, count, refs }`. `text` is what the model reads:

```
URL: https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123/apply
Title: Apply - Analyst
[Start of page]
Business Intelligence Analyst
[1]<button>Apply</button>
[2]<a>Save</a>
Sign In
[3]<input type=email label="Email Address" value="" required>
[4]<input type=password label="Password" value="">
[5]<checkbox label="Remember me" checked=false>
[6]<button>Sign In</button>
[7]<a>Create Account</a>
Error: "Email is required"
[End of page] (1.4 pages below)
```

Rules:
- One line per INTERACTIVE element, numbered in document order: `button`, `a` (with an href or
  a click role), `input` (type kept), `textarea`, `select`, `combobox` (role=combobox or
  aria-autocomplete), `radio`, `checkbox`, `file`, and elements with `role=button|link|option|
  menuitem|tab`. Hidden elements (display none, visibility hidden, zero box, aria-hidden) are
  skipped, EXCEPT `input[type=file]` at opacity 0 (that is how every upload button is built).
- `label` is the engine's `cleanLabel(labelFor(el))`, capped at 80 chars. `value` for a text
  field is `""` or `"(filled)"`, NEVER the contents (section 5). For select/radio/checkbox the
  chosen option label is fine, EXCEPT on a voluntary self-identification question (gender,
  race, ethnicity, Hispanic or Latino, veteran, disability, sexual orientation, pronouns): its
  choice is the person's answer and never leaves the machine. Such a select or combobox reads
  `value="(filled)"`, its radios `checked=hidden answered=true|false` (any radio of the group
  chosen), its checkboxes `checked=hidden`. The choices themselves stay, so the question can
  still be read and asked. `brain.mask_self_id` applies the same rule on the engine side before
  any view reaches the model. `select` carries `options=<count> first="A|B|C|D"`.
- `*` before the index (`*[12]`) marks an element that was not in the previous view: that is
  how the model spots an autocomplete suggestion list or a conditional follow-up.
- Non-interactive text: headings, form section titles, visible validation errors
  (`Error: "..."`), and up to 60 words of other visible text per block, so the model knows
  which page it is on. The whole view is capped at 12 000 chars; a `(N more elements below)`
  tail says when it was cut.
- Frames: the main frame's indices run 1..999, the first cross-origin frame 1001..1999, the
  second 2001.., so one number identifies both the frame and the element. The shell routes an
  action by range. Captcha frames are never entered (frames.js CAPTCHA_RE).
- `refs` is kept IN THE PAGE (`window.__tailorAgentRefs`, an array of elements), rebuilt on
  every view. An index is valid only for the step it was shown in.

## 4. Sign-in and portal accounts

Workday, iCIMS, Taleo, SuccessFactors and most company portals require an account per
employer. The agent handles the wall instead of handing it back:

1. `pageBlocker() == "auth"` or the model reports `needs: "account"`.
2. The shell asks `/api/autoapply/agent/portal-account?host=<host>`:
   - an account exists -> the engine fills the sign-in form with the placeholders
     `{{portal_email}}` / `{{portal_password}}` (resolved locally, never shown to the model);
     the model clicks Sign in.
   - none -> the model clicks Create account / Sign up; the engine fills email + a GENERATED
     password (+ confirm); the model clicks Create; the shell saves `{host, email, created}` with
     the password in the credential store (`_set_cred("PORTAL_<host>")`, App Lock custody when
     on; git-ignored). Opt-in: Settings > Auto-apply > "Create portal accounts for me" (default
     ON once the person has turned Autonomous apply on; the toggle text says what it does).
3. Email verification required before the application continues -> stop with
   `blocked: "verify-email"` and tell the person (bar + Telegram: "Workday sent a verification
   email. Click the link, then press Resume."). Milestone 3 reads the person's own Gmail for the
   link (CLAUDE.md Phase 5) so this completes without them.
4. A phone code, security question or captcha -> stop, same channel, never worked around.

The email is the person's own (profile identity), never a separate "agent" mailbox: recruiters
reply to it.

## 5. The brain: `/api/autoapply/agent/step`

Python (`autoapply/brain.py`) builds ONE system prompt (cached) and ONE user message that
replaces the previous one each step (no growing chat), calls the model through the broker
(`llm.agent_step(system, user)` on AnthropicLLM / BrokerLLM; FakeLLM returns scripted steps
under test), validates the reply, resolves placeholders, and returns concrete actions.

Request:
```json
{ "record_id": 2, "step": 4, "max_steps": 40, "url": "...", "lane": "assisted",
  "view": "<section 3 text>", "filled_this_step": 9, "left": ["Veteran status"],
  "history": [ { "step": 3, "evaluation": "Apply manually opened the form", "memory": "...",
                 "actions": ["click 7"], "results": ["ok", "page changed"] } ] }
```
Reply:
```json
{ "ok": true, "memory": "On page 2 of 5 (Experience). Contact filled.",
  "evaluation": "Next worked: new page with experience fields.",
  "actions": [ { "op": "click", "index": 14 } ],
  "done": null }
```
`actions[].op` is one of `click(index)`, `type(index, text)`, `select(index, option)`,
`upload(index)`, `scroll(down|up)`, `wait(seconds<=5)`; at most 5 per step, `done` alone.
`done` is `{ "outcome": "ready_to_submit" | "submitted" | "blocked", "reason": "..." }` where
`blocked` reasons are `captcha | verify-email | phone-code | needs-person | not-a-job | left-site`.

(2026-11-18) Section 29 replaces two things below: the brain may now answer a question about the
person itself, under the answer protocol, and the fact gate no longer strips its words. The rest
of this section stands.

Prompt rules (the short browser-use "flash" shape, under 60 lines): handle popups and cookie
banners first; on a job page click Apply; prefer "Apply manually" / "Autofill with resume" over
"Apply with LinkedIn"; after typing into a combobox WAIT a step and click the `*` suggestion,
do not press Enter; one page-changing action LAST in the list; never click Submit, Send
application, or Review and submit: report `ready_to_submit` instead; never answer a question
about the person (the engine and the answer library do; what is left is reported in `left`);
never invent values; if the same action failed twice try something else or report `blocked`.

Data boundary:
- The view carries no personal values (section 3). The brain passes the model the list of
  PLACEHOLDER NAMES it may type (`{{first_name}}`, `{{email}}`, `{{phone}}`, `{{city}}`,
  `{{portal_email}}`, `{{portal_password}}`, ...), the same `APPLICANT_KEYS` the plan endpoint
  uses. A `type` whose text is a placeholder is resolved in Python before the reply leaves
  the engine; the reply marks it `sensitive: true` so the shell logs "typed {{email}}".
- A `type` with free text that is NOT a placeholder is allowed only when the field's label is
  plainly non-personal (a search box for a typeahead such as a school or city name taken from
  the profile is passed as a placeholder too: `{{school}}`, `{{city}}`). The fact gate in
  autoapply/planner.py stays the authority on questions about the person.
- Every outbound prompt is passed through a redactor that replaces any profile VALUE that
  slipped into page text (a form echoing the email back) with its placeholder name.

Guards in Python: unknown op or index not in the view -> dropped; `done` with other actions ->
other actions dropped; an empty or garbled reply -> up to three calls with backoff (0.5 s,
1.5 s) and a "reply with JSON only" nudge, then `{ok: false, reason: "no-reply", error}` for the
shell to escalate (section 13), never a park; a model call that fails for credits, a key or
access (`BrokerUnavailable` reasons `upgrade_required`, `no_key`, `no_credit`) is `fatal` at
once (section 13). `ready_to_submit` passes the deterministic ready gate first (section 14).

## 6. Submit, and who presses it

The model says `ready_to_submit`. Then, in the shell:
- `submission_policy(url) == "auto"` (the verified allowlist, Recruitee today) AND the
  person's Autonomous submit is ON -> the shell presses the site's Submit itself
  (`highlightSubmit()` finds it; a real CDP click), waits for `detectSuccess()`, marks the
  record submitted, Telegram "Sent: Role at Company".
- everywhere else -> the bar above the pane shows "Ready to send" with a **Send** button; the
  record moves to "Waiting on you" with `ready: true`; in a batch run the Telegram summary lists
  every ready package with one **Send** button each (Milestone 2). Pressing Send (in the app or
  on Telegram) makes the SHELL click the site's Submit; the person never has to find it.
- A submit click that lands on a captcha -> `blocked: captcha`, the package stays ready.

The shell also refuses, independently of the model, to click any element whose text matches
`/^(submit( application)?|send( application)?|review and submit|complete application)$/i` unless
it is the Send path above. Belt and braces.

## 7. Settling, limits, loops

- `settle()` after every page-changing action: resolve on URL change, or on no DOM mutation
  for 600 ms, or at 8 s; report which fired. (browser-use has no such wait; that is why the
  Workday page showed up blank on 2026-10-27.)
- Per step: one brain call (90 s cap), actions 25 s each, every other call into the pane
  20 s (`SJ_AGENT_PANE_MS`), the fill engine by phase (2026-11-18, `shell-electron/fill_budget.js`: engine 30 s, form read 15 s, plan 30 s, ops 40 s, 120 s overall with `SJ_AGENT_FILL_MS` as the override; a phase that is still filling is cut at its cap and keeps what it filled, one whose page gave no heartbeat for 15 s is a hang). `MAX_STEPS = 60`
  (`SJ_AGENT_MAX_STEPS`), `MAX_FAILED_STEPS = 4` in a row (a failed step = an action error or
  an empty reply) -> the recovery ladder, never a stop (section 13).
- Loop detector: the hash of `(op, index, text)` over the last 12 actions; the same hash 3
  times -> a nudge line in the next prompt ("you repeated X three times; inspect it, read the
  errors, do it another way"), 5 times -> the ladder. The same `(url, view hash)` 4 steps in a
  row with nothing filled -> the ladder.
- Host allowlist per run: the apply URL's host, its registrable domain, and the known ATS
  hosts (`ATS_HOSTS`, new in `submit/allowlist.py`: the sourcing ATS domains plus myworkdayjobs.com, icims.com, taleo.net, successfactors.com, jobvite.com, bamboohr.com, paylocity.com, ultipro.com, adp.com). A navigation anywhere else stops the run
  (`left-site`); a `window.open` tab is closed after its URL is read. `about:blank` is fine.
- JS dialogs: `beforeunload`/`alert`/`confirm` accepted, `prompt` cancelled, the message is
  echoed to the model next step.

## 8. UI

- The row button is **Assisted apply** (replaces "Fill application"; it was "Apply for me" until
  2026-11-18, renamed because the person watches this run and presses Send, and walking away is
  what Auto mode is). Its tooltip: "I fill the form while you watch. I ask you when I need an
  answer, and you press Send." The top-right action
  becomes **Apply to all N** once Milestone 2 lands.
- While it runs, the bar above the pane is the status line, few words:
  "Opening job", "Signing in", "Page 2 of 5 filled, 9 fields", "Ready to send" + **Send**,
  "Stopped: this site asks you to verify your email". The pane stays visible; the drawer
  closes before the run (the 10-27 lesson).
- The person can press **Stop** at any time; the run ends after the current action.
- **Live activity feed (2026-11-01, OpenHands style).** Every progress line carries the run's
  steps as events, `{phase, verb, target, summary, state, at, detail?}` with state
  running | done | retry | asking | parked (`createFeed` in agent.js; the resume builder uses the
  same shape from `ui/activity.py`). While it runs the bar is ONE live line: a spinner, the verb
  in the accent, the summary, the job and "page 2 of 4" (the site's own "Step 2 of 4" when it
  shows one). **Steps** opens the timeline under the bar (time, marker ✓ done, ↻ retry, • now,
  muted detail) with "Steps 14 of 40" and "Nothing sent without you" in its footer. The shell
  adds its own steps (Opening the job, Signing in, Filled N fields from your memory, Accepted the
  X, Asking you, Waiting for the site's email, Trying another way, Paused, Ready to send, Sent);
  each brain step adds the model's own `summary` (about 10 words, "Clicking Next to open page
  3"), and an action that fails turns that line into a retry with the reason in words. The
  summary is screen text, so `brain.clean_summary` reads placeholders as words ("your email")
  and drops any summary that names a profile value, a withheld value, a chosen option or a
  self-identification topic, building one from the actions instead. The messages themselves
  are unchanged, nothing here goes to Telegram, and the run does exactly what it did before.
- "Needs you" tile counts packages stopped on `blocked`; each row says why in one line.

### Asking the person (`/api/autoapply/agent/ask`, autoapply/asks.py)

When the brain reports `blocked: needs-answers`, the shell asks about the REQUIRED questions
still open on the page (at most 6), on the bar and on Telegram, and waits up to an hour
(`agent_ask_wait_min` in the review prefs, 5 to 180 minutes), with ONE Telegram reminder at
about 15 minutes; then the job PARKS (see "Parked jobs and batches" below).

- **Choices.** A native select's every option comes from the element (`selectOptions`; the view
  prints four), a radio group's from the view, a custom dropdown's from its open popup: the
  shell marks what is on screen (`markPopups`), opens it with a real click, reads only the popup
  that click opened (`popupOptions`: the list the control names, a list in its own field box,
  or a list that appeared, never another question's list or the site's nav), and closes it
  with ONE real Escape only while it is open (an Escape on a closed control can close the dialog
  holding the form); a widget that ignores Escape is clicked shut or blurred, and one that
  ignores both (Phenom's select at Lam Research) is pressed shut from just outside it
  (`closePopup(i, "outside")`). Before opening a dropdown the shell presses any OTHER open one
  shut (`closeOthers`), and a list another control names is never read as this one's. The shell sends
  `options` (at most 8 to offer, the decline choice first on EEO questions), `all_options` (up to
  40, for matching) and `partial` (the page has more than were read).
- **Known answers first.** Work authorization, the answer library and the standing
  `eeo_decline` choice answer what they can (`known_only`); only the rest is asked. A saved
  answer maps only to a choice that says the same (`planner.literal_for`: equal, or the same
  words plus only neutral ones like "Online"), never one that adds a qualifier ("College Job
  Board" for "Job board", Phoenix Contact 2026-11-05); on "How did you hear" a choice that
  names no source ("Job Board", "Internet", "Other") stands in, else the person is asked. The
  in-page pickers use the same rule (`autofill_core.answerScorer`), and the shell PICKS a saved
  `{{answer_N}}` typed into a list with it instead of typing it for the brain to click
  whatever the list's own filter leaves. An EEO
  answer fits only the choice it names, or the page's decline choice for a saved decline; never
  a Yes/No or prefix guess. A question whose known answer did not stick is sent in
  `force_questions` and asked for real; the others still get their known answers.
- **Telegram.** One message: one question with its choices as buttons inside the broker's
  contract (3 rows of up to 3, labels of 30 characters; longer or more choices are numbered and
  listed in full in the text), or several numbered questions without buttons. A channel that
  refuses the buttons gets the same question as text. Every message stays under 4096 characters.
- **Reading the reply.** A choice's own words or its listed number need no model; anything else
  goes through one small model call (25 s at most) that keeps only the part answering each
  question, by the numbers the person saw (kept after a partial reply). A question back gets the
  choices (with any answers in the same message taken); an unclear reply is asked again once;
  when the model fails, the plain reading answers and the failure does not count against the
  person. Slash commands are never answers: `/stop`, `/pause` and `/skip` also end the wait.
  Only a question whose Telegram message was delivered takes Telegram text. A BARE answer ("no":
  one short value, no number, `asks.is_bare`) while several questions are open answers ONLY the
  first one still open, never a later one; when that question does not take it, the reply says
  so with its choices and how to number an answer for another question (2026-11-05, Phoenix
  Contact: a bare "no" landed on a background-check consent further down the list).
- **The bar, several questions.** ONE question at a time: "Question 2 of 4", the question on its
  own line, its choices as buttons, a box, Answer, and Back; the answers given so far sit under
  it as links back to change them. Every answer sends the question's `index` and words
  (`/api/autoapply/agent/answer` resolves a stale index by the words, `_ask_pick`), so a tap or a
  typed answer lands on that question only; the reply carries `items` (each question's number
  and answer) so the bar moves on to the next open one. A parked job's `ask_view` keeps each
  open question's number `n` and the answers already given (`answered`).
- **Saved.** The person's answers are saved to the answer library (never a question back, never
  a paragraph for a one-choice question). A raw reply saved before replies were read stays in
  the library, unused, until the person replaces or deletes it.
- **Self-identification from saved answers.** Before the brain is asked, every required EEO
  question still open is read with the page's own choices and checked with `known_only`; when
  the saved answer maps to one choice (autoapply/eeo.py, in code), the reply's `select` names it
  and the SHELL picks it with real clicks (`pickSaved`: open, `optionIndex`, click, `chosenIs`),
  never the model. The choice is never logged (only "N filled from saved answers"), never sent to
  the broker or Telegram, and the view prints the control as `(filled)`. The page's note right
  above a field goes along as `hint` (Lam's "Veteran" sits under a paragraph about protected
  veterans, so a saved "not a protected veteran" answers its plain No).

- **Where known answers come from.** Every reader and writer of the answer library goes through
  `_answer_lib()` in `ui/app.py`, and the work authorization and the standing decline through
  `_work_auth()` and `_eeo_decline()`. With the applicant brain on (the default since Phase 1 of
  `docs/applicant-brain.md`) these read `data/applicant.db`: `BrainLibrary`
  (`applicant/library.py`) has the answer library's methods and gives the same answers, and
  every write is mirrored one way into `answers.db` and `app_prefs.json`, so the browser
  extension and a rollback see it too. `SPONSORJOBS_BRAIN=0` or
  `python -m applicant.migrate --rollback` puts the readers back on `answers.db` and
  `app_prefs.json`.
  Since Phase 4 a wording no rule knows is matched to a saved answer by one batched model call
  per page (`applicant/mapper.py`, labels, options and key names only, cached per wording); a
  legal or self-identification match waits for the person's one tap ("Use my saved answer"), and
  a value that expired or does not fit the job (an ended OPT, a salary older than six months, a
  start date in the past, a US dollar salary on a UK job) is asked, not sent
  (`applicant/expiry.py`). The page view hides a control's chosen legal or self-identification
  value from the model (`brain.mask_values`).

### Terms a site requires (`/api/autoapply/agent/terms`)

A privacy notice, data privacy agreement, terms, a consent to process the application data or an
E-Verify acknowledgement that the site needs accepted to take the application (Lam Research opens
a "Data Privacy Agreement" dialog with Cancel and I Acknowledge when the resume is attached). The
page side finds them (`termsItems`: an open dialog with one Accept / I Agree / I Acknowledge
button, or a REQUIRED box that accepts a notice), never a marketing opt-in (job alerts, a talent
community, texts) or a cookie banner. The person decides: `accept_required_terms` in
data/app_prefs.json is "ask" (default) or "always" (the "Answers every form asks" card). On
"ask" the first notice on a site is asked on the bar and Telegram: "<Company> needs you to accept
its <title> to apply. Accept it?" with "Accept for all applications" (sets "always"), "Just this
one" and "Stop". A typed reply is read strictly (a choice's words or number, yes or no), never by
a model. Then the shell clicks the accept button or ticks the box with a real click, records the
acceptance on the application (`terms_accepted`: company, title, url, time) and names it in the
Ready to send summary ("Accepted Lam Research's Data Privacy Agreement for you."). Stop ends the
run and nothing is accepted; no answer within the wait parks the job, and nothing is accepted
until the person answers. "Just this one" is remembered on that application (`terms_consent`),
so its re-run accepts the same notice without asking again. The brain never accepts terms or follows a link to
a policy (the in-page click refuses one: `policy-link`); it reports `blocked: needs-consent` when
one still blocks the way.

### Parked jobs and batches (2026-10-31)

The owner was away when Lam Research asked for consent; the 5 minute wait ran out and the run
died. Now nothing that only the person can give ever ends a run:

- **The standing choice up front.** `accept_required_terms` is asked once in setup's "Questions
  every form asks" step (an unticked box) and, while still not made (`terms_decided` in the
  review prefs), as one question on the bar before the first Assisted apply or batch starts. It is
  never made for the person; leaving the box unticked decides nothing.
- **Single runs** ask live and wait (chunks of 20 s: `/ask` or `/terms` with `park`, then
  `/ask/wait`; Stop closes the ask between chunks with `/ask/close`). Past the wait the job parks.
- **Batches** (`agent.batch`, "Assisted apply to all" on Waiting on you) never wait: an unknown
  question or consent not given parks the job at once (`no_wait`) and the next job starts. ONE
  digest (`/api/autoapply/agent/digest`) goes to Telegram at the end, and every 30 minutes while
  the batch runs when something new parked, with buttons for up to three jobs.
- **Parked** (`data/agent_parked.json`, `agent_park` on the record): the question stays open for
  7 days on the bar, Telegram and the Auto-apply row ("Paused: needs you", "Answer and continue").
  A captcha, a phone code or a step on the page parks too, with "Continue". The pane is released.
- **Resume.** An answer (bar, Telegram button, or a typed reply while exactly one parked question
  is open) saves as usual and marks the job ready; the shell polls `/api/autoapply/agent/parked`,
  takes it, and re-runs it one at a time, behind any run or batch: from the same page with its
  state when the pane is still there, else by opening the job again, the consent and answers
  applied on the way through. A fresh Assisted apply on a parked job replaces the park.
- **Send after being away.** A job left Ready to send keeps `agent_ready` on its record (the
  review page's fingerprint, `reviewSig`), and its Auto-apply row offers Send. When its page is
  gone (a batch moved on, a restart), ONE Send fills the job again from the saved answers
  (`mode: "resend"`, asks never wait) and presses Submit as the person's own Send only when
  nothing new came up and the review page is the same; otherwise it parks and nothing is sent.
- **Late answers** (PR #143) never take a parked question: it is still open, so the late path
  skips it, and its answer trims the late-answer row.

## 9. What we took from browser-use, what we left

Copied (adapted): one replaced state message with `<step_info>` last so the prefix caches;
`[index]<tag attrs>` page grammar with `*` for new elements; placeholders substituted outside
the model and values redacted on the way back; required `evaluation` of the previous step;
stop the action queue when the page changes; page-changing action last; loop detector with
escalating nudges; forced "done only" at the limit; click ladder (scrollIntoView, box,
elementFromPoint occlusion check, real mouse event, `el.click()` fallback); value readback
after typing; "options on miss" for selects; direct value set for date inputs; nearest
`input[type=file]` search + `DOM.setFileInputFiles`; new-tab diff after a click; dialog
auto-accept; forced single tool call with cache breakpoints on system prompt and state.

Left out: automation-masking Chrome flags, UA override, proxies, captcha solver, bundled
extensions, per-character "human" typing, screenshots on every step (DOM text only; a
screenshot goes with the ladder's reflection call and when the brain asks for one, section
12), todo/file-system actions, history compaction, multi-tab as a model feature, LiteLLM
pricing, client-side model fallback. (The plan field and the read-only tools came later, from
the OpenHands study: sections 12 to 16.)

## 12. Read-only tools: the brain investigates instead of guessing (2026-11-02)

From the OpenHands and Devin study (`scratchpad/openhands-study.md`, phases 4 to 6): OpenHands
never gets stuck because errors go back to the model as text and it can look closer. We give
the brain fixed, read-only tools instead of free code on a live job page (the page is untrusted
input and the person's data is on the machine):

| op | what the shell does | the observation |
|---|---|---|
| `read_page(offset)` | `TailorAgent.readPage(offset, 6000)`: the view's lines past the 12 000-char cap, from that element index | the lines, "(N more elements below; next offset M)" |
| `find(text)` | `TailorAgent.find`: lines whose words contain it, else the page text around it | up to 12 lines |
| `inspect(index)` | `TailorAgent.inspect`: tag, kind, label, attributes (type, name, pattern, placeholder, inputmode, aria-*), required, disabled, visible, covered, the state (`(filled)` or empty, never a value), the text before and after, the errors next to it | one JSON line |
| `list_options(index)` | a native select's options from the element, a custom dropdown's from its popup (the ask path's readers) | "a \| b \| c (and more)" |
| `read_errors` | `TailorAgent.readErrors`: every `Error:` line plus live `role=alert`, `aria-invalid` and error blocks | the errors |
| `screenshot` | `wc.capturePage()` at 900 px wide, base64 PNG | "it comes with your next step" (the next call is `agent_step_image`; never stored) |
| `wait_for(text, selector, seconds<=30)` | polls `TailorAgent.has` every 500 ms | "appeared after 2.5 s" or "did not appear within 10 s" |
| `back`, `reload`, `reopen_job` | the pane goes back, reloads, or opens the job's own apply URL again (no URL from the model, ever) | "went back", "reloaded", "opened the job again"; page-changing, so last in the list |

Every observation goes back in the next user message's `<observations>` block (capped at 7 000
chars each, 12 000 together, redacted like the view), as information, never as a reason to
stop. A failed `click`/`type` adds its own observation ("your type on [12] did not work:
covered. Inspect it or do it another way"). The prompt's rule 8 says what to do with that;
"report blocked" is reserved for a captcha, a phone code, an identity check, or an answer only
the person can give. A `wait_for` selector must match `^[A-Za-z0-9_\-\s\[\]="'#.:*,>+~()^$|]{1,120}$`
(a plain CSS selector, read-only through `querySelector`).

## 13. The recovery ladder, hangs, and the AI being down (2026-11-02)

**Why.** The real batch of 2026-11-02: Nelnet's Workday form showed grey skeleton boxes and the
brain answered `wait` for 10 minutes; then an `await` inside the loop never returned (an
`executeJavaScript` on the skeleton page) and the loop froze for 8 minutes with Stop dead.
Eliassen parked "needs you" on ONE garbled model reply. Barclays signed in with a just-created
account, asked for a reset, and gave up when no mail came in 3 minutes. From 02:29 the company
API key ran out of credits and every job parked "needs you" within 20 seconds.

**Every call into the pane is capped.** `guard(promise, ms, what)` races the call with a
timeout (`PANE_MS` 20 s; the fill 120 s overall with a cap per phase; a brain call 90 s; CDP input events 6 s), with the
run's Stop flag (polled every 150 ms, so Stop takes effect inside a hung call), and with a
navigation (`did-navigate` bumps `navEpoch`; a call to the old document is rejected instead of
left pending, which Electron otherwise does). The run's `webContents` is wrapped in a proxy
whose `executeJavaScript` is guarded, so every helper (the view, the fill, `handleTerms`, the
dropdown readers, `attachResume` in main.js) is covered without touching each call. A timeout
is a `hang` observation: the step's `catch` sends it up the ladder's strategies and the loop
goes on. Nothing in the loop can hold it any more.

**Hang detection.** `TailorAgent.pageShape()` (fields, buttons, a skeleton marker, words) runs
every step. A page with no fields and either a skeleton (`aria-busy`, a `skeleton` class, a
loading indicator, "Loading" with nothing else) or almost nothing on it (at most 6 buttons, under
60 words) that stays the same for `SJ_AGENT_HANG_S` (45 s) goes to the ladder's strategies.
The brain's plain `wait` is capped: `MAX_WAITS` (3) in a row and the shell escalates the same
way.

**The ladder** (`recover()` in agent.js), one per page (keyed on the page's hash, so the rungs
add up on the same page and a new page starts fresh; at most `MAX_LADDERS` 4 per application):
1. one nudge in the next prompt with the exact problem and the last three steps' actions and
   results ("do not repeat it: inspect, read_errors, find, do it another way");
2. one reflection call: the same state plus a `<reflection>` block (the problem, what was tried,
   "list 3 to 5 likely causes, rank them, pick the likeliest, choose ONE strategy from the menu")
   and a screenshot through `agent_step_image` (medium effort). The reply's `strategy` is run
   by the shell after its actions, when they did not already move the page;
3. the strategy menu in order, each once: `wait_for form` (up to 15 s for fields or buttons
   without a skeleton), `reload`, `back`, `reopen_job`, `sign in again` (reopen plus a sign-in
   nudge), `fill the field myself` (the engine stands back one step; the brain types the
   placeholders), `read more of the page`, `use the other input form` (the last three are hints);
4. only then PARK, with "I tried a hint to the AI, a closer look with a screenshot, reload, ...
   and could not get past this: <problem>".
A hang (the page did not answer, a skeleton, too many waits) skips to rung 3 with the short
menu wait_for form, reload, reopen_job. The ladder replaces every former "stuck, have a look"
stop: the same page 4 times, the same action 5 times, 4 failed steps, 4 bad model replies, the
brain's `needs-person`, and the ready gate refused 4 times. Each rung shows on the feed as
"Trying another way: reloading the page" (state retry) and in the debug log as `ladder <name>`.

**Bad model replies.** `brain.step` calls the model up to three times with backoff (0.5 s,
1.5 s) and a "reply with JSON only" nudge before saying `no-reply`; the shell then counts a
failed step (with the real error text on the feed: "Asking the AI again (bad reply: {oops)")
and at four goes up the ladder. **The AI being down is never "needs you".** A call that fails
for credits, a key or access (`BrokerUnavailable.reason` `ai_company` (the broker's own
outage tag, PR #166), `upgrade_required`, `no_key`, the Anthropic "out of usage credits" / 401 /
402 wording) is `fatal` after ONE call: the step route
answers `{ok: false, fatal, message}`, the job stops with "SponsorJobs' AI is unavailable right
now (credits). Nothing was changed; try again later" (reason `ai-unavailable`), the record gets
an `agent_note` the Auto-apply row shows instead of "Approved, ready to fill", ONE Telegram line
goes out per half hour (`/api/autoapply/agent/unavailable`), and a running batch stops after
that job with the same words in its sum-up (for `ai_company` the words are the broker's
`AI_COMPANY_TEXT`). Transient failures (`ai_transient`, 429, 5xx, overloaded, timeouts, offline)
are retried with backoff as above. The debug log and the history line carry the
broker's actual error text, not "no usable reply".

**Email verification, end to end** (Nelnet's Workday flow). `await_email` waits
`SJ_AGENT_EMAIL_WAIT_S` (180 s), then looks once more after `SJ_AGENT_EMAIL_RECHECK_S` (60 s);
still nothing and the job parks with Continue ("The <company> email (a link) did not arrive in
4 minutes. Check your inbox and do the step on the page, then press Continue; I look for the
mail once more first"), remembering what it waited on, and the resumed run asks the inbox once
more (20 s) before reading the page: a late mail still completes the run. An emailed link is
opened only when it is on the site the run applies on (the same registrable domain; the very
host on shared hosting such as oraclecloud.com) or on the same hiring system (`mailLinkOk` in
the shell, `link_on_site` in inbox/detect.py), and only from a message whose sender address is
that site's or its hiring system's (the company name alone reads a code, never a link). Any
other link is left alone and the wait goes on as if no mail came. A link that passes is
opened in the pane (`afterEmailLink`); the "account activated, sign in" page is signed in by
the brain (rule 13); a careers home or job search page right after that is not the end: the
shell opens the job again by itself (and the brain has `reopen_job` for the same, rule 11).
`<portal>` tells the brain whether the saved account was created THIS run (the shell's
`portalNew`: the site does not know it yet, so Create account first, never a reset) or exists
(sign in; on failure sign in once more, then Create account if offered, then Forgot password).
Mock scenarios: `mock_ats.html?skeleton=1` (the form page is a skeleton until a reload) and
`?activate=1` (the "an email has been sent" banner, the activate link, the activated page,
the sign-in that lands on the job search page); e2e sections 18 and 19.

**The sign-in wall, deterministically** (the e2e of 2026-11-02 looped "Filling 2 boxes, then
clicking Sign in" twenty times). The shell reads the wall itself instead of hoping the model
follows rule 14: a click on Sign in that leaves the sign-in form (a password box and a Sign in
button) on screen is a failed sign-in, counted per site. A page that says the account must be
verified ("An email has been sent to you. Please verify your account.", "not verified") leads
straight to the email wait, without a model call, once per site until its link was opened; so
does the "check your email" page after a reset request. Two failed sign-ins: the shell clicks
Create account when the site offers it (the account may be new to the site), the next failure
Forgot password (then the reset mail); four go up the ladder, whose menu on a sign-in page
starts with `create account`, `reset password` and `wait for the site's email`.
(2026-11-18) A create-account form (two password boxes, or one under a "Create Account" heading)
is not a sign-in form: landing on it is not a failed sign-in, and Forgot password is never its
next step. When the create form is refused, the site's own error decides. "Already exists":
the reset path stays open. A password rule ("Password must include: - A numeric character"):
no account exists, so the engine saves one fresh password that meets the rule
(`portal-account` with `rotate`, refused once a sign-in or creation ever worked on that host,
which the shell records with `confirm`), the brain retries once, and a second refusal parks
with "I could not create an account on <site>: <the site's error>". Any other form error
parks the same way after three refusals. Reset and the email wait are off while no account
exists. A sign-in or create-account form's own button is clicked whatever the site calls it
(one Workday tenant labels it "Submit"): on a page that is nothing but that form (a password
box, no file box, text area, dropdown or radio, at most five other boxes) the submit guard
lets the first such button after the password box through, in the engine, the shell and the
page script; the application's Submit stays guarded everywhere else. If the brain still
reports ready_to_submit on such a form, the shell signs in itself (the step route with
`shell_signin`, no model call, the saved account through the same placeholders), twice at
most per site, each counted on the ladder. The same step
on the same page coming round a third time (even with other steps between) goes up the ladder
too, and the same page three steps in a row (was four). After an emailed link, the shell opens
the job again only from a page with no field of its own that reads like a job search ("Search
for Jobs"), within 8 steps, never from the "account activated" page or a form under a header
search box. `back` only ever returns to a page this application was on (Electron's history
keeps the previous job's pages; the e2e's activation run once went back into the skeleton job).

**From the owner's real pages (2026-11-02, `tests/fixtures/forms/real/`).**
- *A closed job* (Oracle "This job is no longer available", US Bank's Workday "The page you are
  looking for doesn't exist", Amazon "Sorry, the job you're looking for isn't available") is
  caught by `closedJob()` before any model call, while no form has been reached: a short line of
  the page's own that says the posting is gone, no Apply control, no field of its own. The run
  stops as `job-closed`, `/api/autoapply/agent/closed` moves the record to the closed-posting
  state (the row reads "Job closed", its action is View, so no batch picks it again), nothing goes
  to Telegram, and a batch counts it apart ("2 closed by the employer") and moves on. Zero AI cost.
- *Live-region announcements are not errors*: Workday's aria-live announcer ("Research Analyst
  page is loaded"), `role=status`/`log` and `aria-live=polite` regions without an error class are
  left out of `Error:` lines, `readErrors` and `pageCheck`.
- *The footer is honest*: the view walks the whole page, so it never says "N pages below" next to
  `[End of page]`; a view the cap cut ends "(N more elements below; read_page offset M shows them)"
  and `[End of view]`.
- *Honeypots are invisible*: a text box labelled for robots (Barclays' Workday "This input is for
  robots only") or parked off the canvas is never in the view, `pageShape` or `pageCheck`; the
  fill engine already skipped it (its `HONEYPOT_RE`, now also "for robots" and "not human").
- *LinkedIn is never clicked*: the shell refuses any click whose words name LinkedIn (and any
  other social sign-in while the page offers an email or manual path); the brain drops such a
  click too, and the prompt prefers Apply with Email or Apply manually.
- *Chat-style apply* (Chewy's Phenom chatbot): the prompt says to open the chat and answer one
  question at a time with placeholders; the cycle check keeps it from looping.

## 14. The plan, page checks and the pre-submit critic (2026-11-02)

- **Plan.** The brain's reply carries `plan`: short lines "done: Job page", "now: Sign in",
  "todo: Review" (at most 12). The shell keeps it on the run, sends it back in `<plan>` every
  step, saves it in the park state so a resumed run continues from it, and shows it above the
  timeline (Steps) with the spend. No extra call.
- **Page check.** Before a click that moves on (`NEXT_RE`: Next, Continue, Save and continue,
  Review...), once per page, `TailorAgent.pageCheck()` lists the required fields still empty
  (text, select, combobox, file; never the site's search box) and the errors shown. Any empty
  one holds the click: the step's result reads "held: required fields still empty", the
  observation says which, and the brain fills them (placeholders) or reports `needs-answers`
  (the person is asked). The second time on the same page the click goes through.
- **Ready gate, deterministic first.** `notReady(view)` in the shell and `brain.not_ready` in
  the engine refuse `ready_to_submit` when the page has a password box, is a sign-in or
  create-account form (an email box with a Sign in / Create account button and no Submit), or
  shows no Submit-like control at all (Gartner, 2026-11-02: "ready" on the sign-in page). The
  brain gets "You reported ready_to_submit but the page has a password box. Carry on"; four
  refusals go up the ladder.
- **Critic, second.** `/api/autoapply/agent/critic` (`brain.critic`, `agent_critic` on the
  client: 300 tokens, low effort, Haiku-priced where the broker routes it) reads the review page
  (values masked as always), the plan and the open fields and lists anything blank or
  suspicious. Issues give the brain ONE more round ("Before I hand over, a check found: ... Fix
  what you can; never Submit"); then the hand-over goes ahead and the Ready to send line names
  what was noted ("A check noted: ..."). The critic never blocks a hand-over; a failed call is
  "ok". FakeLLM's critic is deterministic (an error line or a required empty box is an issue).

## 15. Budgets (2026-11-02)

Steps: `MAX_STEPS` 60 (`SJ_AGENT_MAX_STEPS`). Dollars: every step route reply carries `cost`,
estimated from the client's token counts (`llm.last_usage`) at the model family's list price
(`brain.PRICES`: Sonnet 4.x $3/$15, Sonnet 5.x $2/$10, Haiku 4.5 $1/$5, Haiku 5.5 $0.10/$0.50
per million; a plain high estimate, cache reads not discounted), else from characters (about
four a token). The shell sums it (`spent`, kept over a park, shown as "about $0.12 spent of
$0.80" under Steps); at `COST_CAP_USD` (0.80, `SJ_AGENT_COST_CAP_USD`) the next step is asked
as the last ("done only") and a `needs-person` there parks with "I spent the $0.80 budget for
this application: <last state>". Expected per application after this change (Sonnet 4.6): 20
to 35 steps at about $0.018, 0 to 2 reflection calls with a screenshot at about $0.03 to $0.05,
one critic under $0.01: roughly $0.45 to $0.75, most runs well under the cap; on Sonnet 5.5
about two thirds of that.

## 16. The batch's time cap (2026-11-02)

A batch job that is still running after `JOB_CAP_MS` (12 minutes, `SJ_AGENT_JOB_CAP_MS`)
parks at the next step with "This one took too long (12 minutes): <the feed's last step>" and
the batch moves on; Continue re-runs it later. Single runs have no cap (the person is watching
and can Stop; the pane caps above make a hang impossible anyway).

## 17. The first real Greenhouse run (Gusto, 2026-11-16)

The owner's run on a live Greenhouse posting (Gusto, Lead CX Business Intelligence Analyst)
uploaded the resume into the optional Cover Letter box, ticked every "How did you hear" box, and
asked about questions the person's own material answers. What changed:

- **Uploads go into the chosen box only.** The plan names the file per box (`resolve_plan`
  `letter_path`; main.js `cdpUploadFiles` takes a ref -> path), the brain's `upload` carries `path`
  and `file` (`resume` | `cover_letter`) by the line it names, and the shell's `upload()` finds the
  element's OWN file input (itself, its label's control, the one file input of its field group;
  never "the page's first") and refuses the resume in a box whose words say cover letter.
  `attachResume` uses `TailorAutofill.resumeInput()` (a resume box, never a cover letter box, never
  one already holding a file); the fill's fallback asks `resumeInputOpen()`. `cdpUploadFiles` sets
  the file by objectId (its `DOM.requestNode` needed `DOM.getDocument` first and failed quietly).
- **A cover letter is written, not asked.** The plan reply says `wants_letter` when the form has a
  cover letter box (file or text) and the application has no letter; the shell then calls
  `/api/autoapply/agent/cover-letter` once per run (the Review page's drafter, from the person's
  history and this job; saved on the application with its PDF, `<Name> Cover Letter.pdf` under
  `uploads/<id>/cover/`) and fills again, so the box gets the letter. A failure leaves an optional
  box empty and asks nothing; a required one is the person's. The bar has **Read cover letter**.
- **Ready to send is one short line** ("Ready to send. Check it, then press Send.", plus "A check
  noted N things: see Steps." when the critic found any). What the agent wrote or chose (the cover
  letter, the declined self-identification questions) and each noted issue are their own lines on
  Steps. The critic reads the WHOLE form (a view the cap cut goes with `readPage` of the rest),
  runs one round only, and is told that `{{city}}`-style words are the person's values redacted
  from it, never a template left on the page (the Gusto check said so of the location and the file
  names; the page itself had the real values).
- **Nothing unrendered is ever typed, picked or attached**: `brain.step` drops an action whose
  text, option or file still holds `{{` or `}}`, `resolve_plan` drops such a value, and the shell's
  `act()` refuses one.
- **The Submit past the cap is named**: a view cut by its 12 000-char cap ends with "The page's
  submit, further down:" and its line, so the ready gate finds it (a long job description above a
  one-page Greenhouse form hid it, and every ready was refused).
- **Answered from the person's material:** "Do you currently reside in one of the following
  locations?" from the profile's city (`planner.residence_option`: the option naming the city, else
  the one "No / None of the above" when neither city nor state is listed); "Have you ever ... worked
  at <company>" when the question names the company (`worked_here_question`, never a relative or
  "anyone who works at"), from the employment history; a checkbox group ticks exactly ONE box from
  the saved answer (never every box from its Yes/No polarity), and a saved "Job board" on a list of
  named sources only picks a named job board, general ones first (Indeed, Glassdoor, ...), after a
  generic "Job board" / "Internet" / "Other".
- **Self-identification is never asked per application.** The five saved answers (Memory >
  Self-identification) answer sentence wordings too (`eeo.topic_key`: "How would you describe your
  gender identity?" is `@gender`). A question nothing saved fits (transgender, sexual orientation,
  first-generation professional, or a saved answer none of the choices says) gets the page's own
  decline choice (`origin: eeo_decline`, in the planner and in the shell's known-only check
  `known_answer(eeo_fallback=True)`), and Steps says "Declined to self-identify on N questions you
  have not answered".
- **Verified actions** (from a study of vercel-labs/agent-browser, Apache-2.0): a card-style radio
  or checkbox whose input is hidden is listed with its state and clicked through its card; required
  radio and checkbox groups (required on the group's container too) show in the page check; a click
  on a box is read back (a label.control retry, then a failure); a click an overlay would swallow
  fails and names the cover; a dropdown pick reads only its own list (`aria-controls`), waits up to
  2 s for a late one, never offers a disabled choice, and is read back; `realClick` scrolls
  instantly and re-measures until the box is still.

## 18. Workday My Experience, the CC-305 form, uploads, Lever (Uline and CrossCountry, 2026-11-16)

Defects found on real jobs, each fixed at its cause (tests/test_agent_workday_lever.py and
tests/js/test_agent_workday_fixes.js; fixtures modelled on the real markup):

- **The fill "hang" on My Experience.** Workday's Field of Study and Skills are search prompts whose
  list is portalled to `<body>`, draws a radio in every option row, and ignores Escape and blur.
  The engine opened one to read its options and left it open; the ~100 option rows then counted
  as form fields and the model plan ran past the fill's 40 s cap. Now: a list is closed with a
  press on a neutral heading away from it (`pressAway`), a chosen-values pill list is never read
  as the open menu, controls inside an option list are never fields, option reading has an 8 s
  budget, the plan call is capped (`SJ_AGENT_PLAN_MS`, 25 s) with a reason, and a fill that still
  runs past its cap names its phase ("fill timed out while the AI planned 112 fields").
- **Work history and education** (Apply Manually): `/api/profile/autofill?record_id=` carries the
  tailored resume's jobs, schools and skills (autoapply/history.py); `fillHistory` fills each
  "Work Experience N" / "Education N" section and presses its own "Add Another" for the next
  entry. Workday's Month / Year spinbuttons keep only real key presses, so the engine names them
  (`keys`) and the shell types them (`typeKeyOps`). Field of Study and Skills filter only on a
  real Enter: the shell types the query and Enter (`searchPicks`), the engine picks the best REAL
  option the degree supports (`searchChoice`; an MBA is Business Administration), and nothing
  that fits leaves the optional box empty.
- **Only required questions are "left for you".** `leftover()` lists required boxes only
  (attribute, aria-required, a `*` or `✱` at the end of the question, or a `required` class), so
  an optional box never stops a run, never becomes an ask, and never shows as "N left".
- **CC-305.** The signature "Date" on a self-identification or signature page is today's date in
  the form's own control (`fillSignatureDate`). "Please check one of the boxes below:" takes its
  topic from its choices, so the saved disability answer ticks exactly its box (else the decline
  box); styled 0x0 checkboxes count as fields when their label is on screen.
- **Duplicate uploads.** Before any upload the box's upload area is read: a file listed with the
  same name AND size is not uploaded again, and extra verified copies are removed with the area's
  own Delete buttons (tightened in section 21: a name alone is not enough).
- **Lever.** A custom question's label is the `div.application-label` above it (no `<label for>`),
  with its `✱`. "Which location are you applying for?" and the hybrid-location pick come from the
  profile's city (`office_option`); a visa-details text box gets the saved status sentence
  (`visa_detail_text`), never a bare Yes or No. The page is read again before an ask, so a box
  the site filled after the upload ("Current company") is never asked.
- **Steps and cost.** `needs-answers` with nothing required open: a firm nudge the second time,
  a park with the brain's reason the third (it looped ten times, then a reload restarted the
  Workday application). `settle()` waits up to 8 s more while the page shows a spinner,
  aria-busy or a progress bar, instead of reading a page whose Next is disabled while it saves.

## 19. Review answers at Ready to send (Uline, 2026-11-16)

The owner pressed Send on a Uline (Workday) application without seeing that the agent had picked
"Agricultural Economics" for Field of Study and "JavaScript" for Skills, and that the form held a
resume tailored for another Uline role. Now the bar at Ready to send has **Review answers** next to
Send: every field the run filled, page by page, the question as on the page, the value or choice,
and where it came from; then the files with the application each was tailored for. The Ready line
stays one short line; the list opens on demand under the bar like Steps (`#bbAnswers`).

- **Where it comes from** (no new page read, no model call). The fill engine logs each control it
  marks with its origin (`autofill_core.js` `takeFillLog`: the plan op's `from`, or the pass that
  ran: profile, library, workauth, eeo_decline, history, today, closest, skills), and
  `runAutoApplyOnPane(..., {answers: true})` returns it with the files it uploaded (`attached`, or
  `listed` when the box already held a file of that name). The shell adds its saved
  self-identification picks (`fillSavedEeo`), the brain's own type / select / radio / option
  actions (`brainAnswer`: "you" when the person answered that question this run, a placeholder's
  origin, else "ai"; never the portal email, password or emailed code), and `attachResume`.
  `readyAnswers` assembles the list on `handOver`; it rides on the Ready to send progress line
  (`answers`).
- **Flagged**: an AI pick, an optional list filled by the closest match or from the skills list,
  a fallback (a declined self-identification question nothing saved answers, a worked-out or
  drafted answer), a resume tailored for another application (the upload path names its record),
  and a file the form already held under the same name (not attached again).
- **Self-identification values** are shown only in this local list: `safeReply` logs a count of
  the answers, never the rows; nothing goes to the activity feed or the engine. `answersTelegram`
  builds the same list as one Telegram message with self-identification values left out; the
  Ready to send Telegram notice carries only its flagged lines (section 21).
- Tests: tests/test_ready_answers.py (tests/js/test_fill_log.js, tests/js/test_ready_answers.js).

## 10. Milestones

1. **One job, visible.** Assisted apply on a queued Workday job -> job page -> Apply -> sign in or
   create account -> pages filled -> review page -> "Ready to send" + Send. Offline proof on
   the mock ATS (`tests/fixtures/forms/multipage.html?wall=1` plus a job page in front of it),
   then a real-model run on the fixture, then Kofi's live run on Country Financial.
2. **Batch.** Apply to all N, unattended; Telegram summary with one Send per package;
   auto-submit on the verified allowlist with Autonomous submit on.
3. **Verification email** through the person's own Gmail (CLAUDE.md Phase 5), so account
   creation completes with nobody present.

## 11. Tests

- `tests/test_agent_view.js` (node): the serializer on the fixtures: indices in order, no
  personal values in `value=`, `*` on new elements, file inputs at opacity 0 kept, frames
  offset by 1000, cap honored.
- `tests/test_agent_brain.py`: prompt never contains a profile value; placeholders resolved;
  Submit-like clicks rejected; unknown index dropped; `done` alone; FakeLLM scripted run over
  the multipage fixture views reaches `ready_to_submit`.
- `shell-electron/e2e-agent.mjs`: the real Electron app with `RESUME_AGENT_FAKE_LLM=1` on the
  mock ATS: one click on Assisted apply ends on "Ready to send"; Send submits the mock; a
  `?captcha=1` variant stops with the captcha message and no submit; `?skeleton=1` is reloaded
  by the ladder; `?activate=1` runs the Workday activation mail end to end (sections 18, 19).
- `tests/test_agent_engineer.py` and `tests/js/test_agent_engineer.js` (sections 12 to 16): the
  tools, retries and error classes, the ready gate, the critic, the cost; and under node with a
  fake pane: capped pane calls and Stop inside a hung call, the hang ladder (a view that never
  resolves, a fill that never resolves, a skeleton with the brain waiting), bad replies
  escalated, the account-problem stop that ends the batch, observations, the page check, the
  critic round, the mail that never comes (park, then one more look on resume), the batch's
  time cap, and a debug log that never carries a secret.
- Quality: one real-model run per milestone on the fixture and on a live Greenhouse job page
  (stops before submit), logged step count and cost.

## 20. The accessibility tree and the final form check (2026-11-16)

Reading the Gusto form through Chrome's accessibility tree showed what the DOM view missed (every
"How did you hear" box ticked, the resume in the Cover Letter box, a dropdown left open, three
copies of the resume on Workday); the tree alone hides what the DOM view has (page text, errors,
react-select's chosen value, Lever's "✱", long labels). The agent now reads both
(`shell-electron/ax_view.js`, behind `SJ_AX=1`; **off by default** until the mock e2e has run
with it on, then `DEFAULT_ON` flips).

- **Merge.** For every frame of every CDP session (`Page.getFrameTree`), `Accessibility.getFullAXTree`
  gives each control's role, name, value and states (checked, mixed, expanded, required, disabled,
  invalid, focused) by `backendDOMNodeId`. The view's own elements (`window.__tailorAgentRefs`) are
  resolved to the same ids through `DOM.describeNode`, so each `[N]` line meets its node (by name
  when the node has none). One in-page function adds what only the page knows: the files a box
  lists (Workday's file-upload-item rows, Greenhouse's filename, Lever's success name, else the
  input's own files), a "✱"/"*" required mark, which question a box belongs to. Role list and
  property reading follow vercel-labs/agent-browser `snapshot.rs` (Apache-2.0).
- **What the model sees** (hook 1 in `view()`): the same lines with `required` (a "✱" counts),
  `invalid`, `expanded=true`, `checked=mixed`, `value="(filled)"` when only the tree sees the text,
  `file=chosen holds="resume x3"` (file KINDS, never names), and the accessible name of a one-letter
  button (`<button name="Remove file">x</button>`). Never a value, a file name, or a
  self-identification choice. An empty required native box is not also "invalid" (Chrome marks both).
- **The final check** (hook 2, after `notReady`, before the critic): `verifyForm` lists required
  fields still empty (groups too), a group with more than one box ticked when the question takes
  one (Yes/No options, "select one", "How did you hear", or a "None" ticked with others) or with
  every box ticked (3 or more), a resume in a cover letter box (or the reverse; the run's own file
  names decide when the names say neither), the same file listed twice, a dropdown left open, a
  field marked invalid. First time: the brain gets the list with the line numbers ("Fix these on
  this page ... never Submit"). Second time: the person is asked once (the run parks: "Before I hand
  over: ... Fix it on the page, then press Continue."). After that Continue, the hand-over goes ahead
  and Steps names each remaining problem ("Check noted"). Clean: Steps shows "Checked N required
  fields: all filled". Without the debugger (DevTools holds it) the page facts alone still run the
  check. Capped at `SJ_AX_MS` (6 s); a failure leaves the view and the gate as they were.
- **Tests:** `tests/js/test_ax_view.js` (`tests/test_ax_view.py`) on `tests/fixtures/forms/ax_*.html`.

## 21. The right resume, the Ready to send notice, labels, Greenhouse's confirmation (2026-11-16)

- **Never skip the right resume.** Every tailored resume leaves the app as "<Name> Resume.pdf"
  (tests/test_outgoing_filenames.py keeps that employer-facing name on purpose), so a Workday draft
  that kept an EARLIER application's resume lists a file of the same name, and the name-only check
  skipped the resume tailored for this job (Uline). Now (`shell-electron/uploads.js`
  `uploadDecision`, `autofill_core.js` `fileListing`), per listed row of that name:
  - same size, within the rounding the page shows ("106.26 KB" is within 5 bytes, in 1000 or 1024
    units; a "5MB max" limit is never read as a size): this file. Not uploaded; extra verified
    copies are removed.
  - another size, or no size shown: not verified. Ours is uploaded and theirs is left alone, and
    Review answers flags it ("earlier_file": "The form also holds an earlier file with the same
    name. Check which one to keep."). Exception: a row with no size that THIS run put there (the
    run's upload log: the same path and size) is ours, so nothing is uploaded again; a row this
    run uploaded from another file of that name is removed and replaced. A file the run did not
    upload is never removed.
  The page shows nothing else (no hash), so the size is the only identity it offers.
- **Ready to send on Telegram.** On hand-over the shell's post to `/api/autoapply/agent/ready`
  carries `notice` (`readyNotice`: the run's start, fields filled, lines flagged, and the flagged
  lines in `answersTelegram`'s words, never a self-identification value). The engine sends ONE
  message per run (`notify/ready.py`; rules and template in docs/notify.md, "Ready to send"). No
  Send button: notify/ has no sanctioned way to approve an assisted submission from Telegram.
- **Labels keep their words.** `cleanLabel` (engine and view) strips "select one" / "required"
  only as furniture: alone in brackets, or at the end or start ("Gender Select One Required").
  Greenhouse's "How would you describe your gender identity (please select one)?" had read
  "(please )?".
- **A ticked box names itself.** In a group, the Review answers value is the box's own label
  ("How did you hear about this opportunity?": "Indeed"), from the engine's read-back and from the
  brain's own tick (the page text above the boxes is the question); "Checked" only for a lone box.
- **Greenhouse's confirmation page.** After Submit the board lands on
  `<board>/jobs/<id>/confirmation` with a "Track your application" card; both are success markers
  in autoapply/recipes.json, so Send reports "Sent, and the site confirmed it."
- Tests: tests/js/test_upload_identity.js (tests/test_upload_identity.py), tests/test_ready_telegram.py,
  tests/js/test_answer_labels.js and the notice checks in tests/js/test_ready_answers.js
  (tests/test_ready_answers.py), tests/js/test_success_greenhouse.js on
  tests/fixtures/forms/greenhouse_confirmation.html (tests/test_fill_recipes.py).

## 22. Grounded answers before any ask; optional self-identification; the check's false alarms (2026-11-17)

**Grounded answers** (`autoapply/grounded.py`). The planner answers a question about the person
only in wordings a rule was written for, so each new wording went to the bar and Telegram
although the profile says it ("Where in the US do you currently reside?" at Upgrade, "Current
company" on Lever, "Do you currently reside in one of the following locations?" at Gusto). Now the
ask route (`/api/autoapply/agent/ask`, both the shell's `known_only` check and the ask itself)
first sends the page's still open questions, in ONE call, with the person's saved facts: where
they live (city, state, postal code, country), each job, each school, their skills, and their own
saved answers (the 30 most related to the page). Never a self-identification or legal value, a
drafted answer, a password or code, nor resume text beyond those facts; stored self-identification
values are withheld from the payload as a guard. Never asked of the model: self-identification,
work authorization, visa, citizenship, legal attestations, signatures, consents, terms, essays,
why-this-job questions, and salary unless a salary is saved.

The model returns {answer, fact} or none per question. Code checks each one against the fact it
cites and drops what it cannot verify (the person is then asked, once, and the answer saved as
before):

- a list question's answer is one of its options, exactly;
- a place: names the fact's city, state, postal code or country, and no other state or country
  (a free-text place has no other words); Yes only when the question or the choice names where
  they live, No only when nothing on the question or its choices does and some place is named;
- a job: the saved organization or title (a "current" question needs the current job), or dates
  whose years are that job's; Yes only when the question names that organization or title;
  never a No (a history is not proven complete);
- a school: its school, degree, field or degree level (a "highest" question needs the highest);
  Yes for a level only at or above it, and not while still studying for "completed"; never a No;
- skills: each part is a saved skill; Yes only when the question names one (never "years of");
- a saved answer: its question asks the same (the same key, or nothing this question asks is
  new), and the answer is the saved one, or the option the saved one names.

A grounded answer carries origin `grounded` and its fact's kind to the shell (`origins`, `facts`
on the reply), is never saved to the library, and shows in Review answers flagged "Answered from
your saved address. Check it." (work history, education, skills, one of your saved answers). Tried
questions are cached per wording, options and facts for the session, so the check and the ask
that follows never pay twice. Cost: about 2,000 tokens in and 150 out per page that has an
eligible open question, about $0.003 on Haiku 4.5 and $0.006 on Sonnet 5.5; nothing when nothing
is open.

**Optional self-identification** (Upgrade, Greenhouse). "What is your veteran status?" and "What is
your disability status?" stayed on "Select..." with both answers saved: the shell picked saved
self-identification answers only for REQUIRED open questions. `fillSavedEeo` now also takes every
empty self-identification control in the view (`emptySelfId`): the saved answer when a choice says
it, else the page's decline.

**The pre-submit check's false alarms.** The critic read "no resume attached" (Greenhouse shows
the file's name where the upload box was) and "LGBTQ+, ethnicity still empty" (react-select keeps
its choice beside its input). The shell now sends what the run filled (labels) and attached (file
kinds) from its own record; the view marks those dropdowns filled, the prompt names both lists,
and a line the record contradicts is dropped in code (`brain.false_alarm`).

## 23. Standing rules for opt-ins, acknowledgements and worked-here; the park reason (Phoenix Contact, 2026-11-17)

After 15 fields on a Greenhouse form (Phoenix Contact) the run asked five questions; four are now
answered by category rules (`autoapply/consents.py`, `planner.worked_here_question`), in the
planner and in the ask route's known check, before the grounded call:

- **Worked here** in its "employee" wordings ("Are you a current or former Phoenix Contact
  employee?", "<company> employee (current or former)", "previously employed by/with <company>",
  "worked for <company> before"; never a relative, a referral, or "why do you want to work at")
  is the per-company question. `applicant/facts.worked_here` now answers **No from the work
  history** when the person has one and the company is not in it (origin `history`); a company in
  it is still the one-tap confirm. The label mapper never sees these wordings (`rule_known`), and
  a mapped @previously_worked wording that names the company needs no tap. The person's Yes or No
  to such a wording is saved for that company only.
- **Marketing and communication opt-ins** (texts, SMS policy, newsletters, job alerts, talent
  community) get the page's No / decline choice (origin `optin`), unless the person saved a yes
  under `@marketing_optin` (default no; onboarding will expose it). An optional box stays
  unticked. A required question with no way to decline is the person's (a forced consent).
- **Acknowledgements** (a background check or drug screening as a condition of employment, also
  worded "would you be willing to ... complete any background check", GDPR, a privacy notice,
  at-will employment, "I certify the information is true") get the affirmative choice under the
  same standing consent that accepts required terms (`accept_required_terms` "always"; origin
  `consent`). Without it they are asked as before. A question that asks the person to disclose
  (a conviction, a failed screen, an existing non-compete, a termination) is never one.
- **Restrictive covenants** are a fact: one key, `@restrictive_covenants`, asked once and reused on
  every wording ("Are you bound by a non-compete agreement?"); never grounded.
- The ask's context line ("After “...”, the application asks") is cut at a word boundary with an
  ellipsis and never inside a bracket (`trim_label`, `trimLabel`).
- A new run on a record clears an earlier run's park reason on the Auto-apply row even when the
  park file no longer holds the entry (`/parked/<id>/take`).
- Tests: tests/test_phoenix_asks.py.

## 24. Apply setup: answer the usual questions once, before the first form (2026-11-17)

The agent kept stopping to ask questions a one-time setup could answer. AIApply's onboarding (studied
in the owner's account) collects them up front; we do the same in seven short, skippable steps, and
avoid what they get wrong (fake previews and countdowns, answers sent unreviewed, half-written answers
like "In" for a link, invented claims). Every step is pre-filled from what the app already knows and
saves into the store that already owns the answer; the setup has no store of its own beyond its place
(`apply_setup`: seen, step, done) and the wants no other store holds (`apply_wants`).

| Step | Saves into |
|---|---|
| 1. Resume and contact | profile + essentials identity (name, email, phone, street, city, state, ZIP, country, links), the brain's contact facts; a link the person has none of goes on `essentials.declined` (answered No, never half-written); "apply with my SponsorJobs email" (`apply_with_alias`) |
| 2. Work authorization | `work_auth` (prefs store) and the country of citizenship (`@citizenship`, `@citizenship2`); the "please explain" sentence (`workauth.note`) previews live |
| 3. What you want | `aa_focus` titles and places (Auto-apply's picks), "Never apply to" (`aa_learn.json` exclude), `work_auth.relocate`, `@start_date` + `earliest_start`, `@notice_period`, `@salary` + `desired_salary`; level, job type, work style, distance and industries in `apply_wants` |
| 4. Form questions | `@heard_about` + `hear_about_us`, `@over_18`, `@restrictive_covenants`, `@marketing_optin` (default No), `@forced_consent` (default ask), `accept_required_terms`, `@security_clearance`, `@drivers_license`, `@gpa`, `@related_degree`, `@current_salary`, `@current_bonus`, `@current_equity`; worked-here is shown as answered from the work history |
| 5. Self-identification | the eight keys of `autoapply/basics.py`, and `eeo_decline` |
| 6. How to apply | the apply mode, Auto / Assisted / Hybrid (section 26; the old autonomous switch migrates into it), Auto's match threshold (`auto_min_match`), `daily_cap`, `autopilot_per_day` |
| 7. Finish | nothing: "The agent can now answer N of the M questions forms usually ask", from `autoapply/setup.py` `coverage()` over the real stores, each open item with a jump to its step |

New canonical keys (answer library `_CANON`, brain registry, label mapper): `@current_salary`,
`@current_bonus`, `@current_equity`, `@security_clearance` (held, never "able to obtain"),
`@drivers_license`, `@gpa`, `@related_degree` (only when the person sets it) and `@forced_consent`
(a required opt-in with no way to decline: asked unless the person saved a standing yes,
`consents.saved_forced`).

It opens by itself once, the first launch after sign-in (`suMaybeFirstRun` in app.js, from `boot()` and
right after sign-in), and resumes at the saved step; My profile and Auto-apply each have an "Apply
setup" button. Stop on Auto-apply ends the application in progress before its next step and the rest
of the list; the review or autonomous choice applies to the next list started (the batch reads it once).
## 25. Fill-through: never a question mid-form; one batch at the end (2026-11-17)

The owner: "ours must never stop to ask questions" mid-application. Until now a required question
the planner and the brain left open parked the run on the bar ("Question 1 of N", "After X, the
application asks ...") and waited, page after page. Now the run fills everything it can and asks
what only the person knows ONCE, at the end. It still stops at Ready to send (CLAUDE.md section 7:
auto-submit only on the verified allowlist with Autonomous submit on); it never stops BEFORE it.

| On the form | What the run does | Review answers |
|---|---|---|
| a field the profile, the library, work authorization, the standing choices or a grounded fact answers | filled, as before (section 22, 23) | its origin |
| an optional box nothing answers | left EMPTY (never "N/A", "Not provided", a lone "In") | "Left empty: optional." (shown, not counted, not on Telegram) |
| a required choice nothing answers, with one neutral option ("Prefer not to say", "I do not wish to answer", "Decline"), not about a fact | the neutral option, picked by the shell with real clicks | "Chose “Prefer not to say”: nothing saved answers this." |
| a required free-text question, a required choice with no neutral option, or any question about a fact (work authorization, a degree, an employer, a legal fact, a covenant, a clearance, salary history, a conviction) | left empty and noted for the person; the run carries on through the rest of the form | asked at the end; "Needs you" if it is still empty at Ready to send |
| a page that will not move on without a left question (Workday blocks Next) | everything else on that page is filled and Next tried first; then the batch is asked on that page | as above |

- **The shell** (`shell-electron/agent.js`). On `needs-answers` the open questions go through the
  ask route's known-only check with `fill_through` (the person's material, then
  `asks.neutral_option`); what is still open is noted on the run (`ans.deferred`, which rides on
  the Review answers record and so carries over a park) and the brain is told to leave it and carry
  on (a nudge, and the step route marks the `<left>` line "(left for the person: leave it empty)",
  brain rule 15). The page check before Next never holds a click for a left question. The brain
  saying `needs-answers` again for questions already left: when the page's own error names one,
  or the second time on that page, the batch is asked there. At `ready_to_submit`, after the
  deterministic gate: any required question still open joins the list, a left question the page
  now shows filled drops out, the known-only check runs once more (a saved answer that arrived from
  setup or the library while the run went on is used, with no ask), and ALL the rest is asked as
  ONE ask (`batch`). The answers come back as placeholders, the brain fills them, and the next
  `ready_to_submit` goes on to the form check, the critic and Ready to send.
- **The bar.** While questions wait: "Filling everything I can; I'll ask what only you know at the
  end". The ask: "I filled everything I could; these N need you." with every question as one
  numbered list, each with the page's choices and its own box (the one-question controls); an
  answered one folds to its answer, a link to change it. A batch run (Apply to all) never waits:
  the job parks once with the whole list, as before.
- **Telegram.** ONE message for the batch (docs/notify.md), answers saved to the library (asked
  once ever).
- **Never typed.** A stand-in typed on the brain's own word ("N/A", "Not provided", "TBD", a lone
  "In") is refused in the shell (`isFiller`, mirrored in `asks.is_filler`); the person's own saved
  answer of that kind ({{answer_N}}) still goes in.
- **Never invented.** No neutral pick on a fact question (`asks.is_fact_question`), and nothing is
  ever made up: a fact waits for the person.
- Tests: tests/test_agent_fill_through.py (tests/js/test_agent_fill_through.js: a form with 10 known
  fields and 3 unknown required questions fills 10, picks the neutral one, leaves two, reaches the
  Submit page and asks once; a Workday page that blocks Next fills everything else first; a saved
  answer that arrives meanwhile means no ask; no stand-in typed; Review answers and the notice).

## 26. Three apply modes: Auto, Assisted, Hybrid (2026-11-17)

(2026-11-19) Auto no longer sets an application aside for a question with no saved answer: it
answers with a flagged best guess and goes on. Section 31 replaces what this section and section
29 say about that.

ONE saved choice (`_apply_mode` in ui/app.py, the small file `apply_mode` beside the old switch's;
the rule is `autoapply/modes.py`), made in Apply setup step 6 ("How to apply") and on the
Auto-apply page's Today panel. An install from before the modes has none saved, so the old
Autonomous submit switch decides it: on is Auto, off is Assisted. `_autonomous_on()` is now "the
mode is Auto", so every existing gate (the submit driver, the alerts' Apply for me, the Home agent)
reads the same choice.

| | Auto ("I'm away") | Assisted ("I'm here") | Hybrid ("I'm around") |
|---|---|---|---|
| Where it runs | in the background: the pane is kept out of view, no Browser tab, no bar | in the Browser tab | in the Browser tab |
| What starts it | the app, on a timer, through the approved queue (Waiting on you) up to `daily_cap` a day, each application once a day | the person (Assisted apply, Assisted apply to all) | the person |
| Questions only the person knows | never asked: the job parks (Needs you) and the daily summary counts it | the guided ask on the page (section 27) | the guided ask, and the same questions on Telegram |
| Who presses Submit | the agent, ONLY on the verified allowlist (`submit/allowlist.py`, one system today) and, with a threshold set, only at that match or better; everything else is filled and left at Ready to send | the person (Send) | the person (Send, in the app) |
| Telegram | "Sent: Role at Company" per application it sent, and ONE daily summary | the Ready to send notice only; no asks, no digest | asks, the digest, the Ready to send notice |

- **Nothing here widens the AUTO lane.** `modes.may_send_alone` only narrows what the allowlist
  and the person's opt-in allow. The shell still asks `/api/submit/decision` with the page that is
  open before any Submit click; `/api/autofill/applied` believes `by: "auto"` only when the mode is
  Auto AND that decision says `click_submit`, and then counts it against the daily limit.
- **Auto's queue** (`POST /api/autoapply/auto/plan`): approved applications still to be filled,
  oldest approval first, at most what is left of `daily_cap` today. To review is NOT taken: a
  tailored resume still needs the person's approval (in the app, or Approve all on Telegram).
  The page asks for it every 3 minutes (`aaAutoTick` in app.js) and hands the jobs to the shell as
  a QUIET batch (`agentBatch(jobs, {quiet: true})`), never while the person runs something or has
  a page open in the Browser tab.
- **Out of view** (shell-electron/main.js `setPaneQuiet`): the pane keeps a real size (1280 by
  900, so sites lay out and real clicks land) parked past the window's left edge, the page is told
  the pane is closed (so the right panel never switches to the Browser tab), nothing takes the
  focus, and every progress line is marked `quiet` (the page shows no bar for it). When the batch
  ends the hidden page is closed. If the person opens a page themselves meanwhile, the run stops
  and the browser is theirs.
- **What the person hears in Auto** (`notify/daily.py`, docs/notify.md "Auto mode"): one "Sent"
  notice per application the agent sent, and one daily summary, "{n} sent for you, {m} ready for
  your Send, {k} need an answer", with **Send all** in the app.
- **Send all** (`agent.sendAll`, the summary line on Auto-apply): each application at Ready to
  send goes through `agent.send`, the same path as the bar's Send (the person's own click, so it
  is allowed on every site), one at a time, in the Browser tab. A captcha stops the list there;
  nothing is solved or stepped around. Not offered from Telegram: nothing in notify/ approves an
  assisted submission from Telegram ("Approve all" only marks packages approved and submits
  allowlisted ones), so Telegram says what is ready and Send stays in the app.
- **The employer's confirmation mail.** When the inbox ties an acknowledgement to a job
  (inbox/matching.py), `_attach_confirmation` keeps its subject, sender, date and first lines on
  that job's application; the Submitted list, the drawer and the Tracker card say "Confirmation
  received".
- Honest copy: the mode's one line names how many systems Auto can send on by itself today, from
  the allowlist, and says that everything else waits for Send all. Since that is one system, Send
  all is the main path: the summary line shows from the first ready application in Auto.
- Tests: tests/test_apply_modes.py, tests/js/test_apply_modes.js.

## 27. The guided ask: answer on the page, one outlined field at a time (2026-11-17)

Replaces the question list on the bar (section 25's "The bar"). The owner: the list took over the
top of the Browser pane; and on Phoenix Contact a required select whose ONLY option is "Yes", a
typed "No" and "I still could not tell" looped for ever while an answered sibling was never filled.

- **The bar is ONE slim line**: "{n} fields need you. {i} of {n}", **Next**, **Skip this job**.
  No question text, no answer box.
- **The field is outlined on the page** (`guide` in shell-electron/agent.js; `HL_ON` scrolls it to
  the middle and outlines it, `HL_OFF` puts the page's own styles back). The person answers with
  the site's own control and its real options.
- **The shell watches that field** in the view. A choice counts at once; typed text counts once
  the box lost the keyboard (or stopped changing), so half a word is never taken. Then the outline
  goes, the answer is handed to the engine (`/api/autoapply/agent/answer`, `exact`, `on_page`),
  and the next open field is outlined. The engine saves the answers to the answer library when
  the ask closes, as before (asked once ever; self-identification answers follow the same local
  rules as any answer).
- **Next** passes the field without an answer (`action: "leave"`, `standing: false`): it stays
  empty, is flagged "Needs you: the form will not submit without it" in Review answers, and is
  never asked again in this run. **Skip this job** stops the run.
- **Hybrid**: the same questions are on Telegram with the page's choices. An answer from there is
  filled into the form by the shell (a real pick for a choice, typed for text) and counts the
  same; whichever comes first wins. **Assisted**: nothing on Telegram. **Auto**: never asked.
- **Another page.** A field that is not on the page in view is outlined as soon as the person is
  on its page; the shell does not navigate back by itself (see the PR's open points).
- **A required consent whose only choice is yes** (`consents.is_forced`): Yes, on the page or on
  Telegram, saves the standing Yes (`@forced_consent`); "No" typed to it leaves it empty, flags
  it and saves the standing No, after which such a question is never asked (`leave` in the ask
  route's known-only reply): the run leaves it empty and flags it, and an Auto run sets the job
  aside (Needs you) instead of offering a form that will not submit.
- **No dead ends.** A reply that is none of a closed question's choices is told "This form only
  offers: Yes." with the ways on (a choice, leave it empty, skip this job), and nothing loops. An answer already given
  travels to the shell at once (`partial` on a waiting ask).
- Tests: tests/test_agent_batch_exits.py, tests/js/test_agent_guide.js.

## 28. Common-sense screeners: at least 18, applied before, Yes / No acknowledgements, relatives (2026-11-17)

A Workday "Application Questions" page stopped a filled application with "5 fields need you": five
required Yes / No selects, four of which needed no person. The standing rule is "apply common sense,
never stop to ask simple questions". Four category rules, on the question's words (never one site's
markup), in the planner (`plan_known`) and in the ask route's known check (`asks.known_answer`),
before the grounded call, which never sees these questions (`grounded.eligible`); the label mapper
never sees them either (`mapper.rule_known`). The pure half is `autoapply/commonsense.py`; the
stores are read in ui/app.py (`_common_sense`, `_earlier_applications`).

| Question | Answered from | Review answers | Stays a question when |
|---|---|---|---|
| **At least 18 / legal working age** ("Are you 18 years of age or older?", "Are you over 18?", "Are you 18+?", "Are you of legal age to work?") | the saved answer (`@over_18`) first; else **Yes** when the saved profile shows it (`adult_proof`): a higher-education entry (a bachelor's, master's, MBA, doctorate, associate degree, a university or college diploma; by the degree's words, never the school's name alone) that is finished or was started at least a year ago, or a job or school date at least 4 years before today (a date with no month counts as December) | "Saved answer", or "Your resume" (origin `history`) | a high-school-only profile with a recent date, a first job under 4 years ago and no degree, no dates; the inverse or another age ("under 18", "between 18 and 21", "21 or older", a work permit). Never a No, never a guess |
| **Applied to / interviewed with this company before** ("Have you previously applied for employment with Acme or any of its predecessor firms?") | the person's own answer to that wording first; else their records: **No** when no earlier application to the company exists, **Yes** when one does. Earlier applications are saved applications marked applied or further, and Tracker cards that were applied to; the company name is normalised (`facts.same_company`), and the application being filled and its Tracker card are left out | "Your tracker" (origin `tracker`) | the records could not be read; a Yes to a question about a time window ("in the last 12 months") or this very position; the question names another employer, a relative, a visa, a clearance. "Applied to or worked for" needs both verdicts: Yes when either says Yes, No only when both say No (a worked-here Yes read from the resume still waits for its one tap) |
| **An acknowledgement worded as a Yes / No choice** ("Please select "Yes" to acknowledge that you have received and understand this requirement", "Please be aware that ..." with Yes and No, "I understand ...", "I acknowledge ...") | the affirmative choice, under the same standing consent as the required checkboxes (`accept_required_terms` "always") | "Your consent to required notices" (origin `consent`) | the preference is off; the text asks something ("Are you aware of any conflicts of interest?", "... Are you willing to travel?"; only "Do you understand / agree?" may follow a notice); a marketing or SMS opt-in (its own rule: declined, or a forced consent); a covenant, a conviction or any other disclosure (`_DISCLOSE`); a question a saved key answers; a statement the page view cut at 80 characters (its end is not known) |
| **Relatives employed by the company** ("Are any of your relatives presently employed by Acme?") | only a standing **No** the person saved in Apply setup (`@relatives_at_employer`): then No on every form | "Saved answer" (origin `library`; "Your answer" when it went in through the ask route, like every saved answer there) | nothing saved, or "Yes, ask me each time"; a question about a government or public official, a competitor, a client, a referral. Never a default |

- **Apply setup, step 4 (Form questions)** asks once: "Do any of your relatives work at companies you
  apply to?" with **No** and **Yes, ask me each time**; unanswered by default. "Ask" is saved as Yes
  (some do, so each form's question is the person's). The key is saved by the setup's own wording
  only, so an answer to one company's question never becomes the standing answer.
- **A long notice is read whole.** The ask route keeps a question's first 300 characters as its
  name; the standing rules for opt-ins and acknowledgements now read up to 1,500 (`full`), so a
  "Please select Yes to acknowledge" that closes a long paragraph is seen.
- **The model never answers an acknowledgement**: `planner.is_fact_question` is true for one, so
  with the preference off it is left for the person, never handed to the fill model.
- **Review answers** keeps the origin of an answer the ask route worked out by rule (`history`,
  `tracker`, `consent`; `noteOrigins` in shell-electron/agent.js), so the list and the Telegram
  summary name the source instead of "Your answer".
- Company names: "LLP", "PLLC" and "LP" are dropped like "Inc" and "LLC" when a question is matched
  to the company (`planner._CO_SUFFIX`), also for the worked-here rule.
- Nothing here presses Submit or changes who may: section 6 and section 26 are untouched.
- Tests: tests/test_agent_common_sense.py.

## 29. The agent decides: hard limits, advice, the answer protocol (2026-11-18)

**Why.** A day of runs on real forms showed one pattern again and again: the brain had the right
idea and a hand-written rule overruled it, or a per-question rule did something silly. A sign-in
button called "Submit" (the brain typed the account and clicked it; `submit_guard` emptied its
actions and reported ready). "How did you hear about us?" (the page script refused the brain's
pick: "do not pick one for them, report needs-answers"). "Phone Device Type" (the planner read any
label with "phone" in it as the phone number). "Have you worked for <employer> before?", "What is
the highest level of education you have completed?", "List any aliases ... or indicate N/A": each
needed a rule of its own before the agent would answer, although the answer is on the resume the
agent itself built. The owner: treat applying "like an engineering problem an AI agent has to
fix"; "if any rule is not serving our purpose we can remove them as we progress, no rule is cast
in stone."

**The principle (step 1 of 4).**

1. **The agent decides.** The brain is given the person's own material and may answer a question
   itself.
2. **Hard limits stay hard stops**, in code, whatever the model says. There are eight (below).
   A hard stop always tells the brain which limit applied.
3. **Fast-path rules are advice.** A rule that answers a question without a model step stays,
   because it saves a step. It is never the only way that question gets answered, and nothing
   strips the brain's own action because a rule would have done otherwise.
4. **Rules that mostly block sensible actions, or exist only to patch another rule, are deleted.**

### The hard limits

| # | Limit | Where it is enforced |
|---|-------|----------------------|
| 1 | The application's final Submit is pressed only by the send paths: the person's Send, Send all, or the autonomous send on the verified allowlist with the mode and threshold checks. The button of a sign-in or create-account form is that form's own, whatever it is called. | `brain.submit_guard`, `brain.auth_form`, `brain.not_ready`; agent_view.js `doClick`; agent.js `act()`, `send`, `pressSubmit`; `modes.may_send_alone`; submit/allowlist.py |
| 2 | Captcha, bot wall, MFA, identity check: stop. Never solved, never evaded. No stealth. | agent.js blockers and `PARK_REASONS`; agent_view.js never enters a captcha frame; prompt rule 10 |
| 3 | Nothing on LinkedIn. | `submit.policy.fill_permitted`; `brain.step` (LinkedIn click); agent.js `socialRefused` |
| 4 | Consents and terms: only what the person's saved consent settings allow. | autoapply/consents.py; agent.js `handleTerms`; agent_view.js `termsItems`; `qclass.is_legal` (an acknowledgement or opt-in is never the brain's to tick) |
| 5 | Facts about the person are never invented when they are knowable: a question the saved answers or the person's material (profile, resume, posting) answer is answered from those, and a legal or attestation question from the saved answer for exactly that question. When nothing is saved: Assisted and Hybrid ASK the person. Auto never stops to ask (section 31): it answers with a flagged best guess, and an application holding a guess on a legal question is held at Ready to send for the person's own Send. Never guessed, in any mode: a government ID number or SSN, a date of birth, bank or payment details, a password or code, a typed legal signature the consent settings do not allow. | `qclass.is_legal`, `qclass.never_guess`, `protocol.check` (`auto`), autoapply/guess.py; planner's work authorization, EEO, citizenship, consent and worked-here rules; agent.js `pickSaved`, `skipFor`; `modes.may_send_alone` |
| 6 | Credentials only through placeholders, never shown to the model or logged. Files: only this application's resume and cover letter. | `brain.resolve_actions`, `brain.redact`, `mask_values`; agent.js `upload`, `safeReply` |
| 7 | Payment, purchases, account deletion, anything outside applying for this job: never. | `qclass._LEGAL` (payment and bank words are legal class, so never the brain's); the allowed-hosts rule |
| 8 | The run stays on the employer's application flow. | agent.js `onSite` / `allowedHosts` (`left-site`) |

### What the brain is shown: `<person>`

autoapply/material.py builds one compact block from the application's tailored resume (else the
saved profile) and the answer library, reusing autoapply/grounded.py `build_facts` (the same facts
the grounded call already sent to the model): where the person lives, each job (title,
organization, dates, current or not), each school (degree, field, dates), years since the first
job, skills, languages, the links they saved (as placeholder names, never the address), and their
saved answers (question and answer). Each line has an id (`job1`, `edu1`, `ans3`).

Never in it: a self-identification or work authorization answer, a saved answer to any other
legal-class question (it fills its own question as a placeholder), a drafted answer, a password,
a code, and the contact details (name, email, phone, street), which stay placeholders.

It is sent on every step that has open questions (`<left>` or what was left for the person), as
the SECOND system block. The rules are the first. Both blocks are marked for prompt caching
(`llm.agent_step_blocks`; the broker passes `cache_control` through). Before this change the
step call sent its system prompt as a plain string, so nothing was cached, although section 5
said it was. The block is the same text on every step of one application, which is what lets
the cache hold. Size, with a realistic made-up profile (three jobs, two schools, nine skills,
two languages, one saved answer): about 190 tokens for the block (230 with its lead line); the
rules grew from about 2,490 to about 2,905 tokens. The cap is 4,200 characters (about 1,050
tokens) for a long profile.

### The answer protocol (autoapply/protocol.py)

(Section 30 made the FORMAT forgiving after the first real run: the fields may be missing or sit
in a `proof` object, and the engine attaches the evidence itself when the answer is in the block.
It also added the source `posting` and the common-sense defaults. What is checked is unchanged.)

An action that answers a question with the brain's own choice (a `type` of text that is not a
placeholder, a `select`, a click on a radio, a checkbox or an option) carries three fields:

```json
{"op": "select", "index": 5, "option": "Master's Degree",
 "source": "resume", "evidence": "edu1: Master of Science, 2018", "class": "factual"}
```

- `source`: `profile`, `resume`, `saved_answer` or `common_sense`.
- `evidence`: the short fact used ("edu1: MBA, 2019", "no employer named Acme in <person>").
- `class`: `factual`, `preference`, `common_sense` or `legal`.

The engine checks it before the action runs (`brain.check_answers` -> `protocol.check`):

1. **The question decides, not the brain's label.** `qclass.question_class` (ONE function,
   autoapply/qclass.py) reads the field's own label. A legal question is refused whatever class
   the brain claimed: only the person's saved answer fills it, and that travels as a placeholder
   (`{{answer_N}}`) the brain cannot read. "When unsure, it is legal": the legal patterns are
   the union of every pattern the engine already held (grounded.py, eeo.py, workauth.py,
   consents.py, commonsense.py, planner.py).
2. **Worked here before.** With the employer in the work history it is legal (a Yes needs the
   person's confirmation, as before). With the employer not in it, the history answers No.
3. **Factual and preference answers cite something that is in the block.** A cited id must
   exist; otherwise the evidence's own words (an employer, a degree, a year) must be found in
   the block, and every year it gives must be there. A typed answer must itself be made of the
   material's words. An answer that says "nothing like that" may cite an absence ("no PMP in
   skills"), and then the thing named must really be absent, and the answer may not be a Yes.
4. **Common sense** is for a question with no fact about the person: how they heard about the
   job, the kind of phone, a follow-up that does not apply. Never on a factual, preference or
   legal question. "List any aliases ... or indicate N/A" is an attestation, so N/A there is not
   common sense: the Apply setup question (PR #304) stays the only source.
5. **Stand-ins.** "N/A" or "None" is an answer only where the question asks for it or does not
   apply; filler ("In", "TBD", "see resume") never is. A run of seven or more digits is never
   typed as free text (hard limit 6).
6. **A saved value on a list** (`select` with `{{answer_N}}`) is the strict pick, as before.

A refusal never strips in silence. The step reply carries `observations` (each refusal with its
reason, a hard limit named) and `rejected` (`{index, label, hard}`). The shell feeds the
observations to the brain's next step and counts the refusals per question: the second time (the
first, for a hard limit) the brain is told to report needs-answers; the third time the shell
reports it itself. From there the question takes the usual road (the person's material, then left
for the person and asked ONCE at the end). In Auto nothing is asked: the job is set aside and
the reason names the question ("Set aside: no saved answer for ...").

An accepted answer keeps `proof` (`{source, origin, evidence, class}`). The shell records it in
Review answers with the origin label that already existed for that source ("Your profile", "Your
resume", "Saved answer", "Common sense"), flags it as one to check ("The agent answered this
itself, from: ... Check it.", five locales), says so in the Telegram list, and posts the rows to
`/api/autoapply/agent/ready` (`agent_answers`), which keeps them on the application's record.
The debug log never holds the answer, its evidence or an observation.

### Choice fields

The page script's refusal "no choice on this list says the same as the person's answer; do not
pick one for them, report needs-answers" is gone. The strict match stays for the saved answer's
own pick (the fast path, with no model looking). When no choice says the same, the list's options
go back to the brain in the observation, and the brain picks one itself under the protocol or
reports needs-answers. An option the brain names itself is applied as asked.

### Fast-path rules are advice

- `<left>` words them as advice. The person's own saved answer: `Question (saved answer: type
  {{answer_1}})`. A rule's answer (common sense, the resume, the tracker): `Question (the engine
  suggests Job board: type {{answer_1}})`, in words, so the brain may take it or do better.
- The four per-question rules of PR #304 (how-did-you-hear's default, highest education,
  worked-here No from the history, phone type) can each be switched off:
  `SJ_FASTPATH_OFF="education,worked_here"` (`commonsense.fast_path`). With one off, the engine
  leaves the question open, the brain answers it from `<person>`, and the answer passes the
  check: tests/test_agent_first.py proves it for highest education and worked-here.

### The inventory: every place that overrides, strips, refuses or second-guesses

HARD = one of the eight limits. ADVICE = a fast path or a hint the brain can go round. DELETE =
removed by this change. "Was" is what the rule did before.

**The brain's prompt (autoapply/brain.py `SYSTEM_PROMPT`)**

| Rule | What it forbade or forced | Why it was added | Class | Now |
|------|---------------------------|------------------|-------|-----|
| 5 | Clicking Submit, Send application, Review and submit | section 6: the model never presses Submit | HARD 1 | kept, with the sign-in form exception |
| 2 | LinkedIn; another social sign-in while an email path exists | CLAUDE.md section 7; the agent holds no account for those | HARD 3, HARD 6 | kept |
| 6 (old) | "Never answer a question about the person" | the first data boundary: the model saw nothing about the person, so anything it wrote was a guess | DELETE | replaced by the answer protocol |
| 7 (old) | "You may type ONLY a placeholder" | the same boundary | DELETE | narrowed to contact details and secrets (HARD 6) |
| 2 (chat) | "a question about the person with no placeholder is needs-answers" | the same boundary, for chat-style applies | DELETE | the protocol applies there too |
| 15 (old) | "if none says the same, clear the box and report needs-answers" | Phoenix Contact, 2026-11-05: "College Job Board" picked for "Job board" | DELETE | the options come back and the brain picks under the protocol |
| 15 (old) | "Only a REQUIRED question with no placeholder is the person's: do not guess" | the same boundary | DELETE | answered from `<person>` when it answers; needs-answers when nothing does |
| 15 | "(left for the person: leave it empty)", never a stand-in | fill-through, 2026-11-17: "In", "Not provided", "N/A" reached forms | ADVICE | left empty unless `<person>` answers it; a stand-in is still refused (protocol rule 5) |
| 10 | Captcha, phone code: stop | CLAUDE.md section 7 | HARD 2 | kept |
| 18 | Privacy notices and terms are never ticked or accepted by the brain; marketing boxes stay unticked | Lam Research, 2026-10-30 | HARD 4 | kept |
| 3, 4, 8, 9, 11, 19 | Never press Enter; one page-changing action last; never repeat a failed action; ignore the site's search box; not-a-job; no more than two plain waits | lessons from real runs | ADVICE | unchanged: they are how to work, and nothing strips an action for them |
| 21 (new) | The eight limits, stated once | this change | HARD | new |

**The brain's code (autoapply/brain.py)**

| Where | What it blocked | Why it was added | Class | Now |
|-------|-----------------|------------------|-------|-----|
| `resolve_actions`: `is_fact_question` | Free text on any field whose label matched the planner's fact pattern was dropped, with only a note | the fact gate of section 5 | DELETE | gone; `protocol.check` decides and says why |
| `resolve_actions`: `_DIGIT_RUN` | Free text with four digits in a row was dropped (a year too) | "a phone, an SSN, a card: never smuggled as free text" | DELETE | replaced by seven or more digits, as hard limit 6, with the reason |
| `submit_guard` | A reply with a Submit click lost ALL its actions and became ready_to_submit | section 6 | HARD 1 | kept; only the Submit click is dropped, the other actions run, and the brain is told. Alone, the click still means ready |
| `auth_form` | The exception: a sign-in form's own button | a Workday tenant, 2026-11-18 | HARD 1 | kept |
| `not_ready` | ready_to_submit refused on a password page, a sign-in form, a page with no Submit | Gartner, 2026-11-02 | HARD 1 | kept; it already returned its reason |
| `step`: LinkedIn click | Dropped | CLAUDE.md section 7 | HARD 3 | kept; now also an observation |
| `step`: unrendered `{{...}}` | Dropped | 2026-11-16, belt and braces | HARD 6 | kept; now also an observation |
| `resolve_actions`: a placeholder with no value, or inside other text | Dropped | the data boundary | HARD 6 | kept; now also an observation |
| `step`: upload routing | A cover letter box never takes the resume | Gusto, 2026-11-16 | HARD 6 | kept; now also an observation |
| `parse_reply` | Unknown op or index dropped; five actions at most; done alone; one page-changing op, last | reply validation | ADVICE | kept; a dropped op or index is now told back |
| `step`: last step | A reply with no `done` on the last step becomes blocked | the step budget (section 15) | ADVICE | unchanged |
| `mask_self_id`, `mask_values`, `redact`, `clean_summary` | The model never reads a stored legal or self-identification choice or a contact value; the bar never shows one | docs/applicant-brain.md section 9 | HARD 5, HARD 6 | kept |
| `false_alarm` (critic) | Drops a critic line the run's own record contradicts | Upgrade, 2026-11-17 | ADVICE | unchanged |
| `check_answers` (new) | The answer protocol | this change | HARD 5 for legal; a check otherwise | new |

**The engine's routes (ui/app.py)**

| Where | What it blocked | Why it was added | Class | Now |
|-------|-----------------|------------------|-------|-----|
| step route: `fill_permitted` | No step on LinkedIn, Indeed, Glassdoor and the other prohibited boards | CLAUDE.md section 7 | HARD 3 | kept |
| `_known_answers_for`: self-identification | Never handed to the brain as a placeholder | Lam Research: a bare "No" is half an answer; the shell picks it | HARD 5 | kept |
| `_known_answers_for`: hold, withhold, confirm, `residence_withheld`, expiry | A saved answer is not used when two disagree, it expired, it is for another country, or the mapping is unconfirmed | docs/applicant-brain.md phases 3 and 4 | HARD 5 | kept: these are legal-class answers |
| step route: the left-line mark | "(type {{answer_N}})" was the only way a list or text question got answered | 2026-10-31 | ADVICE | reworded: "saved answer" or "the engine suggests ..." |
| plan route: `planner.parse_plan` fact gate | In the model's fill plan, a literal on a question about the person and all free text are dropped | section 5: that pass sees no personal data | HARD 5 | kept. That model call is not given `<person>`, so what it wrote would be invented. Candidate for step 2 |
| ask route: `grounded.eligible`, `grounded.verify` | The grounded call never sees legal, consent, essay or common-sense questions; an answer whose fact does not state it is dropped ("a No about history is never proven") | 2026-11-17 | ADVICE | unchanged; it is now the second chance after the brain. Stricter than the protocol on purpose |
| ask route: `asks.neutral_option`, `asks.is_fact_question` | The neutral choice is never taken on a fact question | fill-through, 2026-11-17 | ADVICE | unchanged (the engine's own pick, no model looking) |
| ask route: `_forced_declined` | A required consent with no way to decline stays empty under a standing No | Phoenix Contact, 2026-11-17 | HARD 4 | kept |

**The planner and its rule modules (the fast path)**

| Where | What it does | Why it was added | Class | Now |
|-------|--------------|------------------|-------|-----|
| `planner.is_fact_question` (`_FACT`) | A question about the person nothing answers goes to "left", never to the model | the fact gate | ADVICE | no longer consulted for the brain; "left" is now what the brain reads and answers |
| `planner.contact_key`: phone type exclusion; `_NOT_ME`; autofill_core.js `PHONE_KIND_RE`, `NAME_VETO` | A label that only looks like a contact field is not filled as one | PR #304 (phone type); references and managers | ADVICE | kept: the fast path must not do something silly |
| `planner.match_option(strict)`, `adds_qualifier`, `_NEUTRAL`; autofill_core.js `answerScorer` | A saved answer is matched to a list only when a choice says the same | Phoenix Contact, 2026-11-05 | ADVICE | kept for the fast path only; a miss goes to the brain with the options |
| `planner.generic_sources`; autofill_core.js `heardChoices`, `pickHeard`; agent_view.js `doSelect` | How-did-you-hear lands on the choice that names no source | PR #304 | ADVICE | kept as a fast path; no longer the only way |
| `commonsense`: heard default, `education_answer`, `phone_type_answer`; worked-here No from the history | Answers with no model step | PR #304; 2026-11-17 | ADVICE | kept, each switchable (`SJ_FASTPATH_OFF`) |
| `commonsense.applied_answer` | "Applied here before?" from the tracker | 2026-11-17 | ADVICE | unchanged |
| `commonsense.adult_proof` | "At least 18?" Yes from a degree or a date four years back | 2026-11-17 | HARD 5 (see the note below) | unchanged |
| `commonsense`: relatives, other names, connections list | Only from the standing answers of Apply setup | 2026-11-17, PR #304 | HARD 5 | kept |
| `planner._eeo_fill`, `eeo_option`, `listed_citizenship`, `citizenship_text`, workauth and expiry | Self-identification, citizenship and work authorization only from the person's saved answers, their standing decline, or the page's decline | CLAUDE.md, docs/applicant-brain.md | HARD 5 | kept |
| `planner` 1b; `applicant/facts.worked_here` | A worked-here Yes read from the resume waits for the person's one tap | phase 4 | HARD 5 | kept |
| `consents.standing_answer` | Opt-ins declined, acknowledgements under the standing consent, a forced consent the person's | Phoenix Contact, 2026-11-17 | HARD 4 | kept |
| `answers.is_per_job`, `answers.unusable` | An answer saved for one job is not reused for another | answer library hygiene | ADVICE | unchanged |

**The page script (extension/agent_view.js)**

| Where | What it blocked | Why it was added | Class | Now |
|-------|-----------------|------------------|-------|-----|
| `doSelect`, saved answer | "no choice on this list says the same as the person's answer; do not pick one for them, report needs-answers" | Phoenix Contact, 2026-11-05 | DELETE | the options go back to the brain |
| `doClick`: `submitLike`, `authSubmit` | "submit-guard" | section 6 | HARD 1 | kept |
| `doClick`: `policyLink` | A link to a privacy notice or terms is never followed | Lam Research, 2026-10-30 | HARD 4, HARD 8 | kept: the link usually leaves the employer's flow, which ends the run. See "what remains" |
| `doClick`, `doType`, `doSelect`: "use select", "use upload", "use click", "disabled", "covered by ..." | The wrong tool for the control | mechanics | ADVICE | unchanged: each already comes back as an observation |
| `view()`: honeypot fields, captcha frames, self-identification choices | Never listed, never entered, never shown | Barclays' robot-only box; CLAUDE.md | HARD 2, HARD 5 | kept |
| `termsItems` | A marketing box never counts as a term to accept | section 8 | HARD 4 | kept |

**The fill engine (extension/autofill_core.js), where no model is looking**

| Where | What it leaves | Why | Class | Now |
|-------|----------------|-----|-------|-----|
| Whole module | Never clicks Submit, never sets a file from script | CLAUDE.md section 7 | HARD 1, HARD 6 | kept |
| Screening rules | A question that matches both work authorization and sponsorship is left | an inverted answer | HARD 5 | kept |
| Site search, honeypot | Never filled | Amazon's job search got the person's city | ADVICE | kept |
| "never overwrite what you typed" | A filled box is left | assisted apply | ADVICE | kept |
| Strict saved-answer matching, optional lists by closest match, `leftover` (required only) | What is not matched is left, and reaches the brain in `<left>` | 2026-11-05, 2026-11-16 | ADVICE | kept |
| Captcha | Fills the form, reports the captcha, never touches it | CLAUDE.md section 7 | HARD 2 | kept |

**The shell (shell-electron/agent.js)**

| Where | What it blocked | Why it was added | Class | Now |
|-------|-----------------|------------------|-------|-----|
| `act()`: `FILLER_RE` | "refused: placeholder text" for N/A, None, "In" typed on the brain's own word | 2026-11-17 | DELETE | the protocol decides, and allows N/A where the question asks for it |
| `act()`: saved answer typed into a list | Turned into the strict pick; a miss ended in the refusal above | Phoenix Contact, 2026-11-05 | ADVICE | kept; a miss now carries the options |
| `act()`: unrendered placeholder | Refused | 2026-11-16 | HARD 6 | kept |
| `act()`: `SUBMIT_RE`, `authForm` | "submit-guard", and the nudge | section 6 | HARD 1 | kept |
| `act()`: `socialRefused` | LinkedIn always; another social sign-in while an email path exists | CLAUDE.md section 7 | HARD 3, HARD 6 | kept |
| `upload()` | The cover letter box takes only the cover letter | Gusto, 2026-11-16 | HARD 6 | kept |
| `pickSaved`, `fillSavedEeo` | Self-identification is put on the page by the shell, never through the model | Lam Research | HARD 5 | kept |
| `handleTerms`, needs-consent | The person's consent, never the model's click | section 8 | HARD 4 | kept |
| `leftEmpty`, `LEFT_NUDGE` | A question the person chose to leave empty is never filled | Phoenix Contact, 2026-11-17 | HARD 4 (the person's own choice) | kept |
| `onSite` | "left-site" | section 7 | HARD 8 | kept |
| Captcha, phone code blockers | Park | CLAUDE.md section 7 | HARD 2 | kept |
| `notReady`, the critic, the form check, the resend signature | ready_to_submit is checked before the hand-over | Gartner, 2026-11-02; section 14, 20 | HARD 1 | kept; each tells the brain why |
| Page check before Next | Holds the click once per page when required fields are empty | section 14 | ADVICE | unchanged: once, with the reason, then the brain decides |
| Forgot password hold | Held while no account exists on the site | 2026-11-18 | ADVICE | unchanged in this pass (it holds every time). See "what remains" |
| needs-answers with nothing open (three times parks), `MAX_WAITS`, the ladder, the step and cost budgets, the job cap | The shell takes over | sections 13, 15, 16 | ADVICE | unchanged: limits about progress, each told to the brain |
| `noteRejected` (new) | A refused answer counts toward asking or setting aside | this change | new | new |

**Note on hard limit 5 and "at least 18".** `commonsense.adult_proof` answers "Are you at least
18?" with Yes from a degree or a date four years back, when nothing is saved. Read strictly, hard
limit 5 (date of birth and age: only a saved answer) does not allow that. It is left as it was and
reported to the owner, not changed here.

### What remains (steps 2 to 4)

- The model's fill plan (`planner.build_fill_plan`, `parse_plan`) is not given `<person>`; its fact
  gate stays until it is.
- The grounded call (autoapply/grounded.py) and the protocol overlap. With runs on real forms
  showing the brain answers first, the grounded call can go.
- The policy-link refusal and the Forgot password hold still block every time. Turning them into
  "advise once, then allow" needs a real-form run, because both can end a run (leaving the site;
  a reset mail that can never come).
- The autonomous send (Auto, the allowlist) sends without the person reading Review answers. The
  agent's own answers are kept on the application; whether an application that holds one should
  wait at Ready to send is the owner's call.
- The rules of autoapply/commonsense.py stay as fast paths. Each can be retired with
  `SJ_FASTPATH_OFF` once real runs show the brain answers the question as well.

### Tests

tests/test_agent_first.py (the classifier, the block and its size, the validator, the brain's
step, the option-choice path, the proof with the fast-path rules off, the step route's advice
lines, the application's record, the locales) and tests/js/test_agent_first.js (the Review
answers record, counting refusals, a refused legal answer asked once at the end, the Auto set
aside that names the question, a stand-in the protocol passed). tests/test_agent_view.js covers
the page script's option hand-back.

## 30. After the first real run: format, hands, defaults, written answers (2026-11-18)

Section 29 was run on a real Greenhouse form with many custom questions (hidden, real model). It
was the right direction: the brain cited its facts and no rule vetoed a sensible action in
silence. The job still ended set aside with 7 questions after 23 steps, for four reasons.

### 1. A format slip never costs a step

Five steps were lost to the FORMAT of an answer (the fields missing, then moved). The check
(autoapply/protocol.py) is now hard to fail:

- The three fields are read from the action itself or from a `proof` object on it; "reason" is
  read as evidence, "job posting" as `posting`, and so on (`protocol.meta`).
- A missing `class` is the question's own class. It decided anyway.
- A missing `source` or `evidence` is filled in by the engine: it looks for the answer's own words
  in the block (`protocol.ground`) and attaches the fact it found ("edu1: matched by the engine").
  Evidence that names nothing is treated the same way.
- On a question common sense answers (how did you hear, the kind of phone) nothing at all is
  needed.
- Only an answer that is truly ungrounded, or legal, is refused, and the refusal shows one worked
  example. The prompt has the same example on its `select` line and says so in rule 6.

The "wrong index" the brain reported on step 1 was not an index problem. The view and `<left>`
never disagreed (`<left>` carries no indices). The engine had refused a typed School for its
format, and the brain misread the observation; on the next view the same field had another
index because the page had grown. With the lenient check that refusal no longer happens.

### 2. The brain's hands on a react-select

Seen on the real form: typing into the School typeahead answered "value differs" four times,
`list_options` read nothing from five closed comboboxes, and a click on an option "did not
select" three times. The shell sent real key and pointer events (CDP), which did nothing in a
pane out of sight and still said ok. Only `select` worked, because it used the engine.

Now every one of the agent's ops on a combobox goes through the engine's own machinery
(extension/autofill_core.js `agentPick`, `agentOptions`, `agentClickOption`: the same open,
filter, wait, press and read-back the fill uses, inside its own budget of 9 s):

- **`type` on a combobox or typeahead** is a pick. It opens the list, types so the list filters,
  waits for the results, presses the best match, reads the value back and closes. It answers
  `picked "X"`, or `no result for what was typed; options: a, b, c`. For a search with thousands
  of entries (schools) it tries the whole value, then its first three and two words, then its
  first distinctive word. When nothing matches, the brain's own type takes the list's "Other" or
  "Not listed" choice when there is one, and says so. A saved answer (`{{answer_N}}`) keeps its
  strict match and never takes "Other".
- **`list_options` on a closed combobox** opens it, reads every choice and closes it again
  (`TailorAgent.listOptions`).
- **A click on a `role=option`** is pressed the way the engine's pick presses it (mouseover, then
  pointer and mouse down, up and click on the option) and read back from its box
  (`pick_option`).
- **`select`** with the brain's own option uses the same pick.
- The real keys and pointer stay as the fallback for a box that opened no list at all to the
  page's own pick (Workday's prompt widgets), and for an option that belongs to no combobox the
  engine knows.
- What was picked goes to the brain; the debug log shows `(picked)`.

Tests through the agent's own ops (never the engine's fill) on tests/fixtures/forms/
react_select.html and greenhouse_live.html: tests/test_agent_view.js.

### 3. Common-sense defaults

Employment type, the offices and overtime were refused as "a preference without a source". The
owner's rule is common sense first. These are the defaults the brain may choose, each checked in
code (`protocol.default_answer`) and flagged in Review answers like every answer the agent chose:

| Question (`qclass.default_kind`) | The default | Evidence |
|---|---|---|
| Type of employment | The posting's own type; full-time when the posting says full-time or names none | `post`, or common sense |
| Which office or location | One the posting's location names; else one in the person's own city or state; else the first one listed | `post`, `loc`, or common sense |
| A routine condition of the job: overtime, days on site, travel, a shift or schedule | Yes, and only when the posting itself states that condition. A No, or a condition the posting does not state, is the person's | `req1`... |
| How did you hear, the kind of phone | As before | common sense |

"Nearest office" is not computed: with no office in the person's city or state, the default is
the first one listed.

Never a default, saved answer only: compensation and salary expectations, a reason for leaving or
a separation, a start date or notice period, relocation, a background check or drug screen
consent (the consent settings), anything in the legal class. Two fixes to the classifier came
with this: "Discipline" (a field of study) was read as a disciplinary question, and "able to
work weekends" as work authorization.

So that these choices have evidence, the block now carries the posting's key facts
(`material.posting_facts`): its title, the location it states, the employment type it names, and
up to six sentences that state a condition of the role. The posting is third-party text: hidden
text is taken out first (llm/textguard.py) and only those short lines reach the prompt. A new
source, `posting` (shown as "The job" in Review answers), is checked the same way a profile fact
is. With the made-up profile and posting of the tests the block is about 256 tokens (186
without the posting), and the rules are about 3,185.

### 4. Written answers

"Please tell us what you liked and disliked about your last role. What interests you most about
this opportunity?" was left for the person. Drafting from the person's real history is what this
product does, and the Review page's drafter already exists (drafting/drafter.py
`screening_answers`). The agent now uses it (autoapply/drafts.py, the step route's
`_agent_drafts`):

- **Which questions.** `qclass.is_essay`: an open-ended question on a text box (motivation,
  interest in the role, experience with something, a strength, a project). Never a legal or
  saved-only question, a follow-up that only applies to a Yes, the cover letter box (its own
  path) or an "anything else" box.
- **When.** On the step where the question is open and holds no draft: at most two new drafts a
  step, one try per question. The draft is saved on the application's screening answers
  (`by: "agent"`), so a later step or a re-run reuses it.
- **How the drafter is told** (`drafts.guidance`, added to the question it is given): plain first
  person, inside the limit, no bullets, no em or en dashes, every part of the question answered,
  only what the profile and the job show. For a question whose honest answer could hurt the
  person if guessed (what they disliked, a weakness, a conflict, a failure): neutral and
  professional, no person or employer spoken ill of, nothing that happened unless the profile
  shows it, said as what they would like more of in terms of what this role offers.
- **Length.** A limit the question states (characters, words, sentences), else about 600
  characters; never over 1,500.
- **Cleaning** (`drafts.clean`): dashes out, any sentence with a figure found in neither the
  profile nor the posting dropped (tailoring/fact_gate.py), cut at a sentence end inside the
  limit. Nothing usable left is no draft, and the question is the person's.
- **On the form.** It reaches the brain as `Question (drafted for the person from their resume
  and the posting: type {{answer_N}})` and is typed through the placeholder. Review answers
  shows it as "Drafted" with "Written for this job. Read it.", counted as one to check.
- **Never sent alone.** An application that holds a draft the person has not edited stops at
  Ready to send, also in Auto on the verified allowlist: `modes.may_send_alone(..., drafted=True)`
  is False, the board's `auto_send` says so, and the shell's hand-over checks its own Review
  answers rows.

### Tests

tests/test_agent_first.py (either shape of the fields, engine-attached evidence, the real run's
three reply shapes, each default and what is never one, the posting facts, which questions are
drafted, the limit, the guidance, the cleaning, the route's draft and the send-alone rule),
tests/js/test_agent_first.js (the shell's routing of a combobox type, an option click and
list_options to the page's own pick; a drafted answer held at Ready to send) and
tests/test_agent_view.js (five fixture tests of the agent's ops on react-select).


## 31. What Auto does when it does not know (2026-11-19)

The owner: "For auto apply, we can't stop to ask the user anything. A user asking for auto apply
to do the work should understand the risk based on our terms and conditions, but we will also
first try to apply to jobs that don't ask for questions that will require us to stop and ask, and
if it does require, we will use a common sense approach to pick the best option possible and
proceed with the application." Stopping to ask belongs to Assisted and Hybrid only.

Until now an Auto (quiet) run set the whole application aside for one question with no saved
answer ("Set aside: no saved answer for ..."), legal ones included. An Auto application is no
longer set aside because of a question.

### Every question gets the best available answer

In this order, and the run goes on to Ready to send (or to sending, where allowed):

1. the person's **saved answer** (Apply setup, the answer library, work authorization);
2. the person's **material** under the answer protocol (profile, resume, the posting; section 29);
3. a **common-sense default** (section 30);
4. a **best guess**, with its reason (autoapply/guess.py):
   - an explicit neutral choice the form offers ("Prefer not to say", "Decline to self-identify",
     "I do not wish to answer", "N/A" where the question invites it);
   - else the choice that is true for the great majority of applicants and commits the person
     to the least: No to relatives at the employer, a conflict of interest or a public office, a
     non-compete, a conviction or pending charge, a dismissal, having applied or worked there
     before; Yes to being at least 18 and to being able to do the job;
   - else the first choice that commits to nothing ("Other", "Not applicable").

Who guesses: on a question whose choices the engine can read, the engine does, in code
(`guess.choose`, through the ask route's known-only check with `auto_guess`); the shell puts it
on the page. Everything else is the brain's own best guess: every step of an Auto run tells it
so (`<mode>` in the state message, `brain.AUTO_MODE`; the cached system prompt is the same text
in every mode), and `protocol.check(auto=True)` takes an answer nothing supports as a best guess
instead of refusing it, with these limits (`guess.accept`): where the form offers a neutral
choice, the guess is that choice (or the majority answer); a typed guess is a few words, never a
number or a date about the person, never a long number; filler is still refused.

**Self-identification** (gender, race, ethnicity, veteran, disability) always takes the decline
choice unless a saved answer exists, as before. With no decline choice on the list it is never
guessed: the question is left, and the application goes over flagged "Needs you".

### What each class of question gets in Auto

| Class | With a saved answer or material | With nothing |
|---|---|---|
| Factual (employer, degree, dates, skills, where they live) | From the material, cited | Best guess from the choices; a typed number or date is never made up |
| Preference (employment type, office, a condition of the job) | Saved answer, or the section 30 default | Best guess |
| Common sense (how did you hear, phone type, a follow-up that does not apply) | As before | As before |
| Legal and attestation (non-compete, conviction, dismissal, relatives, clearance, salary, start date) | The saved answer only | Best guess, flagged, counted as `guessed_legal`, and never sent by the agent alone |
| Self-identification | The saved answer, or the standing decline | The form's decline choice; never guessed without one |
| Consents, acknowledgements, terms, marketing | The consent settings (autoapply/consents.py) | The consent settings: unchanged, never a guess |
| Never guessed (below) | The saved value | The application is skipped |
| Open-ended text | Drafted (section 30), held for the person's Send | The same |

### Recorded and flagged

Every best guess is recorded with `source: best_guess`, its reason and the question's class. In
Review answers it reads "Best guess" with "Best guess: <reason>. Check it." (five locales), and
it counts as one to check. The application keeps the counts (`agent_guessed`: `guessed`,
`guessed_legal`; the agent's ready route).

### Protections that do not stop the run

- **Never sent alone with a guessed legal answer.** `modes.may_send_alone(..., guessed_legal=True)`
  is False; the shell's hand-over checks its own Review answers rows too (and a required
  question still open). Such an application waits at Ready to send. Its row says "N answers
  need your check", the daily summary says how many answers need a check, and the line beside
  Send all says how many of them are best guesses on legal questions before the person presses
  it. The person's own Send works as before. **Send all** asks ONCE when any application it
  would send holds a guessed legal answer (ui/static/send_all.js, the outage card's frame): it
  names each one (role at company) with its count, and offers "Review them first" (sends
  nothing, opens the first one's Review answers), "Send the other N, keep these" (only the
  applications without one; the rest stay at Ready to send) and "Send all anyway". Closing it
  sends nothing. With no guessed legal answer there is no confirmation.
- **A saved work authorization the form words differently.** When the profile answers a work
  authorization question but none of the form's choices can be matched to it, the answer is
  still a best guess (class legal), and its flag reads "your work authorization is saved but
  this form words it differently" (`guess.reason_for`), so it explains itself.
- **Never guessed, in any mode** (`qclass.never_guess`): a government ID number, an SSN or
  national ID, a passport or licence number, a date of birth, bank, card or payment details, a
  password, a code or a security answer, and a typed signature. A wrong value there is not a
  judgement call. When such a field is REQUIRED and nothing is saved, the agent cannot do that
  application: in Auto it is skipped (`/api/autoapply/agent/skip`: the record moves to "Needs
  you" with the reason "The form requires something I never fill in for you" and the field
  named), the batch moves on, and the daily summary counts it as a form the agent could not do,
  not as a question. Signatures the consent settings allow keep their own path (section 18).
- **Consents and terms** keep following autoapply/consents.py.

One case still pauses an Auto run on a page: the form would not take any answer into a required
box after three rounds (a widget problem). The reason then says so and names the box; it is not
"no saved answer".

### Asking once, up front: Auto's checklist

So that guesses are rare, Apply setup holds the standard declarations Auto would otherwise guess,
each answered once and saved under its own key:

| Declaration | Where | Key |
|---|---|---|
| Authorized to work; visa sponsorship | Work authorization step | the work authorization profile |
| Other names used | Forms step | `@other_names` |
| Relatives at an employer | Forms step | `@relatives_at_employer` |
| Non-compete or other restrictive agreement | Forms step | `@restrictive_covenants` |
| Conviction or pending charge (new) | Forms step | `@criminal_record` |
| Ever dismissed or asked to resign (new) | Forms step | `@dismissed` |
| Excluded, suspended or sanctioned from a health care or government program (new) | Forms step | `@excluded_programs` |
| Background check and drug screen | Forms step, "Background checks and notices a site requires" | `accept_required_terms` (the consent setting) |
| Willing to relocate | Wants step | the work authorization profile |
| Notice period or earliest start date | Wants step | `@notice_period`, `@start_date` |
| Current salary; wanted salary | Forms and Wants steps | `@current_salary`, `@salary` |
| Security clearance | Forms step | `@security_clearance` |

Where Auto is switched on (the Auto-apply page and the setup's own step) the list is shown with
how many are still unanswered and the sentence: "Auto never stops to ask. Answer these once so
it never has to guess. Anything left blank, I answer with my best guess and flag for you." It
never blocks turning Auto on (`/api/autoapply/auto/checklist`, `setup.auto_checklist`). Under it,
in plain words: "In Auto I may answer a form's questions with my best judgement when you have
not saved an answer. Those answers are flagged, and you are responsible for checking them before
an application is sent."

### Preferring jobs that need no guess

`/api/autoapply/auto/plan` keeps its rules (approved, waiting, the daily limit, oldest approval
first) and then sorts by how likely an application needs a guess (`_form_score`, lowest first):

1. **The form's own questions, where a public feed lists them before the form is opened.** Only
   Greenhouse does: its keyless job board API (the feed sourcing/ats.py and sourcing/liveness.py
   already read) returns a job's questions with `?questions=true` (sourcing/formquestions.py).
   The required ones are counted against what the person's saved answers cover; contact fields,
   files, self-identification, essays, consents and common-sense questions are not unknowns.
   The count is kept on the record for a week; one plan call makes at most four feed requests.
   Lever's and Ashby's public feeds do not list a form's custom questions, and nothing is
   scraped.
2. **What earlier runs met**, for everything else: per employer, else per form system, the
   average number of best guesses a run and how often a form had to be skipped
   (`form_history` in the prefs store: counts only, never a question or an answer).
3. Nothing known: 0.5, after the forms known to be clean and before the ones known to need
   guesses.

### Assisted and Hybrid

Unchanged: they ask. `auto` is false on their steps, the check refuses a legal answer with no
saved answer (hard limit 5), nothing is guessed, and the question is asked once at the end.

### The first real form: Ochsner on Workday (2026-11-19)

A hidden Auto run reached Application Questions and was set aside for three questions. Two were
bugs:

- **"Are you legally authorized to work in the United States without requiring visa sponsorship
  (e.g., H-1B visa status, etc.) from an employer now or in the future?"** The classifier read it
  correctly (`authorized_no_sponsor`), but its choices were not Yes and No. They were three
  statements: "Yes, I am legally authorized ... (I do not and will not need visa sponsorship)", "I
  currently need a visa sponsorship ...", "In the future, I will need visa sponsorship ...". Every
  one read as a yes, so nothing was picked. `workauth.pick_sponsor_state` now reads each choice as
  none, now or future, and picks the person's own. "Can you work ... without requiring
  sponsorship" is now read as this question too (it was read as "do you need sponsorship", the
  inverse).
- **"Have you previously been employed by Ochsner or any of companies, partners, or affiliates?"**
  and **"Are you currently employed by any of the following Ochsner companies, partners, or
  affiliates? Companies Include: ..."** The worked-here matcher read "partners" as someone else (a
  domestic partner). A lone "partner" still is, "companies, partners, or affiliates" is the
  employer's own group. "Currently employed by" is answered No when the employer is nowhere in the
  history; a Yes from the history is never given to it (it may be a past job).

The third, **"Have you ever been suspended, sanctioned, excluded, or otherwise precluded from
participating in Medicare, Medicaid, or any other federal, state or private health insurance
program?"**, is a legal declaration with no saved answer: in Auto it is a flagged best guess (No:
most applicants were never excluded), held for the person's Send, and it is now one of the setup
checklist's declarations (`@excluded_programs`).

### Tests

tests/test_auto_never_stops.py and tests/js/test_auto_never_stops.js.

## 32. The hidden Auto run of 2026-11-19: eight applications, five fixes

### Left-site: where it went, aggregators, hand-offs, surveys

Three of eight runs stopped as "left-site", and the host they went to was kept nowhere.

- **Where it went is kept.** The stop logs `left-site step N <from host> -> <host>` and posts
  `/api/autoapply/agent/left-site` (`agent_left` on the record: the host, the step, the host it
  came from, never the address with its tokens). The Auto-apply row says "Stopped: the site sent
  me to <host>" (five locales). A new run clears it.
- **An aggregator's ad link** (Adzuna's `adzuna.com/land/ad/...`, and the other aggregators the
  sourcing code reads: Remotive, Arbeitnow, RemoteOK, freehire; `submit.policy.AGGREGATOR_HOSTS`)
  only redirects. The first page that is not the aggregator becomes the run's own site
  (`offSiteVerdict` "adopt": the hosts the engine allows for that page are added), when the agent
  made no click on the aggregator, or one click on the ad's own Apply link. Once per run, and
  never a prohibited site.
- **A careers site's hand-off to its hiring system.** Gainwell's careers site runs on SAP
  SuccessFactors and applies on `career41.sapsf.com`, which was not on the list. `ATS_HOSTS`
  (submit/policy.py, mirrored in agent.js) now holds the hiring systems' own domains: SAP
  SuccessFactors (sapsf.com, sapsf.eu, successfactors.eu), Workday's career sites, Jibe, Phenom,
  Avature, Eightfold, BrassRing, Cornerstone, Dayforce, Paycom, UKG and the small ATSs. Oracle
  Recruiting Cloud is allowed by its shape (`<pod>.fa.<region>.oraclecloud.com`,
  `ATS_HOST_PATTERNS`), never all of oraclecloud.com.
- **A third-party survey is the person's.** A bank's Workday requires an assessment that opens
  EY's tax-credit survey (`wotcgs.ey.com`). A survey host (`SURVEY_HOSTS`: tax-credit screeners
  and assessment vendors), or any host reached by a click named like an assessment or survey,
  parks the job as a person step: "This employer needs a short survey on another site (<host>).
  Do it yourself, then press Continue and I will finish." Continue opens the application's own
  page (the last allowed page), never the survey; a resumed run that finds itself still on the
  survey goes back to that page once. In Auto the job is set aside with that reason and the batch
  moves on.
- Unchanged: a run never wanders to an unrelated site, and never to LinkedIn or another
  prohibited board.

### Workday's Languages rows

Home Depot's My Experience had a Languages row open (Language, Read, Speak, Write and "I am fluent
in this language"); nothing filled it, its fields became required, and Save and Continue held for
48 steps. The fill engine now handles the rows (`fillLanguages` in extension/autofill_core.js,
called from the history pass): the profile's languages (`autoapply/history.form_languages`, in
the fill payload as `languages: [{name, level}]`, this application's profile first) go one per
row, "Add" for more; each proficiency list takes the closest level it offers ("Native" takes
"Native or bilingual", else "Fluent", else "Advanced"), and native or fluent ticks the fluent box.
A row nothing fills is removed with its own Delete. A language or its level is never a best guess
(`guess.is_language_question`).

Common sense on a required row (2026-11-19, Home Depot): with NO languages saved, a first
Language row that is required and has no Delete of its own, and a resume written in English
(`history.resume_in_english`, the app's `language_default` in the payload), the row takes
English at the highest level the list offers that is not native (the fluent box is left alone).
Each fill is logged as common sense with its reason ("your resume and application are in English,
and no languages are saved") and flagged as one to check in Review answers. Never another
language, never another row. Only a Language control with
proficiency lists or the fluent box beside it counts, never a site's own language picker.

### The export-control question

Lam's form (Phenom) asks "For the sole purpose of determining export licensing requirements,
please provide your most recent country/region of citizenship or legal permanent residence. If
your country/region is not listed please select Other." Its list holds only the controlled
countries and Other. The saved country of citizenship was not on it, so nothing matched, the
brain's own "Other" was refused (a legal question), and the job was set aside for a question the
saved answers settle.

`planner.export_question` (a country-of-citizenship question with export words: export
licensing or control, deemed export, ITAR, EAR, the Export Administration Regulations) and
`planner.export_answer` answer it from the saved countries of citizenship and, when it asks about
a permanent residence and the person holds one (a green card, settled status), that residence: it
is the more recent of the two. On a list: the country when the list names it, else the list's own
"Other" or "Not listed" (only on a list read in full); in a text box the countries, and a
residence as "<country> (permanent resident)". It stays legal class, answered from the saved
answer (origin "library"), through the planner and the ask's known-only check. A permanent
resident's answer was left to the person before; it is now answered this way. More wordings
reach the citizenship key ("Under the Export Administration Regulations (EAR), please state your
citizenship", "What is your citizenship for export control purposes?"); a citizenship STATUS or a
yes/no question keeps its own.

### An upload box is never a question

On Schneider Electric's iCIMS profile the agent attached the resume, saved the profile, and the
page came back from its resume parse with the upload box empty again ("Please upload your
resume/CV (Max size: 5 MB)", required). The page has a password box (the account is made on the
same form), so the engine's fill and its resume attach never ran there, and the empty box went
into the list of questions: the job was set aside for "no saved answer" on an upload.

- `questionsFor` never takes an upload label (`isUploadLabel`: upload or attach with resume, CV,
  cover letter, document or file, or a "(Max size" note), so an upload never becomes a question,
  a deferred question or a "Needs you" row.
- Every step, a required upload box of the resume still empty (`uploadsOpen`: a file box with no
  file, a box worded as an upload, or one the engine left) gets the application's own resume:
  the page's file box first (`attachResume`, which still never uploads a copy the box already
  lists), else the upload button's own file chooser (`attachViaChooser` in main.js,
  shell-electron/chooser.js: the chooser is intercepted through the debugger, a real press on the
  button found by `uploadButtonPoint`, and the file box it opened gets the resume). Never a cover
  letter's box or button. At most twice per box.
- When neither takes it twice, the run stops: "I could not attach the resume on this site." The
  record moves to Needs you with that reason (`no_upload`, five locales: "Open it to attach it
  yourself"), a site problem, never a question.

The same run's other open question was "Preferred Engagement Language", a required list the
brain's pick was refused on (a preference with nothing to cite). It is not handled here.

### Today's date on a self-identification form

Uline's CC-305 disability form (Workday) showed "Invalid Date: 11/19/": the engine's Month, Day
and Year were typed one segment at a time with a focus from script in between, and the year never
registered. The brain then typed the year itself and the answer protocol refused it as a legal
date.

- The date a self-identification or signature form is signed is today's, not a fact about the
  person. `protocol.signature_date` (a box named "Date", "Today's date", "Date signed" and the
  like, on a page that is a self-identification or signature form, and only today's date, whole
  or one of its segments) lets the brain's typed date through as common sense ("today's date: the
  date this form is signed"). The self-identification answers themselves are unchanged (the
  decline unless saved).
- The fill's date segments follow main's measured in-page recipe (PR #310: `setSpin`, the native
  setter with focus, input, change and blur, read back), with real keys only as the fallback on a
  page in sight. A brain `type` on a date segment does the same (`spinType`): `setSpin` first,
  then, only when the page did not keep it, a real press and the digits as real keys (never
  select-all and Backspace, which steps back into the segment before).
- The fixture tests/fixtures/forms/workday_date.html models a widget that keeps only real keys:
  the in-page recipe cannot set it, so the fallback is what fills it there.

### Tests

tests/test_queue_run_2026_11_19.py (with the headless fixtures workday_languages.html,
icims_upload.html and workday_date.html), tests/js/test_queue_run_2026_11_19.js and
tests/js/test_icims_upload.js.

### A Workday date that shows digits but was never committed (2026-11-19, measured at Uline)

On the CC-305 form the Month / Day / Year boxes showed 11/19/2026 (the box, aria-valuetext and the
display text) while Workday said "Error: Enter today's date"; the agent parked after four tries.
The plain input and change of `setSpin` set what the boxes show, not Workday's own state of the
date, and the rule that a date showing digits is the person's then kept it from being tried again.

- `setSpin` now commits a segment the way that page takes it (measured): focus; the native setter
  with "" and an input event of type deleteContentBackward; the native setter with the value and
  an input event of type insertText carrying it; change; blur; a bubbling focusout.
  `commitSpins` does a date's segments in order, 300 ms apart. `fillDate` is async for that pace
  (its callers await it). The brain's typing on a date segment (`spinType` in agent.js) calls the
  same `setSpin` first, so it commits the same way.
- A date that shows digits is still the person's and left alone, unless the page reports a date
  error beside it (`dateError`: a `[data-automation-id=errorMessage]` or role=alert text in its
  area, such as "Enter today's date" or "Invalid Date"). Then the digits it shows are committed
  ONCE, as they are, and the date counts as filled only when the error is gone.
- Fixture: tests/fixtures/forms/workday_date_commit.html, whose state takes a segment only from an
  insertText input event and checks the date on focusout. Tests: tests/test_workday_date_commit.py.

## 33. A notice behind a button; a job on a board that forbids automation (2026-11-19, the re-run)

### A required privacy notice behind a button

SAP SuccessFactors' Create Account at Gainwell takes the data privacy statement through a
REQUIRED link, "Terms of Use: Read and accept the data privacy statement", which opens a dialog
with Accept and Decline. Create Account was refused until it was accepted. The page view listed
neither shape `termsItems` knew (an open dialog, a required box), and the in-page click refused
it as a policy link, so the brain reported needs-consent.

- `termsItems` has a third kind, "opener": a required button or link that opens the notice to read
  and accept it (OPENER_RE: read and accept, review and accept, accept the privacy statement),
  never a marketing one, never one inside a dialog, and only when no dialog is already open.
- `handleTerms` treats it like a required box under the person's consent to required notices
  (`accept_required_terms`, or their answer). Measured on the real page: SuccessFactors'
  `validateAndOpenDpcsDialog` first checks the required "Country/Region of Residence" list and,
  while it is empty, only moves the focus there and opens nothing (the first version of this
  pressed too early and saw no dialog). So, in order:
  1. the opener's form is read (`openerForm`): an empty required country list is set from the
     profile's country (`setCountry`, the list's own option by name or code, native setter with
     input and change); with any OTHER required box still empty the notice waits for the fill
     (not counted as a try), except after the brain reported needs-consent;
  2. the opener is pressed and the dialog awaited for about 5 s (`noticeDialog`: role=dialog or
     aria-modal, SAP UI's `<id>:container` box, or a large fixed or absolute box with Accept and
     Decline, about a notice, with exactly one accept button);
  3. no dialog, and the site moved the focus to an empty required country list: it is filled and
     the opener pressed once more (at most two presses per notice in a run);
  4. the dialog's single Accept is pressed, and the form's own record of it is checked
     (`noticeAccepted`: a hidden box such as `fbclc_dpcsId` that holds the acceptance); only then
     is it recorded through `/api/autoapply/agent/terms/accepted` (kind "opener") and never opened
     again. Without consent nothing is pressed and nothing is filled (in a batch the job parks on
     the question). Marketing boxes are never touched.
- The planner reads "Country/Region of Residence" as the country field (`contact_key`), so the fill
  sets that list wherever the engine runs.
- Such an opener is no longer refused as a policy link (`policyLink`); a plain link to read a
  policy still is.
- Tests: tests/js/test_consent_dialog.js on tests/fixtures/forms/sapsf_privacy.html.

### A job on a board that forbids automated applications

An Adzuna ad (record 28) went to dice.com. The run stopped, rightly, but said "the site sent me to
dice.com".

- The shell now says it plainly: "This job is on Dice, which does not allow automated
  applications. Open it and apply yourself." (`BOARD_LINE`, the board named from PROHIBITED_HOSTS:
  LinkedIn, Indeed, Glassdoor, ZipRecruiter, Monster, Dice). The run ends with reason
  "prohibited-board" and `aside`; a batch counts it as set aside, never as stopped.
- The record keeps it (`agent_left`, kind "prohibited", with the board). The Auto-apply row shows
  the line (five locales) and offers "Open and finish" (`open_self`), which opens the posting in
  the browser for the person; the agent never runs it again, so Auto's plan does not pick it.
- Auto's plan also skips every OTHER job with the same ad link (`_ad_key`: host and path, no
  query) once a run has recorded that ad landing on such a board. Only from what a run recorded;
  nothing is fetched to find out.
- Tests: tests/test_consent_dialog_and_dice.py, tests/js/test_prohibited_board.js.

## A work-authorization question takes only the saved answer (2026-11-23, measured at Uline)

"Will you now or in the future require employer visa sponsorship to work in the U.S.?" held the
person's saved Yes (they will need sponsorship later). The brain, which never sees the work
authorization profile, judged the value "likely a wrong guess" and selected No as a best guess in
Auto. That would have misstated the person's legal status to an employer.

- `protocol.work_auth_verdict` runs before every other check, in every mode, on a question
  `workauth.classify` reads as work authorization (sponsorship now / later / either, authorized,
  citizen, status) and whose words name it (sponsor, visa, authorized, citizen, status...): only
  the answer `workauth.answer` gives from the saved profile passes (source saved_answer); any other
  is refused (hard limit 5) and the brain is told the right answer. With nothing saved for that
  question it is left, never guessed.
- When the profile answers it but the form's choices cannot be matched to that answer, the old path
  stays: a best guess flagged "your work authorization is saved but this form words it differently".
- The engine attaches the saved work authorization to the material (`Material.work_auth`).
- Tests: tests/test_work_auth_never_guessed.py.

## 34. What the site fills, checked against the profile (2026-11-26, Ashby, Workday, Lam)

A site's own resume parse (Workday's "Autofill with Resume", Ashby's parse after the upload, Lam's
portal) or a saved draft fills the form before or after the engine. On the real runs that put the
email printed on the resume in place of the application email, the parse's job list (a project as
a job, a wrong title and dates) and the street in County, and none of it was corrected.

- **The contact boxes** (`enforceContact`, autofill_core.js): after every fill, every email box
  holds the application email (`_application_email`: the alias with the agent on, else the
  profile's own) and is read back; a referrer's, reference's or manager's email box is never
  touched. A phone box holds the profile's digits, without the country code when the page has its
  own Country Phone Code control. An address box holding another part of the address (County with
  the street) gets its own part, or is emptied. After a resume upload `guardContact` keeps these
  right in the page for 15 s, since the site's parse lands seconds later.
- **The work history** (`checkHistory`): each "Work Experience N" whose company is none of the
  profile's employers is removed with its own Delete; a profile job with the wrong title or From /
  To month gets the profile's; an "Education N" school the profile does not have is removed. Never
  with an empty profile.
- **Workday's two ways in**: with "Apply Manually" next to "Autofill with Resume" the shell clicks
  Apply Manually itself, once per run, with no model call (`manualOverParse`). The brain's rule 2
  says the same.
- **The phone loop**: a phone the brain types takes plain digits (`brain.contact_form`; a saved
  answer "+1 (...)" was refused six steps in a row), an email box the application email. When the
  page still names the phone in an error, the shell empties the box and types the digits with real
  keys, once per page (`fixPhone`, `phoneRetype`), and tells the brain not to type it again.
- **The final check** before Ready to send (`checkPrefill`, `prefillIssues`): puts right what it
  can on the page; what is still wrong (in words, never a value: "Work Experience 2 is not a job in
  your profile", "The page shows an email that is not your application email") goes back to the
  brain once, then is named at Ready to send under "Check noted".
- Tests: tests/test_autofill_site_fills.py (fixture tests/fixtures/forms/workday_prefilled.html),
  tests/js/test_agent_site_fills.js.

## 35. Autofill, then the agent finishes (2026-11-26)

The Autofill button is ONE button: the fast fill first (about $0.004 an application), then the agent
only where the fill falls short. It works with the apply agent switched off (the launch): it is the
person's own click, attended, and it stops at Ready to send for the person's own Submit.

- **The trigger** (`shell-electron/finish.js needsFinish`): after the fill, the agent steps in only
  when required fields are still empty, the page marks a field invalid (`aria-invalid`), the form
  has further pages, or Autofill landed on the job or overview page. A sign-in, a captcha, LinkedIn
  and the other boards that forbid automation are never handed over. Otherwise it ends after
  Autofill, as before.
- **It carries on from the page Autofill filled**: `agent.run` with `finish: true` and
  `resume.samePage`; no precheck, no fresh start, the job is never opened again (the ladder's
  `reopen_job` does nothing in this mode). A step only the person can do ends the run with the page
  as it is ("Press Autofill again when you want me to carry on"); nothing is parked to carry on by
  itself.
- **The plan is asked once per page** (`planKey`, `unplannedFields`): an agent step on a page whose
  fields the plan already saw skips `/api/autoapply/plan`; a new field (a follow-up question)
  is planned.
- **One cost figure** for the application: the Autofill plan (`/api/autoapply/plan` returns
  `cost`), each step's plan, the brain, the critic and a cover letter (`cost` from the letter
  route). Cap $0.30 (`SJ_FINISH_COST_CAP_USD`): the run stops, everything filled stays, and the bar
  says what is left in plain words.
- **Never Submit**: `handOver` in finish mode never sends, whatever the mode, the lane or
  `autonomous`; the bar has no Send button; the site's Submit is outlined; `send()` refuses a
  finish run (`your-click`). No Ready to send notice goes to Telegram.
- **The allowance** (enforced in the broker Worker, `worker/src/metering.ts`, migration 0018
  `usage.agent_finishes`): applications the agent finishes a calendar month, Free 3, Student 40,
  Standard 60 (the same as each plan's tailored resumes). `POST /apply/finish` counts one when the
  agent steps in (the engine's `/api/autoapply/finish/start`, once per application a month; Autofill
  alone is never counted), 402 `finish_limit` when used up: the fast Autofill stands with one line,
  "The agent finish for this month is used up, so I filled what I could. It resets on <date>."
  Extra packs hold live interviews only, so they add none. `/me/usage` reports `finishes_left`,
  `finishes_limit` and `finishes_reset`; the account menu shows "Agent finishes: N of M left". On the
  person's own AI key nothing is counted.
- **The gate**: with `SJ_APPLY_AGENT` off the engine opens the agent's page routes only while a
  finish run is open (`_finish_active`, 20 minutes at most, closed by `/finish/end`); Auto mode's
  queue routes stay shut, the parked list stays empty, and `/api/submit/decision` never allows a
  click on Submit. The Applications queue, Auto mode and "Apply for me" stay behind the flag.
- **The pointer** (`extension/pointer_overlay.js`): while Autofill or the agent works, a drawn
  pointer moves beside each field it fills or element it clicks, with a short label ("Filling:
  First name", "Clicking: Next", "Uploading resume"). A picture in its own shadow root,
  pointer-events none, placed right of the field (or under it) so it never covers it; glides
  0.2 s, jumps under reduced motion; gone when the run ends or after 10 s idle. One colour token, `--sj-pointer`, violet `#7957FC` (white label text 4.58:1).
- Tests: tests/test_autofill_finish.py, tests/js/test_autofill_finish.js,
  worker/test/finishes.test.ts.
## 36. Self-identification on any wording; the AI reads the saved answers (2026-11-26, owner decision)

At Lam Research the agent asked for "Veteran" although the person's veteran status was saved: the
site asked a plain Yes / No under the Title 38 notice, and the word rules did not read it. Word
lists break on the next site, so:

- `autoapply/eeo.py` stays the instant first pass (no model call when it is sure). It now also
  reads the federal notice (`4212`, VEVRAA, Vietnam era) as the protected veteran question, the
  protected veteran classifications by name, "I don't identify as a protected veteran", "Male /
  Man" and "I am Hispanic or Latino".
- When it is not sure, `autoapply/selfid.py` gives the model the question (with its notice text),
  the site's options and the person's saved answer, and it picks the option or says none fits. The
  pick is cached on this computer (`selfid_options.json`) by question, options and saved answer, so
  a repeat costs nothing. When none fits the person is asked once, and their pick is kept for that
  wording (`learn`, from the ask's answer).
- The standing "Decline to self-identify" always wins (`eeo.decline_pick`): the page's decline
  choice, found by the model only when the words are unusual.
- Self-identification answers are no longer withheld from the AI: the agent's view shows the
  choice a self-identification question holds like any other (`agent_view.js`,
  `brain.mask_self_id` passes the view as it is; the old masking stays as `hide_self_id`), saved
  self-identification answers are facts in `<person>` (`grounded.build_facts`), and
  `redact.sensitive_values` withholds legal values only by default. The Memory page's old promise
  ("no model, server or chat ever does") is removed; the privacy policy says the profile data,
  self-identification answers included, is sent to the AI provider to fill application forms.
- Tests: tests/test_selfid.py (fixtures: real systems' wordings, tests/fixtures/selfid/wordings.json).

## 37. Where Autofill and the agent stop: the stop log (2026-11-26)

So the brain is fixed in general, not one site at a time, every stop makes one small event:
asked the person, stuck in a loop, the cost cap, a captcha, a sign-in wall, required fields left
empty, a site error, a closed posting, other. The person's own Stop makes none.

- **The event** (`autoapply/stoplog.py build`): time, hiring system and host, the kind of page
  (job page, form, review, sign-in), the type, the PUBLIC question and choices involved (cut), the
  agent's one-line reason, steps, cost so far, app version. Only these fields are kept, and every
  text loses the person's saved values (contact details, links, longer saved answers), email
  addresses, links and long digit runs, whatever the shell sent. Never answers, names, resume text
  or anything typed.
- **Where they come from**: the agent loop (`agent.js logStop`: the cost cap, a park, a stop, the
  ladder giving up, each question asked at the end, required fields left at Ready to send) and the
  fast Autofill (`main.js`: a sign-in or captcha met, the month's agent finishes used up).
- **Kept and sent**: `data/stoplog.jsonl` (the newest 2,000), sent by a background thread in
  batches of 50 to the broker's `POST /v1/autofill/events` (account token). Fire-and-forget: never
  in a run's way; offline, the next batch takes it.
- **Broker** (`worker/src/stoplog.ts`, migration 0019 `autofill_events`): the same field list and
  cleaning again; 50 events a request, 300 rows an account a UTC day, kept 90 days (pruned on about
  one write in 50). Its rows written show as their own line on the Cloudflare Free Plan card.
- **Admin**: "Autofill stops" (`/stops`): counts by type over 7 and 30 days, by hiring system, and
  every stop grouped by its question (else its host), seen most first, with the newest example and
  the agent's reason.
- Tests: tests/test_stoplog.py, tests/js/test_stoplog_events.js, worker/test/stoplog.test.ts.
