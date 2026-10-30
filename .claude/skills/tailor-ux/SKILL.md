---
name: tailor-ux
description: The UI/UX design system, principles, and hard rules for SponsorJobs (this resume app). Invoke BEFORE any visual/UX work in ui/ — new screens, redesigns, copy, layout, or CSS. Keep it updated as decisions are made.
---

# SponsorJobs UI/UX

This is the living design language for SponsorJobs. Read it before touching anything the
user sees. When a UI decision is made or a mistake is corrected, **append it here** so
the next change stays consistent. This file is the source of truth over habit.

## 1. Aesthetic DNA (the one-line brief)

**Dark, minimal, almost no colour, generous space, few words.** The reference point is
Cursor: a calm near-black canvas where content sits in quiet framed cards and the eye is
never fought for. The user has said this repeatedly. When in doubt, remove.

- **Colour is rationed.** Neutral greys on near-black. The one accent (a near-white pill
  on dark, `--accent` / `--accent-ink`) is spent in *one* place per screen: the single
  primary action, or one "Recommended" badge. Never two competing pops.
- **Type carries hierarchy, not colour.** Size and weight separate levels. Titles
  `~22-26px/650`, body `~13-14px`, muted meta `~11.5-12.5px`.
- **Space does the grouping.** Related things sit in a card; unrelated things get air.

## 2. Copy rules (hard)

- **No em or en dashes in any user-facing text.** They read as AI-written. Enforced by
  `tests/test_no_dashes.py` (scans index.html, app.js, copy modules, and mdash/ndash
  entities). Use `&rarr;` for arrows, a comma or full stop otherwise. Hyphens are fine.
- **"Resume", never "CV"** in English UI. Code internals (cv ids, routes, i18n keys) stay
  as-is; non-English locales keep their native term.
- **One line, not a paragraph.** Users scan. If a sentence explains what the screen
  obviously is ("A one-way recorded screen where you answer on camera..."), cut it.
- **Do not narrate the obvious.** Redundant labels ("Recorded Interview" written three
  times across a stepper, a header, and a title) are clutter. Say a thing once.
- **A control says what it does.** Button = the verb ("Start interview", "Get started"),
  and the result confirms in the same words.

## 3. Layout system

- **One centered column, 1040px.** `.page-head` is `max-width:1040px; margin:0 auto`.
  Every view's content must share that column so headings and content left-align. When a
  panel needs a narrower inner width (e.g. a 720px recording grid), keep the OUTER
  container at the centered 1040 and cap the INNER content, so the left edge still matches
  the page title at every viewport width. (Left-aligning a narrower panel instead only
  *looks* aligned at the current window size and drifts when resized. This bit us once.)
- **Cards are the unit of grouping.** Surface bg, 1px border, `--r-lg` (14px) radius,
  ~28-32px padding: `background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:30px 32px`. A bare form floating on the page
  background reads as broken next to the carded landing. Frame it.
- **Progressive disclosure.** Show the one decision this step needs; defer the rest to the
  step that needs it. Don't front-load every option on the first screen.

## 4. Domain fidelity (training screens must feel like the real thing)

When a screen simulates a real product the user is practicing for (a one-way recorded
video interview, a live interview, an ATS form), **mirror that product's real UX flow** so
using ours builds muscle memory for the real one. Study the real flow (web research / the
product's own help center) before designing.

**Interview prep has exactly two rounds and two entry paths (approved 2026-10-02).** The
landing offers "Full mock interview" (Recommended: Round 1, then Round 2) plus "Round 1 only" and
"Round 2 only"; each opens its setup page (role, JD, optional resume) first (2026-10). The progress
tracker reads "Round 1 / Round 2". There is no local voice-only fallback round and no text
practice or typed mock; those were removed.

**Round 1 reference flow, a HireVue-style one-way recorded interview.** Never use the name in
a feature name, icon or heading; the only mention is the one-line disclaimer on the landing
("Practice for a HireVue-style one-way video interview. Not affiliated with HireVue, Inc.").
Our Round 1 tracks the real candidate flow screen by screen:
1. **Landing**: company + role, "5 questions, about 20 minutes", the kinds of questions, one
   primary action ("Get started"), the disclaimer.
2. **Notice**: recordings stay on this computer, can be deleted from the report, no facial
   analysis. An "I need extra time" accessibility checkbox doubles both clocks. This replaced
   the old Realistic/Relaxed switch.
3. **Device check**: camera preview, mic meter, a connection note, tips in the real product's
   wording ("light in front of you", "a spot free from distractions and noise", "don't worry
   about eye contact, just be natural").
4. **Practice**: unlimited practice questions in the exact real format, unscored, never saved,
   with "Do another practice question" and "Start the interview".
5. **Each question**: question text on the LEFT, "Prep time 0:30" at the TOP, the self view in
   the centre, a "Not Recording" / red-dot "Recording" status, "Start Recording" (or auto-start
   when prep ends), a 3-2-1 overlay, an answer countdown ("2:00"), "I'm done, stop recording",
   auto-submit at time-out, "Retake (1 left)" once per question, then "Next question". No back
   button. Progress "Question 2 of 5".
6. **All done** while answers save, transcribe and score (a typed fallback appears here only
   if this computer cannot transcribe).
7. **Report**: score/100, passed at 70, why, do this next, competency bars, per-answer feedback
   with fillers and hedges highlighted, "See a stronger version", .docx, delete recordings,
   and the API's `disclaimer`.
Screens 3 to 6 run on a **light, plain, white surface** like the real thing (owner's choice),
scoped to the stage container (`.r1-light`) with its own `--r1-*` tokens; the rest of the app
stays on the app theme. Calm, minimal tone.

**Round 2 is one live interview with the Tavus interviewer (15 minutes)** in the Electron
video pane (`renderCviStage`, `window.tailorShell.openBrowser`), then "End interview" leads to
the scored report. It is PAID ONLY (2026-10-14): a pass or a prepaid pack, on the company
Tavus key held by the broker; there is no own-key box anywhere. In the full mock, a PASS goes to
"Start Round 2" when `GET /api/interview/round2/status` is available; otherwise to a gate screen
("Live interviews come with a pass" with one button per pass on sale, names and prices from the
broker; or the packs when a pass is used up; "Finish here"). "Round 2 only"
calls cvi/start with `skip_screen: true`.
Sources: hirevue.com/candidates, HireVue Candidate Help Center (Zendesk), platform.tavus.io.

## 5. Verification workflow (how to prove a UI change, not guess)

- **Measure geometry over CDP; do not trust screenshots for alignment.** Connect to the
  Electron page target on `127.0.0.1:9223` (`suppress_origin=True`), and read
  `getBoundingClientRect()` via `Runtime.evaluate`. Pin a fixed viewport with
  `Emulation.setDeviceMetricsOverride` (e.g. 1200x900) so numbers are reproducible. A
  faint overflow once looked fine in a screenshot and shipped broken twice (#137/#138).
- **Hard-reload to pick up edits.** `Page.reload {ignoreCache:true}`. app.js/styles.css
  serve fresh, **but editing `index.html` requires an engine restart** (Jinja caches it).
- **Capture console + exceptions.** Enable `Runtime`, watch `exceptionThrown`. A silent
  null-ref (`$("#removedEl").hidden = ...`) can leave a screen blank with no visible error.
- **Selectors over CDP**: use unquoted attribute selectors like `[data-nav=prep]` to dodge
  shell/JSON/JS quote-escaping. `Page.captureScreenshot` has wedged in this env; prefer
  DOM eval.
- Run `node -c ui/static/app.js` and the relevant pytest (`test_no_dashes`,
  `test_interview_prep`) before committing.

## 6. Decision log (append here as we go)

- 2026-08: Interview prep landing is memory-first: a "Full mock" guided card (Recommended)
  over two à-la-carte tiles (Recorded / Live), seeded from built resumes. Minimal, no form.
- 2026-08: Recorded Interview rebuilt to the HireVue flow (§4): minimal welcome → equipment
  check (camera preview + mic meter + mode choice) → warm-up → questions. The old
  intro dumped an explanation, fact chips, and the mode picker onto the welcome; all cut.
- 2026-08: Resume *variant labels* (", A: experience-first") must be stripped from
  role titles shown in the interview UI (`prepRole()` in app.js). They are internal.
- 2026-08: Interview prep entry is two steps (progressive disclosure): step 1 picks the
  role (it shapes the whole mock), step 2 reveals the format choices with the role
  confirmed + a Change link back. A prerequisite decision comes before the choices it
  governs. Most recent resume pre-selected so step 1 is one click in the common case.
- 2026-08: Serve local assets `no-store` (SEND_FILE_MAX_AGE_DEFAULT=0 + after_request) or the
  Electron shell caches stale app.js/styles.css and UI changes only show after a forced reload.
- 2026-08 (supersedes the picker): prep step 1 is a PLAIN MANUAL form by preference: type the
  role, paste the JD, attach the CV, Continue (role + JD required, CV optional). The earlier
  memory-first resume dropdown / select-or-create control was removed. Lesson: the researched
  "best" pattern still lost to what the owner wanted; propose, but let preference decide.
- 2026-10: Jobs board notices are one muted line in the `.feed-note` family, dismissible with a
  quiet x, never a modal (`showBoardNotice` in app.js; the H-1B data vintage after a refresh uses it).
  Only a real failure takes `--flag`; "USCIS never published this year" is a note, not an error.
  Aggregator credit ("via Remotive") is a muted fact on the card and a follow link in the detail,
  because the card is a `<button>` and a nested link would be invalid markup.
- 2026-10-02: Interview prep converged to two rounds, two paths (§4). Round 1 rebuilt to the
  HireVue-style candidate flow with a LIGHT, white stage for the device check through "All done"
  (the owner chose light over the app theme there), an "I need extra time" checkbox instead of
  Realistic/Relaxed, unlimited unsaved practice questions, one retake per question, no back
  button. Round 2 is Tavus only, on the person's own free key, with a Settings block and a gate
  screen; the local voice-only fallback round, the text practice and typed mock modes, and the
  heartbeat-metered broker minutes were removed. Every new string carries a locale key in all
  five locales (`T(key, fallback)` in app.js, `data-i18n` in index.html).
- 2026-10-02: Telegram moved out of Connections into its own Settings tab, "Notifications": one
  "Connect Telegram" action (official bot: t.me link button plus a client-side QR from
  `ui/static/qr.js`, no CDN; own-bot setup only as the fallback, in three numbered steps), then
  switches for "New job matches" / "Application updates", a 1/3/5 segmented cap, and quiet-hour
  selects. Telegram buttons are honest: "Apply for me" only on the verified auto allowlist with
  autonomous submission on, otherwise "Tailor and queue" with "you click submit" said in the message.
- 2026-10-02: Telegram CV review is preview first. After a tailoring the chat started, the bot sends
  a picture of page 1 with the contact line under a solid bar (name visible; Settings, Notifications,
  "Show my contact details in Telegram previews", default off), a caption that says what changed
  ("Tailored for {Role} at {Company}: 2 bullets reworded, summary updated. Added the job's terms:
  PnL, ETL. Missing: Kafka."), and Show changes / Send PDF / Edit / Looks good. Media always goes
  with protect_content, and the first Send PDF says plainly that bot chats live on Telegram's
  servers and can only be deleted by the bot within 48 hours. Grounding: bot chats are cloud
  chats, not end to end encrypted.
- 2026-10-02: Chat edits are scoped and verifiable (InkSync UIST 2024: scoped inline edits plus
  flagging new information; Vasconcelos CSCW 2023: make AI output verifiable; FineEdit 2025: LLMs
  over-edit). Every part of the page has a stable id (S, E1.2, E1.title, P3.1, ED1.degree, K2),
  "Show changes" lists them with Before / After, an edit touches only the named ids (anything
  else changing rejects it), new skills or figures are flagged as "Check:" lines, and every edit
  comes back as a preview with Accept / Undo; nothing is saved before Accept. A fact (dates,
  titles, employer, education) always asks first: "This CV only / Also update my profile /
  Cancel". Accept never submits; the submit line follows the per-site policy.
- 2026-10: Prep landing shows the choices first (Full mock card + Recorded / Live tiles, unchanged look; no
  "Interviewing for" line). Start on any opens a setup page per choice ("Set up your full mock /
  recorded / live interview") with an "Interview prep" back link above the title (the `.tpl-back`
  pattern, never a close x): role + JD required, resume optional, one "Start interview". Fields are
  pre-filled from the newest tailored resume or Tracker job (`GET /api/preps/prefill`, its tailored
  resume attached when there is one), so most people just click Start. The STAR story bank was
  removed (UI, routes, `suggest_star_stories`). Supersedes the 2026-08 two-step and plain-form entries.
- 2026-10: Application status must be TRUE. One stage model (`submit/stages.py`) feeds Review,
  Ready to apply and the Tracker: To review -> Approved (not submitted) -> Submitted (by the auto
  lane or by you) or Needs you (an auto attempt came back, with a reason: posting closed, form
  needs you, sign-in or verification, site error). Approving keeps the card on Review and changes
  its pill at once; Ready to apply lists approved-not-submitted; the Tracker has an Approved
  column. Never say "filled" unless a form was actually filled. The template name is a layout,
  never part of a role title; it shows as its own meta on Resumes only.
- 2026-10: Auto-apply page = stage tiles + row list + Today panel + detail drawer; Review folded in.
  Header: title, one-line subtitle, "How it applies" popover (3 sentences), one primary "Tailor &
  queue N". Run settings sit in the Jobs filter bar classes. Four clickable tiles with real counts from
  submit/stages.py (To review, Waiting on you, Submitted, Needs you with a reason summary); the
  selected tile uses the Jobs selected-card border and fills a slim row list (logo, title, company and
  location, one sponsorship chip, stage text, match, ONE outlined action named for the stage). Up next
  shows what the next run tailors (saved first; x skips a role for the run only, it stays on Jobs).
  The Today panel shows auto sends against the daily cap, the Autonomous submit switch, Telegram (a
  quiet Connect when not set up), the inbox only when Gmail can actually be used, and recent sends. A
  row opens a right drawer (scrim, close x, Esc, focus trapped) with the stage's actions, what will
  be sent (resume terms, letter, screening answers), the timeline from the record's own timestamps,
  and site/lane/posted. Unknowns are left out, never estimated. The Review nav item is gone: approve
  (one or all), skip, edit and Send to Telegram live here, "One-tap approve all" moved to Settings,
  Notifications, and an old "review" destination opens Auto-apply on To review.
- 2026-10: "Your answers" is its own page (`view-answers`, 1040 column, `.tpl-back` back link named
  for where you came from: Auto-apply, Your profile, Application). Two surface cards: Work
  authorization (country switch US / UK / Canada / Australia / EU, status select, Yes/No segmented
  controls for authorized, sponsorship now, sponsorship in the future, relocate, a permit end date
  only for dated statuses, EEO decline; one primary Save) and Saved answers (search, Add answer,
  rows with Edit / Delete, a dashed "Drafted" tag on answers drafted from the profile). A citizen
  or permanent resident sees authorized Yes / sponsorship No locked, shown not asked. Doors: a
  Today-panel row on Auto-apply, a "Your answers" button on the drawer's Screening answers card
  and on the package page's Screening card, and the profile's Application answers card. Screening
  answers on the package page are editable in place; a saved edit also lands in Your answers
  unless the question is about that one company.
- 2026-10: Skip with a reason (AIApply "decline with a reason" pattern). Every skip stays one click
  (Up next x, the drawer's Skip, Jobs Dismiss); a small bottom bar then offers one-tap reasons
  (Wrong role, Too senior, Too junior, Location, Pay too low, Company, No sponsorship, Other with an
  optional note) and fades if ignored. "Company" asks once "Skip all roles at X?". Reasons shape the
  next runs (autoapply/learning.py): similar titles, levels and places move to the back, a known-pay
  floor and "no sponsorship" employers are left out. A Preferences card under Today holds "Never
  apply to" and "Prioritize" chips and "What I've learned" lines, each with Undo, plus one line when
  citizen-only and clearance roles are hidden (needs sponsorship). On Jobs a "Never apply to"
  company's roles stay listed, dimmed with a quiet "Never apply" fact: hiding them would make a
  search for that company look broken. Citizen-only / clearance / will-not-sponsor ads are hidden
  on Jobs and in runs while the person needs sponsorship.
- 2026-10-20 (design pass 15): Back links (`.tpl-back`) put their chevron flush with the page
  title's left edge (margin-left: -9px cancels the 8px padding + 1px border; the chip hangs into
  the gutter) on every page that has one. Every `.view` reserves its scrollbar gutter so a long
  page (Jobs) and a short one share the same left edge. Resumes has a page title like every view.
  The Jobs note is muted when quiet and red only after 24 hours with nothing new (never while a newer
  list downloads); an empty Jobs list hides the detail pane. A Home job card OPENS the role on Jobs
  (selected), "Tailor & apply" stays the explicit action. The package page never repeats its resume
  in the side panel. The builder has one download control (the footer's). A recorded interview's
  Quit asks in the page ("Leave the interview? Your answers so far are kept.") once an answer is
  recorded. A quiet outlined "Pass" marks the live round (Live tile, Full mock) without a pass. The
  Tracker's weekly bars are a muted fill on an axis of at least 5. Match scores count stated skills,
  and an ad naming fewer than 8 skills blends toward title similarity (lowered for a senior title
  when the person has none), so a thin ad can't read as a 100% fit.
- **2026-10-27 Home suggestions, not example text.** The Home box keeps a plain hint; example text
  inside a box (or rotating placeholders) is an anti-pattern. The requests it understands open as a
  list under the box on focus while it is empty ("Ask me to", arrows/Enter/Escape, overlays the
  roles), plus one quiet right-aligned "See examples" link. Home's roles leave out ones closed to a
  visa holder by title too ("USC & GC only"), via `/api/jobs?home=1`.
- **2026-10-30 Answers every form asks.** Your answers has a third card between Work authorization
  and Saved answers, in the Work authorization card's grammar (`.ans-card`, the 3-column `.ans-grid`,
  `.ans-field`, `.ans-seg`, `select.fld`): country of citizenship (a select; "Add another" reveals a
  second one), desired annual salary (USD), then the "Decline to self-identify" checkbox, moved here
  from Work authorization because it governs these rows and placed ABOVE them so the box never moves
  under the pointer when ticked, then Gender (segmented, spans two columns so it stays one row),
  Hispanic or Latino and Disability (segmented), Race and Veteran status (selects: long labels). With
  the decline on, the five rows fold into one muted line aligned with the checkbox text. One accent
  per page: Work authorization keeps the primary Save; this card's Save is outline and confirms
  "Saved" only after the list below has refreshed. The title carries "n of 7"; Auto-apply's Today row
  says "n of 7 form answers saved". Before the agent's first run of a session with answers missing,
  the agent bar says "Before I apply: n questions every form asks." with Answer them (Your answers at
  this card, focus on the first open answer, back returns to Auto-apply) and Apply anyway; never sent
  to Telegram. The bar sits on the browser chrome, dark in both themes, so `.bb-fill` carries the dark
  tokens (the light theme's near-black primary had vanished into it). Measured at 1893x1183@2x and
  2490x1556@1.52 (76% zoom), dark and light: card left = title left, no sideways scroll, bar contrast
  over 7:1. Screens: docs/design/answers/.
- 2026-10-30: Setup asks the answers every form asks as its last step, right after "A few details to finish your
  profile": the Your answers card itself is lent to the wizard (moved into the panel, handed back on close), so
  there is one set of rows, one decline choice and one save. Only rows still missing show; none missing, the step
  never opens. Skip (ghost, left) saves nothing and never blocks; Save is the one primary. In the 620px wizard the
  card drops its frame, heading and own Save, and its grid goes to two columns. "Not now" on the details step moves
  setup on to this step instead of closing it. The "Before I apply" prompt stays as the safety net. Screens:
  docs/design/onboarding/.
