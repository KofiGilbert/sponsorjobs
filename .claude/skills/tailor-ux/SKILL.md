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

**Interview prep has exactly two rounds and two entry paths (approved 2026-10-02).** After
the prep form (role, JD, optional resume), the chooser offers "Full mock interview"
(Recommended: Round 1, then Round 2) plus "Round 1 only" and "Round 2 only". The progress
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
the scored report. It needs the person's own Tavus key: Settings has a "Live interviewer
(Tavus)" block (key field, save/revoke, a "Get a free key" explainer: 25 minutes a month, no
card, platform.tavus.io) with a status pill from `GET /api/interview/round2/status`. In the
full mock, a PASS goes to "Start Round 2" when the status is available; otherwise to a gate
screen ("Round 2 needs a live interviewer", the free-key steps, "Finish here"). "Round 2 only"
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
