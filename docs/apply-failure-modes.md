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
7. **Email-step account creation (iCIMS: email first, then a code or a link).** Gap: assisted
   apply stops at the wall and the person finishes it by hand; the inbox's verification reader
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

## The top gaps, in order

1. Site popups replace a half-filled form (Dropbox / Drive / "Apply with" buttons): the person
   loses their work. Child window for popups from a filled page.
2. Email-step account creation in assisted mode (iCIMS and others): offer to read the code from
   the person's own inbox, from that site only.
3. Drag-and-drop-only upload zones.
4. The bar saying "the site did not take it" and "this job closed" after Submit.
5. Non-English confirmation emails and markers.
6. The daily sweep reaches the person on the Tracker card and as one Orb nudge ("Did you send
   your <company> application?", kind `sent`), inside the Orb's daily limits; no OS notification
   of its own.

## What is not verified on a real site

The network patterns for Workday, iCIMS, SmartRecruiters, SuccessFactors, Taleo and Workable are
the shapes their submit addresses are known to take, not recorded captures (their fixture says
so). A miss there only means the network does not add evidence; the page, the form reset and the
ask still work. The Instacart case is proved on a rebuilt sequence, not a re-run on the live
site.
