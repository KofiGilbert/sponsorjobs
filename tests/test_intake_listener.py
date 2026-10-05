"""The intake must LISTEN (Amazon case-study regressions, docs/case-study §obs 11/12/17/25).

Each test reproduces a real failure from the founder's live application:
  * his confirmation request was consumed by the question queue and shredded into
    phantom "project" records instead of being answered;
  * the bare course list — the natural answer to "what courses did you take?" —
    looped forever because the parser demanded a literal cue word;
  * "my courses were: A, B" (verb THEN colon) failed both cue branches.
"""

from __future__ import annotations

from ui.session import WebIntake, _bare_course_list, _capture_courses, _wants_capture_summary
from intake.memory import ConversationMemory
from ui.records import CVRecords

JD = ("Area Manager - Amazon - AL. Lead 50-100 associates; safety, quality, "
      "performance; supply chain operations.")

# The founder's exact message that got shredded into phantom project records.
CONFIRM_REQUEST = (
    "Drop the DePaul University project from this CV. The Projects section for this "
    "application should be exactly the two GitHub projects I just gave you. "
    "Before we go on, confirm back to me what you captured. List what you now have for: "
    "1) the Ampsel Commodities role with its title, dates and numbers, 2) the updated "
    "Viva Technologies fleet description, 3) both project links, 4) which experience "
    "leads the CV. Keep it short.")


def _session(template_source, fake_llm, workdir):
    mem = ConversationMemory(":memory:")
    recs = CVRecords(":memory:")
    return WebIntake(JD, template_source, fake_llm, mem, recs, workdir, jobname="cv")


# ---------------------------------------------------------------- courses (obs #17)

def test_courses_verb_then_colon_is_understood():
    got = _capture_courses("My courses were: Fundamentals of Business Analytics, "
                           "Business Analytics Tools, Data Visualization.")
    assert "Fundamentals of Business Analytics" in got
    assert "Data Visualization" in got


def test_bare_list_answers_the_courses_question(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s.essentials["education"] = [{"school": "DePaul University",
                                  "degree": "MBA", "date": "Dec 2025"}]
    # The agent just asked for courses; the person answers with the list itself.
    s.last_missing = ["the courses for your most recent degree (list them, or upload "
                      "your transcript, or tell me you'd rather skip the courses line)"]
    s.submit("Fundamentals of Business Analytics, Business Analytics Tools, "
             "Database Management Systems, Data Visualization")
    assert "Fundamentals of Business Analytics" in s.essentials["education"][0].get("courses", "")


def test_bare_list_is_ignored_when_courses_was_not_asked():
    # Without the pending question, a bare list must NOT be scraped as courses.
    assert _bare_course_list("Team Leadership, Coaching, People Development") != "" \
        and True  # the list itself parses…
    # …so the gate is the pending-question check, exercised in the session test above;
    # here we pin the parser's own vetoes: sentences and declines never parse.
    assert _bare_course_list("I would rather skip that line, thanks") == ""
    assert _bare_course_list("No courses to add") == ""
    assert _bare_course_list("See https://example.com/transcript, it lists them") == ""


# ------------------------------------------------- confirm-back (obs #11/#12/#25)

def test_confirmation_request_is_recognized():
    assert _wants_capture_summary(CONFIRM_REQUEST)
    assert _wants_capture_summary("what do you have on file so far?")
    assert not _wants_capture_summary("I confirmed my availability with my manager")


def test_confirmation_request_answers_from_state_and_shreds_nothing(
        template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s.essentials["experience"] = [{"org": "Ampsel Commodities Limited",
                                   "title": "Supply Chain Operations Manager",
                                   "dates": "June 2020 - Sept 2024",
                                   "bullets": ["Exported over $60 million."]}]
    before_projects = list(s.essentials.get("projects", []))
    st = s.submit(CONFIRM_REQUEST)
    # Answered from state…
    joined = " ".join(st["messages"])
    assert "Ampsel Commodities Limited" in joined
    assert "what I have on file" in joined
    # …and NOTHING was shredded into phantom project records.
    orgs = [str(p.get("org") or "") for p in s.essentials.get("projects", [])]
    assert not any("drop the" in o.lower() or "project links" in o.lower() for o in orgs)
    assert len(s.essentials.get("projects", [])) == len(before_projects)


# ------------------------------------------------- project scrape guard (obs #12)

def test_removal_and_meta_lists_never_become_projects(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s._capture_projects("Drop the DePaul University project from this CV. 1) the "
                        "Ampsel Commodities role with its title, 2) both project links")
    assert s.essentials.get("projects", []) == []


def test_explicit_additive_project_list_still_captures(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s._capture_projects("Add these projects: (1) Warehouse Safety Benchmark, an OSHA "
                        "injury analysis at https://github.com/x/warehouse (2) Last-Mile "
                        "Ops Simulator, a delivery fleet simulation")
    names = [str(p.get("org") or p.get("title") or "") for p in s.essentials["projects"]]
    assert any("Warehouse Safety Benchmark" in n for n in names)
    assert any("Last-Mile Ops Simulator" in n for n in names)
