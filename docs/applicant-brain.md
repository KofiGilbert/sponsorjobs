# The applicant brain

> The build plan for one local memory that knows the person: what is true about them (facts),
> how they want to come across for each kind of job (choices), and what has happened (history).
> The resume builder and the apply agent both read it. Every place that learns something writes
> to it. Approved by the owner on 2026-10-30. Read this before touching `intake/memory.py`,
> `intake/palace_memory.py`, `autoapply/answers.py`, `autoapply/basics.py`, `autoapply/planner.py`,
> `_known_answers_for` or `_answer_lib` in `ui/app.py`, or the profile and Your answers pages.
>
> Status: Phases 1 to 5 built (the store, the migration and the apply agent's readers; the diary
> without MemPalace; every writer saves facts and the readers ask the brain first; new wordings,
> legal confirm once and expiry; choices). Phases 6 and 7 are plans. Changes made while building are marked
> "(Phase N build)" below.

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
| `store` | (Phase 1 build) The legacy home of the fact while the brain runs beside the older stores: `answers.db`, `app_prefs.json`, or empty for a brain-only fact. Each legacy view reads only its own facts, so every answer stays exactly what that store gave, and a write is mirrored into that file. Phase 3 joins the views into one truth |
| `confidence` | 1.0 said or chosen by the person; 0.9 read from their resume; 0.8 drafted from the profile; 0.7 extracted from chat |
| `confirmed` | 1 when the person saw it and kept it |
| `status` | `active`, `replaced`, `needs_check` |
| `check_reason` | For `needs_check`: `raw_reply` (a whole chat reply saved as an answer), `conflict`, `expired`, `unconfirmed_legal`; (Phase 1 build) `drafted_eeo` (an EEO answer the legacy library holds from a draft: kept, never created again) and `from_chat` (a chat fact proposed for a canonical key) |
| `sensitivity` | `normal`, `legal`, `eeo`. Set from the key registry, never by the caller |
| `valid_from`, `valid_to` | When it became true and when it stops (an OPT end date, a past role) |
| `replaced_by` | The newer fact's id |
| `uses`, `last_used` | How often it filled a form, kept from the answer library |
| `created_at`, `updated_at` | Dates shown on the memory page |

Indexes: `(person, key, subject, status)` and `(source_ref)`.

Single-valued keys (salary, gender) have at most one `active` row per subject (and, until Phase
3, per `store`: the library's `@salary` and the prefs file's `desired_salary` are two facts, as
they are two values today). Multi-valued keys (citizenship, skills) can have several. (Phase 1
build) A value the person forgets is overwritten on disk (`PRAGMA secure_delete`), not left in a
free page, and a third index `(person, store, status)` serves the legacy views.

### 5.2 The key registry: `applicant/keys.py`

A list in code, not a table. Each key has: type (text, money, date, yes_no, choice, country,
country_list, json), sensitivity, single or multi, the allowed choices, a reconfirm window, the
ask wording, and the legacy names it maps from.

| Group | Keys | Sensitivity |
|---|---|---|
| Contact | `name`, `email`, `phone`, `address.street`, `address.city`, `address.state`, `address.postal_code`, `address.country`, `@linkedin`, `@portfolio`, `github`, `@preferred_name`; (Phase 1 build) `address` and `location`, the whole line as the profile holds it today (the parts are filled in Phase 3); (Phase 3 build) every writer of an address also writes its parts, split by code (`applicant/facts.py split_address`), and a new city or state retires the parts the new line does not give (kept as history) | normal |
| Work and legal | `workauth` (subject = country, the entry from `autoapply/workauth.py`), `@citizenship` (multi), `citizenship.complete` (yes when the list holds every country), `permanent_resident` (subject = country), `@over_18`, `@previously_worked` (subject = company), `security_clearance`; (Phase 1 build) `@citizenship2`, the answer library's second-citizenship key, folded into `@citizenship` in Phase 3; (Phase 3 build) not folded yet: the basics card, the planner and the export screens read the two library keys, so the fold waits for Phase 7, when the library view goes | legal |
| Preferences | `@salary` (with currency and period; subject = country when not US), `@start_date`, `@notice_period`, `relocate`, `remote`, `@heard_about`, `@years_experience`; (Phase 1 build) `accept_required_terms` (`always`, or absent for the default `ask`: terms a site requires to apply, PR #146) | normal |
| EEO | `@gender`, `@sex`, `@pronouns`, `@hispanic_latino`, `@race`, `@ethnicity`, `@veteran`, `@disability`, `@eeo_decline` (the standing decline) | eeo |
| Career (open facts) | `role`, `school`, `project`, `skill`, `certification`, `language` (subject = item id) | normal |
| One question | `q:<normalized wording>`: an answer to a question no canonical key covers, keyed the way `answers.normalize()` keys it today | normal, unless its wording classifies as legal or EEO |
| Chat notes | `preference`, `constraint` (subject = the key `distill_memory` gave), migrated from `memory_facts`; (Phase 1 build) `fact` for a chat fact that names no canonical key | normal |

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
     `ask`. (Phase 4 build) `applicant/mapper.py`; the keys it may answer include the work
     authorization kinds as `workauth:<kind>` (`workauth:citizen`), all legal.
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
  guard (`applicant/redact.py for_model()`) runs on every payload the agent and the label mapper
  build, and a test scans captured prompts for every stored EEO and legal value.
  (Phase 1 build) Phase 1 ships the guard and the scan: `tests/test_brain_parity.py` captures
  what the fill route hands the model and finds no stored EEO or legal value outside the form's
  own labels and options. The guard works against the form's own text because the stored words
  are often the form's options ("Female", "Asian"); run as a filter over the agent's page view
  it would blank those options, so it is wired in with the label mapper (Phase 4), whose
  payload is built from scratch.
  (Phase 4 build) Two places now. The label mapper's payload goes through `for_model` with only
  the form's own options (and our key list) allowed: a label that carries a stored value ("Is
  your Ghana passport valid?") reaches the model as "[withheld]". The agent's page view gets the
  guard where it is safe: only a control's CHOICE is hidden (`autoapply/brain.py mask_values`): a
  select or combobox whose chosen value is a stored legal or EEO value reads `value="(filled)"`,
  and a radio group or checkbox whose chosen option is one reads `checked=hidden`. The options,
  the labels and the page's own text are never touched, so nothing the model needs to read the
  page is blanked. Before this, a filled "Country of citizenship" select showed the model
  `value="Ghana"`.
- (Phase 1 build) **A drafted EEO answer is never saved.** The legacy library kept a drafted
  answer to any question; the brain refuses one for an EEO key, so `BrainLibrary.save` returns
  None for it (nothing writes one today: drafted answers come from screening questions). An EEO
  row the legacy library already holds from a draft is migrated as `needs_check`
  (`drafted_eeo`) and handed out exactly as before until Phase 3 asks the person.
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

(Phase 1 build) In Phase 1, Export is still the JSON file `/api/memory/export` gives, with new
sections: `brain` (every table), `answers` (the library rows), `prefs`, `interviews`,
`aa_learn`, `linkedin_review` and `backups` (each backup's answers and prefs). The zip and
"Include my files" come with the memory page in Phase 6.

**Erase my memory** deletes `applicant.db`, the diary and the old palace store, `answers.db`,
the answers in `app_prefs.json`, the `conversation_memory` and `memory_facts` rows, the
interview files, `aa_learn.json`, and the migration backups (`data/brain-backup-*`). A second
tick, "and my resumes and applications", also deletes `cv_record` rows and their files. Today's
erase misses `answers.db`, `app_prefs.json` and the interview files; Phase 1 fixes that,
because the brain now holds copies of them. (Phase 1 build) Found while testing: a row deleted
from `resume_agent.db` (`memory_facts`, `conversation_memory`) can still sit in SQLite's free
pages. Phase 6's byte search (`tests/test_brain_erase.py`) needs `PRAGMA secure_delete` or a
`VACUUM` on that file too.

## 11. Migration from the five stores

Runs once at start-up in Phase 1 (`main()` in `ui/app.py`, and on the first read if the
engine was started another way). Safe to run again. Deletes nothing. (Phase 1 build) It reads
every legacy store read-only: the answer library from a temporary copy opened by
`AnswerLibrary` (so its rows carry the keys the app would give them today), the rest through
read-only SQLite connections.

**What goes where**

| Source | Becomes |
|---|---|
| `answers.db` rows with a canonical key | `fact` with the same key, `source` kept (`you` or `drafted`), `uses`, `last_used` and dates kept, `source_ref = answers.db:<key>` |
| `answers.db` rows keyed by wording | `fact` with key `q:<that key>` and the question kept; a `label_map` row for the wording |
| Rows `answers.unusable()` rejects (whole replies, "what are the options?") | `fact` with `status = needs_check`, reason `raw_reply`. Never used to fill, shown on the memory page. Nothing is deleted |
| `app_prefs.json work_auth` | one `workauth` fact per country, confirmed |
| flat prefs answers (`desired_salary`, `earliest_start`, `hear_about_us`, `years_experience`, `over_18`, `willing_to_relocate`) | facts under `@salary`, `@start_date`, `@heard_about`, `@years_experience`, `@over_18`, `relocate`, `source_ref = app_prefs.json:<field>` |
| `eeo_decline` | `@eeo_decline` |
| `accept_required_terms` | (Phase 1 build) `accept_required_terms` when it is `always`; read through the brain (`_terms_pref`, the basics card) and mirrored to `app_prefs.json` |
| `conversation_memory` profile | open facts: contact at 0.9, each role, school and project with its dates, skills. Source `resume`. The profile JSON itself is untouched and stays the resume material until Phase 6 |
| `memory_facts` | `preference` and `constraint` facts at their own confidence, source `chat`. A fact row whose key names a canonical key is proposed for that key as `needs_check` (Phase 1 build: reason `from_chat`; an EEO one is refused and counted as `refused_eeo`; a retracted row is skipped; any other fact row becomes a `fact` note) |
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

(Phase 1 build) The backup is made once, the first time (`meta.backup_dir`). The plan's counts
per source are: `answers.db` active and needs_check; `app_prefs.json` active;
`conversation_memory` contact, role, school, project, skill; `memory_facts` active,
needs_check, refused_eeo, skipped_retracted, skipped_empty; `transcripts` indexed and scratch.
`GET /api/brain/migrate` is always a dry run; `--apply` on the command line migrates (and
turns the brain back on after a rollback).

**Catch-up.** (Phase 1 build) While mirroring is on, each open of the library and each read of
the prefs answers first carries into the brain any change the legacy file holds that the brain
has not seen: a row written while `SPONSORJOBS_BRAIN=0`, a write by an older build or by code
that writes `answers.db` directly, a mirror write that failed. A row deleted there is forgotten
here. With the mirror working this changes nothing, and it means a rollback followed by a
return to the brain loses nothing either.

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

(Phase 2 build) What changed while building:

- **Recall rules.** `Diary.recall` searches only the person's lines and app notes (`user`,
  `note`), never the app's own questions (`agent`). Words are lower-cased, stop words and the
  words every job post uses ("experience", "team", "skills") are dropped, and a light suffix
  trim lets "dashboards" meet "dashboard"; tech tokens such as `c++`, `node.js` and `ci/cd` stay
  whole. The same text said twice is recalled once. Each hit is `{text, similarity, ref}`:
  `similarity` is the share of the query's words found, four words counting as a full match
  (0.25 per shared word), and `ref` is the diary line.
- **Career highlights threshold.** `_career_highlights_for_job` now keeps hits at
  `similarity >= 0.5`: at least two of the job's words in the line (or every word of a shorter
  query). One shared word is too weak to put in a resume unasked. The 0.3 of the embedder days
  meant nothing for word overlap.
- **Line refs.** `remember_turn` returns the line's `diary:<file>:<line>` ref (line counts are
  cached per file and checked against the file size, under a lock, so the Telegram thread and
  the chat agree). The session keeps the ref of the person's latest turn and `_distill_facts`
  appends it to the `memory_facts` provenance (`turn 12 · 2026-10-30 · diary:default.jsonl:212`).
  The migration takes that ref as the fact's `source_ref`; a row without one keeps
  `resume_agent.db:memory_facts:<type>:<key>`. Live writes of chat facts into the brain are
  Phase 3, and they take the same ref.
- **Erase** removes every diary file, including leftover `autoapply_*` scratch diaries, and the
  `store/` folder (`intake/diary.py` `erase_all`). Nothing else ever deletes `store/`.
- **The packaged build.** `packaging/launcher.py` no longer sets `RESUME_AGENT_PALACE_INDEX`
  (nothing reads it; a leftover value does nothing). `packaging/tailor.spec` keeps `chromadb`
  and `mempalace` in `excludes` as a guard for a build venv that still has them installed.
- **Install size.** The spec already left both out of the installed app since the OCR change
  (f89d4c3, 2026-10-30), so the installer itself shrank then, by about the 30 MB above, not in
  this phase. What Phase 2 removes is the dependency: a fresh `pip install` no longer pulls 59
  packages (about 246 MB in site-packages on macOS: kubernetes 80 MB, chromadb 58 MB, grpcio
  41 MB, tokenizers 10 MB, pygments 9 MB, hf-xet 8 MB, huggingface_hub 7 MB, mempalace 3 MB and
  smaller ones). Eight of them (pydantic, anyio, click, idna, h11 and their helpers) stay,
  pulled in by Flask and the model SDKs. The measured installer size is still to be recorded on
  the next build Kofi asks for.

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
- Install size: about 30 MB smaller in the installer (MemPalace's native libraries; taken by the
  OCR change's spec excludes), about 246 MB smaller for a source install (section 12).
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
| Label mapping (Phase 4) | a page with wordings never seen before | ~1,000 in, 100 out (Phase 4 build, measured by characters) | $0.0015 | $0.003 |
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

(Phase 1 build) Result on a copy of Kofi's data folder, 2026-10-30, counts only:
`answers.db` 13 active and 1 needs_check; `app_prefs.json` 3 active; `conversation_memory`
4 contact, 4 roles, 2 schools, 2 projects, 24 skills; `memory_facts` none; transcripts 1
indexed and 6 scratch. Apply: 0 verify mismatches, the three legacy files byte-identical
afterwards, a second apply added nothing, and the agent's known answers, work authorization
and decline were identical with the brain on and off.

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

(Phase 2 build) Also changed: `applicant/migrate.py` (a diary ref in a chat fact's provenance
becomes its `source_ref`), `packaging/launcher.py` (the index switch removed),
`requirements-broker.txt` (comment), `tests/test_packaged_ocr.py` (the frozen-app check now
asserts numpy present, `import chromadb` failing and the diary recalling), and the
`RESUME_AGENT_PALACE_INDEX` lines dropped from the tests that set it. The source-tree check
parses imports (a string that mentions a package is not an import).

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

(Phase 3 build) What was built, and what changed while building:

- **One module for the writers and readers**, `applicant/facts.py`: `put` (one brain-only fact,
  idempotent; an unchanged value writes nothing, `confirm` raises a value the person saw and kept
  to 1.0), `write_contact`, `write_profile`, `add_document`, `sync_chat`, `notes`, `contact`,
  `employers`, `worked_here`, `save_worked_here`, `salary_conflict`, `mark_asked`,
  `settle_chat_salary`. The rule "a lower-confidence value never replaces a confirmed one, it
  opens a conflict" lives in `put` and in `Store.add_fact` (the new value waits as `needs_check`,
  `conflict`); the salary question itself is built in `facts.py`, not `store.py`, because it
  reads the answer library's ranking and `basics.parse_salary`.
- **Writers and their source refs.** Details step: `you`, `details` (only what the answers
  changed). Resume import: `resume` at 0.9, `document:<id>`, the `document` row on the `app` or
  `telegram` channel. My profile's contact form: `you`, `profile`, confirmed. The builder's chat:
  the contact details the person types in answer to the builder are `you` at 1.0 with the diary
  line as the ref (they answer the builder's own question and see them on the resume they
  review; at 0.7 they would turn every contact field into a one-tap confirm, a loss against
  today, where the profile identity already fills them). Distilled chat facts: `chat` at their
  own confidence, written by `sync_chat`, which keeps the brain equal to `memory_facts` (a
  retracted row is marked replaced, a row the person deleted is forgotten). A chat key that
  names a form answer (`desired_salary`, `start_date`, ...; `keys.CHAT_ALIASES`) is a proposal
  for the canonical key (`needs_check`, `from_chat`; a legal one `unconfirmed_legal`). The
  extension's corrections: `extension`, the address parts as the form had them. Answer library
  writes keep the library's own `you`/`drafted` source (the mirror and the ranking need it) and
  take a ref: `ask_bar:<ask id>`, `ask_telegram:<ask id>`, `late_telegram:<ask id>`, `card`,
  `answers_page`, `package:<record id>`, `extension`. The onboarding step saves through the card,
  so its ref is `card`. An answered ask also logs `ask_answered` with the key and the number of
  the form's options (never the answer). Library facts now carry `code` (the yearly salary, the
  country, the self-identification choice).
- **Where the profile's facts live.** Beside the person's memory file (`DB_PATH`), which in the
  app is the data folder, so there is one brain. A test that points `DB_PATH` elsewhere gets its
  own brain there (`_profile_brain`), so tests never share profile facts.
- **Readers.** The fill (`/api/autoapply/plan`) and the agent's step build the applicant through
  `applicant_from_record(record, memory)`: the record's own values first, then city, state, ZIP
  and country split from the record's own address line, then the brain's contact facts at 0.9 or
  more. A ZIP from another city is never mixed into the record's address. The intake gate and the
  profile gate (`/api/profile/missing`) fill an empty contact detail, a role's dates or location
  (matched on org and title), a school's date and a project's dates from the brain before
  asking; in the builder these land in the session's essentials, so the resume shows them too.
  `_apply_memory_selection` and `_avoid_token_sets` read the brain's preferences and
  constraints (after `sync_chat`). The Telegram summary reads the brain: the name, roles, schools
  and skills it holds when the profile has none, and the form answers saved outside the library
  (named by their question only).
- **"Have you worked here before?"** (`facts.worked_here`), with the brain on: the person's own
  answer for this company (subject = company key, the employer normal form); else Yes when the
  company is one of their employers (role facts and the saved profile, by name); else No when it
  is not and the person has answered No before for a company outside their employer history, or
  saved a standing No on Your answers; else asked once, and that answer is saved for that company
  only. A standing Yes on Your answers is never used for another company (it was, before). With
  the brain off the answer library answers as before.
- **The salary question.** The values are every usable library wording of the salary, the
  prefs' `desired_salary` and a salary said in chat. Two amounts: "Forms say $90,000 (April); you
  just said $110,000. Which one?" ("you just said" for chat, "the browser extension fills" for
  the prefs, "you also saved" for the library); three or four: "You saved 3 different salaries.
  Which one is right?", the amounts being the buttons beside it. Shown on Your answers, in the
  "Answers every form asks" card as a "Needs a look" line (the memory page comes in Phase 6), and
  in the agent's next ask: a REQUIRED salary field is left open by the planner and
  `_known_answers_for`, the ask offers the amounts as choices (a typed amount counts), and it is
  marked asked when it goes out (`meta asked:@salary`, per set of amounts). After that the newest
  salary fills as before; the question stays on Your answers until settled. An optional salary
  field fills as before. Settling (a button on Your answers, the ask's answer, or the card's own
  Save) makes the chosen amount the salary everywhere: the library (older wordings of the
  question go, exactly as the card's Save does), the prefs the extension fills, and a chat
  proposal (confirmed when it is that amount, else replaced).
- **The shell.** `shell-electron/agent.js` takes the ask route's `reword` (the question's own
  words and its choices) for the bar, and a stuck run's message no longer names a label already
  answered this run (`stillOpen`). Telegram buttons carry `ask:<id>:<n>` only (checked, no change
  needed).
- **The Phase 3 upgrade.** A brain migrated before Phase 3 gets the address parts of the saved
  profile's contact once at start-up (`migrate._phase3`, `meta.phase3`).
- **Self-identification.** `_distill_facts` drops every turn `notify/assistant.carries_eeo`
  flags before the model call, and the store refuses an EEO fact from chat, as before.
- **Packaging.** `applicant.facts` added to the hidden imports in `packaging/tailor.spec`.
- **Tests and e2e.** New `tests/test_brain_writers.py` (26 tests). `tests/test_brain_parity.py`
  marks the fixture's salary question asked before comparing (Phase 3 asks it once; after that the
  agent answers exactly as the legacy stores did). `e2e-agent.mjs` section 12 types an address in
  the builder's chat and runs a `?address=1` job (City, State and a required ZIP code on the
  contact page; `tests/fixtures/forms/mock_ats.js`). The test model's scripted agent brain
  (`llm/base.py agent_step`) read any box labelled "code" as an emailed code, so "ZIP code" sent
  that run to "Waiting for the site's email"; it now takes only a verification code box
  (`tests/test_agent_brain.py` guards it).

Not settled in Phase 3 (open for the owner):

- Whether a legal fact derived from the resume (Yes to "worked here before" because the company is
  on the resume) needs a one-tap confirm the first time. Built: it fills, and the handover lists
  it with the other legal fields. (Phase 4 build) Decided by the owner's rule "never send a legal
  answer the person did not confirm": it gets the one-tap confirm, once per company (below).
- Whether the brain's contact facts should also feed the browser extension's autofill
  (`/api/profile/autofill`). Phase 7 moves that route to the brain; until then the extension
  still reads the profile identity.

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

(Phase 4 build) What was built, and what changed while building:

- **The mapper**, `applicant/mapper.py`. Only a label the rules do not know goes to it: not a
  contact field, not work authorization (`workauth.classify`), not a canonical wording
  (`answers.normalize`), not a self-identification question (`asks.is_eeo`), not a sanctions
  screen, not a cover letter, resume, per-job question, or the portal's own furniture (password,
  verification code, terms, privacy, consent), and not a wording the answer library already holds.
  The rest of a page goes in ONE call (at most 20 labels; options cut to 20 of 60 characters),
  through the redact guard (section 9). The reply is validated: a key not on our list becomes
  `ask`. It is cached in `label_map` per cleaned, lowercased label and options hash (the
  `rule` rows the Phase 1 migration seeded for the library's own wordings are left alone: the
  library answers those), and a field read without
  its options (the agent's step) finds the newest mapping of its label, so the step never calls
  again for a field the fill already mapped. A failed or stalled call (20 seconds) caches nothing,
  and the field is asked as before. `meta.mapper_calls` counts calls; `/api/brain/status` shows
  `mapper: {calls, by_how}` (counts only).
- **Where it runs.** The fill (`/api/autoapply/plan`): after the known pass, the fields left for
  the person AND the fields left for the model are mapped; a hit re-runs the known pass. Mapping
  the model's fields too keeps a wording like "How do you identify?" (no self-identification word
  the fact gate knows) away from the model's own fill. The agent's step (`_known_answers_for`)
  and the ask route map their open labels from the cache (or one call). Brain on only.
- **What a mapping does** (`planner.mapped_value`, shared with `asks.known_answer`). A normal key
  (salary, start date) answers at once from that key, by the same rules as its own wording
  (held, expired, this country's salary). A legal or EEO key waits for the person's one tap; until
  then the field is theirs (never the model's). `none`: the person's answer is used on this job
  and never saved (`skip_save`, also kept out of the late Telegram answers). `ask`: asked, and
  saved under its own words as before.
- **The one-tap confirm.** Offered only when the key holds an answer for this field (else the
  question is simply asked). The bar and Telegram say: "New wording: "Are you a U.S. person?" If
  it means "Are you a citizen of the job's country?", use your saved answer. If not, pick an
  answer." The choices are "Use my saved answer", then the page's own. It never names the value,
  so an EEO confirm says nothing on Telegram. "Use my saved answer" confirms the mapping for good
  (`how = person`, `confirmed = 1`) and the field gets the saved answer (not saved again; the tap's
  words are never saved as an answer). Any other choice or a typed answer is this form's answer,
  saved under the wording as before. Built on the Phase 3 `reword` (the shell needed no change):
  the options sent back keep up to 39 of the page's choices after the confirm.
- **"Have you worked here before?"** A Yes read from the resume (the company is one of the
  person's employers) now has origin `confirm`: it is never filled, and the ask says "Your resume
  lists Acme Corp. Have you worked for Acme Corp before?" with the form's choices. The answer is
  saved for that company, so it is asked once per company. A No derived from the person's own No
  answers (and their employer history) still fills: it is a code rule over confirmed facts
  (7.1 step 6). An optional "worked here before" field with only the resume's Yes stays blank.
- **Expiry**, `applicant/expiry.py`, through the keys `_recall_ctx` adds (`withhold`,
  `job_salary`, `wa_ended`, and `@salary` in `hold`):
  - a time-limited status (`workauth.DATED`: OPT, STEM OPT, EADs, permits) whose `auth_end` passed
    (a month means its last day): authorized, sponsorship now or any, status and end date are
    asked, with "Your saved work authorization ended on June 30, 2025." before the question. An
    answer the person gave after that date still fills. Citizenship, permanent residence, future
    sponsorship and relocation still fill;
  - a salary older than six months (183 days, the key's `reconfirm_days`) on a US job: a REQUIRED
    field asks "Still $90,000? Tap it to keep it, or type a new amount." with the amount as the
    choice, once per amount and date (`meta asked:stale:@salary`), like a conflict; then it fills
    as before. An optional field fills as before. A conflict, when there is one, is asked instead;
  - a start date that is a real date in the past is never filled (required or optional): "Your
    saved start date (May 1, 2026) has passed. When can you start?". Words ("Two weeks after an
    offer") never expire;
  - a salary for a job outside the US (`_job_country`): a US dollar or bare amount (the card saves
    bare amounts, in US dollars) is never filled; "This job is in the UK. What salary do you want
    for it?". The answer is kept as `@salary` with subject = the country (brain only, never the
    answer library, never the prefs), used for that country's jobs from then on, and left out of
    the salary conflict. A salary already in the job's currency ("£60,000") fills.
- **Telegram.** A `say` that already quotes the question is sent without "The application asks:"
  before it (`asks.prompt_text`). A late reply knows the job's country (the kept ask holds it).
- **The bar** (`ui/static/app.js aaAgentBar`): a question longer than 90 characters reads on its
  own row with the choices and the answer box under it (`bb-stack`); beside three choices it was a
  207px column of five lines. Measured at 1893x1183 DSF 2 and 2490x1556 DSF 1.52, dark and light.
- **LLM.** `map_labels(system, user)` on `AnthropicLLM` (so the broker and OpenAI clients too):
  400 tokens out at low effort. The FakeLLM maps by keywords and counts its calls
  (`FakeLLM.map_calls`). `workauth.answer` takes an explicit `kind` for a confirmed mapping.
- **Cost, measured by characters** (about 3.5 characters a token) on a page of six new wordings
  with their options: system 649 and user 2,793 characters, about 1,000 tokens in (the key list is
  about 600 of them), about 100 out. Haiku 4.5 about $0.0015 a page, Sonnet 5.5 about $0.003; a
  wording is never mapped twice.
- **Tests.** New `tests/test_brain_mapper.py` (14) and `tests/test_brain_expiry.py` (10).
  `test_brain_writers.py` now expects the confirm for the resume's Yes;
  `test_brain_parity.py` expects the UK job to leave the US dollar salary out (asked), everything
  else identical. E2E (`e2e-agent.mjs` section 13): a `?pay=1` job and a second one; the mock's
  questions page asks the required base pay question (`tests/fixtures/forms/mock_ats.js`).

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

(Phase 5 build) What shipped, and where it differs from the plan above:

- `applicant/scope.py`: the job's family (title, then the top job tag), industry (sponsor NAICS,
  then the JD's banking or investing tags as `52`, then the model) and company type (model only),
  cached in `company_class`; the person's correction (`source = person`) is never replaced.
  `classify_company` (`llm/base.py`, `llm/anthropic_client.py`) is ONE call per new company that
  returns industry and type together, reads the company name and 1,500 characters of the ad, and
  runs only when an active choice is scoped to a label nothing else settled. The builder's ask
  never triggers it: it offers the most specific label already known. The sponsor lookup is
  wired from `ui/app.py` (`set_sponsor_source`). A company scope stores the company's display
  name (not its key) and matches the same employer or a longer name of it ("Stanbic" covers
  "Stanbic Bank Ghana"), never a shorter one.
- `applicant/rules.py`: item ids (`role:<org>|<title>` with the title the person gave, so a title
  aligned to a job keeps its id; `project:<name>`), CRUD (the same choice twice is one row, a
  contradicting one for the same item and scope replaces the older), `rename_item`, `sync_items`
  (a choice naming a removed item is paused and comes back with it; one the person paused stays
  paused), `plan` (per item the most specific show, hide or lead wins; this resume's own switches
  beat every saved choice), plain sentences, "Just this time" for 30 days, the overlap answers,
  chat and Telegram proposals, the "do not mention" offers, and the `application` row.
  A choice that names a role whose title was edited since still finds it when it is the only role
  at that employer.
- `tailoring/assembler.py`: `select_roles_for_jd(..., choices=, report=)`. With no choices it is
  the old code path, untouched. With choices: hidden items go; `prefer` drops the second of two
  and puts the first in the second's place when the scorer would have shown the second; `apart`
  drops the weaker match (a `prefer` or a pin settles which); a pin made for a narrower kind of
  job keeps an item a broader `prefer` or `apart` would drop. Projects take hide, prefer and
  apart too. The grow pass builds from the person's material MINUS what the choices left out
  (`choice_pool`), so a hidden role never comes back to fill a short page. `lead` marks the item
  (`_lead`, stripped before the page leaves): the shrinker takes bullets from other entries
  first, and the summary's sentence naming it moves to the front (reordered, never reworded).
  `AssembleResult.choices_report` says what was shown, what was left out and why, and which
  saved choices changed the page.
- The builder (`ui/session.py`, `ui/app.py`): `POST /api/session/shown`, `/swap`, `/choice`,
  `/overlap`, `/proposal`; `state.choices` carries each item's switch, its "Swap for ..." (a
  shown role with an overlapping role that is not shown) and its "why" line ("Hidden for bank
  and finance jobs." "That beat your data and AI jobs choice." with Change, which opens the
  choice on Your answers). The overlapping-roles question is asked once, ever, per pair (the
  answer or the asking is kept in `meta.overlap_asked`), and only on a page that shows both
  roles; a one-month handover is a job change, not two jobs (three shared months or more).
  The record's `render_profile` (the Word export) leaves out what the choices left out, and
  `data.choices` keeps the shown ids and the rule ids. A choice saved from a resume counts that
  resume as a use. The unattended runs build under a scratch name for the same person and read
  the choices too (`_rules_store` ignores the profile name; only the person's taps write).
- Chat: `distill_memory` returns `rules` (names and scope words only, from the last message);
  code resolves them to the person's own items by the fixed table and keeps them as proposals in
  `meta.rule_proposals` (seven days). The builder's chat shows a chip with Save (one per option
  when a name fits two roles) and Not quite; a turn that is a choice gets "Want me to keep this
  choice for next time?" instead of being read as resume material. On Telegram,
  `NotifyHub(rule_proposer=)` checks by code that the message names one of the person's roles and
  a choice word before ONE `distill_memory` call; the reply carries Save and Not quite
  (`rule:s:<token>:<n>`, `rule:n:<token>`), and "Saved" comes only after code saved it.
- The list: Phase 6's memory page is not built yet, so every choice sits on Your answers in a
  fourth card, "Resume choices": the sentence, "Used on N resumes", Edit (what it does, for which
  jobs, from lists), Pause or Resume, Delete; and the chat's "do not mention X" lines that name
  one of the person's items as "Save as a choice" or "Not now" (`GET/POST /api/brain/rules`,
  `POST /api/brain/rules/<id>`). "Add a choice" from lists has its route (`POST
  /api/brain/rules` with items) but no UI until Phase 6.
- `/api/brain/status` adds `choices` (active choices, company calls, applications).
- Screens: `docs/design/choices/` (1893x1183 DSF 2 and 2490x1556 DSF 1.52, dark and light).

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

- Token counts are estimates. The real figures come from Phase 4 and 5 runs. (Phase 4 build) The
  label mapping figure is now measured by characters, not yet by a real model's usage report.
- The installer saving is an estimate from the installed engine's folders; the next build
  records the measured size.
- Company type from a JD will be wrong sometimes. The person's correction wins, and a wrong
  label only changes which choice applies, never what a form says.
- Whether people want choices by company type at all is untested. If nobody uses them after a
  month, the small call stays off and only industry and family scopes remain.
- Data stays unencrypted on disk, as today.
