# SponsorJobs architecture

How the engine works, for contributors. For install and usage, see the [README](../README.md).
The full design rules live in [CLAUDE.md](../CLAUDE.md).

## The three inputs

- **TEMPLATE** — a LaTeX skeleton (`config/resume_shetty.tex`). Structure only.
- **PROFILE** — the person's own material, saved and reused (`intake/`).
- **JOB** — one job description to tailor toward.

Tailoring selects, rewords, and re-emphasizes PROFILE material to fit the JOB,
poured into TEMPLATE's bullet slots — nothing outside those slots is touched.

## Layout

```
tailoring/
  latex_template.py   parse the template into addressable \item bullets
  surgical_editor.py  apply edits + PROVE they're confined to bullet interiors
  compiler.py         run pdflatex; extract page count + overfull \hbox warnings
  self_heal.py        splice → compile → check → shorten → retry, else roll back
  keywords.py         JD term extraction + ATS coverage report
  tailor.py           orchestrator: reword → self-heal → coverage report
intake/
  profile_store.py    saved profile in SQLite: reuse / refresh / add (§4b)
  intake.py           JD-driven questions (§4a) + guided/autonomous modes (§4c)
llm/
  base.py             LLMBackend protocol + deterministic FakeLLM (offline tests)
  anthropic_client.py real bring-your-own-key backend (§5)
tests/                the CLAUDE.md §10 acceptance suite
config/               template + credentials.env.example (secrets git-ignored)
```

## The three hard checks (CLAUDE.md §8/§9)

Exit code 0 is **not** proof of success. After every compile the engine verifies:

1. the compile succeeded (exit 0, PDF produced),
2. the page count is unchanged (still **one page**),
3. **no new** `Overfull \hbox` warning appeared versus the baseline.

If any check fails, it diagnoses the offending bullet(s), shortens them, and
recompiles (capped retries). If it still can't pass, it **rolls back** to the
last known-good template so the user never receives a broken or two-page PDF.

## Running the acceptance tests

```bash
python -m pytest -v
```

Tests that invoke a real `pdflatex` are **skipped** (not failed) if no LaTeX
toolchain is present, so the pure-logic tests run anywhere. Every test uses the
deterministic `FakeLLM`: no network, no API key, no job board.

toolchain is present, so the pure-logic tests run anywhere. Every test uses the
deterministic `FakeLLM` — no network, no API key, no job board.

| §10 acceptance criterion | Test |
| --- | --- |
| Diff changes ONLY targeted bullet interiors | `test_edits_are_confined_to_bullet_interiors`, `test_tampering_outside_interiors_is_detected` |
| Tailored PDF still one page, no new overfull | `test_tailored_pdf_is_one_page_no_new_overfull` |
| JD-driven intake asks only role-relevant questions | `test_intake_questions_are_jd_specific_not_fixed` |
| Saved answers reused / refreshed / added | `test_profile_reuse_returns_saved_as_is`, `test_profile_add_merges_onto_saved`, `test_profile_refresh_replaces_entirely` |
| Autonomous mode from saved profile, no input | `test_autonomous_builds_from_saved_profile_no_input` |
| Content sections aggressively reworded toward JD | `test_experience_and_projects_are_reworded_toward_jd` |
| Broken LaTeX rejected and rolled back | `test_broken_latex_is_rejected_and_rolled_back` |
| Coverage report (present vs missing), JD phrasing, no stuffing | `test_coverage_report_lists_present_and_missing`, `test_no_hidden_text_or_keyword_stuffing` |
| No RED-lane code anywhere | `test_no_red_lane_code_exists` |
| Self-heal never ships a failing output | `test_self_heal_shortens_overflow_back_to_one_page`, `test_never_ships_failing_output_invariant` |

## Using the engine

```python
from llm.anthropic_client import AnthropicLLM     # or llm.base.FakeLLM for demos
from tailoring.tailor import tailor_resume

template = open("config/resume_shetty.tex", encoding="utf-8").read()
profile  = {...}   # the person's saved PROFILE
jd_text  = "..."   # one job description

result = tailor_resume(template, profile, jd_text, AnthropicLLM(), workdir="data/build")
print(result.summary())          # status + coverage report
print(result.pdf_path)           # one-page, self-healed PDF (or rolled-back valid one)
```

## Compliance (CLAUDE.md §6/§7)

No RED-lane code exists, and `test_no_red_lane_code_exists` fails the build if any
is introduced later: no logged-in LinkedIn automation, no stealth/anti-detection,
no captcha interception, no rotating proxies.
