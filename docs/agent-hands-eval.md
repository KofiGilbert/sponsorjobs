# Agent hands: ours vs agent-browser (evaluation, 2026-11-16)

> Question: should the apply agent's "eyes and hands" (the page view the brain reads and the
> actions it takes) be vercel-labs/agent-browser (v0.43.0) instead of ours
> (`extension/agent_view.js` + `shell-electron/agent.js`), keeping our brain, planner and fill
> engine, answer library, consent, allowlist and the stop at Submit?
>
> Answer: **keep ours.** Borrow two small ideas from agent-browser (below). Use agent-browser as a
> developer's inspection tool, not in the product.

## 1. Method

**The adapter** (`shell-electron/abhands.js`, off unless `SJ_HANDS=agent-browser`; nothing changes
by default). Per step:

1. The shell builds OUR view as usual (so the shell's own helpers keep working: page check,
   saved answers, dropdown readers, the read-only tools) and tags each of our elements with
   `data-sj-ix="<our index>"`.
2. `agent-browser --cdp <the shell's own CDP port> snapshot -i --json` of the Browser pane's tab
   (selected by URL; never the app window's tab).
3. One `agent-browser batch` reads `data-sj-ix` through every ref (about 60 ms), so an element both
   sides see keeps our index and one only agent-browser sees gets 5001 and up.
4. The snapshot is converted to our line grammar (docs/apply-agent.md section 3) and that is what
   the brain reads. Typed values are never shown (`value="(filled)"`), self-identification
   choices are hidden, a file box keeps its words so the brain's cover-letter rule still works,
   `*` marks refs that are new since the last view, and an unnamed control takes agent-browser's
   own `hint="in:..."` text as its label.
5. The brain's `click`, `type` (`fill`; a typeahead gets `fill ""` then real keys with `type`),
   `select`, `upload` and `scroll` run as agent-browser commands on the ref; its JSON result or
   error is the step result. Our refusals still apply first (Submit, LinkedIn and social sign-ins,
   the resume never in a cover letter box). A saved answer typed into a list we know stays on our
   own known-answers pick.

Unchanged in both modes: the deterministic fill engine runs first on every page, the brain
(`autoapply/brain.py`), the asks, consent, the allowlist, the ready gate, the critic and the stop
at Submit. So the comparison isolates what the BRAIN sees and how ITS actions are carried out.

**Runs.** Real postings, each started in the app the way a person does it (Jobs, the job, "Tailor &
apply" or "Open your resume", then Apply), on a separate app instance with a copy of the owner's
data (engine 57050, CDP 9263, official edition on the live broker, Telegram off for this
instance). The same saved resume per job for both sides. Nothing was submitted; every run stopped
at Ready to send or earlier. When a run asked the person something, nothing was answered: the
question was recorded and the run stopped. The final form of every run was read with a FULL
agent-browser snapshot (`snapshot`, not `-i`, so chosen dropdown values show) and counted by
hand: required fields, filled, wrong.

The Workday job named for the test (Uline, Business Intelligence Analyst) closed during the test
(Workday answered 404; the app said "This job is closed. Nothing was spent."). Uline's "Business
Analyst - Facilities" on the same Workday site was used instead. That site lets you apply as a
guest, so no portal account was created and no email verification came up.

## 2. Scorecard

| # | Job (system) | Hands | Ready to send | Required: filled / wrong / empty | Asked the person | Brain steps | Wall time | AI cost (bar) |
|---|---|---|---|---|---|---|---|---|
| 1 | Gusto (Greenhouse) | ours | **yes** | 20: 20 / 0 / 0 | 0 | 2 | 0:30 | $0.04 |
| 2 | Gusto (Greenhouse) | agent-browser | no (parked: "still stuck") | 20: 20 / 0 / 0 (all by our fill engine) | 0 | 22 | 1:54 | $0.35 |
| 3 | CrossCountry (Lever) | ours | no (waiting on 1 question) | 12: 9 / 0 / 3 | 1 | 2 | 0:16 to the question | $0.02 |
| 4 | CrossCountry (Lever) | agent-browser | yes, but early (critic noted 3 issues) | 12: 9 / **1** / 2 | 0 | 16 | 1:42 | $0.17 |
| 5 | Uline (Workday) | ours | no (parked: fill timed out on My Experience 4 times) | page 2 of 6: 0 of 6 | 0 | 11 (+6 hang steps) | 7:48 | $0.11 |
| 6 | Uline (Workday) | agent-browser | no (waiting on 6 questions) | page 2 of 6: 1 of 6 | 6 | 10 | 1:32 to the questions | $0.10 |
| 7 | Uline (Workday) | ours, again | no (waiting on 1 question) | reached step 6 of 7; that page 3: 1 / 0 / 2 | 1 | 49 | 4:27 to the question | $0.60 |
| 8 | Uline (Workday) | agent-browser, again | no (waiting on 6 questions) | page 2 of 6: 1 of 6 | 6 | 25 | 5:39 to the questions | $0.26 |

Totals: 8 real runs, about $1.65 of AI spent on the brain (the bar's figure; the one cover letter
written for Gusto in run 1 and the two resume tailorings are not in it).

## 3. Per-run notes

1. **Gusto, ours.** The fill engine filled 21 fields, attached the resume, wrote and attached a
   cover letter and declined the four self-identification questions the person has not answered.
   The brain read the rest of the form and said ready; the critic found nothing. Every value
   checked against the person's saved material.
2. **Gusto, agent-browser.** The same fill engine filled the same 20 required fields before the
   brain's first look. But `snapshot -i` does not show what a Greenhouse (react-select) dropdown
   has chosen: the choice is a sibling text node, not the input's value. Every answered dropdown
   read `value=""`, so the brain spent 20 steps re-typing answers into "empty" boxes (its own
   screenshots told it they were filled), the critic then read the same view and called required
   fields empty, and the recovery ladder parked the run. Also: none of the 13 dropdown inputs
   mapped to our elements (ours lists a filled react-select by its chosen text, not by its
   zero-width input), so `inspect` and `list_options` on them answered "no such element".
3. **CrossCountry, ours.** Cookie banner denied, Apply clicked, 7 fields and the resume filled in
   one step. Then it asked the person "Current company", which the engine had already filled from
   the profile (a false ask), while three required custom questions stayed empty: "Which location
   are you applying for?", the hybrid-schedule location, and a free-text box asking for visa
   details. All three are answerable from the person's own material.
4. **CrossCountry, agent-browser.** 11 of the 16 steps were lost on the job page: agent-browser's
   clicks on "Apply for this job" each took about 5 s, reported success and did not navigate (our
   `inspect` said the link was covered); a scroll finally let one through. On the form, Lever
   marks its custom questions required only with a "✱" in the text, so agent-browser's snapshot
   did not mark the two location dropdowns required and showed their placeholder ("Select...") as
   a value; the ready gate let the hand-over through with both empty. Worse, agent-browser names
   the visa-details box by a hint cut at 40 characters, and that short label matched a saved
   yes/no answer, which the brain typed into the free-text box: **a wrong answer on the page**.
   Ours never typed it, because our label is the whole question.
5. **Uline, ours.** No account wall (guest apply). My Information filled (13 fields); every click
   on Save and Continue led to a fill that timed out (40 s) on My Experience, then the ladder
   reloaded back to page 1; parked after four rounds. Nothing on My Experience was filled.
6. **Uline, agent-browser.** Reached My Experience on the first pass (about 70 s). There, the
   engine attached the resume and filled the school, and the run asked the person six questions
   (job title, company, dates, degree): the person's own work history and education, which the
   profile already holds. agent-browser's `upload` on the "Select files" button failed ("Node is not
   a file input element"); our engine's own attach had already put the resume there.
7. **Uline, ours, again.** This time the brain took the "Autofill with Resume" path and got through
   five of seven steps (My Information, My Experience, Application Questions, Voluntary
   Disclosures). It cost 49 brain steps and about $0.60 for one application (close to the $0.80
   cap), with two "trying another way" ladders and several loops before the resume page. It ended
   asking the person "Date" on the disability self-identification form (CC-305): that is the
   signature date, today's date, which the agent should fill itself and never ask. The saved
   disability answer was not picked either (a checkbox group), so the page stayed open. Stopped
   there, unanswered. The site's required data-privacy box was ticked by the person's standing
   "always accept required terms" choice. **Cost finding:** 49 steps and about $0.60 for one
   Workday application that still did not reach Review is too much on its own, whichever hands.
8. **Uline, agent-browser, again.** The same fill timeout on My Experience as run 5 (once, then
   four ladders: reload, reopen the job, wait for the form, reload), then the same six questions as
   run 6. agent-browser's `upload` on "Select files" failed again; the resume was already attached
   by our engine.

**Defect seen on Workday (both hands): duplicate resume uploads.** Workday keeps a guest's draft
application across runs and page revisits, and our fill engine attaches the resume again each time
it reaches My Experience: after runs 5 to 8 the draft held three copies of the same resume. The
uploads came from our engine's attach in both modes (agent-browser's own `upload` never succeeded:
0 of 2), so it is an engine defect, not a hands one. The two extra copies were deleted by hand at
the end; the draft now holds one. Fix (not in this experiment): before attaching, check whether the
field already lists a file with the same name and size and skip it; remove duplicates when found.

The My Experience fill timeout hit run 5 (ours) and run 8 (agent-browser) and not runs 6 or 7, so
it belongs to the engine and the page, not to either side's hands; the path the brain picks on
Workday (Apply Manually or Autofill with Resume) changes a run more than the hands do.

## 4. What each side's eyes and hands do well and badly

**agent-browser, eyes.** Fast and steady (about 40 to 250 ms per snapshot, refs that survive
re-renders), and the accessibility names are often cleaner than ours. But for our job, filling
real ATS forms, `snapshot -i` drops the state the brain needs most:
- a custom dropdown's chosen value (Greenhouse react-select): it looks empty;
- "required" marked only in the text (Lever's ✱): it looks optional;
- a native select's placeholder ("Select..."): it looks answered;
- the page's own text: no job text, no section text, no validation errors (ours prints `Error:`
  lines), so the brain cannot tell why a page will not move on;
- labels: an unnamed control's hint is cut at about 40 characters, which led to a wrong saved
  answer being matched.

**agent-browser, hands.** Coordinate clicks with a built-in wait: about 5 s on a covered or
unresponsive link, then "ok" with nothing happening; no read-back of what a click or a pick did;
`upload` needs the real file input (a Workday "Select files" button fails), where ours finds the
box's own input.

**Ours.** Shows chosen values, required groups, errors and the page's words, and maps an upload
to the field's own box. Its weaknesses in these runs are in the brain and engine, not the hands:
false asks ("Current company" already filled), unfilled questions the profile answers (location,
hybrid location, visa details, Workday work history), the CC-305 date asked instead of filled, a
fill that timed out on one Workday run, and the step and dollar cost of Workday (49 steps, $0.60).

## 5. Recommendation: keep ours

Switching would make the agent worse on the two systems that matter most here: on Greenhouse it
turned a 30-second, $0.04 Ready to send into a $0.35 park, and on Lever it produced a wrong answer
and handed over a form with required questions empty. Neither failure is a bug in the adapter; both
come from what `snapshot -i` chooses to show. A hybrid that keeps agent-browser's eyes would need our
values, required flags, errors and page text added back, which is our view again.

Worth taking from agent-browser (small, in our own code):
1. **A context label for unnamed controls**, the way its `hint="in:..."` does, but from the
   whole question (never cut), for controls our label finder misses.
2. **Keep agent-browser as a developer tool** for reading a live pane (it is how every run here
   was verified: `agent-browser --cdp <port> snapshot`), never in the product: it is another
   binary and daemon on the person's machine and another CDP client on the pane.

What would actually move the numbers (outside this experiment, for the brain and engine):
fill the CC-305 date and pick the saved disability answer on a checkbox group; never ask for a
field the engine already filled; answer location and hybrid-location questions from the profile's
city; fill Workday's My Experience from the work history; and bring a Workday application well
under 49 steps and $0.60.

## 6. Reproduce

```
# the adapter is off by default; to try it in a dev run:
SJ_HANDS=agent-browser ./shell-electron/dev-start.sh     # needs agent-browser on PATH
node tests/js/test_abhands.js                              # the converter, no browser needed
```
Optional: `SJ_AGENT_BROWSER_SESSION` (default `sponsorjobs-hands`) keeps its daemon apart from any
other agent-browser session; `SJ_AGENT_BROWSER_BIN` points at the binary.
