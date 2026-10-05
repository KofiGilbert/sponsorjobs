"""JD-driven intake and the two run modes (CLAUDE.md §4a/§4c).

Intake is *not* a fixed questionnaire. The AI reads the JD first and asks only
the questions relevant to that role. Two ways to run:

* **guided**     — surface JD-driven questions, tailor from the answers,
* **autonomous** — "just build it for this JD" from the saved profile, no
  per-application input.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llm.base import LLMBackend
from tailoring.tailor import TailorResult, tailor_resume
from .profile_store import ProfileStore


@dataclass
class IntakePlan:
    """What the guided flow will ask for this specific JD."""

    jd_text: str
    questions: list[str]
    reuse_available: bool  # is there a saved profile to reuse/add onto?


def plan_intake(
    jd_text: str,
    llm: LLMBackend,
    store: ProfileStore | None = None,
    profile_name: str = "default",
) -> IntakePlan:
    """Read the JD and produce role-relevant questions (guided mode, §4a)."""
    saved = store.load(profile_name) if store else None
    questions = llm.generate_intake_questions(jd_text, saved)
    return IntakePlan(
        jd_text=jd_text,
        questions=questions,
        reuse_available=saved is not None,
    )


def run_guided(
    jd_text: str,
    answers_profile: dict,
    template_source: str,
    llm: LLMBackend,
    workdir: str | Path,
    store: ProfileStore | None = None,
    profile_name: str = "default",
    persist: str | None = "add",
) -> TailorResult:
    """Guided run: tailor from the person's answers for this JD.

    ``persist`` controls how the answers update the saved profile:
    ``"add"`` (merge), ``"refresh"`` (replace), or ``None`` (don't save).
    """
    if store is not None and persist:
        if persist == "add":
            profile = store.add(answers_profile, profile_name)
        elif persist == "refresh":
            profile = store.refresh(answers_profile, profile_name)
        else:
            raise ValueError(f"unknown persist mode {persist!r}")
    else:
        profile = answers_profile

    return tailor_resume(
        template_source=template_source,
        profile=profile,
        jd_text=jd_text,
        llm=llm,
        workdir=workdir,
    )


def run_autonomous(
    jd_text: str,
    template_source: str,
    llm: LLMBackend,
    workdir: str | Path,
    store: ProfileStore,
    profile_name: str = "default",
) -> TailorResult:
    """Autonomous run (§4c): build the best-fit CV from the SAVED profile only.

    No per-application input is requested. Requires a saved profile.
    """
    profile = store.reuse(profile_name)  # raises if nothing is saved
    return tailor_resume(
        template_source=template_source,
        profile=profile,
        jd_text=jd_text,
        llm=llm,
        workdir=workdir,
    )
