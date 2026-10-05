"""Acceptance tests for Commit 2 (CLAUDE.md §8): transcript -> real JD-relevant
courses, and flagged placeholder projects that can never survive into a finalized
CV (``suggested: true`` drives the badge, the .tex marker, a blocked accept(), and
a disabled download).
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from tailoring.assembler import extract_preamble, render_cv
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = "Data Scientist - Acme - Chicago. Python, SQL, machine learning, statistics."

TRANSCRIPT = (
    "University Transcript\nStudent: Ana Cruz   GPA: 3.8\n"
    "Fall 2015\nCS 229 Machine Learning        A\n"
    "MATH 221 Introduction to Statistics   A-\n"
    "HIST 101 Ancient History       B\n"
    "Spring 2016\nCS 246 Data Mining             A\n"
    "ART 100 Basket Weaving         B+\n"
)

# Everything the gate needs EXCEPT projects, so the projects flow stays open.
COMPLETE_NO_PROJECTS = (
    "I'm Ana Cruz, ana@example.com, https://github.com/ana, "
    "https://www.linkedin.com/in/ana, https://ablog.com . I live at 5 Elm St, Chicago, IL 60607. "
    "M.S. in Statistics at Northwestern in 2018. My courses were Machine Learning, Statistics. "
    "I work as Analyst at Acme (Chicago, IL) since Jan 2020."
)


def _mk(template_source, fake_llm, workdir):
    return WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


# -- transcript -> courses --------------------------------------------- #

def test_transcript_populates_courses_from_real_content(template_source, fake_llm, workdir):
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    # Give a degree but no experience yet, so ingesting the transcript asks for more
    # (rather than building) and we can inspect the courses it populated.
    s.essentials["education"] = [{"school": "Northwestern", "degree": "M.S. in Statistics",
                                  "date": "2018", "courses": ""}]
    s.ingest_transcript(TRANSCRIPT, "transcript.pdf")

    courses = s.essentials["education"][0]["courses"]
    assert "Machine Learning" in courses          # real, JD-relevant courses selected
    assert "Introduction to Statistics" in courses
    assert "Basket Weaving" not in courses         # irrelevant real course dropped
    assert "Ancient History" not in courses

    # A clean summary went to memory; raw transcript text was NOT indexed.
    mem = " ".join(t["content"] for t in s.palace.load_history())
    assert "uploaded a transcript" in mem and "courses populated" in mem
    assert "GPA" not in mem and "Basket Weaving" not in mem


# -- flagged placeholder projects -------------------------------------- #

def test_suggested_project_is_visibly_marked_in_tex(template_source):
    profile = {"identity": {"name": "Ana Cruz"},
               "projects": [{"org": "Credit Risk Model", "location": "Personal Project",
                             "dates": "2024", "suggested": True, "bullets": ["Modeled defaults"]}]}
    tex = render_cv(extract_preamble(template_source), profile)
    assert "[SUGGESTED" in tex                      # marked in the document itself


@requires_latex
def test_flagged_placeholder_blocks_accept_and_export(template_source, fake_llm, workdir):
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s.submit(COMPLETE_NO_PROJECTS)
    st = s.submit("suggest some projects I could build")
    assert st["phase"] == "review"
    assert st.get("suggested_projects")             # a flagged placeholder is present
    assert st.get("blocked_finalize") is True       # export/accept disabled in the UI

    blocked = s.accept()                            # server-side hard block
    assert blocked.get("ok") is False and blocked.get("blocked") is True
    assert "replace" in blocked["reason"].lower()

    # the rendered CV marks it, so even a preview PDF can't pass as final
    assert "[SUGGESTED" in render_cv(extract_preamble(template_source), s.profile)


@requires_latex
def test_declining_projects_removes_it_without_forcing_details(template_source, fake_llm, workdir):
    """A project on the CV must have location + dates, but the person is free to have
    NO projects: 'I don't have projects / skip this project' removes it gracefully,
    never forcing them to detail one."""
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s.submit(COMPLETE_NO_PROJECTS)
    s.submit("suggest some projects")
    assert s._suggested_projects() and s.accept().get("ok") is False   # present, blocks export

    st = s.submit("actually I don't have projects, skip it")           # decline, don't detail it
    assert not s._suggested_projects()
    assert st["phase"] == "review"
    assert s.profile.get("projects") == []
    assert s.accept().get("ok") is True                               # finalizes cleanly


def test_capture_projects_from_explicit_chat_list(template_source, fake_llm, workdir):
    """An explicit chat list ('add these projects: (1) X, (2) Y') is captured
    deterministically with the GitHub link, even when the LLM extractor misses it."""
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s._capture_projects(
        "Please include my GitHub: https://github.com/KofiGilbert . From my "
        "Data-Analytics repo, add these projects to the Projects section: "
        "(1) Iris Flowers Classification, a machine-learning classification project on "
        "the Iris dataset; (2) an Equation Solver; and (3) a set of SQL data-analytics "
        "projects. Keep the descriptions accurate, do not overstate them.")
    projs = s.essentials.get("projects") or []
    names = [p.get("org") for p in projs]
    assert "Iris Flowers Classification" in names
    assert "Equation Solver" in names            # leading article + trailing '; and' stripped
    assert any("SQL" in n for n in names)
    assert not any("overstate" in (p.get("org", "") + " ".join(p.get("bullets", []))).lower()
                   for p in projs)                        # trailing instruction not captured
    assert all(p.get("link") == "https://github.com/KofiGilbert" for p in projs)
    assert all(p.get("personal") for p in projs)          # portfolio, not employment
    assert s.essentials["identity"]["github"] == "https://github.com/KofiGilbert"


def test_linked_project_skips_dates_location_gate():
    """A personal/linked portfolio project must NOT trigger the employment-style
    location+dates gate (that friction was blocking real GitHub projects)."""
    from ui.session import _missing_skeleton
    linked = [{"org": "Iris Classification", "link": "https://github.com/x", "personal": True}]
    gaps = _missing_skeleton({"name": "A"}, [], [], linked, declined=[], critical_only=True)
    assert not any("dates for" in g or "location for" in g for g in gaps)


def test_validate_skills_drops_unsupported_terms(template_source, fake_llm, workdir):
    """Anti-fabrication: a drafted skills line is pruned to only what the person's own
    material supports — invented tools (PyTorch, AWS) are dropped, real ones kept."""
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s.essentials["skills_input"] = ["Python", "SQL", "Java", "Tableau"]
    s.essentials["experience"] = []
    s.profile["experience"] = []
    s.profile["skills"] = {
        "Languages & Tools": "Python, SQL, Java, Tableau, PyTorch, TensorFlow, AWS, Docker",
    }
    s._validate_skills()
    kept = s.profile["skills"]["Languages & Tools"]
    for real in ("Python", "SQL", "Java", "Tableau"):
        assert real in kept
    for fake in ("PyTorch", "TensorFlow", "AWS", "Docker"):
        assert fake not in kept


def test_transcript_courses_survive_next_message(template_source, fake_llm, workdir):
    """Courses populated from a transcript must not be wiped by the extractor on the
    NEXT chat turn (the cross-turn clobber bug)."""
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s.essentials["education"] = [{"school": "Northwestern", "degree": "M.S. in Statistics",
                                  "date": "2018", "courses": ""}]
    s.essentials["experience"] = [{"org": "Acme", "title": "Analyst",
                                   "dates": "Jan 2020 - Present", "location": "Chicago, IL"}]
    s.ingest_transcript(TRANSCRIPT, "transcript.pdf")
    assert "Machine Learning" in s.essentials["education"][0]["courses"]
    s.submit("Thanks, that all looks right.")             # an unrelated follow-up turn
    assert "Machine Learning" in s.essentials["education"][0]["courses"]   # still there


@requires_latex
def test_removing_placeholder_unblocks_finalization(template_source, fake_llm, workdir):
    s = _mk(template_source, fake_llm, workdir)
    s.start()
    s.submit(COMPLETE_NO_PROJECTS)
    s.submit("suggest some projects")
    assert s._suggested_projects()                  # flagged placeholder present
    assert s.accept().get("ok") is False            # blocked

    st = s.submit("please remove the suggested project")
    assert not s._suggested_projects()              # gone
    assert not st.get("blocked_finalize")
    assert s.accept().get("ok") is True             # now finalizes
