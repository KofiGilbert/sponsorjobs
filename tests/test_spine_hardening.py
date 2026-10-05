"""Spine hardening: the tailoring/self-heal/coverage invariants an adversarial audit
found unenforced or untested against CLAUDE.md §8-9. Each test pins one guarantee:

  * a reworded bullet can neither over-expand (§8 length budget) nor smuggle in an
    unsupported skill (§8 no-stuffing) — it rolls back to the person's real bullet;
  * a hyperlink target can't break out of \\href and inject LaTeX (§8 single-column);
  * the coverage report's "present" reflects the RENDERED CV, not the whole profile;
  * the self-heal checker enforces an ABSOLUTE one-page floor and a real new-overfull diff;
  * accept() refuses to export a CV that didn't compile to a clean one page (§8/§9);
  * the skills packer never pulls a term that lives only in a drafted placeholder bullet.

Nearly all are pure-logic (no TeX engine); the end-to-end escaping test is @requires_latex.
"""
from __future__ import annotations

import pytest

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.assembler import (_rendered_cv_text, _safe_url, _tailor_bullets,
                                 assemble_cv, render_cv)
from tailoring.compiler import CompileResult, Overfull
from tailoring.keywords import build_coverage_report
from tailoring.self_heal import _checks
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

MIN_PREAMBLE = "\\documentclass{article}\n\\begin{document}"


# ------------------------------------------------- bug 1: per-bullet length ceiling
class _OverLongLLM(FakeLLM):
    """Reword blows way past budget; shorten returns a within-budget sentinel."""

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        return "Delivered value " * 60          # ~900 chars, over any budget

    def shorten_bullet(self, text, max_len_chars):
        return "SHORTENED to budget."


def test_tailor_bullets_clamps_an_over_budget_rewrite():
    prof = {"experience": [{"org": "Acme", "roles": [
        {"title": "Eng", "bullets": ["Managed the firm's quarterly reporting."]}]}]}
    out = _tailor_bullets(prof, "Data engineering role.", _OverLongLLM())
    got = out["experience"][0]["roles"][0]["bullets"][0]
    assert got == "SHORTENED to budget."         # the clamp fired via shorten_bullet
    assert "Delivered value Delivered value" not in got


# ------------------------------------------------- bug 7: no fabricated skill in prose
class _InjectLLM(FakeLLM):
    """Reword smuggles in JD skills the profile doesn't support."""

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        return "Built streaming pipelines with Kafka and Kubernetes at scale."


def test_tailor_bullets_reverts_a_fabricated_skill_to_the_real_bullet():
    original = "Built streaming data pipelines for analytics."
    prof = {"skills": {}, "experience": [{"org": "Acme", "roles": [
        {"title": "Eng", "bullets": [original]}]}]}
    jd = "Senior Engineer. Must have Kafka and Kubernetes experience."
    out = _tailor_bullets(prof, jd, _InjectLLM())
    got = out["experience"][0]["roles"][0]["bullets"][0]
    assert got == original                        # fabricated reword discarded
    assert "Kafka" not in got and "Kubernetes" not in got


def test_tailor_bullets_keeps_a_reword_that_only_uses_supported_terms():
    # Control: FakeLLM weaves only supported terms, so the reword is NOT reverted.
    original = "Built risk models in Python for the desk."
    prof = {"skills": {"Computing": "Python, SQL"},
            "experience": [{"org": "Acme", "roles": [{"title": "Eng", "bullets": [original]}]}]}
    out = _tailor_bullets(prof, "Risk role in Python and SQL.", FakeLLM())
    got = out["experience"][0]["roles"][0]["bullets"][0]
    assert "Python" in got                        # supported term stays; not reverted


# ------------------------------------------------- bug 3: href target can't inject LaTeX
def test_safe_url_neutralizes_break_bytes_but_keeps_legit_chars():
    out = _safe_url("https://x.com/p}\\twocolumn{evil")
    assert "}" not in out and "\\" not in out and "{" not in out
    assert "%7D" in out and "%5C" in out
    # hyperref handles # % & _ inside \href, so a real fragment/query URL is untouched.
    assert _safe_url("https://x.com/a?b=1&c=2#frag") == "https://x.com/a?b=1&c=2#frag"


def test_render_cv_href_target_is_neutralized():
    profile = {"identity": {"name": "X", "github": "https://x.com/p}\\twocolumn{"},
               "experience": []}
    tex = render_cv(MIN_PREAMBLE, profile)
    assert "p}\\twocolumn" not in tex             # the raw brace-break is gone
    assert "%7D" in tex                           # encoded instead — link preserved


# ------------------------------------------------- bug 4 + gap A: one-page & overfull checks
def _cr(pages, overfulls=(), count=None, ok=True):
    ov = list(overfulls)
    return CompileResult(ok=ok, returncode=0, pages=pages, overfulls=ov,
                         overfull_count=(len(ov) if count is None else count))


def test_checks_rejects_two_pages_even_when_baseline_page_count_unknown():
    passed, reasons, _fresh, _extra = _checks(_cr(None), _cr(2))
    assert passed is False
    assert any("one page" in r for r in reasons)


def test_checks_passes_a_clean_one_page():
    passed, reasons, _fresh, _extra = _checks(_cr(1), _cr(1))
    assert passed is True and reasons == []


def test_checks_flags_a_fresh_overfull_when_baseline_already_overflows():
    base = _cr(1, [Overfull(5.0, 10, 12)])
    cand = _cr(1, [Overfull(5.0, 20, 22)])        # different line range -> fresh
    passed, reasons, fresh, _extra = _checks(base, cand)
    assert passed is False and len(fresh) == 1
    assert any("overfull" in r for r in reasons)


def test_checks_same_overfull_key_is_not_new():
    base = _cr(1, [Overfull(5.0, 10, 12)])
    cand = _cr(1, [Overfull(9.9, 10, 12)])        # same (line_start,line_end) key
    passed, _reasons, fresh, _extra = _checks(base, cand)
    assert passed is True and fresh == []


def test_checks_surplus_unkeyed_overfull_counts_as_new():
    base = _cr(1, [], count=0)
    cand = _cr(1, [], count=2)                     # 2 overfulls with no line range
    passed, _reasons, _fresh, extra = _checks(base, cand)
    assert passed is False and extra == 2


# ------------------------------------------------- bug 5: coverage reflects the rendered CV
def test_rendered_cv_text_excludes_unrendered_sections():
    profile = {"identity": {"name": "X"}, "summary": "Seasoned data engineer.",
               "extracurricular": [{"title": "Club", "bullets": ["Ran Kubernetes workshops."]}]}
    only_summary = _rendered_cv_text(profile, sections=["summary"])
    assert "Kubernetes" not in only_summary and "engineer" in only_summary.lower()
    with_extra = _rendered_cv_text(profile, sections=["summary", "extracurricular"])
    assert "Kubernetes" in with_extra


def test_coverage_marks_an_unrendered_supported_term_as_missing_not_present():
    profile = {"identity": {"name": "X"}, "summary": "Data engineer.",
               "extracurricular": [{"title": "Club", "bullets": ["Ran Kubernetes workshops."]}]}
    jd = "We want strong Kubernetes experience."
    cv_text = _rendered_cv_text(profile, sections=["summary"])   # extracurricular omitted
    report = build_coverage_report(jd, cv_text, profile)
    assert "Kubernetes" in report.missing_supported     # in the profile, not on the page
    assert "Kubernetes" not in report.present


# ------------------------------------------------- bug 6: skills packer uses genuine material
def test_fill_skills_ignores_a_term_only_in_a_drafted_bullet(template_source, fake_llm, workdir):
    jd = "Senior Engineer. Must have Python and Kubernetes experience."
    s = WebIntake(jd, template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.essentials["skills_input"] = ["Python"]          # genuine material: Python only
    s.essentials["experience"] = []
    # A DRAFTED placeholder bullet mentions Kubernetes — must NOT count as support.
    s.profile["experience"] = [{"org": "Acme", "roles": [
        {"title": "Engineer", "dates": "2020", "drafted_bullets": True,
         "bullets": ["Delivered Kubernetes initiatives at Acme."]}]}]
    s.profile["skills"] = {"Computing": "Python"}
    s._fill_skills()
    line = " ".join(s.profile["skills"].values())
    assert "Python" in line
    assert "Kubernetes" not in line                    # only in a drafted bullet -> never packed


# ------------------------------------------------- bug 2: accept() won't export a bad CV
def test_accept_blocks_when_the_cv_is_not_one_page(template_source, fake_llm, workdir):
    s = WebIntake("Engineer role.", template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()

    class _Overflowed:                                  # stand-in for an AssembleResult
        ok = False
        status = "overflow"
        pdf_path = None
    s.assembled = _Overflowed()
    s.dirty = False                                     # so accept() doesn't re-assemble
    res = s.accept()
    assert res.get("ok") is False and res.get("blocked") is True
    assert "one page" in res.get("reason", "")


# ------------------------------------------------- gap D: special chars survive the assembler
def test_render_cv_escapes_special_latex_chars_in_content():
    profile = {"identity": {"name": "A & B"},
               "skills": {"Computing": "C, 40% growth, $2M, #1"},
               "experience": [{"org": "R&D Lab", "roles": [
                   {"title": "Eng", "bullets": ["Cut cost by 50% & saved $2M for R&D."]}]}]}
    tex = render_cv(MIN_PREAMBLE, profile)
    for esc in ("\\&", "\\$", "\\%", "\\#"):
        assert esc in tex
    assert "%" not in tex.replace("\\%", "")            # no BARE % that would comment out a line


@requires_latex
def test_assemble_survives_hostile_content_and_stays_one_page(template_source, fake_llm, workdir):
    """End-to-end: special LaTeX chars in a bullet AND a brace-injecting URL still compile
    to a clean one page (bug 3 + gap D together)."""
    profile = {
        "identity": {"name": "Case Test", "email": "case@example.com",
                     "github": "https://x.com/p}\\twocolumn{evil"},
        "education": [{"school": "State U", "location": "NY",
                       "degree": "B.S. Computer Science", "date": "2019",
                       "courses": "Algorithms, Databases"}],
        "skills": {"Computing": "Python, C, SQL", "Focus": "50% faster, $2M saved, R&D"},
        "experience": [{"org": "R&D Lab", "location": "NY", "roles": [
            {"title": "Engineer", "dates": "2019 - Present",
             "bullets": ["Cut cost by 50% & saved $2M for R&D on tight #1 deadlines.",
                         "Built data pipelines in Python with SQL."]}]}],
        "projects": [{"org": "Toolkit", "location": "NY", "title": "Creator",
                      "dates": "2022", "link": "https://x.com/q}\\evil{",
                      "bullets": ["Shipped an open tool used across teams."]}],
        "extracurricular": [{"title": "Mentor", "date": "2021",
                             "bullets": ["Mentored newcomers in coding."]}],
        "interests": "Chess, cycling",
    }
    res = assemble_cv(template_source, profile, "Engineer role in Python.", fake_llm,
                      workdir, tailor=False)
    assert res.ok, res.summary()
    assert res.compile.pages == 1


# ------------------------------------------------- issue #279: no skill migration across roles
class _MigrateAILLM(FakeLLM):
    """The real failure: the model reframes a banking bullet as AI/LLM work, because the
    person genuinely has AI/LLM experience elsewhere (a side project)."""

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        if "banking" in original_text:
            return ("Spearheaded enterprise AI architecture engagements, translating "
                    "customer requirements into production-ready LLM integration strategies.")
        return original_text


def _career_changer_profile(bank_bullet):
    return {
        "skills": {"Computing": "Python, LLM"},
        "experience": [{"org": "Stanbic Bank", "roles": [
            {"title": "Senior Product Manager", "bullets": [bank_bullet]}]}],
        "projects": [{"title": "RAG study assistant", "bullets": [
            "Built an LLM-powered study assistant in Python with retrieval (AI)."]}],
    }


def test_skill_real_in_a_project_is_not_moved_onto_an_unrelated_role():
    bank = "Led product delivery and stakeholder management across retail and digital banking."
    prof = _career_changer_profile(bank)
    jd = "Applied AI Architect. Design LLM integrations for enterprise customers."
    out = _tailor_bullets(prof, jd, _MigrateAILLM())
    got = out["experience"][0]["roles"][0]["bullets"][0]
    assert got == bank                            # AI/LLM claim on the bank role discarded
    # The project, where the skill IS real, keeps its material.
    assert "LLM" in out["projects"][0]["bullets"][0]


def test_role_only_gets_offered_the_jd_terms_it_demonstrates():
    seen = {}

    class _Spy(FakeLLM):
        def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
            seen[original_text[:10]] = list(supported_jd_terms)
            return original_text

    bank = "Led product delivery and stakeholder management across retail and digital banking."
    prof = _career_changer_profile(bank)
    _tailor_bullets(prof, "Applied AI Architect: LLM, Python.", _Spy())
    assert "LLM" not in seen[bank[:10]] and "Python" not in seen[bank[:10]]
    assert "LLM" in seen["Built an L"]


def test_non_jd_vocab_skill_invented_by_the_model_is_also_caught():
    from tailoring.keywords import introduced_skills
    # "Machine Learning" is not in this JD, but it is a recognised skill the role never showed.
    assert introduced_skills(
        "Ran branch operations.", "Ran branch operations using machine learning forecasts.",
        "Branch Manager\nRan branch operations.", jd_text="Operations manager role.")
    # Re-wording within the role's own material is fine.
    assert not introduced_skills(
        "Built SQL reports.", "Automated weekly SQL reporting for finance leads.",
        "Analyst\nBuilt SQL reports.", jd_text="SQL analyst.")


# --- _grounded(): a stem must be a whole word, not a bare prefix (code review) ---

_PREFIX_LOOKALIKES = [
    # (skill, text that merely shares a prefix, text that genuinely grounds it)
    ("Excel", "delivered excellent client outcomes", "built Excel models"),
    ("Accounting", "was accountable for the branch", "ran the accounts"),
    ("Design", "was the designated lead", "designed the onboarding flow"),
    ("Testing", "gave testimony at hearings", "tested every release"),
    ("Marketing", "sold on the marketplace", "ran markets research"),
    ("Training", "managed trainees", "trained the new hires"),
]


@pytest.mark.parametrize("skill, lookalike, real", _PREFIX_LOOKALIKES)
def test_grounded_rejects_a_prefix_lookalike_but_accepts_the_real_word_form(skill, lookalike, real):
    from tailoring.keywords import _grounded
    assert not _grounded(skill, lookalike)
    assert _grounded(skill, real)


# Curated-vocabulary skills (the ones ``introduced_skills`` actually screens) that share a
# prefix with an ordinary word a bullet might contain.
_VOCAB_LOOKALIKES = [
    ("Excel", "delivered excellent client outcomes"),
    ("Scala", "built a scalable ingestion layer"),
    ("React", "owned the incident reaction plan"),
    ("Redis", "redistributed the budget across desks"),
    ("Spark", "wrote sparkling release notes"),
    ("Agile", "praised for agility under pressure"),
]


@pytest.mark.parametrize("skill, lookalike", _VOCAB_LOOKALIKES)
def test_introduced_skills_catches_a_skill_smuggled_in_on_a_prefix_lookalike(skill, lookalike):
    from tailoring.keywords import introduced_skills
    original = f"Worked on the desk and {lookalike}."
    reword = f"Used {skill} on the desk and {lookalike}."
    jd = f"{skill} required."
    # The original only shares a prefix with the skill: the reword invents it.
    assert introduced_skills(original, reword, original, jd_text=jd) == [skill]
    # Once the role's own material names the skill, the same reword is honest.
    grounded_original = f"Worked on the desk with {skill} and {lookalike}."
    assert introduced_skills(grounded_original, reword, grounded_original, jd_text=jd) == []


@pytest.mark.parametrize("skill, inflection", [
    ("Reporting", "wrote the weekly reports"),
    ("Forecasting", "owned the revenue forecast"),
    ("Optimization", "optimized the ETL pipeline"),
])
def test_introduced_skills_still_accepts_an_ordinary_inflection_as_grounding(skill, inflection):
    from tailoring.keywords import introduced_skills
    original = f"On the desk, {inflection}."
    reword = f"Led {skill} on the desk."
    assert introduced_skills(original, reword, original, jd_text=f"{skill} required.") == []


def test_grounded_still_allows_ordinary_inflections():
    from tailoring.keywords import _grounded
    assert _grounded("Reporting", "wrote the weekly reports")
    assert _grounded("Forecasting", "owned the revenue forecast")
    assert _grounded("Optimization", "optimized the ETL pipeline")
    assert _grounded("Excel", "modelled it in excel")
    assert _grounded("Training", "a trainer for new analysts")


def test_grounded_excel_example_end_to_end():
    from tailoring.keywords import introduced_skills
    original = "Delivered excellent client outcomes across the desk"
    reword = "Built Excel models that delivered excellent client outcomes"
    assert introduced_skills(original, reword, original, jd_text="Excel required") == ["Excel"]


# --- the two bullet-slot walkers must agree (code review) ---

def test_bullet_slot_walkers_visit_the_same_slots_in_the_same_order():
    from tailoring.assembler import _iter_bullet_slots, _iter_bullet_slots_with_grounding
    prof = {
        "projects": [{"title": "P1", "bullets": ["p1a", "p1b"]},
                     {"title": "P2", "tech": "Go", "bullets": ["p2a"]},
                     {"title": "P3 no bullets"}],
        "experience": [{"org": "Org A", "roles": [
                            {"title": "R1", "bullets": ["a1", "a2"]},
                            {"title": "R2", "bullets": []},
                            {"title": "R3", "bullets": ["a3"]}]},
                       {"org": "Org B", "title": "Flat role", "bullets": ["b1"]},
                       {"org": "Org C", "bullets": "not a list"}],
        "extracurricular": [{"org": "Club", "bullets": ["x1", "x2"]}],
    }
    plain = [(id(bl), i) for bl, i in _iter_bullet_slots(prof)]
    full = [(id(bl), i) for bl, i, _ in _iter_bullet_slots_with_grounding(prof)]
    assert plain == full
    # Every editable bullet, in document order, exactly once.
    assert [bl[i] for bl, i in _iter_bullet_slots(prof)] == \
        ["p1a", "p1b", "p2a", "a1", "a2", "a3", "b1", "x1", "x2"]
    # And the grounding text is the bullet's own entry, never another one's.
    for bl, i, grounding in _iter_bullet_slots_with_grounding(prof):
        assert bl[i] in grounding
        assert ("x1" in grounding) == (bl[i] in ("x1", "x2"))


@pytest.mark.parametrize("tool,lookalike,real", [
    ("Docker", "We docked the boat.", "Docker containers for every service."),
    ("Spark", "A sparkling launch.", "Batch jobs on Spark."),
    ("Python", "Pythonic style guides.", "Scripts in Python."),
])
def test_tool_names_are_grounded_only_by_an_exact_mention(tool, lookalike, real):
    from tailoring.keywords import _grounded
    assert not _grounded(tool, lookalike)
    assert _grounded(tool, real)
