"""At most 3 projects on a resume, at most 2 with 3 or more years of work, and projects are cut
before jobs (Kofi, 2026-11-24).

A person with 8 years of work got 4 projects while the page fit cut a 6-year role to one bullet.
The rules:

* A page shows at most 3 projects, at most 2 once the person has 3 or more years of work
  (overlapping roles merged, "Present" is today, internships and placeholder dates like "20XX"
  count for nothing). The most relevant to the job stay.
* When the page overflows: the least relevant project goes, then project bullets down to one
  each, and only then experience bullets. Every job keeps at least 2 while anything else can go.
* A project the cap left out is listed as cut to fit, with Put back; putting it back (or naming
  it in the chat) beats the cap for that resume.

Made-up data only.
"""

from __future__ import annotations

import copy

from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import assembler as A
from tailoring.assembler import (_Cuts, _shrink_once, assemble_cv, experience_years,
                                 normalize_profile, project_cap, ranked_roles,
                                 select_roles_for_jd, trim_id)
from ui.records import CVRecords
from ui.session import WebIntake

JD = ("Data Engineer. Build data pipelines in Python and SQL on AWS, orchestrate them with "
      "Airflow, and model the warehouse with dbt. Kafka streaming is a plus.")

_WORK = [
    "Rebuilt the nightly revenue pipeline in Python and SQL, cutting the finance close from three days to one and giving the controller a daily view of booked revenue.",
    "Moved forty legacy cron jobs into Airflow with retries and alerting, ending the weekly manual reruns and paging the owner within five minutes of any failure.",
    "Designed a dbt model layer for marketing attribution that analysts now query directly instead of exporting to Excel, with tests on every model.",
    "Cut warehouse spend by a fifth by partitioning the largest event tables and dropping unused nightly snapshots, then published a monthly cost report by team.",
    "Wrote data quality checks on order and refund feeds that caught a billing bug before quarter end and now block bad loads until an engineer reviews them.",
    "Led the migration from Redshift to Snowflake for twelve teams, with a parallel run and a signed cutover plan, finishing two weeks early with no missed reports.",
    "Built a Kafka consumer that streams shipment scans into the warehouse within a minute for dispatch, replacing an hourly batch export the team had called too slow.",
    "Trained six analysts on SQL window functions and code review, and wrote the team's query style guide that new hires now read in their first week.",
]


def _role(org, title, dates, n=6):
    return {"org": org, "location": "Chicago, IL", "roles": [{"title": title, "dates": dates, "bullets": [
        f"At {org}, {_WORK[(i + len(org)) % len(_WORK)][0].lower()}{_WORK[(i + len(org)) % len(_WORK)][1:]}"
        for i in range(n)]}]}


def _project(name, tools, bullets, dates=""):
    return {"org": name, "title": "", "tools": tools, "dates": dates, "bullets": list(bullets)}


PROJECTS = [
    _project("Recipe Swap", "React, Firebase", [
        "Built a recipe sharing app where home cooks trade weekly meal plans with friends.",
        "Added photo uploads and a shopping list that groups ingredients by store aisle."]),
    _project("Pipeline Kit", "Python, Airflow, dbt", [
        "Built an open-source Python toolkit that tests Airflow DAGs and dbt models before a deploy.",
        "Wrote a command line runner so other teams can check their own SQL pipelines in CI."]),
    _project("Garden Planner", "Swift", [
        "Made an iPhone app that reminds gardeners when to water and plant by local frost dates."]),
    _project("Stream Monitor", "Kafka, Python, AWS", [
        "Streamed public transit feeds through Kafka on AWS and alerted on late buses within a minute.",
        "Stored the events in S3 and built a small SQL report of the worst routes each week."]),
    _project("Chess Openings", "JavaScript", [
        "Built a web page that drills chess openings with spaced repetition flash cards."]),
]


def _senior() -> dict:
    """8 years over 4 jobs (2018 to today, in 2026), 5 projects."""
    return {
        "identity": {"name": "Sam Rivera", "email": "sam@example.com", "address": "Chicago, IL"},
        "education": [{"school": "State University", "location": "Columbus, OH",
                       "degree": "Bachelor of Science in Computer Science", "date": "May 2018"}],
        "skills": {"Data": "Python, SQL, Airflow, dbt", "Cloud": "AWS, Docker, Kubernetes",
                   "Streaming": "Kafka, Spark", "Tools": "Git, Tableau, Looker"},
        "experience": [_role("Acme Bank", "Senior Data Engineer", "March 2023 - Present"),
                       _role("Beacon Health", "Data Engineer", "June 2021 - February 2023"),
                       _role("Cobalt Retail", "Data Analyst", "June 2019 - May 2021"),
                       _role("Delta Freight", "Junior Analyst", "June 2018 - May 2019")],
        "projects": copy.deepcopy(PROJECTS),
    }


def _student() -> dict:
    return {
        "identity": {"name": "Lee Park", "email": "lee@example.com", "address": "Austin, TX"},
        "education": [{"school": "State University", "location": "Austin, TX",
                       "degree": "Bachelor of Science in Computer Science", "date": "May 2027"}],
        "skills": {"Data": "Python, SQL, Airflow", "Cloud": "AWS"},
        "experience": [_role("Acme Bank", "Data Engineering Intern", "June 2026 - August 2026", 3),
                       _role("Beacon Health", "Software Engineering Intern", "June 2025 - August 2025", 3)],
        "projects": copy.deepcopy(PROJECTS),
    }


def _names(profile: dict) -> list[str]:
    return [p["org"] for p in profile.get("projects") or []]


def _counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for _rank, _e, r in
            sorted(ranked_roles(profile), key=lambda x: x[0])]


# --------------------------------------------------------------------------- years of work
def test_years_merge_overlaps_and_skip_internships_and_placeholders():
    p = normalize_profile({"experience": [
        {"org": "A", "title": "Analyst", "dates": "January 2020 - December 2021", "bullets": ["a."]},
        {"org": "B", "title": "Consultant", "dates": "January 2021 - December 2022", "bullets": ["b."]},
        {"org": "C", "title": "Summer Intern", "dates": "June 2015 - August 2019", "bullets": ["c."]},
        {"org": "D", "title": "Engineer", "dates": "20XX - 20XX", "bullets": ["d."]},
        {"org": "E", "title": "Engineer", "dates": "", "bullets": ["e."]}]})
    assert experience_years(p) == 3.0                     # 2020 to 2022, the overlap once
    assert project_cap(p) == 2
    assert experience_years(normalize_profile(_student())) == 0.0
    assert project_cap(normalize_profile(_student())) == 3
    assert experience_years(normalize_profile(_senior())) >= 8


# --------------------------------------------------------------------------- the cap
def test_eight_years_keeps_the_two_projects_most_relevant_to_the_job():
    rep: dict = {}
    out = select_roles_for_jd(normalize_profile(_senior()), JD, report=rep)
    assert _names(out) == ["Pipeline Kit", "Stream Monitor"]          # in the person's order
    capped = [p["org"] for _n, p in rep["projects_capped"]]
    assert capped == ["Recipe Swap", "Garden Planner", "Chess Openings"]
    assert not any(i.startswith("project:") and "recipe" in i for i in rep["shown"])


def test_a_student_with_internships_only_keeps_three():
    out = select_roles_for_jd(normalize_profile(_student()), JD)
    assert len(out["projects"]) == 3
    assert {"Pipeline Kit", "Stream Monitor"} <= set(_names(out))


def test_a_shown_choice_beats_the_cap():
    """"Use these 4 projects": each is a "show" choice for this resume, so all four stay."""
    from applicant.rules import project_id
    prof = normalize_profile(_senior())
    four = [p for p in prof["projects"] if p["org"] != "Chess Openings"]
    choices = {"show": {project_id(p): {"rule": 0, "rank": [0]} for p in four}}
    rep: dict = {}
    out = select_roles_for_jd(prof, JD, choices=choices, report=rep)
    assert _names(out) == [p["org"] for p in four]
    assert [p["org"] for _n, p in rep["projects_capped"]] == ["Chess Openings"]


def test_a_project_put_back_stays_past_the_cap():
    prof = normalize_profile(_senior())
    keep = {trim_id("project", "Garden Planner", "Garden Planner")}
    out = select_roles_for_jd(prof, JD, keep=keep)
    assert _names(out) == ["Pipeline Kit", "Garden Planner", "Stream Monitor"]


def test_the_chat_naming_four_projects_pins_them(tmp_path):
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    s.profile = normalize_profile(_senior())
    s.essentials["projects"] = copy.deepcopy(s.profile["projects"])
    s._capture_projects("Use these 4 projects: 1) Recipe Swap 2) Pipeline Kit "
                        "3) Garden Planner 4) Stream Monitor")
    plan = s._choices_plan()
    out = select_roles_for_jd(s.profile, JD, choices=plan)
    assert _names(out) == ["Recipe Swap", "Pipeline Kit", "Garden Planner", "Stream Monitor"]
    # A plain mention is not a choice.
    s.session_choices.clear()
    s._capture_projects("My projects: 1) Recipe Swap 2) Pipeline Kit")
    assert not s.session_choices


# --------------------------------------------------------------------------- the cut order
def test_overflow_cuts_projects_before_any_job_bullet():
    """One cut at a time on a page at the selection's budgets: the least relevant project goes,
    then project bullets down to one, and only then a job bullet; no job goes below two while a
    course, a skills item, a skills line or a school can still go."""
    page = select_roles_for_jd(normalize_profile(_senior()), JD)
    page["skills"]["Extra"] = "Excel, Word"                         # 5 lines
    trims: list = []
    shapes = []
    while _shrink_once(page, trims):
        shapes.append((len(page["projects"]), [len(p["bullets"]) for p in page["projects"]],
                       _counts(page), len(page["skills"])))
        if len(trims) > 60:
            break
    first_job = next(i for i, t in enumerate(trims) if t["section"] == "experience")
    assert all(t["section"] == "projects" for t in trims[:first_job])
    # The less relevant of the two goes first; the most relevant stays, down to one bullet.
    assert (trims[0]["kind"], trims[0]["label"]) == ("project", "Stream Monitor")
    assert _names(page) == ["Pipeline Kit"]
    assert shapes[first_job - 1][:2] == (1, [1])
    below_two = next((i for i, s in enumerate(shapes) if min(s[2]) < 2), len(shapes))
    assert all(min(s[2]) >= 2 for s in shapes[:below_two])
    kinds_before = {t["kind"] for t in trims[:below_two]}
    assert "skills_line" in kinds_before                          # a skills line went first


def test_making_room_for_the_staircase_takes_a_project_before_giving_up():
    page = select_roles_for_jd(normalize_profile(_senior()), JD)
    trims: list = []
    cuts = _Cuts(trims, (), {})
    order = sorted(ranked_roles(page), key=lambda x: x[0])
    undo = []
    while (u := A._make_room(page, cuts, order, older_roles=False)) is not None:
        undo.append(u)
    assert len(page["projects"]) == 1 and len(page["projects"][0]["bullets"]) == 1
    assert any(t["kind"] == "project" and t.get("room") for t in trims)
    assert len(page["skills"]) == A.SKILL_LINES_FLOOR
    for u in reversed(undo):
        u()
    assert len(page["projects"]) == 2 and trims == []


# --------------------------------------------------------------------------- the real page
@requires_latex
def test_eight_years_on_the_real_page(template_source, workdir):
    res = assemble_cv(template_source, _senior(), JD, FakeLLM(), workdir, tailor=False,
                      fill_writes=False)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    page = res.profile_used
    assert len(page["projects"]) <= 2
    assert set(_names(page)) <= {"Pipeline Kit", "Stream Monitor"}
    c = _counts(page)
    assert min(c) >= 2 and A.staircase_ok(page), c
    assert c[0] >= 4, c
    cut = {t["label"] for t in res.trims if t["kind"] == "project"}
    assert {"Recipe Swap", "Garden Planner", "Chess Openings"} <= cut
    capped = [t for t in res.trims if t.get("cap")]
    assert all(t["id"] == trim_id("project", t["label"], t["label"]) for t in capped)
    assert not any("_rel" in p for p in page["projects"])
    assert res.fill_ratio is not None and res.fill_ratio >= A.TARGET_FILL


@requires_latex
def test_put_back_a_capped_project_shows_it(template_source, workdir):
    s = WebIntake(JD, template_source, FakeLLM(), ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.profile = normalize_profile(_senior())
    s.stage = "review"
    s._assemble()
    st = s._state()
    gone = next(t for t in st["trims"] if t["what"] == "Project" and t["label"] == "Garden Planner")
    out = s.put_back(gone["id"])
    assert out["put_back_ok"] is True, out.get("put_back_error")
    assert "Garden Planner" in _names(s.assembled.profile_used)
    assert all(t["id"] != gone["id"] for t in s.assembled.trims)
