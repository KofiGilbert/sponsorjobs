"""Acceptance tests for the intake front door (CLAUDE.md §4).

Covers:
  * the JD-driven conversation collects essentials and DRAFTS the rest,
  * a reworded job TITLE is flagged for confirmation (company/dates kept as-is),
  * the collected answers are saved to the local profile store,
  * a second job reuses the saved profile without re-asking essentials,
  * both runs produce a one-page CV.

Drives the flow with a scripted IO and the deterministic FakeLLM — no console,
no network.
"""

from __future__ import annotations

import pytest

from intake.conversation import ScriptedIO, run_intake
from intake.profile_store import ProfileStore
from conftest import requires_latex

# A short JD keeps the deterministic FakeLLM output bounded and stable.
JD = (
    "Senior Machine Learning Engineer. Build ML systems in Python on AWS with "
    "SQL and RAG. Experience with MLOps is a plus."
)

# Answers for a first-time run, in the exact order the conversation asks them.
FIRST_RUN_ANSWERS = [
    # identity
    "Maya Rodriguez",
    "maya@example.com",
    "(312) 555-0148",
    "Chicago, IL",
    "https://linkedin.com/in/maya",
    "https://github.com/maya",
    "https://maya.dev",
    # education block (one row, then blank to finish)
    "Northwestern University | MS in Artificial Intelligence | June 2018 | Evanston, IL",
    "",
    # experience block (two rows, then blank)
    "Continental Trust Bank | Senior Engineer | Feb 2021 - Present | Chicago, IL",
    "Meridian Analytics | ML Engineer | July 2018 - Feb 2021 | Chicago, IL",
    "",
    # confirm the suggested title on the first job
    "keep",
    # accept the drafted bullets
    "accept",
]


@requires_latex
def test_first_run_collects_drafts_saves_and_builds_one_page(
    template_source, fake_llm, workdir
):
    io = ScriptedIO(FIRST_RUN_ANSWERS)
    store = ProfileStore(":memory:")

    result = run_intake(io, JD, template_source, fake_llm, store, workdir)

    # It produced a one-page CV.
    assert result.mode == "new" and result.reused is False
    assert result.assembled.ok, result.assembled.summary()
    assert result.assembled.compile.pages == 1
    assert result.pdf_path is not None and result.pdf_path.exists()

    profile = result.profile
    # Essentials came from the person, verbatim.
    assert profile["identity"]["name"] == "Maya Rodriguez"
    assert profile["experience"][0]["org"] == "Continental Trust Bank"
    # Dates are reformatted to the template's abbreviated-month convention.
    assert profile["experience"][0]["dates"] == "Feb 2021 - Present"
    assert profile["education"][0]["school"] == "Northwestern University"

    # The agent DRAFTED the content-heavy parts.
    assert profile["skills"] and profile["projects"] and profile["interests"]
    assert profile["experience"][0]["bullets"]

    # A reworded TITLE was flagged for confirmation (a flag, not a block); the
    # person's original title is preserved for reference.
    assert any(p.startswith("[confirm title]") for p in io.prompts)
    assert profile["experience"][0]["title_original"] == "Senior Engineer"

    # Saved to the local store for reuse.
    assert store.exists("default")
    assert store.load("default")["identity"]["name"] == "Maya Rodriguez"


@requires_latex
def test_second_job_reuses_saved_profile_without_reasking(
    template_source, fake_llm, workdir
):
    store = ProfileStore(":memory:")

    # First job: full intake (populates the store).
    run_intake(
        ScriptedIO(FIRST_RUN_ANSWERS), JD, template_source, fake_llm, store, workdir,
    )

    # Second job: a different JD; choose to reuse, then accept.
    io2 = ScriptedIO(["reuse", "accept"])
    jd2 = "Machine Learning Platform Lead. Python, AWS, Kubernetes, MLOps."
    result = run_intake(io2, jd2, template_source, fake_llm, store, workdir)

    assert result.reused is True and result.mode == "reuse"
    assert result.assembled.compile.pages == 1
    assert result.pdf_path is not None and result.pdf_path.exists()

    # It did NOT re-ask any essentials the second time.
    joined = " ".join(io2.prompts)
    assert "Full name" not in joined
    assert "work history" not in joined
    assert "education" not in joined.lower()
    # It's still the same person's profile, built from memory.
    assert result.profile["identity"]["name"] == "Maya Rodriguez"


def test_reuse_mode_selection_maps_answers():
    from intake.conversation import _ask_reuse_mode

    assert _ask_reuse_mode(ScriptedIO(["reuse"])) == "reuse"
    assert _ask_reuse_mode(ScriptedIO(["refresh"])) == "refresh"
    assert _ask_reuse_mode(ScriptedIO(["add"])) == "add"
    assert _ask_reuse_mode(ScriptedIO([""])) == "reuse"  # default


def test_confirm_titles_revert_restores_original(fake_llm):
    from intake.conversation import confirm_titles

    profile = fake_llm.draft_profile(JD, {
        "identity": {"name": "X"},
        "education": [],
        "experience": [{"org": "Acme", "title": "Engineer", "dates": "2020-2023"}],
    })
    # The fake flags the first title; reverting restores the person's original.
    assert profile["experience"][0]["title_suggested"] is True
    confirm_titles(ScriptedIO(["revert"]), profile)
    assert profile["experience"][0]["title"] == "Engineer"
    assert profile["experience"][0]["title_suggested"] is False
