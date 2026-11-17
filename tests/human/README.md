# Human tests (developer only)

Plain-English UI checks for the running SponsorJobs window, written with
[Midscene.js](https://github.com/web-infra-dev/midscene) (MIT) on top of Playwright.
A vision model looks at a screenshot of the window and does what each step says, like a
person would.

This folder is a developer tool. It has its own `package.json`, is not part of the shipped
app, and does not touch `shell-electron`'s dependencies.

## What it does and does not do

- Attaches to the dev window that is **already running** over CDP (`127.0.0.1:9233`). It
  never launches a browser, Electron or the engine.
- Picks the SponsorJobs tab (URL contains `127.0.0.1:57010`), never a job-site tab.
- Starts each test from Home and only looks and navigates.
- Two safety nets for the whole run (see `fixtures.ts`):
  - every request that is not GET/HEAD/OPTIONS is blocked, so nothing can tailor (paid
    credit), send, submit, save or delete;
  - clicks on Tailor & apply, Send, Submit, Edit, Show, Forget, Delete, Change photo,
    checkboxes and file pickers are swallowed, and typing into the page is blocked.
  Anything blocked is attached to the test result as `safety-guard`.
- Screenshots of your window go to the model provider (Google). Use a key from a project
  with billing on: on the free tier Google may use what it sees to improve its products.

## The tests

| File | Checks |
|---|---|
| `specs/home.spec.ts` | Home shows "What are you applying for?" and at least one role card under Today |
| `specs/open-a-job.spec.ts` | Jobs, open the first job, its detail has a "Tailor & apply" button (never clicked) |
| `specs/memory-page.spec.ts` | Profile photo, My profile, Self-identification lists Gender, Transgender, Sexual orientation, First-generation professional (Edit and Show never clicked) |

## Setup (once)

```sh
cd ~/sponsorjobs/tests/human
npm ci
cp .env.example .env   # then put your Gemini key in MIDSCENE_MODEL_API_KEY
```

`npm ci` does not download any browser: these tests reuse your running window.

## Environment variables

Required (in `tests/human/.env`, git-ignored):

- `MIDSCENE_MODEL_BASE_URL`
- `MIDSCENE_MODEL_API_KEY`
- `MIDSCENE_MODEL_NAME`
- `MIDSCENE_MODEL_FAMILY`

Optional:

- `MIDSCENE_MODEL_REASONING_ENABLED` (defaults to `false` here, which is cheaper)
- `SJ_CDP_URL` (default `http://127.0.0.1:9233`)
- `SJ_APP_URL_MATCH` (default `127.0.0.1:57010`)

## Run

The dev window must be open and idle (no apply run in progress), on any screen.

```sh
cd ~/sponsorjobs/tests/human && npx playwright test
```

One test: `npx playwright test specs/home.spec.ts`. List without running:
`npx playwright test --list`. Type check: `npm run typecheck`.

## The report

After a run, Midscene writes one HTML report with a screenshot and the model's reasoning for
every step to:

```
tests/human/midscene_run/report/<timestamp>.html
```

The terminal prints the exact path. `npm run report` opens the folder.

## Cost

About 10 model calls for all three tests (each tap, wait or check is one call; a wait that
has to re-check costs one more per check). With `gemini-3.6-flash` that is roughly 5 cents for
the whole run in 2026, about double from 1 January 2027 when Google's price doubles. Set
`MIDSCENE_MODEL_NAME=gemini-3.5-flash-lite` for about half that, at some risk of less exact
clicks. No retries are configured, so a failure is not re-run automatically.
