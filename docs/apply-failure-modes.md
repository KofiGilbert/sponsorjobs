# Autofill to Tracker: every way it can break (2026-11-26)

The journey: the person opens a job, presses Autofill, the app fills the form in the Browser
pane, the person checks it and presses the site's own Submit, and the Tracker card moves to
Applied. The app never presses Submit (assisted apply, the launch mode). This page lists every
scenario we know that can break that journey, with its status:

* **covered**: handled before this change, with where;
* **fixed**: fixed in the sent-detection change (branch `feat/sent-detection`), with its test;
* **gap**: not handled yet, with the plan.

(2026-11-27) The top gaps were closed on branch `feat/autofill-gaps` ("Autofill: close the top
audit gaps"): each case below says **closed (gaps)** with where and its test. The tests are
`tests/test_audit_gaps.py` (it runs `tests/js/test_job_identity.js` and `test_popup_policy.js`),
`tests/js/test_sent_detect.js`, `tests/js/test_applied_watch.js` and two new replay scenarios.

The sent detector itself is `shell-electron/sent_detect.js` (header comment); the engine side is
`_sent_evidence` / `_sent_state` / `_ask_submitted` in `ui/app.py` and `inbox/matching.py`.

## How "sent" is known now

One detector combines independent evidence per application and marks it Applied once:

| Evidence | Confidence | Alone enough? | Where |
|---|---|---|---|
| Thank-you page, recipe marker (top page or any frame) | 0.95 | yes | `detectSuccess`, `frameSuccess` |
| Thank-you page, the model reading the changed page | 0.9 | yes | `applied_watch.js`, `/api/autofill/confirm-check` |
| The hiring system's submit request (POST/PUT, 2xx/3xx) after the person's own Submit click | 0.8 | no | `armNetWatch`, `classifyRequest` |
| Any form post to the application's own site after that click | 0.65 | no | same |
| The filled form went blank or went away after that click | 0.6 | no | `CLICK_PROBE_JS` |
| A window the site opened after that click | 0.3 | no | `setWindowOpenHandler` |
| The person's confirmation email (also for applications sent outside the app) | sure lanes only | yes | `inbox/matching.py`, `inbox/suggestions.py` |
| The person said yes | 1.0 | yes | the bar's ask, the Tracker's ask |

It fires at 0.85: the page alone, or a submit request plus the form clearing. A submit request
alone is not enough on purpose: a site that answers 200 with "please fix the errors" keeps the
filled form on screen. Each Applied move records which evidence decided it, and the Tracker's
timeline says so ("Application submitted: sent to Greenhouse, form cleared after Submit").

## The scenarios

### Where the form lives

1. **Form inside a cross-origin frame (Instacart: Greenhouse inside instacart.careers).**
   Fill: covered by `frames.js` (isolated worlds through CDP); the visible walk inside frames is
   PR #364. Sent: **fixed**. The frame reset to a blank form after Submit and nothing was read;
   now the click probe runs inside every cross-origin frame, the network watch covers the frame's
   own process, and Greenhouse's post plus the reset fires (`test_sent_detect.js`, the Instacart
   case). A thank-you page inside a frame was already read by `frameSuccess`.
2. **Form inside a same-origin frame or a shadow root.** Covered: the engine walks both
   (`searchRoots`).
3. **Confirmation in a modal over the page (Lam's Phenom window, Workday's "Application
   Submitted").** Covered: dialog markers (`successDialog`), and the model reads the page text.
   Fixture: `tests/fixtures/sent/confirmation_pages.json` (`phenom_lam_modal`).
4. **Confirmation as an in-place change with no navigation (single-page apps).** Covered: the
   model watch reads after a submit or Submit-type press (`applied_watch.js`), the probe polls
   every 2.5 s.

### Getting through the form

5. **Multi-page forms (Workday, iCIMS, SuccessFactors).** Covered: `armPageWatch` fills each new
   page after the person presses Next. Sent: only a Submit-worded click counts as the click
   (Next / Continue / Save never do), so a page change is not mistaken for a send.
6. **Login or account wall.** Covered: `pageBlocker() === "auth"` stops the fill, the bar says to
   sign in, and the page watch fills the next page after it. The app never signs in.
7. **Email-step account creation (iCIMS: email first, then a code or a link).** Closed (gaps), the
   owner's decision of 2026-11-26. The page is read from its shape (`autofill_core emailStep`: one
   visible email box, no password box, nothing else to fill; after the job-page check), never its
   words. The Orb and the bar ask once per site: "This site creates a profile with your email.
   Fill it in for you?" [Yes] [I'll do it]. Yes: the pointer types the application's email in
   sight into that box and stops; the bubble says "Tick the check box and press Next. I'll fill the
   form after." It never presses Next, never touches the check box or a captcha, never types a
   password, never creates an account with one, never reads a code (`main.js emailStepTurn`,
   `typeEmailStep`; the agent never takes this page, `finish.js`). The answer is kept per site
   (`/api/autofill/email-step`, prefs `email_step_sites`) and can be changed or forgotten on
   Memory ("Sites that ask for your email first"). The page watch fills the form once the person
   has pressed Next. Tests: `test_audit_gaps.py` (the email step), the replay's iCIMS scenario
   (seen) and every other form (never read as one). Still the person's: the code or link the
   site emails (reading it is a later decision).
8. **Captcha.** Covered: never solved; `WALL_JS` and `pageBlocker` say "solve it, then press
   Autofill again". A captcha that appears only after Submit leaves no thank-you page: the
   detector does not fire and the person is asked when they move on.
9. **File uploads.**
   * Hidden `<input type=file>` behind a styled button: covered, CDP `DOM.setFileInputFiles`
     (`cdpUploadFiles`, `uploadInFrame`).
   * Drag-and-drop only zones (no input at all): closed (gaps). `autofill_core dropZones` finds a
     box that takes a dropped file by the drag-and-drop contract (it cancels `dragover` for a file),
     never by its words; a page that cancels it everywhere tells nothing, so nothing is found
     there. When no file input took the resume, `main.js dropResume` scrolls to the box, the
     pointer says "Uploading resume", and the file is dragged in through CDP
     (`Input.dispatchDragEvent` with the file's path, `drop_upload.js`), then read back
     (`dropTook`). The replay's `drop_only_upload` (a decoy box beside the real one) proves a
     trusted drop with the real file. Not covered: a drop box inside a cross-origin frame.
   * Dropbox / Google Drive / "Attach from LinkedIn" buttons: closed (gaps). A site window over
     an application the person is filling opens as a small window of its own above the app, so
     the form stays in the pane and the site's chooser can still hand the file back
     (`popup_policy.js`, `setWindowOpenHandler`); the bar says "The site opened its own window.
     Your form stays here". A window the app's own click opened is never followed, and the brain
     is told never to click a control that brings a file or profile in from another service
     (`brain.py` rule 15). A page the site REPLACES (an "Apply with" that navigates away) is case
     35's: the bar says "You left the <company> form. Press Back to return to it", and on coming
     back "Back on your form. Press Autofill to fill anything the site cleared". Not restored by
     itself: what the site cleared is filled again on the person's Autofill.
   * A listed earlier resume with the same name: covered (`uploads.js`).
10. **The site's own "autofill from resume" overwrites the boxes (Ashby, Workday parse).**
    Covered: `guardContact` for 15 s after the upload, the Workday parse checks (PR #356).
11. **Slow page, hydration, timeouts.** Covered: `waitPaneLoad`, the fill budget
    (`fill_budget.js`), `fillKeys.capped` timeouts; a fill that times out leaves the rest for
    the person on the bar.

31. **A required box left empty while the bubble says "check and submit" (Instacart Canada,
    2026-11-26).** The end of an Autofill rests on Submit only when the engine's needs list is
    empty, and that list holds only the boxes the engine itself read as required. A required box it
    reads as optional (its mark drawn in a way the engine does not know, or inside a frame the
    top page's marking never reached) is left empty and unmentioned. Fixed as a guard: the replay
    suite reads every question as a person sees it ("*", a required mark, required /
    aria-required) and fails when the end says "check and submit" with a required question
    empty; a planted copy of the bug proves the check catches it. On the Instacart Canada replay
    (its real question set) the end now names the 3 empty questions. Gap: the frame case in the
    live shell is the frame walk's (#364: marking needs inside frames); the replay checks the
    engine inside the frame, not the shell's marking. Seen again in the live check on the
    Instacart Canada frame: "2 need you" during the fill, then after the agent was stopped the bar
    said "Filled 19. Check it, then submit on the site" with those 2 still empty. #364 (merged)
    marks needs inside frames; plan: the replay drives the shell's own end over frames too.
32. **The country warning did not show (Instacart Canada).** Closed (gaps):
    `autoapply/jobplace.py` reads where the job is: a country named in full (the whole country
    list and its other names), Canada's 13 provinces and territories by name or by code after a
    comma ("Vancouver, BC"; none collides with a US state code), a sure US place; else the model
    reads the location in any wording or language, or, with no location at all, the job page's
    own words (cached on this computer). A place it cannot tell is "", never the US; a job in a
    country the profile has no entry for (India) gets the note too. A record with no location:
    the app opens the job first and reads the page (JobPosting, then its words) before anything
    is filled (`countryGo`, `pane:open-wait`). Tests: `test_audit_gaps.py`,
    `test_country_note_page.py`. "Remote - CA" goes to the model. Before: The warning ("This job is in Canada;
    your saved work authorization is for the US") reads the job's location from its record. A job
    whose record has no location (opened from the Browser, a pasted link, some feeds) counts as the
    US, so no warning (seen again live: the Instacart Canada job opened in the Browser filled
    with no warning). The location reader also misses provinces written short ("Vancouver, BC"
    reads as the US) and "Remote - CA" is read as the US (it may be California). Gap. Plan: when
    the record has no location, read it from the posting's own location line (the Browser job
    reader already finds it) or the description's first lines; teach the reader the Canadian
    province codes when a Canadian city or "Canada" is near; and the replay's Instacart Canada
    form gains the check that the warning is said before the walk.
33. **"N need you" counted checkboxes, not questions.** Stripe's "countries you anticipate
    working in" (a checkbox per country) made the bar say "38 need you" for 9 questions; Lever's
    Yes / No checkbox question counted twice; Workday's month and year boxes of one date counted
    twice. Fixed in this change (the replay found it): the needs list counts a checkbox or radio
    group, and the boxes of one question, once; a group with a box ticked counts as answered.

34. **Two jobs on one careers path (live check, 2026-11-26).** Every Instacart job is
    instacart.careers/job?gh_jid=<id>; the page key kept host and path only, so Autofill on the
    Account Manager job said "You applied to this job on Nov 26" (that was the BI Manager job),
    and a confirmation on one could have marked the other. Fixed: the key keeps the parameters
    that name the job (gh_jid, token, jobId, req, ...), tracking parameters still dropped
    (`_url_key`, `test_two_jobs_on_one_careers_path_are_two_pages`); re-checked live.
35. **The Browser keeps the last application after another job opens (live check).** Closed
    (gaps). `shell-electron/job_identity.js` reads a page's job from its address: its job ids
    (gh_jid, jobId, req, token, a number or UUID or "Title_R12345" in the path, reduced so the
    careers page and its Greenhouse frame match), its path up to the first application step, a
    sign-in host, the hand-off from a job page Autofill found no form on. `main.js paneNavigated`
    checks every page the pane reaches against the pages the application was filled on (never
    while the app fills or the agent runs, never right after the person's own Submit): another
    job stops the page watch (it would fill the new job for the old record) and the sent watch
    (asking about the old one only when its Submit was pressed), and tells the app, which lets the
    application go, so Autofill there takes the "any job opened in the Browser" path (#363). A
    resume tailored for a Browser job is never filled into another job's form. Coming back to the
    same job gives the application back. Tests: `tests/js/test_job_identity.js` (26 cases: two jobs
    on one careers path, Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, slugs, SSO,
    hand-offs), `test_audit_gaps.py`. Not clicked through in the dev window yet.

### After the Submit click

12. **The site shows an error after Submit (a required field, a server error).** The bar: closed
    (gaps). The detector reports a 4xx or 5xx answer to the application's own send after the
    person's Submit (once per click, never a send, `onProblem`), and the model check has an
    "error" verdict, used only right after their Submit. The bar and the Orb say "The site did not
    take it yet. Check the page for what it asks" or "The site had a problem and did not take it.
    Press its Submit again in a minute", replacing the old line. Fixed before: the
    network watch only counts a 2xx/3xx answer, a submit request alone never marks it sent, and
    the model answers "no" for an error page (`_CONFIRM_SYSTEM`). Before, only the page decided,
    so this was already safe; now the stronger network signal cannot override it either
    (`test_sent_detect.js`, "a 200 with errors keeps the form"). Gap: the bar does not yet say
    "the site showed an error" in its own words. Plan: when the model answers "no" right after a
    Submit click and the form is still filled, the bar says "The site did not take it yet. Check
    the page."
13. **An ATS that shows nothing and emails nothing.** Fixed as far as it can be: the submit
    request plus the form clearing fires; with neither, the person is asked once when they move
    on, the Tracker asks after 30 minutes, and once more a day later (the daily sweep).
14. **Confirmation page in another language.** Closed (gaps), by meaning, not word lists. The
    page: the model's prompt says the page may be in any language and is data. The email: the
    model's acknowledgement moves a card by itself when the rules cannot read the mail (it is not
    in English, measured by English function words), found nothing contrary, it asks nothing, it
    came through a sure lane, and the model's deciding phrase stands in the mail as written; an
    English mail still needs the rules to agree (`inbox/replies.py rules_can_read`). Tests: French,
    German, Spanish and Portuguese confirmations, an English one, a made-up phrase and a question
    (`test_audit_gaps.py`). Not checked with real mails yet. The recipe markers stay English; the
    model covers the rest.
15. **The confirmation lands on another site (a job board hands off to the company).**
    Covered: the model watch still reads the first page on another site
    (`applied_watch.js`), and the network watch follows the pane.
16. **A popup or new tab carries the confirmation.** Covered: popups load in the pane, which is
    read; a popup right after the click is also a hint to the detector (fixed, `popup`).

### The person

17. **Leaves the page mid-way, or after Submit with nothing seen.** Fixed: leaving the site,
    closing the Browser, or starting Autofill on another job asks ONCE on the bar (the Orb's
    bubble mirrors the bar) or as a toast when the Browser is closed: "Did you send your <company>
    application?" Yes records "you said yes" (`assist:ask-sent`, `test_sent_detect.js` the
    last-resort ask). A fill that times out quietly is left to the Tracker's ask.
18. **Closes the app mid-fill or before the confirmation loads.** Covered by the Tracker's ask
    (the fill report stamps `filled_at`) and fixed by the daily sweep: a card filled over a day
    ago with no evidence is asked once more, on the card and as one Orb nudge
    (`test_sent_detection.py`). The confirmation email
    also lands later.
19. **Applies outside the app (their own Chrome, the phone).** Fixed as far as the inbox reaches:
    a confirmation email moves the card when it surely names that application; the new ATS
    no-reply wordings are recognised ("Thank you for your application", "We've got your
    application", "Confirmation of your application"). Needs the inbox connected.
20. **Two jobs at one company.** Fixed: an email that fits several cards and names none of their
    roles asks instead of moving the newest (`match_detail`, `ambiguous`). The page and network
    evidence belong to the application the fill was armed for (by record id), so they never land
    on another card.
21. **A nameless confirmation from the hiring system (Greenhouse no-reply) with one Greenhouse
    card on the board.** Fixed: before, it moved that card by itself, though the mail could be
    for a job applied to outside the app; now it asks.
22. **Reposted job (same role, same company, a new card).** Fixed: Autofill on it first asks
    "You applied to this job on Nov 26. Fill it again?" (`_sent_state` `repost`); an email that
    fits both cards asks.
23. **Autofill again on a card already Applied (or with a confirmation email).** Fixed: the
    duplicate guard asks first; "Fill again" lets that application be marked sent again, and its
    later pages are not asked about (`applyInPane`, `fillAgainOk`).
24. **Two app windows.** Covered: a second launch focuses the first (`second-instance`); there is
    one Browser pane and one detector, armed for one application at a time.
25. **Two applications filled one after another without sending the first.** Fixed: the second
    Autofill asks about the first ("switched").

### The world

26. **The job closes mid-fill.** Closed (gaps): the model check's "closed" verdict (any language)
    moves an application not sent yet to Closed with "Job closed" (`_mark_record_closed`), and the
    bar says "This job is closed. I moved it to Closed". The posting check still moves closed
    postings off To apply.
27. **Offline.** Covered: the engine is local; the model check answers "unsure" (never a guess),
    the network evidence and the form reset still work without a model, and the ask covers the
    rest.
28. **The site's session expires before Submit.** Covered: the site shows its sign-in wall; the
    page watch sees the wall and says so; nothing is marked sent. The app's own session is local.
29. **DevTools holds the pane's debugger.** Covered: CDP is unavailable, so frames, uploads and the
    network watch fall back; the page reading and the ask still work.
30. **LinkedIn or a board that bans bots.** Covered: never filled, never watched (CLAUDE.md
    section 7).

## The replay suite (how form bugs are caught before the owner sees them)

`tests/js/test_replay_forms.js` (CI job "replay", real headless Chromium, no network) runs the
guided Autofill with a made-up test profile against offline copies of the day's forms:
Greenhouse direct with each job's own question set (Stripe, Axon, Meriton, Vestmark: read from
Greenhouse's public job board API, `tests/fixtures/forms/replay/gh_*.json`), the Instacart Canada
form inside an iframe from another origin, Ashby, Lever, Lam's Phenom form with its privacy
window, Uline's Workday experience page and iCIMS's email step. For each it checks that every
required question is filled or outlined, nothing was filled out of sight, nothing was submitted,
no list is left open, and the end ("check and submit" or "N need you") matches the page. For the
Greenhouse confirmation and the Instacart frame reset, a real Submit click is followed by the
sent detector firing exactly once. It already caught two bugs fixed here: the detector dropped
a post that answered before the click note was read (the Instacart case), and the needs count
above. The Greenhouse pages are rebuilt from each job's real questions in the board's markup
(the live pages need their own scripts and hold the applicant's data); Ashby, Lever, Phenom,
Workday and iCIMS are the sanitized fixtures already in `tests/fixtures/forms`, modelled on
those sites. Adding a form: save its questions or markup (labels only, no values), add a line
to `SCENARIOS`.

## The top gaps, in order

1. Site popups replace a half-filled form: closed (gaps), case 9.
2. Email-step account creation: closed (gaps) as the owner decided, case 7 (the app types the
   email when the person says so, per site); reading the emailed code stays open (a later decision).
3. Drag-and-drop-only upload zones: closed (gaps), case 9.
4. The bar saying "the site did not take it" and "this job closed": closed (gaps), cases 12, 26.
5. Non-English confirmation emails and markers: closed (gaps) by the model, case 14.
6. The country warning for a job with no saved location, and short province codes: closed
   (gaps), case 32.
7. A page opened from a link keeps the last application: closed (gaps), case 35.
8. The daily sweep reaches the person on the Tracker card and as one Orb nudge ("Did you send
   your <company> application?", kind `sent`), inside the Orb's daily limits; no OS notification
   of its own.

### Still open after the gaps branch

* The emailed code or link of an email-first site (case 7): the person's, by decision.
* Case 31's frame part: the replay checks the engine inside a frame, not the shell's own end.
* A drop-only upload box inside a cross-origin frame; a page that cancels drops everywhere.
* A form a site replaces without navigating (a single-page app swapping the form for a chooser):
  the child window covers windows, not in-page swaps.
* What a site cleared when the person left and came back is filled again on their Autofill, not
  by itself.
* None of the gaps branch was clicked through on a live site yet (see below).

## What is not verified on a real site

The gaps branch (2026-11-27): the job identity rule, the child window for a site's chooser (and
whether Dropbox and Google Drive choosers hand the file back to the form from it; Google may
refuse sign-in in an embedded window), the CDP drop on a real drop-only box (proved in headless
Chromium in the replay, not in the Electron pane), the email-first step on a live iCIMS site, and
the model reading places and confirmations in other languages with the real model: none of it
was run in the owner's window yet.

The network patterns for Workday, iCIMS, SmartRecruiters, SuccessFactors, Taleo and Workable are
the shapes their submit addresses are known to take, not recorded captures (their fixture says
so). A miss there only means the network does not add evidence; the page, the form reset and the
ask still work. The Instacart case is proved on a rebuilt sequence, not a re-run on the live
site.
