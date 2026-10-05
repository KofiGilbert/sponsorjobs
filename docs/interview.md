# Interview prep: two rounds, two entry paths

Decision of 2026-10-02 (owner-approved). Interview prep has exactly **two rounds**:

- **Round 1: a HireVue-style one-way recorded video screen.** No interviewer. 5 questions by
  default (3 to 5), 30 s to prepare, 120 s to answer, one retake per question, auto-submit at
  time-out (client side), unlimited unscored practice prompts first. Answers are transcribed
  locally and the set is scored; pass at 70.
- **Round 2: ONE live Tavus CVI interview (15 min)** with an interviewer briefed on the JD and the
  person's résumé, followed by a scored report. Runs on a pass through the broker first,
  else on the **person's own Tavus API key** (free tier, 25 min/month) saved in Settings. See
  "Who pays for Round 2" below.

Two entry paths: **Full mock** (Round 1, then Round 2 if passed AND Round 2 available) or either
round on its own (`skip_screen` starts Round 2 without a passed screen).

Gone: the Realistic/Relaxed switch (replaced by the `extra_time` accommodation), the 6 to 11
question screen with a warm-up, the essentials augmenter on preps, the three-round local voice
simulation (`POST /api/interviews`, `/rounds/*`, `/ask`, `generate_interview_plan`), the
pre-rendered Tavus clip path (`interview/avatar.py`), `_avatar_tier`, and the `live_avatar` pref.

Hard rules: no facial analysis, ever. Scoring is content, STAR structure, specificity, JD-term
coverage, fillers and pace, from the transcript. Recordings stay on disk under `data/recordings/`
until the person deletes the screen. Nothing of the person's is stored by us or by the broker.

## Python side

- `ui/app.py`: Round 1 (`/api/screens*`), Round 2 (`/api/interview/round2/status`,
  `/api/interviews/cvi/*`, `/api/interviews/<id>`), `/api/avatar/settings`, readiness.
- `backend/tavus_client.py`: the one Tavus CVI client (conversations, PALs, end, transcript),
  used by the engine (own key) and by `backend/provider_tavus.py` (the broker's company key).
- `llm/anthropic_client.py` / `llm/base.py`: `generate_screen_questions(profile, role, company,
  jd, kinds)` asks for exactly N questions with kinds; `score_interview_answer` and
  `score_interview_screen` score both rounds.
- `interview/transcribe.py`: local STT + delivery metrics (unchanged).

## API contract as implemented

### Preps (unchanged)

`POST /api/preps` `{role, company?, jd?, cv?, record_id?, focus?, difficulty?}` ->
`{prep: {id, role, company, focus, difficulty, questions: [{q, type, competency, why}]}}`.
The questions are exactly what the JD-grounded model produced; the fixed essentials
(sponsorship, weakness, ...) are no longer appended.

### Round 1

`POST /api/screens` `{prep_id, extra_time?: bool, questions?: int (3..5, default 5)}` ->

```json
{
  "id": "...", "prep_id": "...", "role": "...", "company": "...", "created": 1760000000,
  "config": {"prep_s": 30, "answer_s": 120, "retakes": 1, "questions": 5, "extra_time": false},
  "questions": [{"i": 0, "text": "...", "kind": "motivation", "competency": "Motivation"}, ...],
  "practice": {"text": "...", "kind": "practice"},
  "answers": [{"i": 0, "q": "...", "kind": "...", "competency": "...", "transcript": "",
               "per_answer_feedback": null, "delivery_metrics": null, "recorded": false,
               "takes": 0, "takes_left": 2}, ...],
  "result": null, "passed": false, "stt_available": false,
  "disclaimer": "Practice for a HireVue-style one-way video interview. Not affiliated with HireVue, Inc. This score is ours, not theirs.",
  "screen": { ...the same object again, for callers that read `screen`... }
}
```

- `extra_time` doubles `prep_s` and `answer_s` (60 / 240), nothing else.
- Kinds: `motivation | behavioural | situational | technical`. Mix for 5: 1 motivation, 3
  behavioural, 1 situational (the last slot may be a JD-specific light `technical` question when
  the model judges the role technical); 4 and 3 drop behavioural first. The mix is enforced
  server-side (`_fit_screen_questions`): the model's questions fill the slots, then the prep's
  questions, then generic defaults, so the screen always has exactly N.
- 400 when `questions` is outside 3..5; 404 for an unknown prep.
- `GET /api/screens/<id>/practice[?seen=N]` -> `{text, kind: "practice"}`. Unlimited, random
  (or the N-th of the pool with `seen`), never stored, never one of the scored questions.
  Practice prompts come from a local pool, not the model (deliberate: instant, no cost, no leak).
- `GET /api/screens/<id>` -> the object above (minus `practice`), also under `screen`.
- `POST /api/screens/<id>/answers/<i>` multipart `file` (webm/mp4/ogg/mp3) -> `{ok, recorded,
  takes, takes_left}`. The server enforces `retakes`: a second upload is allowed once, the third
  is `409 {error, no_takes_left: true, retakes: 1}`. A new take clears the earlier transcript and
  feedback for that question. 400 non-media, 413 over 32 MB, 404 bad index.
- `POST .../answers/<i>/transcribe` (local STT; `{available: false, message}` when not
  installed), `POST .../answers/<i>/transcript` `{transcript}` (typed fallback),
  `POST .../answers/<i>/coach`, `GET .../report.docx`, `POST .../delete`: unchanged.
- `POST /api/screens/<id>/score` -> `{screen: {...}, result: {...}}` where `result` =
  `{score, passed, threshold: 70, why, improvements, competencies: [{name, score, count}],
  answers: [{i, q, kind, competency, transcript, score, feedback, delivery_note,
  delivery_metrics}], is_practice: true, assessment_disclaimer, disclaimer}` and `disclaimer` is
  the HireVue text above. 400 until at least one answer has a transcript.

### Round 2

`GET /api/interview/round2/status` ->
`{available: bool, reason: "ok"|"no_key"|"no_minutes", source: "plan"|"own_key"|null,
minutes_left: int|null, key_set: bool, can_buy: bool, packs: [{id, interviews, seconds,
price_label}], tier: "free"|"pass30"|"pass90"|null, pass_until: int|null,
offer: "passes"|"packs"|null}`.

- `plan` FIRST: when the broker is reachable and its `GET /me/usage` reports
  `avatar_seconds_left >= 900` (pass remainder + purchased extra interviews cover a full one);
  `minutes_left` = whole minutes (the UI shows `floor(minutes_left / 15)` interviews).
- else `own_key` whenever the credential `AVATAR_API_KEY` (the person's Tavus key) is set;
  `minutes_left` is `null` there (Tavus meters the person's own account).
- Otherwise `no_minutes` when the broker reports an active pass (`tier` pass30/pass90) without a
  full interview left (`minutes_left` = what is left, `offer: "packs"`), else `no_key` (nothing
  set up, or the broker is unreachable; `offer: "passes"` when the broker is reachable).
- `packs` = the broker's `GET /billing/packs`, cached 5 minutes, and only during a pass (packs
  are not sold otherwise); `[]` when the broker is unreachable or billing is off. `can_buy` = at
  least one pack offered.

`POST /api/interview/round2/buy` `{pack}` -> `{url}` (a Stripe Checkout page the app opens in
its pane, or the system browser via `/api/open`). Proxies the broker's
`POST /billing/packs/<pack>/checkout`; 400 bad/unknown pack, 402 `{offer: "passes"}` when no
pass is active, 503 billing off or unreachable,
502 other failures.

`GET /api/avatar/settings` -> the status plus `configured` (= `key_set`).
`POST /api/avatar/settings` `{key}` saves the key, `{key: ""}` revokes it; returns
`{ok: true, ...status, configured}`. Changing the key also forgets the interviewer PAL created
under the old key (`AVATAR_PAL_ID`). The `enabled` toggle is gone.

`POST /api/interviews/cvi/start` `{prep_id, screen_id?, skip_screen?}` ->

- `404` unknown prep.
- `403 {error: "screen_not_passed", gated: true, message}` when `screen_id` is given but that
  screen did not pass (or does not exist), or when no `screen_id` and `skip_screen` is not true.
- `402 {error: "round2_unavailable", reason, source, can_buy, packs, ...}` when the status is
  not available.
  Also used when Tavus rejects the person's key (`reason: "no_key"`) or reports no credit
  (`reason: "no_minutes"`), and when the broker returns 402 (`reason: "no_minutes"`) and no own
  key is saved; with an own key saved, a broker 402 falls through to the own key.
- `503 {error: "service_unavailable"}` when Tavus/the broker cannot be reached;
  `502 {error: "tavus_error"|"service_error", message}` on other upstream failures.
- `200 {interview_id, join_url, conversation_id, max_minutes: 15, source, role, company,
  disclaimer}`.

With the plan it posts the briefing to the broker's `/avatar/session/start` and returns its
`session_url`. With the person's own key the engine calls Tavus directly (see below). The
interview record keeps the `source` it actually started on.

`POST /api/interviews/cvi/heartbeat` `{seconds, interview_id?}` -> `{remaining, stop, source}`.
Metered by the interview's own source: on the plan it forwards to the broker's
`/avatar/heartbeat` (the app sends one every 30 s and the remainder at the end, and ends the
call on `stop`); on an own key it answers `{remaining: null, stop: false}`. Without an
`interview_id`, a saved own key means no meter.

`POST /api/interviews/cvi/end` `{interview_id, transcript?, duration_s?}` ->

- own key: `POST /v2/conversations/{id}/end`, then `GET /v2/conversations/{id}?verbose=true` up
  to three times (2 s apart) for the `application.transcription_ready` event.
- If Tavus has no transcript (or on the plan path, where this app holds no key), the body's
  `transcript` is scored instead: plain text (one turn per line, optional `Interviewer:` /
  `Me:` prefixes; unprefixed lines alternate starting with the interviewer) or a list of
  `{role: "assistant"|"user"|"interviewer"|"candidate", content}`.
- `400 {error: "no_transcript"}` when there is neither.
- `200 {report, interview_id, interview}` with `report` =
  `{score, passed, threshold, why, improvements, competencies, answers: [{q, transcript,
  competency, score, feedback, delivery_note, delivery_metrics}], transcript: [{role, content}],
  duration_s, disclaimer, is_practice: true, round: 2, source}`. `disclaimer` is
  "This is a practice simulation, not a real interview or hiring decision."

Scoring a dialogue: each interviewer turn (or run of turns) is a question and the candidate turns
that follow are its answer; answers under 8 words (yes / thank you / bye) are not scored; each
question is tagged with the competency of the planned question it most resembles (word overlap,
else "General"); then the existing per-answer and aggregate scorers run exactly as for Round 1.
`duration_s` comes from the body, else from the transcript timing, else from the wall clock,
capped at 15 minutes.

`GET /api/interviews/<id>` -> `{interview: {id, prep_id, screen_id, skipped_screen, role, company,
source, conversation_id, started, ended, max_minutes, report, is_practice, disclaimer},
report}` (`report` is `null` until `/end`). `POST /api/interviews/<id>/delete` -> `{ok}`.

`GET /api/interview/readiness` folds Round-2 reports in with the scored screens:
`{attempts, screens, live, best, latest, improved, trend_from, passed_any, verdict, headline,
competencies, weakest, role, company, is_practice}`.

### Removed (404)

`POST /api/interviews`, `GET /api/interviews`, `/api/interviews/<id>/rounds/*`, `/.../ask/*`,
`/api/interviews/<id>/readiness`.

### Deviations from the contract as written

- Responses that the contract lists flat are returned flat **and** repeated under `screen` /
  `interview` so older callers keep working; `/score` returns `{screen, result}`.
- `POST /api/preps` still wraps its result in `{prep: ...}` (that was the existing shape).
- The optional `heartbeat` route was kept for the plan source.
- `GET /api/interviews` (list) was removed so that `POST /api/interviews` is a true 404; the
  readiness overview is the cross-attempt view.

## Who pays for Round 2

Decision of 2026-10-02 (owner). Round 2 is one 15-minute interview (900 s,
`backend.metering.INTERVIEW_SECONDS`), so allowances are counted in whole interviews. Full
pricing: docs/bundled-api-backend.md, section 5 "Pricing". No subscription, nothing auto-renews.

1. **A pass first.** Live interviews come with a one-time pass and run on the COMPANY's Tavus
   key, which lives only on the broker and never leaves it (the app gets a join URL, never a
   key). Job Hunt Pass ($29, 30 days): 3 interviews. Season Pass ($69, 90 days): 9. Free has
   none. The allowance belongs to the pass period (`pass_until`), not the calendar month; buying
   a pass during a pass extends the end date and adds the interviews. The broker only starts an
   interview when the pass's remaining seconds plus purchased extra interviews cover a full
   900 s (`Meter.can_start_avatar`), so nobody is cut off midway; time is deducted from the pass
   first, then from extra interviews.
2. **Extra interviews** when a pass's interviews are used up: 1 for $9, 3 for $24, 5 for $35,
   prepaid, never expire. Sold ONLY while a pass is active (`POST /billing/packs/<id>/checkout`
   answers 402 `{reason: "pass_required"}` otherwise); once bought they stay usable after the
   pass ends. A Stripe Checkout Session in `mode="payment"` with
   `metadata={kind: "credit_pack", pack, seconds}`; the `checkout.session.completed` webhook
   (paid) adds the seconds as credits, once per session id (the store records processed
   session ids). Prices come from `STRIPE_PRICE_PACK_1` / `_3` / `_5`; an unset pack is not
   offered.
3. **The person's own Tavus key** (free users, or anyone whose interviews are used up and who
   would rather not buy). Tavus meters their own account.

`/api/interview/round2/status` reports `tier`, `pass_until` and `offer`: `"passes"` for a free
account (the gate shows "See passes" next to the free-key steps) or `"packs"` during a pass
(the gate lists the extra-interview packs with their prices).

| Case | Status | Gate |
|---|---|---|
| Pass (or extra interviews), a full interview left | `plan`, available | green room, "N interviews left" |
| Pass, used up, packs on sale, no key | `no_minutes`, `can_buy`, `offer: "packs"` | "Buy 1 interview, $9", "Buy 3 interviews, $24", "Buy 5 interviews, $35", "Use my own Tavus key", "Finish here" |
| Pass, used up, own key saved | `own_key`, available | green room on their key |
| Free, own key | `own_key`, available | green room |
| Free, no key | `no_key`, `offer: "passes"` | the free-key steps and "See passes" |
| Broker down | `own_key` if a key is saved, else `no_key`; `packs: []`, `offer: null` | as above |

## Tavus (own-key path)

Read from docs.tavus.io on 2026-10-02. Tavus renamed *persona* to **PAL** and *replica* to
**face**; the body fields are `pal_id` / `face_id` (the older `persona_id` / `replica_id` are not
listed in the current reference). All calls send `x-api-key: <the person's key>`.

| Step | Call | Notes |
|---|---|---|
| Interviewer, once per key | `POST /v2/pals` `{pal_name, system_prompt, default_face_id, pipeline_mode: "full", greeting}` -> `{pal_id}` | id stored locally as `AVATAR_PAL_ID`; the system prompt is the standing interviewer behaviour (one question at a time, follow-ups, 15-minute structure). If creation fails the conversation is minted on the bare face with a `custom_greeting`. |
| Start | `POST /v2/conversations` `{pal_id` or `face_id, conversation_name, conversational_context, require_auth: true, properties: {max_call_duration: 900, participant_absent_timeout: 300, participant_left_timeout: 90}}` -> `{conversation_id, conversation_url, meeting_token}` | `conversational_context` is the per-interview briefing: hiring-manager persona for this company/role, the structure, the questions to cover (from the prep, with competencies), the JD (2500 chars) and the profile/CV (2500 chars). The join URL gets `?t=<meeting_token>` appended. |
| End | `POST /v2/conversations/{id}/end` | no body; 200 with no content. |
| Transcript | `GET /v2/conversations/{id}?verbose=true` -> `events[]` | the `application.transcription_ready` event carries `properties.transcript: [{role: "assistant"\|"user", content, timestamp, seconds_from_start, duration}]`. |

Default face: `r9d30b0e55ac` (a Tavus stock face, "Luna"); override with the credential
`AVATAR_FACE_ID`.

**Unverified (no live key in this session):** that the current Tavus API still accepts the
older `persona_id`/`replica_id` spellings (we send the documented `pal_id`/`face_id`); that the
free tier permits PAL creation; how long after `/end` the `transcription_ready` event appears
(we retry 3 times, 2 s apart, then fall back to a client-supplied transcript); the exact HTTP
status Tavus returns when a free account is out of minutes (402 and 429 are mapped to
`no_minutes`). Everything in the table above is covered by offline tests with a fake transport
(`tests/test_provider_tavus.py`, `tests/test_round2_cvi.py`); the live smoke test is the one
remaining step.

## Getting a free Tavus key

1. Sign up at <https://platform.tavus.io> (the free plan includes 25 conversational-video
   minutes per month and the stock faces; no card needed at the time of writing).
2. In the platform, open **API Keys** and create a key.
3. In SponsorJobs: Settings -> Live interview -> paste the key. It is stored in the git-ignored
   local credential store (`AVATAR_API_KEY`) and only ever leaves the machine as the
   `x-api-key` header on the person's own Tavus calls. Revoke it from the same place.

Round 2 is 15 minutes, so a free key covers about one full mock a month; a pass through the
broker (`docs/bundled-api-backend.md`) is used first when the person has one.
