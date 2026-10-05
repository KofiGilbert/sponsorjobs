"""The intake front door — a JD-driven conversation that builds a tailored CV.

CLAUDE.md §4: the agent reads the JD first, then asks the person only for what it
genuinely can't infer (name, contact, links, schools/degrees/dates, companies +
employment dates). For skills, projects, experience bullets, and extracurriculars
it *drafts* strong JD-matched content and asks the person to confirm or edit —
never a field-by-field interrogation. It leads with a complete draft.

The conversation logic is decoupled from I/O via :class:`IntakeIO`, so the same
flow drives a console chat (:class:`ConsoleIO`) and the acceptance tests (a
scripted IO). Answers are saved to the local SQLite profile store and reused on
later jobs (reuse / refresh / add).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMBackend
from tailoring.assembler import AssembleResult, assemble_cv, normalize_profile
from .profile_store import ProfileStore


# --------------------------------------------------------------------------- #
# I/O abstraction
# --------------------------------------------------------------------------- #

class IntakeIO:
    """Minimal prompt/print surface the conversation depends on."""

    def ask(self, prompt: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def say(self, message: str = "") -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleIO(IntakeIO):
    def ask(self, prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""

    def say(self, message: str = "") -> None:
        print(message)


class ScriptedIO(IntakeIO):
    """Test IO: answers are popped in order; every prompt is recorded."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else ""

    def say(self, message: str = "") -> None:
        self.output.append(message)


# --------------------------------------------------------------------------- #
# Collecting the essentials (only what the agent can't infer)
# --------------------------------------------------------------------------- #

def _read_block(io: IntakeIO, header: str, example: str) -> list[list[str]]:
    """Read pipe-delimited rows until a blank line; return split, stripped cells."""
    io.say(header)
    io.say(f"  Format: {example}")
    io.say("  (enter one per line; blank line to finish)")
    rows: list[list[str]] = []
    while True:
        line = io.ask("> ").strip()
        if not line:
            break
        rows.append([c.strip() for c in line.split("|")])
    return rows


def _cell(row: list[str], i: int) -> str:
    return row[i] if i < len(row) else ""


# Single source of truth for the essentials the agent can't infer. Both the
# console driver and the web session (ui/session.py) build their questions from
# these, so "what we ask" lives in exactly one place.
IDENTITY_QUESTIONS = [
    ("name", "Full name", False),
    ("email", "Email", False),
    ("phone", "Phone", True),
    ("address", "City / address", True),
    ("linkedin", "LinkedIn URL", True),
    ("github", "GitHub URL", True),
    ("blog", "Blog / portfolio URL", True),
]
EDUCATION_FORMAT = "School | Degree | Graduation date | Location"
EXPERIENCE_FORMAT = "Company | Job title | Employment dates | Location"


def parse_education_row(cells: list[str]) -> dict:
    return {
        "school": _cell(cells, 0), "degree": _cell(cells, 1),
        "date": _cell(cells, 2), "location": _cell(cells, 3), "courses": "",
    }


def parse_experience_row(cells: list[str]) -> dict:
    return {
        "org": _cell(cells, 0), "title": _cell(cells, 1),
        "dates": _cell(cells, 2), "location": _cell(cells, 3),
    }


def collect_identity(io: IntakeIO) -> dict:
    io.say("First, a few contact details (only what should appear on the resume).")
    ident = {}
    for key, label, optional in IDENTITY_QUESTIONS:
        suffix = " (optional): " if optional else ": "
        ident[key] = io.ask(label + suffix).strip()
    return {k: v for k, v in ident.items() if v}


def collect_education(io: IntakeIO) -> list[dict]:
    rows = _read_block(io, "Your education:", EDUCATION_FORMAT)
    return [parse_education_row(r) for r in rows]


def collect_experience(io: IntakeIO) -> list[dict]:
    rows = _read_block(io, "Your work history:", EXPERIENCE_FORMAT)
    return [parse_experience_row(r) for r in rows]


def collect_essentials(io: IntakeIO) -> dict:
    return {
        "identity": collect_identity(io),
        "education": collect_education(io),
        "experience": collect_experience(io),
    }


# --------------------------------------------------------------------------- #
# Flagging reworded titles, and reviewing the drafted bullets
# --------------------------------------------------------------------------- #

def _roles_of(profile: dict):
    for e in profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            yield e, r


def apply_title_decision(role: dict, decision: str, custom: str = "") -> None:
    """Resolve a flagged (reworded) title — shared by console and web (§4).

    ``decision``: "keep" (accept the suggestion), "revert" (restore the person's
    original), or "custom" (use ``custom``). Clears the suggested flag either way.
    """
    d = (decision or "").strip().lower()
    if d in ("revert", "original", "no"):
        role["title"] = role.get("title_original", role.get("title", ""))
    elif d in ("custom", "edit") and custom.strip():
        role["title"] = custom.strip()
    role["title_suggested"] = False


def confirm_titles(io: IntakeIO, profile: dict) -> None:
    """CLAUDE.md §4/persona: a reworded TITLE is a flag, not a block.

    Company, dates, and schools are the person's as-is; only a suggested title
    needs confirming.
    """
    for _entry, role in _roles_of(profile):
        if not role.get("title_suggested"):
            continue
        original = role.get("title_original", "")
        suggested = role.get("title", "")
        ans = io.ask(
            f"[confirm title] I suggested \"{suggested}\" to match the job "
            f"(you entered \"{original}\"). "
            f"Keep it, revert, or type a different title? [keep/revert/<text>]: "
        ).strip()
        low = ans.lower()
        if low in ("revert", "r", "original", "no"):
            role["title"] = original
            role["title_suggested"] = False
        elif low in ("keep", "k", "yes", "y", ""):
            role["title_suggested"] = False  # confirmed
        else:
            role["title"] = ans
            role["title_suggested"] = False


def _prose_bullet_slots(profile: dict):
    """Ordered (list, index) for every editable prose bullet."""
    slots = []
    for e in profile.get("projects", []) + profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            bl = r.get("bullets")
            if isinstance(bl, list):
                for i in range(len(bl)):
                    slots.append((bl, i))
    for it in profile.get("extracurricular", []):
        bl = it.get("bullets")
        if isinstance(bl, list):
            for i in range(len(bl)):
                slots.append((bl, i))
    return slots


def review_and_edit(io: IntakeIO, profile: dict) -> bool:
    """Show drafted bullets; let the person edit before accepting.

    Returns True if any bullet was edited (so the caller re-assembles).
    """
    edited = False
    while True:
        slots = _prose_bullet_slots(profile)
        io.say("\nDrafted bullets (edit any before accepting):")
        for n, (bl, i) in enumerate(slots, 1):
            io.say(f"  {n}. {bl[i]}")
        ans = io.ask(
            "Type 'accept', or 'edit <n> <new text>' to rewrite a bullet: "
        ).strip()
        if ans.lower() in ("accept", "a", "ok", "done", ""):
            return edited
        if ans.lower().startswith("edit"):
            parts = ans.split(None, 2)
            if len(parts) == 3 and parts[1].isdigit():
                n = int(parts[1])
                if 1 <= n <= len(slots):
                    bl, i = slots[n - 1]
                    bl[i] = parts[2].strip()
                    edited = True
                    continue
            io.say("  (couldn't parse — use: edit 2 Your new bullet text)")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class IntakeResult:
    profile: dict
    assembled: AssembleResult
    reused: bool
    mode: str  # "new" | "reuse" | "refresh" | "add"

    @property
    def pdf_path(self) -> Path | None:
        return self.assembled.pdf_path


def _ask_reuse_mode(io: IntakeIO) -> str:
    ans = io.ask(
        "You have a saved profile. Reuse it as-is, refresh (start over), or add "
        "to it? [reuse/refresh/add]: "
    ).strip().lower()
    if ans.startswith("ref"):
        return "refresh"
    if ans.startswith("add"):
        return "add"
    return "reuse"


def run_intake(
    io: IntakeIO,
    jd_text: str,
    template_source: str,
    llm: LLMBackend,
    store: ProfileStore,
    workdir: str | Path,
    profile_name: str = "default",
    jobname: str = "cv",
) -> IntakeResult:
    """Drive the full JD-driven intake and produce a tailored one-page CV."""
    io.say("Reading the job description first...\n")

    mode = "new"
    if store.exists(profile_name):
        mode = _ask_reuse_mode(io)

    if mode == "reuse":
        profile = normalize_profile(store.reuse(profile_name))
        io.say("Building a fresh resume from your saved profile, no re-asking.")
        # Reword the saved bullets toward THIS job.
        assembled = assemble_cv(
            template_source, profile, jd_text, llm, workdir,
            jobname=jobname, tailor=True,
        )
        review_and_edit(io, profile)
        return IntakeResult(profile, assembled, reused=True, mode=mode)

    if mode == "add":
        profile = store.reuse(profile_name)
        io.say("Add more experience to your saved profile.")
        new_jobs = collect_experience(io)
        if new_jobs:
            essentials = {
                "identity": profile.get("identity", {}),
                "education": profile.get("education", []),
                "experience": new_jobs,
            }
            drafted = normalize_profile(llm.draft_profile(jd_text, essentials))
            profile.setdefault("experience", []).extend(drafted.get("experience", []))
            confirm_titles(io, profile)
        store.save(profile, profile_name)
    else:
        # "new" or "refresh": collect essentials and draft from scratch.
        essentials = collect_essentials(io)
        io.say("\nDrafting a JD-matched resume from your details...")
        profile = normalize_profile(llm.draft_profile(jd_text, essentials))
        confirm_titles(io, profile)
        if mode == "refresh":
            store.refresh(profile, profile_name)
        else:
            store.save(profile, profile_name)

    # Draft bullets are already JD-matched, so just render + fit (no re-tailor).
    assembled = assemble_cv(
        template_source, profile, jd_text, llm, workdir,
        jobname=jobname, tailor=False,
    )
    if review_and_edit(io, profile):
        assembled = assemble_cv(
            template_source, profile, jd_text, llm, workdir,
            jobname=jobname, tailor=False,
        )
    store.save(profile, profile_name)  # persist any edits

    return IntakeResult(profile, assembled, reused=False, mode=mode)
