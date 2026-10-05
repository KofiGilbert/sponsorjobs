# CLAUDE.md — Autonomous Resume-Tailoring Job Agent

> This file is the source of truth for how this project is built. Read it fully
> before scaffolding or writing code. Follow the phase order. Respect the
> "Do Not Build" list without exception.

## 1. What this is

> **BRAND (2026-10):** the product is **SponsorJobs** (domain pending: sponsorjobs.ai/.app).
> "Tailor" now names only the résumé-tailoring FEATURE inside the app (the Tailor tab, "Tailor
> your résumé", the tailoring engine). Python packages, env vars (`TAILOR_*`), routes and the
> repo keep their names.

A **local-first desktop application** that helps a person apply to jobs. For each
job, the agent:

1. **Sources** the job posting from official, public job feeds.
2. **Tailors** a LaTeX resume to that specific job — rewording content freely to
   match the JD and build the strongest-fit persona from the person's supplied
   material, without breaking the one-page layout.
3. **Self-heals** the compile: if the PDF fails or overflows, it diagnoses the
   log, shortens the offending content, and recompiles until it's clean.
4. **Drafts** a cover letter and answers to screening questions from the
   person's real history.
5. **Assembles** a submission-ready package and puts it in a review queue.
6. **Submits** by the per-site policy (§7): auto-submits where the site permits it
   (an official API or a bot-allowed portal) and hands the rest to the person to
   click (LinkedIn always).
7. **Follows up** — with the person's consent, reads their OWN mailbox to complete
   any email verification an application requires (so an autonomous apply actually
   goes through) and to surface recruiter replies, drafting answers from the
   person's real history for the person to send.

The product runs on the user's own machine, uses the user's own LLM API key,
and stores all data locally. There is **no server in v1.**

> **EVOLVING (approved 2026-07-29):** paid AI features (Tavus avatar; bundled LLM)
> will run through a **thin company-run broker** that holds the company API keys and
> meters usage, so users never need their own key. This does NOT change the privacy
> model: personal data (resumes, history, applications) still lives on the user's
> machine and is never stored by us; the broker keeps only usage counts for billing.
> The promise becomes "we do not store or mine your data," not "nothing ever leaves
> your machine." The app stays local-first for all personal data. Full design and
> phased plan: `docs/bundled-api-backend.md`.

## 2. The three-input mental model (critical — do not conflate these)

The system always works with three *separate* inputs:

- **TEMPLATE** — a LaTeX skeleton (`.tex`). Structure only. Generic and reusable.
  Every field (name, address, bullets) is an empty slot. The template's *shape*
  never changes. Do not treat any content in a sample template as facts about
  the user.
- **PROFILE** — the person's own material, collected through the intake step
  (§4a) and saved for reuse: names, experiences, projects, dates, skills, and
  anything else they choose to provide. The person is the source of truth; they
  decide what to include and what to submit.
- **JOB** — a single job description the resume is being tailored toward.

Tailoring = select, arrange, reword, and re-emphasize the person's PROFILE
material to fit the JOB, poured into TEMPLATE's slots, to present them as the
strongest-fit candidate for that role. The person reviews and owns the output.

## 4. Intake, saved profile, and modes

**4a. JD-driven intake.** When the person picks a job, the AI reads the JD first,
then asks only the questions relevant to *that* role — e.g. "this role emphasizes
X and Y; which of your experiences and projects should I use for it, and what
name and dates should appear?" It does not ask a fixed generic questionnaire; the
JD drives what it asks. People often hold several overlapping roles and choose
what to present per application, so intake is about the person selecting from
their own material for this specific JD.

**4b. Saved & reusable profile.** The person's intake answers are saved locally
and reused. On a new application, the AI asks whether to (a) reuse the saved
profile as-is, (b) provide fresh information, or (c) add/adjust on top of what's
saved. This means they answer once and reuse across many jobs.

**4c. Two ways to run:**
- **Guided** — the AI asks the JD-driven questions and tailors from the answers.
- **Autonomous** — the person can say "just build it for this JD." The AI then
  assembles the best-fit CV for the role from the **saved profile**, with no
  per-application input needed, and drops it in the review queue. (The saved
  profile is the material it draws on; autonomous mode is about skipping the
  manual curation each time, not about needing the person present.)

In both modes the person owns the material and reviews it. They decide what to
submit — or turn ON autonomous submission for sites that permit it (§7), where the
agent submits their reviewed package for them without a per-application click.

## 5. Architecture (local-first)

```
Desktop App (runs on user's machine)
├── sourcing/      pull jobs from official public job feeds (APIs)
├── tailoring/     LaTeX surgical editor + self-healing compiler (the core)
├── drafting/      cover letter + screening answers from PROFILE
├── review/        queue UI: see each package, edit, approve
├── submit/        auto-submit where the site permits it (official API or a
│                  bot-allowed portal); else assisted — the person clicks submit
├── inbox/         the user's OWN mailbox (Gmail API / IMAP, OAuth) — complete
│                  application email verifications, surface & draft recruiter replies
├── config/        template.tex, profile data, settings, secrets (git-ignored)
└── data/          local SQLite db: jobs, tailored outputs, statuses, logs
```

- **No cloud server for v1.** The user's machine does the work.
- **Bring-your-own-key**: the user supplies their own LLM API key. Store it in a
  git-ignored `credentials.env`, never in the repo. (EVOLVING: to be bundled behind
  the company broker so users need no key; see the §1 note and `docs/bundled-api-backend.md`.)
- **SQLite** for all local state. One row per job with its status and artifacts.
- **LLM calls**: use the API directly. Do not add a heavy orchestration
  framework unless a concrete need appears. Keep temperature low (~0.3) for
  faithful rephrasing.

**Email / inbox (the user's OWN account).** The `inbox/` module connects to the
person's own mailbox — **Gmail API via OAuth** first, IMAP/SMTP for other providers
— with their explicit consent. It exists so the loop actually completes: many
portals require **email verification** (or account creation) before an application
goes through, and recruiters reply by email. Rules:
- The OAuth token / credentials live **locally**, git-ignored, like the LLM key —
  no cloud, consistent with local-first.
- **Read-first.** It watches only for messages tied to the person's applications:
  verification/confirmation links and recruiter replies. It never reads, indexes,
  or acts on unrelated mail.
- **Send on consent.** Recruiter replies are **drafted** from the person's real
  history for the person to review and send. In autonomous mode the person may opt
  in to auto-completing an application's own verification link/confirmation — that
  and only that. Never sends unsolicited or bulk mail.
- Only ever the person's own inbox, with their consent; never anyone else's, and
  never as a channel for spam.

## 6. Compliance lanes (these are hard rules, not preferences)

**GREEN — automate freely.** Public ATS job feeds (Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, Recruitee) expose public JSON with no login, no key,
no proxy. Aggregator/remote APIs (Adzuna, Remotive, etc.) with free dev keys.
This is the sourcing backbone. Some ATSs (e.g. Greenhouse) also expose a
documented application-submission endpoint — the cleanest path for automated
submission (secret tokens must never be exposed client-side). Automated submission
is NOT limited to official APIs, though: it is sanctioned on any site or company
career portal whose terms permit automated/bot applications, whether via an API or
by driving the site's own form. Sites that prohibit automation get assisted apply,
never auto-submit. The full per-site policy is in §7.

**YELLOW — avoid as a foundation.** Scraper libraries that need proxies to
"bypass blocking" and get rate-limited/429'd (e.g. JobSpy) live in a terms-of-
service grey zone. Do not build the product's core on them.

**RED — never build.** See the Do Not Build list below.

## 7. Do Not Build (non-negotiable)

- No automation of a logged-in LinkedIn account (applying, scraping, messaging).
- No stealth/anti-detection tooling: no persistent-profile tricks to bypass
  MFA or captcha, no captcha interception, no human-mimicking randomized delays
  whose purpose is to evade bot detection, no rotating proxies to dodge blocks.
- No submission on any site whose terms prohibit automation — and nothing that
  relies on evading bot detection to get through. If a submission would need
  evasion to succeed, that is the site telling you it doesn't allow bots: drop to
  assisted apply instead.
- If a task seems to require any of the above, STOP and surface it to the user
  rather than implementing a workaround.

**Auto-submission IS a first-class feature — gated by each site's own rules.**
Applying autonomously (the agent fills AND submits, unattended) is wanted and
allowed on sites and company career portals whose terms permit automated/bot
applications — via an official submission API where one exists, or by driving the
site's own form otherwise. It runs from the person's OWN reviewed material,
identifies honestly, and respects the site's rate limits. Everywhere else — any
site that prohibits automation, or whose policy is unknown or unclear — the agent
does ASSISTED apply: it fills the form and the person reviews and clicks submit. A
curated per-site submission policy decides which sites are auto vs. assisted;
default to assisted when unsure, and never auto-submit where it isn't clearly
permitted. We never promise auto-apply to every job — only where the site allows
it — and the review queue tells the person which happened for each application.

**The AUTO lane is a curated, evidence-based allowlist — the one hard rule.** A
site is AUTO only if a human maintainer has VERIFIED it has a real, sanctioned,
and **applicant-usable** submission path: an official application-submission API
(or explicit documented permission for programmatic submission) that our
applicant-run app can actually call **without the employer's secret credential**.
This allowlist ships with the product (`submit/allowlist.py`), records the evidence
per entry (endpoint, doc URL, auth model, verified date), and is updated ONLY by a
maintainer after real verification. There is deliberately **no runtime path** that
moves a site into AUTO — no inference from the page, no guessing from robots.txt,
no "looks submittable", no user-editable config. Everything not on the verified
allowlist — unknown, unverified, employer-key-gated, or automation-prohibited — is
ASSISTED. Autonomous (unattended) submission is additionally OPT-IN and OFF by
default. A driver that meets a captcha/auth wall aborts to assisted; it never evades.
(Verified 2026-07-14 across our sourcing ATSs: only **Recruitee** exposes a keyless,
candidate-facing submission API and is AUTO; Greenhouse, Lever, Ashby, Workable, and
SmartRecruiters all gate submission behind the employer's key and are ASSISTED.)

LinkedIn is a **browse-by-hand** source: always assisted, never auto-submit. The
agent may help the user find and open jobs and pre-fill the form, but the user
drives LinkedIn's own UI and clicks submit.

## 8. The tailoring engine — hard constraints

This is the core and the part worth getting exactly right.

- **Surgical edits only.** Parse the template into addressable bullets and edit
  ONLY the text inside specific experience-bullet macros. Never touch the
  preamble, packages, geometry, margins, or formatting macros. After any edit, a
  diff of the `.tex` must show changes confined to targeted bullet interiors.
- **One-page preservation.** Exit code 0 is NOT sufficient proof of success.
  After compiling, verify (a) the page count is unchanged (still one page) and
  (b) no new `Overfull \hbox` warnings appeared versus the baseline log. Reject
  and self-heal otherwise.
- **Per-bullet length budget.** Constrain each rewrite to roughly the original
  bullet's length (±~10%). Tailoring is re-emphasis, not expansion.
- **Tailor aggressively; the person owns the content.** Reword freely, emphasize
  transferable skills, adopt the JD's vocabulary, reorder and foreground the most
  relevant material, and build the strongest-fit persona for the role. The source
  material is the person's saved PROFILE, and the person decides what to include
  and submit — the engine's job is to arrange and present it compellingly, not to
  vet or second-guess it.
- **Section scope.** Every content section is tailorable — Experience, Projects,
  Skills, summary, etc. Fields the person supplies at intake (name, dates, which
  roles to show) are filled from their answers for that application.
- **Rollback.** If a candidate won't compile or fails a check, roll back to the
  last known-good source so the user never receives a broken or two-page PDF.
- **ATS keyword alignment (a primary goal, not a side effect).** Most employers
  screen CVs through an applicant tracking system before a human sees them, so
  the engine must actively align the CV to the JD's language:
  - Extract the JD's meaningful terms — hard skills, tools, methods, certs, and
    the exact phrasings used — and identify which the person's saved PROFILE
    genuinely supports.
  - Work supported terms into the CV using the JD's own wording where natural.
    ATS matching is often literal (e.g. "PnL" vs "profit and loss", "ETL" vs
    "data pipelines"), so mirror the JD's phrasing rather than a synonym.
  - Keep formatting clean and parseable (the single-column LaTeX template already
    is — preserve that; no tables/columns/graphics in tailored content that would
    confuse a parser).
  - Produce a **coverage report** with each output: which JD key terms made it
    into the CV, and which the JD wants that the profile doesn't cover — so the
    person can decide whether to add material.
- **No keyword stuffing.** Do NOT insert hidden/white text, keyword walls, or
  terms the person's material doesn't support. Modern ATS and recruiters penalize
  this and it defeats the goal. Match the JD's vocabulary only where real material
  supports it, phrased naturally, and surface gaps in the coverage report instead
  of papering over them. Note: exact ATS scoring is proprietary and changes;
  optimize for JD-accurate terminology, clean parsing, and real skill coverage
  rather than chasing a specific "score."

## 9. Self-healing compile loop (the Devin-like core)

Extend "roll back on failure" into an iterate-until-green loop:

1. Splice tailored bullets → compile.
2. Read the log. If exit != 0, or page count changed, or a new overfull hbox
   appeared: identify the offending bullet(s), shorten/adjust the wording, and
   recompile.
3. Cap retries (e.g. 3–4). If still failing, roll back and flag for human review.
4. Never ship an output that hasn't passed all three checks (compiles, one page,
   no overfull).

## 10. Definition of done (acceptance tests to write)

- Diff of original vs tailored `.tex` changes ONLY targeted bullet interiors.
- Tailored PDF is still one page with no new overfull warnings.
- The JD-driven intake asks only role-relevant questions, and saved answers can
  be reused, refreshed, or added to on a new application.
- Autonomous mode produces a tailored CV from the saved profile with no
  per-application input.
- Content sections (Experience, Projects, Skills) are aggressively reworded
  toward the JD.
- Deliberately broken LaTeX is rejected and rolled back to a valid PDF.
- Each tailored CV comes with a coverage report showing which JD key terms are
  present and which are missing; supported JD terms appear in the CV using the
  JD's own phrasing, with no hidden text or unsupported keyword stuffing.
- Sourcing pulls only from GREEN-lane feeds. No RED-lane code exists anywhere.

## 11. Suggested stack

- **Python 3.11+** for the core pipeline.
- **LaTeX toolchain** (`pdflatex` via TeX Live / MacTeX) driven by subprocess.
- **Playwright** (or the browser extension) for form automation, driven by the
  per-site policy (§7): unattended auto-submit on sites whose terms permit it, and
  assisted fill (the person clicks submit) everywhere else and by default. It stops
  at any auth wall it isn't authorized to pass and never evades bot detection;
  never automates a logged-in LinkedIn account.
- **SQLite** (via stdlib `sqlite3` or a light ORM) for local state.
- **Desktop shell**: keep v1 simple — a local UI (e.g. a small web UI served
  locally, or a lightweight desktop framework). Don't over-engineer the shell
  before the engine works.

## 12. Build order (do NOT build everything at once)

**Phase 1 — Tailoring engine (build and fully test first).** The JD-driven
intake step, the surgical editor, and the self-healing compiler. Testable offline
with a sample TEMPLATE and a saved PROFILE, no job board or browser needed. This
is the spine.

**Phase 2 — Sourcing.** Pull jobs from GREEN-lane ATS/aggregator feeds into the
local db. Parse job descriptions.

**Phase 3 — Drafting + review queue.** Cover letters and screening answers from
PROFILE; a UI to review, edit, and approve each package.

**Phase 4 — Submission.** A per-site submission policy drives three paths: (a)
auto-submit via an official submission API where sanctioned; (b) unattended
auto-submit by driving the site's own form on sites/portals whose terms permit bot
applications; (c) assisted apply — fill, the person clicks submit — everywhere else
and by default (LinkedIn always). The person opts into autonomous submission; the
review queue shows, per application, whether it was auto-submitted or is waiting on
their click.

**Phase 5 — Email / inbox + follow-up.** Connect the person's own mailbox
(`inbox/`, §5) so autonomous apply completes end to end: detect and complete the
**email verification / confirmation** an application requires, and surface recruiter
replies with drafted answers for the person to send. This is the piece that makes
(b) actually go through on portals that gate applications behind a verified email.

Complete and test each phase before starting the next.

## 13. Deferred / on the horizon

- **UI polish** — intake and the CV output currently run in a plain local CLI to
  prove the flow. Once intake works end-to-end, build a proper, visually polished
  user interface (a real window with input fields, and a nice rendered CV
  preview). Do not lose this — the CLI is a temporary testing surface, not the
  final product.

- **Online-only, real-model-only (now enforced).** Tailor is an online tool: it
  uses a real model (default `claude-sonnet-4-6`, cost-efficient for tailoring;
  overridable via `RESUME_AGENT_MODEL`, e.g. `claude-opus-4-8` for a premium tier)
  to tailor CVs and to source/apply to
  jobs, so it hard-requires an internet connection and the user's own Anthropic
  API key and fails loudly with a clear message if either is missing (`_make_llm`
  in `ui/app.py`). The `FakeLLM` is a TEST DOUBLE only — honored under pytest or an
  explicit `RESUME_AGENT_DEV=1` session, never in a shipped build, never shown as
  product quality. All quality/benchmark verification uses the real model.

- **Licensing / anti-piracy — LAUNCH-TIME, not now.** Do NOT build a from-scratch
  or client-side "am I cracked?" scheme (weak, and it punishes legitimate users).
  At launch, add online license activation via an established provider (e.g.
  Keygen or Cryptolens): SERVER-SIDE license validation on each launch, plus device
  fingerprinting to cap machines per license. Server-side checks only. Until then,
  the built-in economic moat is that the app runs on the user's own API key and
  credits, so a cracked copy still costs the pirate real money to operate.
