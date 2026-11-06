# The apply agent: "Apply for me"

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
  20 s (`SJ_AGENT_PANE_MS`), the fill engine 40 s (`SJ_AGENT_FILL_MS`). `MAX_STEPS = 60`
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

- The row button is **Apply for me** (replaces "Fill application"). The top-right action
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
  review prefs), as one question on the bar before the first Apply for me or batch starts. It is
  never made for the person; leaving the box unticked decides nothing.
- **Single runs** ask live and wait (chunks of 20 s: `/ask` or `/terms` with `park`, then
  `/ask/wait`; Stop closes the ask between chunks with `/ask/close`). Past the wait the job parks.
- **Batches** (`agent.batch`, "Apply for me to all" on Waiting on you) never wait: an unknown
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
  applied on the way through. A fresh Apply for me on a parked job replaces the park.
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
timeout (`PANE_MS` 20 s; the fill 40 s; a brain call 90 s; CDP input events 6 s), with the
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
starts with `create account`, `reset password` and `wait for the site's email`. The same step
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

## 10. Milestones

1. **One job, visible.** Apply for me on a queued Workday job -> job page -> Apply -> sign in or
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
  mock ATS: one click on Apply for me ends on "Ready to send"; Send submits the mock; a
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
