# The applicant brain

> The build plan for one local memory that knows the person: what is true about them (facts),
> how they want to come across for each kind of job (choices), and what has happened (history).
> The resume builder and the apply agent both read it. Every place that learns something writes
> to it. Approved by the owner on 2026-10-30. Read this before touching `intake/memory.py`,
> `intake/palace_memory.py`, `autoapply/answers.py`, `autoapply/basics.py`, `autoapply/planner.py`,
> `_known_answers_for` or `_answer_lib` in `ui/app.py`, or the profile and Your answers pages.
>
> Status: plan only. No product code ships with this document.

## Contents

1. Why
2. What we are building
3. Where memory lives today
4. Decisions already made
5. Data model
6. Matching a choice to a job
7. Recall: answering without asking
8. Learning choices
9. EEO and legal safety
10. Privacy, Export and Erase
11. Migration from the five stores
12. Removing the MemPalace library, keeping the diary
13. The memory page
14. Costs
15. Phased build plan
16. What we will not do
17. Not certain yet

---

## 1. Why

The owner's words:

- "One giant brain that remembers."
- A person can hold overlapping roles, for example two day jobs in the same year. They want to
  "show one of each depending on where I am applying".
- Without recall the app "uses the same profile over and over in a robotic way".
- It "keeps asking irrelevant questions". The rule is: ask only what we never have.
- In August he asked for "no profile page, pure magic". That plan was half built and stalled.

What goes wrong today, found on 2026-10-30:

- The app remembers the person in five separate places. The apply agent reads only one of them
  (the answer library) plus the work authorization and the decline setting.
- An answer given on Telegram is saved only in the answer library. It never updates the work
  authorization, the intake facts or the profile.
- Salary is stored three ways and two of them disagree.
- The resume builder picks roles by word overlap with the job. It has no way to remember "for
  banks, show Stanbic, not Viva". The only rule it honors is "do not mention X".
- MemPalace semantic search has never worked in the installed app (numpy is left out of the
  build and the import error is swallowed). Only the plain chat diary works.
- Export and "Erase everything" skip the answer library, `app_prefs.json` and the interview files.

## 2. What we are building

One local store with three kinds of memory:

1. **Facts.** Exact values with their source, date, confidence, status and sensitivity. Fixed
   keys for the questions every form asks (salary, citizenship, work authorization, EEO), plus
   open facts for the person's roles, schools, skills and projects.
2. **Choices.** How the person wants to come across for kinds of jobs. "For banks and finance,
   show the Stanbic role, not Viva." "For startups, lead with BlackOrigin." "Never show both 2020
   day jobs together." Each choice has a scope: all jobs, a job family, an industry, a company
   type, or one company. Learned by asking once, applied automatically, visible and deletable.
3. **History.** The chat diary (app and Telegram), the documents the person shared, and every
   application with what was shown on the resume and what was answered on the form.

```
 writers                                 the brain                         readers
 -------                                 ---------                         -------
 intake chat (fact extraction)  ---+                                  +--> apply agent
 Your answers page              ---+     data/applicant.db            |    (_known_answers_for,
 "Answers every form asks" card ---+       fact                       |     plan_known, asks)
 onboarding basics step         ---+-->    rule        (choices)  ----+--> resume builder
 agent asks (bar, Telegram)     ---+       application, fill, event   |    (select_roles_for_jd,
 Telegram late answers, imports ---+       label_map, company_class   |     select_for_cv, gate)
 extension corrections          ---+     data/palace/transcripts/     +--> Telegram assistant
 resume import                  ---+       *.jsonl  (the chat diary)  +--> memory page
```

Two rules hold everywhere:

- **Code decides, the model proposes.** The model may suggest that a sentence in chat is a fact
  or a choice. Only code saves it, and only after the person agrees where the fact is legal or
  EEO, and always for choices.
- **Unknown is a real answer.** When the brain does not know, it says so, and the agent asks once.
  It never fills the nearest match.

## 3. Where memory lives today

| Store | File | Holds | Written by | Read by |
|---|---|---|---|---|
| conversation_memory | `data/resume_agent.db` (`intake/memory.py`) | The resume profile, essentials and chat history as JSON | Intake autosave (`ui/session.py _persist`), `/api/profile/from_cv`, `/api/profile/details`, extension corrections (`_learn_from_corrections`) | Tailoring, drafting, interviews. The agent reads identity and first school only |
| memory_facts | same file | Facts, preferences, constraints distilled from intake chat | `ui/session.py _distill_facts` | CV selection (`_apply_memory_selection`, `_avoid_token_sets`), Telegram chat, the Memory panel |
| MemPalace | `data/palace/` (`intake/palace_memory.py`) | JSONL transcripts, plus a ChromaDB index that never worked in the installer | Every intake turn, Telegram chat, interview summaries, Memory notes and documents | `ui/session.py` recall, career highlights, Telegram chat |
| Answer library | `data/answers.db` (`autoapply/answers.py`) | Question to answer, keyed by canonical key or word set | Agent asks, Your answers, basics card, screening edits, extension corrections, drafted answers, Telegram late answers (PR #143) | The planner and `_known_answers_for`: the agent's main source |
| Prefs and work authorization | `data/app_prefs.json` | `work_auth` (structured), flat Yes/No answers, salary, start date, how heard, `eeo_decline` | Extension popup, profile answers, Your answers, basics card (salary) | The agent reads `work_auth` and `eeo_decline`. The extension autofill reads the rest |

Related files that stay where they are: `data/aa_learn.json` (which jobs to skip, by company or
place; that is about picking jobs, not about the person), the interview files
(`interviews.json`, `preps.json`, `screens.json`, `interview_goals.json`), `linkedin_review.json`
and `review_prefs.json` (the mail alias). Export and Erase must cover them (section 10).

## 4. Decisions already made

- **Our own SQLite, no new heavy dependency.** Python's built-in `sqlite3`, one file in the app's
  data folder. Cognee, Graphiti, Mem0 and Letta were judged on 2026-10-30. All of them put a
  model between the person's words and the stored value, return the nearest match instead of
  "unknown", and add install weight or telemetry. Ideas we copy: dated facts that are replaced,
  not overwritten (Graphiti, Cognee); a source for every fact; "the model proposes, code
  decides"; an exact match before any model call; a small always-loaded card kept apart from
  the deep archive (Letta).
- **Not MemPalace's knowledge graph.** It lowercases values, keeps them in rows other facts
  share, and cannot hard delete. "Forget my ethnicity" must really delete.
- **Remove the MemPalace library, keep the diary.** The plain JSONL transcript is our own code
  and it works. Semantic recall never worked in the installer and costs numpy, an 80 MB model
  download and up to about 230 MB of RAM. Section 12.
- **The diary stays a JSONL file.** It is the one part that is not inside `applicant.db`. An
  append-only text file is the safest home for every word the person typed: no schema, readable
  by hand, and it survives a crash mid-write. The brain indexes it (file and line) and one API
  reads and writes both, so Export, Erase and Forget treat them as one store.
- **Nothing is stored by our broker.** The broker relays model calls and Telegram messages. It
  never holds facts, choices or history. EEO values never reach it.
- **Package name `applicant/`.** `autoapply/brain.py` already exists (the agent's next-click
  brain), so the memory lives in a new package `applicant/` and the file is
  `data/applicant.db`.

## 5. Data model

All tables live in `data/applicant.db`. One connection per process with WAL mode and one lock,
the same pattern as `ConversationMemory`. Every table has a `person` column (default
`"default"`) so a second profile later costs nothing. The whole schema is created in Phase 1
(schema version 1) and filled phase by phase, so later phases never change the schema.

### 5.1 `fact`

One row per value. A changed value adds a row and marks the old one replaced.

| Column | Meaning |
|---|---|
| `id` | Integer key |
| `person` | `default` |
| `key` | The fact's key (5.2). Canonical keys reuse the answer library's names (`@salary`, `@gender`, `@citizenship`), so no lookup table is needed between them |
| `subject` | What the fact is about, when it is not the person as a whole: a country code for `workauth` (`US`), a company key for `@previously_worked`, an item id for open facts (`role:stanbic bank|analyst`). Empty otherwise |
| `value` | The exact value, as given. JSON for structured values (a work authorization entry, a role with dates) |
| `code` | The normalized value for choice keys: an EEO code from `autoapply/basics.py CHOICES` (`female`, `decline`), an ISO country code, a yearly salary in whole units |
| `question` | The wording it answered, when it came from a form or an ask |
| `words` | The person's own words, when they differ from the value ("I'm Ghanaian, on OPT"). Never kept for EEO keys: the code and the chosen option are enough |
| `source` | `you`, `card`, `onboarding`, `ask_bar`, `ask_telegram`, `late_telegram`, `extension`, `resume`, `chat`, `telegram_chat`, `drafted`, `derived`, `migration` |
| `source_ref` | Where exactly: application id, ask id, diary file and line, document name, or the legacy row (`answers.db:@salary`) |
| `confidence` | 1.0 said or chosen by the person; 0.9 read from their resume; 0.8 drafted from the profile; 0.7 extracted from chat |
| `confirmed` | 1 when the person saw it and kept it |
| `status` | `active`, `replaced`, `needs_check` |
| `check_reason` | For `needs_check`: `raw_reply` (a whole chat reply saved as an answer), `conflict`, `expired`, `unconfirmed_legal` |
| `sensitivity` | `normal`, `legal`, `eeo`. Set from the key registry, never by the caller |
| `valid_from`, `valid_to` | When it became true and when it stops (an OPT end date, a past role) |
| `replaced_by` | The newer fact's id |
| `uses`, `last_used` | How often it filled a form, kept from the answer library |
| `created_at`, `updated_at` | Dates shown on the memory page |

Indexes: `(person, key, subject, status)` and `(source_ref)`.

Single-valued keys (salary, gender) have at most one `active` row per subject. Multi-valued keys
(citizenship, skills) can have several.

### 5.2 The key registry: `applicant/keys.py`

A list in code, not a table. Each key has: type (text, money, date, yes_no, choice, country,
country_list, json), sensitivity, single or multi, the allowed choices, a reconfirm window, the
ask wording, and the legacy names it maps from.

| Group | Keys | Sensitivity |
|---|---|---|
| Contact | `name`, `email`, `phone`, `address.street`, `address.city`, `address.state`, `address.postal_code`, `address.country`, `@linkedin`, `@portfolio`, `github`, `@preferred_name` | normal |
| Work and legal | `workauth` (subject = country, the entry from `autoapply/workauth.py`), `@citizenship` (multi), `citizenship.complete` (yes when the list holds every country), `permanent_resident` (subject = country), `@over_18`, `@previously_worked` (subject = company), `security_clearance` | legal |
| Preferences | `@salary` (with currency and period; subject = country when not US), `@start_date`, `@notice_period`, `relocate`, `remote`, `@heard_about`, `@years_experience` | normal |
| EEO | `@gender`, `@sex`, `@pronouns`, `@hispanic_latino`, `@race`, `@ethnicity`, `@veteran`, `@disability`, `@eeo_decline` (the standing decline) | eeo |
| Career (open facts) | `role`, `school`, `project`, `skill`, `certification`, `language` (subject = item id) | normal |
| One question | `q:<normalized wording>`: an answer to a question no canonical key covers, keyed the way `answers.normalize()` keys it today | normal, unless its wording classifies as legal or EEO |
| Chat notes | `preference`, `constraint` (subject = the key `distill_memory` gave), migrated from `memory_facts` | normal |

**Item ids.** A role is `role:<org>|<title>`, normalized the way `ui/session.py _role_key` does.
Schools and projects use their names. An item id survives edits: renaming a role in the builder
calls `rename_item()`, which updates the fact's subject and every choice that names it.

### 5.3 `rule` (choices)

| Column | Meaning |
|---|---|
| `id`, `person` | |
| `action` | `show` (always on the resume), `hide` (never), `lead` (always shown, full bullet budget, named first in the summary), `prefer` (when both fit, show the first, not the second), `apart` (never both on one resume) |
| `items` | JSON list of item ids. `prefer` and `apart` take two |
| `scope_kind` | `all`, `job_family`, `industry`, `company_type`, `company` |
| `scope_value` | A value from the fixed vocabularies in section 6 (`Finance & Insurance` as NAICS prefix `52`, `startup`, `Data & AI`, or a company key) |
| `words` | The person's words or the line they answered ("Hide Viva for other bank and finance jobs too?" Yes) |
| `source`, `source_ref` | `builder`, `chat`, `telegram`, `memory_page`, `migration` |
| `status` | `active`, `paused` (an item it names no longer exists, or the person paused it), `replaced` |
| `times_applied`, `last_applied_at` | Shown on the memory page so a stale rule is easy to spot |
| `created_at`, `updated_at` | |

### 5.4 History tables

- **`application`**: one row per application (id = the `cv_record` id): company, role, url,
  job family, industry, company type, `shown` (the item ids on the resume), `rules` (the rule ids
  that changed what was shown), created and submitted dates. The resume itself stays in
  `cv_record`; this table is the index the brain reads.
- **`fill`**: one row per form field the agent filled from memory: application id, label, key,
  fact id, origin (`library`, `workauth`, `eeo_decline`, `derived`, `ask`, `model`), time. It
  never copies the value: the fact holds it.
- **`event`**: an audit line per change: fact added, replaced or forgotten; rule added, applied
  or deleted; ask sent or answered; document added; migration; erase. It holds ids and kinds,
  never EEO values.
- **`document`**: each file the person shared (resume, transcript, note, photo): name, kind,
  where it is stored (`data/uploads/`), channel (app or Telegram), date, and how many facts came
  from it. Forgetting a document can also forget the facts that only it supported.
- **The diary**: `data/palace/transcripts/<person>.jsonl` as today. A turn that produced a fact
  is referenced from that fact's `source_ref` (`diary:default.jsonl:212`).

### 5.5 Caches

- **`label_map`**: a cleaned label plus a hash of its options, mapped to a key, `none` (the
  answer belongs to one job) or `ask`. Columns: `how` (`rule`, `model`, `person`), `confirmed`,
  dates. Each wording is worked out once.
- **`company_class`**: company key to industry (NAICS prefix and label), company type, and the
  source of each (`naics`, `jobtags`, `model`, `person`).
- **`meta`**: schema version, migration report (counts only), whether the brain is active, and
  whether legacy mirroring is on.

## 6. Matching a choice to a job

Every job gets three labels once, cached on the job and in `company_class`:

1. **Job family** from the title: `sourcing/filters.py role_family()` (Data & AI, Engineering,
   Product & Program, Design, Sales, Marketing, Healthcare, Finance & Accounting, People / HR,
   Operations), sharpened by the top tag from `sourcing/jobtags.py job_tags(title, jd)`. No model.
2. **Industry** of the employer, in this order:
   - the sponsor record's NAICS code (`sourcing/sponsors.py lookup()` then `naics_industry()`).
     Most of our jobs are at sponsors, so this usually settles it with no model call;
   - the JD's tags (`Banking & Lending` and `Investment & Trading` mean Finance & Insurance);
   - else one small model call per new company (below).
   Industry is stored as a NAICS prefix, so a rule for `52` (Finance & Insurance) matches a bank
   (`5221`) and an insurer (`524`), while a rule for `522` (banks and lenders) matches only banks.
3. **Company type**: `startup`, `scaleup`, `established`, `public_sector`, `nonprofit`,
   `academic`, or `unknown`. No public data settles this, so it comes from the same small model
   call, which reads the company name and the first 1,500 characters of the JD ("seed stage",
   "Series B", "Fortune 500"). It sends no personal data. The answer is cached per company. The
   person can correct it on the memory page, and their answer wins for good.

The small call runs only when a rule needs the label to decide something. A person with no
company-type rules never pays for it.

**Which rules apply.** All active rules whose scope matches the job. When two rules disagree
about the same item, the more specific scope wins: company, then company type, then industry
(a longer NAICS prefix beats a shorter one), then job family, then all jobs. Inside one level,
the newer rule wins. The builder says which rule won ("For bank jobs you show Stanbic. That beat
your data jobs choice. Change").

**How rules change the resume.** In `tailoring/assembler.py select_roles_for_jd`, before the
word-overlap scoring:

1. `hide` items are removed.
2. `show` and `lead` items are always kept and count toward the four-role limit.
3. `prefer` keeps the first item when both would be shown.
4. `apart` keeps the better-scoring item of the pair, unless a `prefer` rule names one.
5. The scorer fills the remaining places as today.

Chronological order is kept, as the function already promises. `lead` does not move a role up
the page; it gives it the full bullet budget and makes the summary name it first. If pinned
items alone exceed the limit, the most recent ones are kept and the builder says which rule
could not be honored. A rule only ever chooses among the person's own items. It never adds
words.

The rules also go to `select_for_cv` as plain text, so its "why" explanation agrees with what
code did.

## 7. Recall: answering without asking

### 7.1 A form question (the apply agent)

1. **Clean the label.** `answers.clean_label()` strips "Error: The field ..." and required
   markers. Already shipped in PR #141.
2. **Find the key**, cheapest first:
   - contact keys (`planner.contact_key`), work authorization (`workauth.classify`), the
     canonical patterns (`answers.normalize`), the EEO classifiers (`autoapply/eeo.py`);
   - the `label_map` cache;
   - (Phase 4) one batched model call per page for labels still unmapped. It sees labels,
     options and key names only, never values. It may answer `none` (belongs to one job) or
     `ask`.
3. **Read the current fact**: `active`, inside its valid dates, for the right subject (the job's
   country for `workauth` and salary, the company for `@previously_worked`).
4. **Derive by code, never by the model**, and only from confirmed facts:
   - "Are you a US citizen?" is No for F-1 status (`workauth.is_citizen` already does this);
   - "Citizen of Cuba or Iran?" is No only when `citizenship.complete` is yes;
   - the export-control country is the citizenship when there is exactly one;
   - years of experience can be computed from role dates, shown once for the person to confirm.
   EEO answers are never derived.
5. **Fit the form's options** with `planner.match_option` and `eeo.choose`. Exactly one fit, or
   ask.
6. **Fill or ask** by sensitivity:
   - EEO: only the person's own answer or a decline;
   - legal: only confirmed facts at confidence 1.0, or a code rule over them;
   - normal: confidence 0.9 or more and not expired. From 0.7 to 0.89 it becomes a one-tap
     confirm inside the ask, never a silent fill.

Every fill writes a `fill` row, so the memory page can say "Filled from memory on 3
applications".

### 7.2 When the agent asks

Only for a required field when one of these is true:

- no key fits, or the key has no current value;
- the value is below its threshold, or `needs_check`;
- two current values disagree (a guess never beats something the person said);
- the value expired (an OPT end date passed; a salary older than six months gets "Still $X?");
- the value does not fit the job (a USD salary on a UK job);
- no option on the form fits;
- the model mapped a new wording to a legal or EEO key (confirmed once, then cached).

Optional fields with no answer stay blank. All of a page's questions go in one message with the
form's options as buttons (today's `asks.prompt_text` and `telegram_ask`). The reply is read by
`asks.read_reply` and saved under the key, with the question and the options in the event log.

### 7.3 Asking only what we never have

- The onboarding basics step and the "Answers every form asks" card show only keys with no
  active fact.
- The intake gate (`ui/session.py _effective_essentials`, `_missing_required`) checks brain
  facts before asking for a date, a location or a contact detail.
- The agent's re-ask list (`shell-electron/agent.js`, when it gets stuck) drops labels the
  server already answered.
- A question the person skipped is asked once, the first time a form needs it, then saved.

### 7.4 The resume builder

- Identity and contact come from facts, so a corrected phone number is corrected everywhere.
- Roles, schools and projects come from the profile material, which in Phase 6 is assembled
  from open facts (`_memory_first_profile`, finishing the August plan).
- Choices are applied by scope (section 6).
- `preference` and `constraint` facts replace `memory_facts` as the input to
  `_apply_memory_selection` and `_avoid_token_sets`.
- Career highlights recall uses a plain keyword search over the diary (section 12).

## 8. Learning choices

A choice is never learned silently. It always comes from a tap.

### 8.1 In the resume builder

Phase 5 adds a "Show on this resume" switch to each role and project on the builder's edit page,
and "Swap for ..." on roles whose dates overlap. When the person changes what is shown, one line
appears under the preview, at most once per build:

- hiding: "Hide Viva for other bank and finance jobs too?"
- showing: "Show BlackOrigin on other startup jobs too?"
- swapping: "Use Stanbic instead of Viva for other bank and finance jobs?"

Buttons: **[Yes, bank and finance jobs]** **[Only Acme Bank]** **[Just this time]**.

The scope offered is the job's most specific known label: industry when known, else company
type, else job family. "Just this time" is remembered for that item and that label for 30 days,
so the line does not come back on the next similar job.

### 8.2 In chat (app and Telegram)

`distill_memory` (`llm/anthropic_client.py`, `llm/base.py`) already reads intake chat for facts.
Its output gains `rules`: action, the items as the person named them, and the scope in their
words. Code then:

1. resolves item names to item ids (an exact org or project match among the person's own
   items; when two fit, it asks which);
2. resolves the scope words with a fixed table (bank, banking, finance, fintech to NAICS `52`;
   startup to `startup`; a company name to `company`);
3. shows a chip: "For bank and finance jobs: show Stanbic, not Viva. **[Save]** **[Not quite]**".

Nothing is saved until the tap. On Telegram the chip is a message with two buttons. This keeps
PR #143's promise: the chat says "Saved" only after code saved it.

### 8.3 Overlapping roles

The first time a resume would show two roles whose dates overlap, the builder asks once: "You
were at Stanbic and Viva at the same time (2020). Show both, or one per job?" **[Both]**
**[One per job]**. "One per job" saves an `apart` rule for all jobs. Which one wins on a given
job then comes from `prefer` rules learned in 8.1, else from the scorer.

### 8.4 On the memory page

The Choices section has "Add a choice": pick items, an action and a scope from lists. No free
text to parse.

## 9. EEO and legal safety

- **Sensitivity is fixed in code.** `applicant/keys.py` sets it per key. `applicant/store.py`
  sets the column itself and ignores what a caller passes.
- **Where an EEO value may come from.** Only the person's own direct choice: the basics card,
  the onboarding step, Your answers, or an ask answered with a button or a typed reply matched
  locally. The store refuses EEO writes from `chat`, `telegram_chat`, `resume`, `drafted`,
  `derived` and the model. Never from a name, a photo, a nationality or a school.
- **Where a legal value may come from.** Anything may propose one. Only a direct answer or a
  confirm makes it `active` at 1.0. A proposal from the resume or chat waits as `needs_check`
  with reason `unconfirmed_legal`, and it never fills a form.
- **Never to the model or the broker.** Model payloads carry labels, options and key names. A
  guard (`applicant/redact.py for_model()`) runs on every payload the agent and the label
  mapper build, and a test scans captured prompts for every stored EEO and legal value.
- **Telegram.** EEO asks go out with buttons. A typed EEO reply is matched locally
  (`asks._accept`) and never sent to the model, as today. Telegram never echoes an EEO value:
  it says "Saved." The assistant summary says "saved" or "not saved" for EEO questions, never
  the value (PR #143). The broker queue holds a tap or a reply until the app reads it (deleted
  on read, or after 7 days); Phase 3 checks that a button's callback carries the ask id and the
  choice number, not the words.
- **Declines.** "Prefer not to say" is a stored answer per key (`code = decline`). The standing
  `@eeo_decline` applies to every EEO key with no answer of its own. A form with no decline
  option and no saved answer means ask, never a real value. In every EEO ask the decline choice
  comes first (`asks.order_options`, as today).
- **The handover lists legal fields filled from memory**, so the person sees them before
  sending. The ops already carry a `sensitive` flag.
- **Logs never carry values.** Counts, keys and ids only.

## 10. Privacy, Export and Erase

- Everything stays in the data folder. The broker sees only labels, options and key names (label
  mapping), and a company name with public JD text (company type).
- Data stays unencrypted on disk, as today. Disk encryption is the operating system's job.

**Export** (one zip from the memory page) holds everything the app knows about the person:

- `applicant.db` as JSON: facts with their replaced history, rules, applications, fills, events,
  documents, label map, company classes;
- the diary JSONL files;
- the legacy stores while they exist: answer library rows, `app_prefs.json`,
  `conversation_memory`, `memory_facts`;
- the interview files, `aa_learn.json`, `linkedin_review.json`;
- the shared documents, when the person ticks "Include my files".

**Forget one thing** (a fact, a choice, a document):

- deletes the row and its replaced history, and the `fill` rows that point to it;
- deletes the mirrored copy in the legacy stores while mirroring is on;
- for a document, offers to forget the facts only that document supported;
- offers "Also delete the chat lines where I said this", which rewrites the diary without the
  lines in the fact's `source_ref`.

**Erase my memory** deletes `applicant.db`, the diary and the old palace store, `answers.db`,
the answers in `app_prefs.json`, the `conversation_memory` and `memory_facts` rows, the
interview files, `aa_learn.json`, and the migration backups (`data/brain-backup-*`). A second
tick, "and my resumes and applications", also deletes `cv_record` rows and their files. Today's
erase misses `answers.db`, `app_prefs.json` and the interview files; Phase 1 fixes that,
because the brain now holds copies of them.

## 11. Migration from the five stores

Runs once at start-up in Phase 1. Safe to run again. Deletes nothing.

**What goes where**

| Source | Becomes |
|---|---|
| `answers.db` rows with a canonical key | `fact` with the same key, `source` kept (`you` or `drafted`), `uses`, `last_used` and dates kept, `source_ref = answers.db:<key>` |
| `answers.db` rows keyed by wording | `fact` with key `q:<that key>` and the question kept; a `label_map` row for the wording |
| Rows `answers.unusable()` rejects (whole replies, "what are the options?") | `fact` with `status = needs_check`, reason `raw_reply`. Never used to fill, shown on the memory page. Nothing is deleted |
| `app_prefs.json work_auth` | one `workauth` fact per country, confirmed |
| flat prefs answers (`desired_salary`, `earliest_start`, `hear_about_us`, `years_experience`, `over_18`, `willing_to_relocate`) | facts under `@salary`, `@start_date`, `@heard_about`, `@years_experience`, `@over_18`, `relocate`, `source_ref = app_prefs.json:<field>` |
| `eeo_decline` | `@eeo_decline` |
| `conversation_memory` profile | open facts: contact at 0.9, each role, school and project with its dates, skills. Source `resume`. The profile JSON itself is untouched and stays the resume material until Phase 6 |
| `memory_facts` | `preference` and `constraint` facts at their own confidence, source `chat`. A fact row whose key names a canonical key is proposed for that key as `needs_check` |
| MemPalace transcripts | indexed in place: one `event` per file with its line count. The leftover `autoapply_*` scratch transcripts are marked scratch, not deleted. The ChromaDB `store/` folder is not touched |

**Disagreements are kept, not settled.** Phase 1 changes no answer. The two salaries stay two
facts, and lookup ranks them exactly as `AnswerLibrary.best()` does today. Phase 3 turns the
disagreement into one question to the person.

**Steps**

1. **Back up.** Copy `answers.db`, `app_prefs.json` and `resume_agent.db` to
   `data/brain-backup-<date>/`.
2. **Dry run.** Build the plan in memory and count: per source, per outcome (active, needs_check,
   skipped empty). Counts only, never values. Available as
   `python -m applicant.migrate --data <dir> --dry-run` and `GET /api/brain/migrate?dry_run=1`.
3. **Apply** in one transaction.
4. **Verify.** For every legacy key, the brain's lookup must return the same answer the legacy
   library returns. For prefs, the brain's work authorization view must equal
   `workauth.load(prefs)`. Any mismatch rolls the transaction back, logs counts, leaves the app
   on the legacy stores, and retries on the next start (three tries at most).
5. **Activate.** `meta.brain_active = 1`. The report (counts) is stored in `meta` and shown on
   the memory page from Phase 6.

**Mirroring.** While `meta.mirror_legacy = 1`, every brain write is copied one way into the
legacy store it came from: library facts to `answers.db`, prefs facts to `app_prefs.json`. The
browser extension and any old code path keep working, and rollback loses nothing.

**Rollback.** `python -m applicant.migrate --rollback`, or the environment variable
`SPONSORJOBS_BRAIN=0`, sets `brain_active = 0`. Readers go back to the legacy stores, which hold
every write thanks to the mirror. The brain file stays for diagnosis.

**Retiring the legacy stores.** Only in Phase 7, after one release with no rollback used. Even
then the files are kept read-only until the person erases them.

## 12. Removing the MemPalace library, keeping the diary

What we keep: `intake/palace_memory.py`'s transcript layer (append a turn, load the history,
ingest a note, erase). It moves to `intake/diary.py`, and `PalaceMemory` stays as a thin alias
for one release so imports do not break. The files stay at `data/palace/transcripts/` so
nothing moves on disk.

What we remove: the `mempalace==3.5.0` dependency in `pyproject.toml`, `_mempalace()`, the
`RESUME_AGENT_PALACE_INDEX` switch, and the ChromaDB index. The `store/` folder is left on disk
(deleting nothing) and removed by Erase.

What replaces `recall()`: a plain keyword search over the diary (word overlap with the job's
role, company and the first 200 characters of the JD, newest first), in the standard library. It
feeds the same two callers: `ui/session.py` (`self.recalled` for `select_for_cv` and `converse`)
and `_career_highlights_for_job` in `ui/app.py`. In the installed app this is strictly better
than today, where recall returns nothing.

Numpy is coming back to the build for photo OCR. Removing MemPalace means ChromaDB can never
quietly start running when numpy appears.

Install size: the installed engine carries `grpc` (19 MB) and `tokenizers` (9 MB) native
libraries plus the ChromaDB, OpenTelemetry and MemPalace Python code, all pulled in only by
MemPalace. `onnxruntime` (70 MB) stays: OCR uses it. Phase 2 measures the real saving on a
build. `NOTICES.md` loses the MemPalace and ChromaDB entries and their exclusive dependencies.

## 13. The memory page

The page that today shows the profile form becomes "What SponsorJobs knows about you"
(`ui/templates/index.html`, the existing page title). It replaces the profile form, which
finishes the August plan (steps 3b and 4: memory first by default, then remove the form).

Sections:

- **About you**: contact details.
- **Work and visa**: work authorization per country, citizenships, prior employers.
- **Form answers**: salary, start date and the rest, plus the one-question answers.
- **Self-identification**: shown as "Saved" or "Prefer not to say"; the value appears only when
  the person taps Show. Local only.
- **Career**: roles, schools, projects, skills.
- **Choices**: each rule in plain words ("Bank and finance jobs: show Stanbic, not Viva. Used on
  4 resumes."), with Edit, Pause and Delete, and "Add a choice".
- **Needs a look**: conflicts, raw replies from older runs, expired values, unconfirmed legal
  facts. Each has one button that settles it.
- **History**: applications with what was shown and answered ("Acme Bank, Analyst, Oct 30:
  showed Stanbic and BlackOrigin; filled salary and citizenship from memory; gender: Prefer not
  to say"), the documents, and the chat diary.

Every row shows the value, where it came from in words ("You, on Telegram, Oct 28"; "From your
resume, Oct 5"), the date, and **Edit** and **Forget**. Your answers stays reachable as a door
into the Form answers section, so the Auto-apply page's links keep working.

UI rules: invoke the `tailor-ux` skill first; audit against Home, Resumes and Jobs; verify in
Kofi's ~1893x1183 window at 76% zoom and at 100%, light and dark; full width, no sideways
scroll; a back link, not a close button.

## 14. Costs

**Install, disk, RAM**

- New dependencies: none. Python's `sqlite3` only.
- Install size: about 30 MB smaller after Phase 2 (MemPalace's native libraries), measured then.
- Disk: under 1 MB a year for facts, choices and history (`answers.db` is 20 KB today). The diary
  grows by about 1 KB per ten chat turns, as today.
- RAM: one SQLite connection and small caches, a few MB. No embedder, no model in memory.

**Model calls per application** (broker prices on 2026-10-30, per million tokens in and out:
Haiku 4.5 $1/$5 on the free plan, Sonnet 5.5 $2/$10 on the paid plan; token counts are
estimates)

| Call | When | Size | Haiku 4.5 | Sonnet 5.5 |
|---|---|---|---|---|
| Reading facts and choices | every application | none | $0 | $0 |
| Company type | a new company, and only when a rule needs it | ~1,600 in, 60 out | $0.002 | $0.004 |
| Label mapping (Phase 4) | a page with wordings never seen before | ~2,600 in, 300 out | $0.004 | $0.008 |
| Reading a typed ask reply | per ask, already exists | ~1,200 in, 150 out | $0.002 | $0.004 |
| Choices from chat | adds ~300 tokens to the existing `distill_memory` call | | under $0.001 | under $0.001 |

An early application costs about 1 to 2 cents more than today. Once wordings and companies are
cached, close to $0. Phases 1 to 3 add no model calls at all. For scale, the form agent already
makes up to 40 calls per application.

## 15. Phased build plan

Each phase is one PR that ships on its own, with its tests and the e2e checks it must pass. No
phase starts before the one before it is merged. After each merge, Kofi batches fixes before
asking for an installer build.

Before Phase 1, already in flight: PR #143 (Telegram late answers and honest chat) merged; the
onboarding step for the seven basics; numpy back for OCR with the palace index forced off.

Every phase runs: the tests it names, the existing tests it touches, `tests/test_no_dashes.py`,
and `node shell-electron/e2e-agent.mjs` with every existing check passing. No full suite run is
needed for a phase that touches only the files listed.

### Phase 1: the store, migration, and the apply agent's readers (zero behaviour loss)

Goal: one store under the agent, with every answer exactly as today.

Ships:

- the full schema (section 5), the key registry, the store with the sensitivity guard;
- the migration with dry run, verify, mirror and rollback (section 11);
- `BrainLibrary`, a class with the same methods as `AnswerLibrary` (`lookup`, `save`, `best`,
  `candidates`, `get_key`, `update`, `delete`, `mark_used`, `list`, `count`, `close`) over the
  `fact` table, reusing `answers.normalize()` and the same ranking;
- `_answer_lib()` returns `BrainLibrary` when the brain is active. Every reader and writer that
  goes through it switches at once: the planner (`plan_known`), `_known_answers_for`, the agent
  asks, Your answers, the basics card, screening edits, extension corrections, drafted answers,
  Telegram late answers;
- `_work_auth()` and the `eeo_decline` reads come from the brain; `_save_prefs()` writes the
  brain and mirrors to `app_prefs.json`;
- Export and Erase cover the brain, `answers.db`, `app_prefs.json`, the interview files and the
  backups;
- `GET /api/brain/status` (counts only) and `GET /api/brain/migrate?dry_run=1`.

Files:

- new: `applicant/__init__.py`, `applicant/store.py`, `applicant/keys.py`,
  `applicant/library.py`, `applicant/migrate.py`, `applicant/redact.py`
- changed: `ui/app.py` (`_answer_lib`, `_work_auth`, `_save_prefs`, `_load_prefs` callers in
  `autoapply_plan` and `_known_answers_for`, `/api/memory/export`, `/api/memory/erase`, the new
  `/api/brain/*` routes), `autoapply/answers.py` (move the row ranking into a function both
  classes call; no behaviour change), `packaging/tailor.spec` (hidden imports for `applicant.*`),
  `shell-electron/e2e-agent.mjs`, `docs/apply-agent.md` (where known answers come from),
  `docs/applicant-brain.md` (status)
- new tests: `tests/test_brain_store.py`, `tests/test_brain_migrate.py`,
  `tests/test_brain_parity.py`

Tests:

- store: one active row per single-valued key; replace keeps history; hard delete leaves nothing
  (checked with a raw `SELECT` after the delete); EEO writes from `chat`, `resume`, `drafted` or
  `derived` are refused; sensitivity cannot be set by the caller.
- migration: dry run writes nothing and reports counts; apply then rerun gives the same counts
  and no duplicates; every legacy file is byte-identical afterwards apart from mirrored writes;
  the Lam Research raw replies become `needs_check` and are not deleted; a forced verify mismatch
  rolls back and the app stays on the legacy stores; `--rollback` returns readers to the legacy
  stores with every write made after migration present.
- parity: the planner and `_known_answers_for` give identical output with `AnswerLibrary` and
  with `BrainLibrary` over fixture libraries: canonical keys, wording keys, the two disagreeing
  salaries, the Lam Research labels ("Error: The field What is your annual salary
  requirement?"), EEO with and without the standing decline, citizenship and export control.
- existing tests unchanged and passing: `test_answer_library.py`, `test_answers_library.py`,
  `test_autoapply_planner.py`, `test_agent_asks.py`, `test_form_basics.py`, `test_workauth.py`,
  `test_extension_answers.py`, `test_autofill.py`, `test_p1_memory.py`.

E2E (`shell-electron/e2e-agent.mjs`):

- every existing check passes, including "the bar asks for the form answers first" and "the
  answer is saved for next time";
- new: the seed step writes a legacy `app_prefs.json` (US authorized, needs sponsorship) and a
  legacy `answers.db` row before the engine starts; check "the brain migrated the legacy
  answers" (`/api/brain/status` counts) and "the legacy files are still there";
- new: in the ask run, the agent never asks work authorization or sponsorship (they came from
  the migrated prefs);
- new: a sixth job (`?mustanswer=1`) after the ask run; check "the years answer is reused on the
  next application without asking".

Done when: parity tests pass, e2e passes, and a dry run on a copy of Kofi's data folder (copied
to a temp folder, never the live one) reports counts with zero verify mismatches.

### Phase 2: remove the MemPalace library, keep the diary

Goal: no dead semantic layer, a recall that works in the installer, a smaller install.

Ships: section 12. The diary module, keyword recall, the dependency and its notices removed,
the `store/` folder left on disk and covered by Erase. Diary turns that produced a fact get
their `source_ref`.

Files:

- new: `intake/diary.py`
- changed: `intake/palace_memory.py` (alias to the diary for one release), `ui/session.py`
  (recall), `ui/app.py` (`_palace`, `_career_highlights_for_job`, `memory_erase`, Telegram chat
  history), `pyproject.toml`, `NOTICES.md`, `packaging/tailor.spec`, `tests/conftest.py` (drop
  `RESUME_AGENT_PALACE_INDEX`)
- tests: `tests/test_palace_memory.py` (rewritten for the diary), `tests/test_career_highlights.py`,
  `tests/test_career_memory.py`, `tests/test_memory_first_tailoring.py`,
  `tests/test_derive_from_memory.py`; new `tests/test_diary_recall.py` (keyword recall ranks the
  right turns; no import of `mempalace` or `chromadb` anywhere: a test greps the source tree)

E2E: all `e2e-agent.mjs` checks pass. Plus a packaged-build check: the engine starts with numpy
present and `import chromadb` fails (the package is gone), and the measured install size is
recorded in the PR.

### Phase 3: every writer writes facts, the resume side reads them, ask only what we never have

Goal: one truth for everything the person tells us, wherever they say it.

Ships:

- writers: intake chat extraction (`_distill_facts`) writes facts at 0.7 (legal ones as
  `needs_check`, EEO dropped before the model call using `notify/assistant.carries_eeo`); resume
  import and Telegram file imports write open facts at 0.9 with a `document` row; the details
  step and `/api/profile/from_cv` write contact facts; extension corrections write facts;
- readers: the intake gate checks facts before asking; `applicant_from_record` gets city, state,
  postal code and country from facts (the gap that left them empty); `_apply_memory_selection`
  and `_avoid_token_sets` read `preference` and `constraint` facts; the Telegram assistant
  summary reads the brain;
- conflicts become one question: the salary disagreement shows as "Forms say $X (March); you
  just said $Y. Which one?" on the memory page's Needs a look list and in the next ask;
- `@previously_worked` becomes per company (subject = company key);
- Telegram button callbacks carry the ask id and choice number only (checked, fixed if not).

Files: `ui/session.py`, `ui/app.py` (`_learn_from_corrections`, `/api/profile/details`,
`/api/profile/from_cv`, `_assistant_summary`, `_late_save`), `intake/cv_import.py`,
`autoapply/fields.py`, `autoapply/planner.py` (`@previously_worked` subject),
`autoapply/asks.py` (save under key with options), `notify/assistant.py`, `notify/telegram.py`,
`shell-electron/agent.js` (re-ask list drops labels already answered), `applicant/store.py`
(conflict detection).

Tests: new `tests/test_brain_writers.py` (each writer lands one fact with the right source and
confidence; chat never writes EEO; a lower-confidence value never replaces a confirmed one, it
opens a conflict); `tests/test_profile_details_step.py`, `tests/test_intake_chat.py`,
`tests/test_extension_profile.py`, `tests/test_telegram_chat.py`, `tests/test_agent_asks.py`
extended.

E2E: all checks pass, plus new: "the contact page fills city, state and ZIP from memory"
(add those fields to the mock contact page in `tests/fixtures/forms/mock_ats.js`); "a value
typed in intake chat is not asked again on the form".

### Phase 4: new wordings, legal confirm once, expiry

Goal: a question asked in new words still finds the saved answer, safely.

Ships: the batched label-mapping call (labels, options and key names only, through
`applicant/redact.py`), the `label_map` cache, the one-tap confirm the first time a new wording
maps to a legal or EEO key, the expiry rules (OPT end date, salary older than six months, start
date in the past), and the USD-on-a-UK-job check.

Files: new `applicant/mapper.py`; changed `autoapply/planner.py` (step 2 of 7.1),
`ui/app.py` (`_known_answers_for`, the ask route), `llm/anthropic_client.py` and `llm/base.py`
(`map_labels` and its fake), `autoapply/asks.py` (the confirm item).

Tests: new `tests/test_brain_mapper.py` (cache hit makes no call; a payload never contains a
stored value; "US person" is not mapped to "US citizen" without a confirm; `none` keeps a
per-job question out of memory); `tests/test_brain_expiry.py`.

E2E: new mock question in a wording no pattern knows ("What base pay are you targeting for this
position?"): check "the agent fills the saved salary after one mapping call" and "the second run
makes no mapping call" (the fake model counts calls).

### Phase 5: choices

Goal: "show one of each depending on where I am applying".

Ships: job labels and `company_class` (section 6); rules applied in `select_roles_for_jd` and
passed to `select_for_cv`; the builder's "Show on this resume" switch, "Swap for ...", and the
one-line ask (8.1); the overlapping-roles ask (8.3); rule proposals from chat and Telegram with
Save buttons (8.2); the `application` table filled with what was shown and which rules applied;
"Shown because ..." in the builder; `memory_facts` "do not mention" constraints offered as
`hide` rules for the person to confirm.

Files: new `applicant/scope.py` (labels, matching, precedence) and `applicant/rules.py`;
changed `tailoring/assembler.py` (`select_roles_for_jd`, `assemble_cv`), `ui/session.py`
(`_apply_memory_selection`, new `set_shown` and `swap_role` actions, `rename_item` on title
edits), `ui/app.py` (`/api/session/shown`, `/api/session/swap`, `/api/brain/rules`, the Telegram
callback for a rule proposal), `llm/anthropic_client.py` and `llm/base.py` (`distill_memory`
returns rules; `classify_company`), `sourcing/filters.py` and `sourcing/jobtags.py` (read only,
no change expected), `ui/static/app.js`, `ui/static/styles.css`, `ui/templates/index.html`,
`ui/static/locales/*.json`, `notify/telegram.py`.

Tests: new `tests/test_brain_scope.py` (NAICS prefix matching; company beats industry beats
family; newer wins in a level; unknown company type never matches); `tests/test_brain_rules.py`
(hide, show, lead, prefer, apart on overlapping roles; pinned roles over the limit; a rule never
adds text; a renamed role keeps its rules; a deleted role pauses them); `tests/test_assembler.py`
and `tests/test_session_resume.py` extended. The one-page checks (compiles, one page, no new
overfull boxes) must still pass on every rule combination in the fixtures.

E2E: a new `shell-electron/e2e-choices.mjs` (same harness as `e2e-agent.mjs`, test model,
throwaway data folder): a profile with Stanbic and Viva overlapping in 2020; build for a bank
job, hide Viva, answer "Yes, bank and finance jobs"; build for a second bank job and check Viva
is hidden with no ask; build for a startup job and check Viva can show; delete the rule on the
memory page and check the next bank build shows the scorer's pick. Plus all `e2e-agent.mjs`
checks.

### Phase 6: the memory page replaces the profile form

Goal: no profile page, pure magic, finished.

Ships: section 13. The profile form (`buildProfileEditor`, `renderProfileEditor` in
`ui/static/app.js`) is removed. Tailoring reads the profile assembled from memory by default
(`_memory_first_profile` and `/api/memory/derived-profile`, unused since August, become the main
path). Your answers becomes a door to the Form answers section. Export as one zip; Forget with
"also delete the chat lines"; Erase with the resumes tick.

Files: `ui/static/app.js`, `ui/static/styles.css`, `ui/templates/index.html`,
`ui/static/locales/*.json`, `ui/app.py` (`/api/memory`, `/api/memory/export`,
`/api/memory/erase`, `/api/memory/fact/<id>` over the brain, `_memory_first_profile`),
`ui/session.py` (saved profile from memory), `applicant/store.py` (forget with diary lines),
`intake/diary.py` (remove lines), docs screenshots under `docs/design/memory/`.

Tests: `tests/test_profile_screen.py` and `tests/test_profile_editor.py` replaced by
`tests/test_memory_page.py`; `tests/test_memory_first_tailoring.py`,
`tests/test_derive_from_memory.py` extended; new `tests/test_brain_erase.py` (after Erase, no
file in the data folder holds any seeded value: a byte search across every file, backups
included).

E2E: all checks pass, plus new: "Forget the salary on the memory page, the next application
asks for it"; "Export holds the seeded salary and the diary"; screenshots at 1893x1183 and 76%
zoom, light and dark, attached to the PR.

### Phase 7: retire the legacy stores

Goal: one store in fact, not only in effect. Ships after one release with no rollback used.

Ships: mirroring off; `AnswerLibrary`, `memory_facts` and the prefs answers become read-only
importers used only by the migration; `/api/profile/autofill` (the browser extension) reads the
brain directly; the `PalaceMemory` alias and the rollback switch are removed. Legacy files stay
on disk, read-only, until Erase.

Files: `autoapply/answers.py`, `intake/memory.py`, `intake/palace_memory.py` (deleted),
`ui/app.py` (`_load_prefs` answer fields, `profile_autofill`), `applicant/migrate.py`,
`applicant/library.py`, `extension/` (no change expected; verified by
`tests/test_extension_answers.py` and `tests/test_extension_autofill.py`).

Tests: the parity suite now runs brain only; a test that no code path writes `answers.db` or the
answer fields of `app_prefs.json`.

E2E: all `e2e-agent.mjs` and `e2e-choices.mjs` checks pass, and `e2e-apply.mjs` (the in-app
pane fills from `/api/profile/autofill` first, so it proves that route reads the brain).

## 16. What we will not do

- Store answers in a graph database, Cognee, Graphiti, Mem0, Letta or MemPalace's graph.
- Let a model guess an EEO or legal answer, or save anything without code deciding.
- Learn a choice without a tap.
- Delete a saved answer during migration, or without the person's OK.
- Send EEO values to the broker or the model, or echo them on Telegram.
- Build every phase at once. That is how the August plan stalled.

## 17. Not certain yet

- Token counts are estimates. The real figures come from Phase 4 and 5 runs.
- The install saving from Phase 2 is an estimate from the installed engine's folders.
- Company type from a JD will be wrong sometimes. The person's correction wins, and a wrong
  label only changes which choice applies, never what a form says.
- Whether people want choices by company type at all is untested. If nobody uses them after a
  month, the small call stays off and only industry and family scopes remain.
- Data stays unencrypted on disk, as today.
