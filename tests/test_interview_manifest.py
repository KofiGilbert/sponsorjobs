"""Increment 2 (CLAUDE.md §4a, §13): the per-template question bank drives a
natural interview that draws out EVERY template slot — including Extracurricular
and Additional Information (Interests) — via indirect leading questions, never a
robotic "list your extracurriculars". The question bank is template config; the
person's answers live in essentials/MemPalace.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from intake.template_manifest import elicit_leads, load_manifest
from tailoring.assembler import extract_preamble, render_cv
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Data Analyst - Meridian - Chicago. SQL, Python, dashboards, statistics."

SKELETON = (
    "I'm Ada Obi, ada@example.com. I live at 5 Elm St, Chicago, IL 60607. "
    "B.S. in Statistics at DePaul University in 2019. Data Analyst at Meridian in "
    "Chicago, IL since Jan 2021. No github, linkedin, or blog. Skip courses. No projects."
)


def _s(template_source, fake_llm, workdir):
    return WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


def test_manifest_ships_question_bank_for_extracurricular_and_interests():
    m = load_manifest("shetty")
    assert "extracurricular" in m.get("sections", [])
    assert "interests" in m.get("sections", [])
    assert elicit_leads(m, "extracurricular")            # warm leading questions exist
    assert elicit_leads(m, "interests")
    # Style guidance is present so the LLM phrases naturally.
    assert "friend" in m.get("interview_style", "").lower()


def test_enrichment_topics_include_extracurricular_and_interests(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Meridian", "title": "Analyst", "dates": "2021 - Present",
                                   "location": "Chicago, IL", "bullets": ["Real work done"]}]
    keys = [k for k, _ in s._enrich_topics()]
    assert "extracurricular" in keys and "interests" in keys


def test_invite_for_extracurricular_is_indirect_not_robotic(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    # Only extracurricular/interests remain to ask.
    s.essentials["experience"] = [{"org": "Meridian", "title": "Analyst", "dates": "2021 - Present",
                                   "location": "Chicago, IL", "bullets": ["Real work done"]}]
    s.essentials["projects"] = [{"org": "P", "title": "T", "location": "Chicago, IL",
                                 "dates": "2022", "bullets": ["did"]}]
    s.essentials["skills_input"] = ["SQL"]
    s.essentials["certifications"] = ["AWS Certified"]
    invite = s._enrich_invite()
    low = invite.lower()
    assert "list your extracurricular" not in low            # never the robotic ask
    assert any(p in low for p in ("outside of work", "outside work", "throw yourself",
                                  "proud of", "started or organized"))   # a manifest lead


@__import__("conftest", fromlist=["requires_latex"]).requires_latex
def test_interview_draws_out_and_renders_both_sections(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit("At Meridian I built automated dashboards that cut reporting time in half.")
    s.submit("Outside work I volunteer at the Chicago Food Bank and I founded a Data for Good meetup.")
    s.submit("I enjoy chess, hiking, and Formula 1.")
    st = s.submit("that's everything")
    assert st["phase"] == "review"
    assert s.essentials.get("extracurricular")               # drawn out, stored
    assert "chess" in str(s.essentials.get("interests"))
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "Extracurricular" in tex and "Food Bank" in tex   # renders in its template slot
    assert "Interests:" in tex and "chess" in tex


@__import__("conftest", fromlist=["requires_latex"]).requires_latex
def test_material_given_with_thats_everything_is_not_lost(template_source, fake_llm, workdir):
    """Regression: content provided in the SAME message as 'that's everything' must
    still be absorbed before finalizing (absorb-then-finalize)."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit("At Meridian I built dashboards that cut reporting time in half.")
    st = s.submit("I volunteer at the Chicago Food Bank, and I enjoy chess and hiking. That's everything.")
    assert st["phase"] == "review"
    assert s.essentials.get("extracurricular")               # captured, not lost
    assert "chess" in str(s.essentials.get("interests"))
    assert s.essentials.get("enrich_done") is True           # and finalized


def test_extracurricular_and_interests_are_declinable(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Meridian", "title": "Analyst", "dates": "2021 - Present",
                                   "location": "Chicago, IL", "bullets": ["Real work done"]}]
    s._apply_side_channels("I don't have any extracurriculars, and no interests to list.")
    assert "extracurricular" in s.essentials["declined"]
    assert "interests" in s.essentials["declined"]
    keys = [k for k, _ in s._enrich_topics()]
    assert "extracurricular" not in keys and "interests" not in keys
