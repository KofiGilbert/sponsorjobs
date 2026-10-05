"""§10: JD-driven intake, saved-profile reuse, and autonomous mode.

Covers:
  * "The JD-driven intake asks only role-relevant questions, and saved answers
     can be reused, refreshed, or added to on a new application."
  * "Autonomous mode produces a tailored CV from the saved profile with no
     per-application input."
"""

from __future__ import annotations

import pytest

from intake.intake import plan_intake, run_autonomous
from intake.profile_store import ProfileStore
from tailoring.keywords import term_present
from conftest import requires_latex


# -- JD-driven intake --------------------------------------------------- #

def test_intake_questions_are_jd_specific_not_fixed(
    fake_llm, jd_quant, jd_swe
):
    plan_q = plan_intake(jd_quant, fake_llm)
    plan_s = plan_intake(jd_swe, fake_llm)

    # Different JDs must yield different questions (not a fixed questionnaire).
    assert plan_q.questions != plan_s.questions

    quant_blob = "\n".join(plan_q.questions)
    swe_blob = "\n".join(plan_s.questions)

    # Each question set references terms distinctive to ITS OWN job description,
    # proving the questions are JD-driven rather than a fixed questionnaire.
    assert term_present("risk management", quant_blob)      # quant-only term
    assert not term_present("risk management", swe_blob)

    assert term_present("cloud infrastructure", swe_blob)   # swe-only term
    assert not term_present("cloud infrastructure", quant_blob)

    # Both still ask for the identity/date fields the JD itself can't supply.
    assert term_present("name", quant_blob) and term_present("name", swe_blob)


def test_intake_uses_saved_profile_when_present(fake_llm, jd_quant, sample_profile):
    with ProfileStore(":memory:") as store:
        store.save(sample_profile)
        plan = plan_intake(jd_quant, fake_llm, store=store)
        assert plan.reuse_available is True


# -- saved & reusable profile: reuse / refresh / add -------------------- #

def test_profile_reuse_returns_saved_as_is(sample_profile):
    with ProfileStore(":memory:") as store:
        store.save(sample_profile)
        assert store.reuse() == sample_profile


def test_profile_add_merges_onto_saved(sample_profile):
    with ProfileStore(":memory:") as store:
        store.save(sample_profile)
        merged = store.add({"certifications": ["CFA L1"],
                            "contact": {"phone": "(999) 000-0000"}})
        # New top-level field added.
        assert merged["certifications"] == ["CFA L1"]
        # Nested field updated without dropping siblings.
        assert merged["contact"]["phone"] == "(999) 000-0000"
        assert merged["contact"]["email"] == sample_profile["contact"]["email"]
        # Untouched fields preserved.
        assert merged["skills"] == sample_profile["skills"]
        # Persisted.
        assert store.load()["certifications"] == ["CFA L1"]


def test_profile_refresh_replaces_entirely(sample_profile):
    with ProfileStore(":memory:") as store:
        store.save(sample_profile)
        fresh = {"name": "New Person", "skills": ["Go"]}
        out = store.refresh(fresh)
        assert out == fresh
        assert store.load() == fresh
        assert "experience" not in store.load()  # old data gone


def test_reuse_without_saved_profile_errors():
    with ProfileStore(":memory:") as store:
        with pytest.raises(KeyError):
            store.reuse()


# -- autonomous mode ---------------------------------------------------- #

@requires_latex
def test_autonomous_builds_from_saved_profile_no_input(
    template_source, sample_profile, jd_quant, fake_llm, workdir
):
    """Autonomous mode needs only the saved profile + JD — no per-app answers."""
    with ProfileStore(":memory:") as store:
        store.save(sample_profile)
        # Note: run_autonomous takes NO answers argument — that is the point.
        res = run_autonomous(
            jd_text=jd_quant,
            template_source=template_source,
            llm=fake_llm,
            workdir=workdir,
            store=store,
        )
    assert res.ok, res.summary()
    assert res.heal.compile.pages == 1
    assert res.edits  # it actually tailored something from the saved profile
    assert res.pdf_path is not None and res.pdf_path.exists()


def test_autonomous_requires_a_saved_profile(
    template_source, jd_quant, fake_llm, workdir
):
    with ProfileStore(":memory:") as store:
        with pytest.raises(KeyError):
            run_autonomous(
                jd_text=jd_quant,
                template_source=template_source,
                llm=fake_llm,
                workdir=workdir,
                store=store,
            )
