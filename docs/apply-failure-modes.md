# Autofill to Tracker: every way it can break (2026-11-26)

The journey: the person opens a job, presses Autofill, the app fills the form in the Browser
pane, the person checks it and presses the site's own Submit, and the Tracker card moves to
Applied. The app never presses Submit (assisted apply, the launch mode). This page lists every
scenario we know that can break that journey, with its status:

* **covered**: handled before this change, with where;
* **fixed**: fixed in the sent-detection change (branch `feat/sent-detection`), with its test;
* **gap**: not handled yet, with the plan.

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
7. **Email-step account creation (iCIMS: email first, then a code or a link).** Gap. The replay
   shows the email-only first page is NOT seen as a wall (no password box): Autofill types the
   person's email, outlines the privacy box, presses nothing, and says "1 needs you". That is
   harmless (Next stays the person's), but the bar does not say it is a sign-in step. The
   password page after it is a wall and stops. The inbox's verification reader
   (`inbox/detect.py kind_of`, `about_site`) exists for the agent but is not offered in assisted
   mode. Plan: when the wall is an email step, the bar offers "Check my inbox for the code" which
   reads only mail from that site (`about_site`) and shows the code for the person to type.
8. **Captcha.** Covered: never solved; `WALL_JS` and `pageBlocker` say "solve it, then press
   Autofill again". A captcha that appears only after Submit leaves no thank-you page: the
   detector does not fire and the person is asked when they move on.
9. **File uploads.**
   * Hidden `<input type=file>` behind a styled button: covered, CDP `DOM.setFileInputFiles`
     (`cdpUploadFiles`, `uploadInFrame`).
   * Drag-and-drop only zones (no input at all): gap. Plan: dispatch a synthetic `drop` with a
     `DataTransfer` built in the isolated world from the file the shell reads; until then the bar
     lists "Resume" under "Left for you".
   * Dropbox / Google Drive / "Attach from LinkedIn" buttons: gap, and a risk. A site's popup
     is loaded INTO the pane (`setWindowOpenHandler`, "nothing opens a window"), so pressing
     such a button replaces the half-filled form with the chooser. Plan: open popups from a page
     with a filled form in a small child window over the pane (closed when it reports back), or
     at least warn on the bar before the pane leaves a filled form.
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
32. **The country warning did not show (Instacart Canada).** The warning ("This job is in Canada;
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
35. **The Browser keeps the last application after another job opens (live check).** A page
    opened without typing its address (a link, a card's link inside the pane) keeps the previous
    application as the Browser's context, so Autofill fills the new page for the old record, and
    a send there would be counted on the old card. Gap. Plan: the Browser lets the application
    go when the pane reaches another job (another site, or the same site with another job key)
    and asks which job this is.

### After the Submit click

12. **The site shows an error after Submit (a required field, a server error).** Fixed: the
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
14. **Confirmation page in another language.** Covered for the page: the model reads any
    language. The recipe markers and the email rules are English only: gap. Plan: the email
    rules gain the French, German, Spanish and Portuguese wordings of "we received your
    application" (the app's five languages), checked with real mails.
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

26. **The job closes mid-fill.** Covered in part: the posting check moves closed postings off To
    apply; a form that answers "no longer accepting applications" gets a model "no" and nothing
    moves. Gap: the bar does not say the job closed. Plan: the same model call returns "closed"
    as a fourth verdict and the card moves to Closed with "Job closed".
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

1. Site popups replace a half-filled form (Dropbox / Drive / "Apply with" buttons): the person
   loses their work. Child window for popups from a filled page.
2. Email-step account creation in assisted mode (iCIMS and others): offer to read the code from
   the person's own inbox, from that site only.
3. Drag-and-drop-only upload zones.
4. The bar saying "the site did not take it" and "this job closed" after Submit.
5. Non-English confirmation emails and markers.
6. The country warning for a job with no saved location, and short province codes (31, 32).
7. The daily sweep reaches the person on the Tracker card and as one Orb nudge ("Did you send
   your <company> application?", kind `sent`), inside the Orb's daily limits; no OS notification
   of its own.

## What is not verified on a real site

The network patterns for Workday, iCIMS, SmartRecruiters, SuccessFactors, Taleo and Workable are
the shapes their submit addresses are known to take, not recorded captures (their fixture says
so). A miss there only means the network does not add evidence; the page, the form reset and the
ask still work. The Instacart case is proved on a rebuilt sequence, not a re-run on the live
site.
