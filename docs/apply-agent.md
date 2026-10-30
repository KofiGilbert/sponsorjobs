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
  for step in 1..MAX_STEPS (40):
    1. settle(): URL changed OR DOM quiet for 600 ms OR 8 s cap (never a flat nap)
    2. guard(): blocker (captcha -> stop "captcha"; auth -> section 4), host off the
                allowlist -> stop "left-site", success page -> stop "submitted"
    3. fill(): the deterministic engine fills what it recognizes on this page
                (recipes -> labels -> widgets -> /api/autoapply/plan), uploads the resume
    4. view():  serialize the page for the model (section 3), main frame + cross-origin frames
    5. brain(): POST /api/autoapply/agent/step {view, history, ...} -> {memory, actions, done}
    6. act():   run the actions in order; stop the queue when the page changes
    7. record(): one history line {memory, evaluation, actions, results}; loop detector
    done.outcome == "ready_to_submit" -> hand over (section 6)
    done.outcome == "blocked"         -> stop with reason, bar + Telegram
  MAX_STEPS or 4 consecutive failed steps -> stop "stuck", package stays "waiting on you"
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
other actions dropped; empty reply -> one retry, then `blocked: needs-person`.

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
- Per step: one brain call (60 s cap), actions 25 s each. `MAX_STEPS = 40`,
  `MAX_FAILED_STEPS = 4` in a row (a failed step = an action error or an empty reply).
- Loop detector: the hash of `(op, index, text)` over the last 12 actions; the same hash 3
  times -> a nudge line in the next prompt ("you repeated X three times; it is not working"),
  5 times -> stop `stuck`. The same `(url, view hash)` 4 steps in a row -> stop `stuck`.
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
- "Needs you" tile counts packages stopped on `blocked`; each row says why in one line.

### Asking the person (`/api/autoapply/agent/ask`, autoapply/asks.py)

When the brain reports `blocked: needs-answers`, the shell asks about the REQUIRED questions
still open on the page (at most 6), on the bar and on Telegram, and waits up to 5 minutes.

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
  `eeo_decline` choice answer what they can (`known_only`); only the rest is asked. An EEO
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
  Only a question whose Telegram message was delivered takes Telegram text.
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
Ready to send summary ("Accepted Lam Research's Data Privacy Agreement for you."). Stop, or no
answer, ends the run and nothing is accepted. The brain never accepts terms or follows a link to
a policy (the in-page click refuses one: `policy-link`); it reports `blocked: needs-consent` when
one still blocks the way.

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
screenshot is attached only on a `stuck` nudge), todo/file-system actions, plan fields,
history compaction, multi-tab as a model feature, LiteLLM pricing, client-side model fallback.

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
  `?captcha=1` variant stops with the captcha message and no submit.
- Quality: one real-model run per milestone on the fixture and on a live Greenhouse job page
  (stops before submit), logged step count and cost.
