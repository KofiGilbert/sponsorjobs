"""A job title, employer, date, location, school, degree and project name are FACTS: tailoring
rewords bullets, never these (tailoring/facts.py).

Seen on the owner's real resume (2026-11-24): the saved title came out with two of its words
swapped. The page now always shows the person's own value for each entry, whatever a model
re-emits, whatever an older reopened chat still holds, while the person's own edits (the page
editor, the date picker, a title choice, a fact typed in the chat, an uploaded resume) still
apply. All data here is made up.
"""

from __future__ import annotations

import copy

from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import facts as F
from tailoring.assembler import assemble_cv, normalize_profile
from ui.records import CVRecords
from ui.session import WebIntake

TITLE = "Senior AI Data Platform Manager"
ORG = "Northwind Analytics"
DATES = "March 2022 - Present"
SCHOOL = "Harbor Point University"
DEGREE = "Master of Science in Data Science"
PROJECT = "Tidewatch Dashboard"

# The job asks for "Senior Data Platform Manager" and AI: the words of the title, in another
# order and one short, the shape that tempts a "lead with the job's words" pass.
JD = ("Senior Data Platform Manager\nNorthwind Robotics\n\n"
      "We are hiring a Senior Data Platform Manager to lead our AI data platform. You will own "
      "data pipelines, ETL, SQL, Python, dashboards, machine learning and AI model data, and "
      "partner with stakeholders on KPI reporting.")

SAVED = {
    "identity": {"name": "Ada Example", "email": "ada@example.com", "phone": "555-0100",
                 "address": "Springfield, IL"},
    "summary": "Data platform leader with SQL, Python and AI data experience.",
    "education": [{"school": SCHOOL, "degree": DEGREE, "date": "May 2016",
                   "location": "Springfield, IL"}],
    "experience": [
        {"org": ORG, "location": "Springfield, IL",
         "roles": [{"title": TITLE, "dates": DATES, "bullets": [
             "Led the AI data platform team that built SQL and Python pipelines for twelve products.",
             "Built KPI dashboards that tracked usage and cost for the product and finance teams.",
             "Ran the ETL migration that moved nightly loads to a managed warehouse."]}]},
        {"org": "Bluefin Bank", "location": "Springfield, IL",
         "roles": [{"title": "Data Analyst", "dates": "June 2016 - February 2022", "bullets": [
             "Wrote SQL reports for the retail lending team.",
             "Automated a weekly Excel reconciliation with a Python script."]}]},
    ],
    "projects": [{"org": PROJECT, "tools": "Python, SQL", "bullets": [
        "Built a tide data dashboard that pulls public sensor feeds every hour."]}],
    "skills": {"Data & Analytics": "SQL, Python, ETL, dashboards",
               "AI & ML": "machine learning, AI"},
    "extracurricular": [{"title": "Volunteer Data Coach", "org": "Code Club", "date": "2019 - Present",
                         "bullets": ["Coach high school students on Python basics."]}],
    "interests": "Sailing, chess and hiking.",
}


class RewordingLLM(FakeLLM):
    """A test double that re-emits every fact in other words, the way a real model can."""

    def draft_profile(self, jd_text, essentials):
        p = super().draft_profile(jd_text, essentials)
        for e in p.get("experience") or []:
            for r in e.get("roles") or [e]:
                if r.get("title") == TITLE or r.get("title_original") == TITLE:
                    r["title"] = "AI Senior Data Platform Manager"      # words swapped
                    r["title_suggested"] = False
            if e.get("org") == ORG:
                e["org"] = "Northwind"                                 # a word dropped
        return p

    def extract_intake(self, jd_text, known, history, **kw):
        out = super().extract_intake(jd_text, known, history, **kw)
        for j in (out.get("essentials") or {}).get("experience") or []:
            if j.get("title") == TITLE:
                j["title"] = "AI Senior Data Platform Manager"
            if j.get("dates") == DATES:
                j["dates"] = "2022 - Present"
        return out


def _session(template_source, workdir, llm=None, mem=None, saved=SAVED):
    if mem is None:
        mem = ConversationMemory(":memory:")
        mem.save("default", copy.deepcopy(saved), {}, [])
    s = WebIntake(JD, template_source, llm or FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p")
    s.essentials["declined"] = ["address", "github", "linkedin", "blog", "courses"]
    return s


def _page_facts(a) -> dict:
    prof = a.profile_used
    roles = [(e.get("org"), r.get("title"), r.get("dates"), e.get("location"))
             for e in prof["experience"] for r in (e.get("roles") or [e])]
    return {"roles": roles,
            "edu": [(d.get("school"), d.get("degree"), d.get("date")) for d in prof["education"]],
            "projects": [p.get("org") or p.get("name") for p in prof.get("projects") or []]}


# --------------------------------------------------------------------------- the guard itself
def test_a_reordered_title_and_other_changed_facts_are_put_back():
    page = normalize_profile(copy.deepcopy(SAVED))
    page["experience"][0]["roles"][0]["title"] = "AI Senior Data Platform Manager"
    page["experience"][0]["org"] = "Northwind"
    page["experience"][1]["roles"][0]["dates"] = "2016 - 2022"
    page["education"][0]["degree"] = "M.S. Data Science and AI"
    page["projects"][0]["org"] = "Tidewatch AI Dashboard"
    fixes = F.restore_facts(page, normalize_profile(copy.deepcopy(SAVED)))
    assert page["experience"][0]["roles"][0]["title"] == TITLE
    assert page["experience"][0]["org"] == ORG
    assert page["experience"][1]["roles"][0]["dates"] == \
        normalize_profile(copy.deepcopy(SAVED))["experience"][1]["roles"][0]["dates"]
    assert page["education"][0]["degree"] == DEGREE
    assert page["projects"][0]["org"] == PROJECT
    assert {(f["section"], f["field"]) for f in fixes} == {
        ("experience", "title"), ("experience", "org"), ("experience", "dates"),
        ("education", "degree"), ("projects", "org")}


def test_formatting_and_bullet_rewrites_are_not_fact_changes():
    page = normalize_profile(copy.deepcopy(SAVED))
    page["experience"][0]["roles"][0]["bullets"][0] = "Led the AI data platform for twelve products."
    assert F.restore_facts(page, copy.deepcopy(SAVED)) == []
    # A year-only saved date shown with placeholder months (flagged red) is the same fact.
    saved = copy.deepcopy(SAVED)
    saved["experience"][1]["roles"][0]["dates"] = "2016 - 2022"
    page = normalize_profile(copy.deepcopy(saved))
    page["experience"][1]["roles"][0].update(dates="January 2016 - December 2022",
                                             dates_placeholder=True)
    assert F.restore_facts(page, normalize_profile(saved)) == []


def test_a_typed_title_counts_and_a_reordered_one_does_not():
    assert F.typed("Lead Data Platform Manager", "change my title at Northwind to Lead Data Platform Manager.")
    assert not F.typed("AI Senior Data Platform Manager", f"I was a {TITLE} at {ORG}.")


# --------------------------------------------------------------------------- the assembler
@requires_latex
def test_the_assembler_renders_the_persons_facts_even_from_a_drifted_profile(template_source, workdir):
    drifted = copy.deepcopy(SAVED)
    drifted["experience"][0]["roles"][0]["title"] = "AI Senior Data Platform Manager"
    drifted["education"][0]["school"] = "Harbor Point"
    res = assemble_cv(template_source, drifted, JD, FakeLLM(), workdir, tailor=True,
                      facts=copy.deepcopy(SAVED))
    assert res.ok
    assert f"\\textbf{{{TITLE}}}" in res.tex_source
    assert "AI Senior Data Platform Manager" not in res.tex_source
    assert SCHOOL in res.tex_source
    assert {(f["field"], f["saved"]) for f in res.fact_fixes} >= {("title", TITLE), ("school", SCHOOL)}


@requires_latex
def test_a_pass_that_rewrites_a_title_is_undone_before_the_page_ships(template_source, workdir, monkeypatch):
    import tailoring.assembler as A
    real = A._tailor_bullets

    def sneaky(profile, *a, **kw):
        out = real(profile, *a, **kw)
        for e in out.get("experience") or []:
            for r in e.get("roles") or [e]:
                if r.get("title") == TITLE:
                    r["title"] = "AI Senior Data Platform Manager"
        return out
    monkeypatch.setattr(A, "_tailor_bullets", sneaky)
    res = assemble_cv(template_source, copy.deepcopy(SAVED), JD, FakeLLM(), workdir, tailor=True)
    assert f"\\textbf{{{TITLE}}}" in res.tex_source
    assert [f["field"] for f in res.fact_fixes] == ["title"]


# --------------------------------------------------------------------------- the builder
@requires_latex
def test_a_tailored_build_keeps_every_fact_exact(template_source, workdir):
    s = _session(template_source, workdir, llm=RewordingLLM())
    s.start()
    st = s.build_from_saved()
    assert st["phase"] == "review", st.get("messages")
    page = _page_facts(s.assembled)
    assert (ORG, TITLE, DATES, "Springfield, IL") in page["roles"]
    assert page["edu"][0][:2] == (SCHOOL, DEGREE)
    assert PROJECT in page["projects"]
    assert f"\\textbf{{{TITLE}}}" in s.assembled.tex_source
    # and the saved profile still says the same
    saved = s.memory.load("default")["profile"]
    assert TITLE in [r["title"] for e in saved["experience"] for r in (e.get("roles") or [e])]


@requires_latex
def test_a_drafted_page_never_takes_the_models_title(template_source, workdir):
    """The first build drafts the page with the model, which may 'align' a title to the job (the
    FakeLLM puts the job's own title on the latest role, as the real prompt allowed until now)
    or reorder its words. The page keeps the title the person gave."""
    s = _session(template_source, workdir, llm=RewordingLLM())
    s.start()
    s.essentials = {
        "identity": dict(SAVED["identity"]),
        "education": [dict(SAVED["education"][0])],
        "experience": [{"org": ORG, "title": TITLE, "dates": DATES, "location": "Springfield, IL",
                        "bullets": list(SAVED["experience"][0]["roles"][0]["bullets"])},
                       {"org": "Bluefin Bank", "title": "Data Analyst",
                        "dates": "June 2016 - February 2022", "location": "Springfield, IL",
                        "bullets": list(SAVED["experience"][1]["roles"][0]["bullets"])}],
        "projects": [dict(SAVED["projects"][0])],
        "extracurricular": copy.deepcopy(SAVED["extracurricular"]),
        "interests": SAVED["interests"],
        "declined": ["github", "linkedin", "blog", "courses"],
    }
    s._draft()
    assert s.assembled is not None and s.assembled.ok
    roles = _page_facts(s.assembled)["roles"]
    assert (ORG, TITLE) in [(o, t) for o, t, _d, _l in roles]
    assert "Senior Data Platform Manager}" not in s.assembled.tex_source.replace(TITLE + "}", "")
    assert s.title_flags() == []


@requires_latex
def test_an_older_reopened_chat_does_not_put_an_old_title_back(template_source, workdir):
    """The bug's real path: a chat opened before the person changed their title, reopened and
    saved later, wrote its old copy over the new title, and the next resume printed it."""
    mem = ConversationMemory(":memory:")
    old = copy.deepcopy(SAVED)
    old["experience"][0]["roles"][0]["title"] = "AI Senior Data Platform Manager"
    mem.save("default", old, {}, [])
    stale = _session(template_source, workdir, mem=mem)           # opened with the old title
    stale.start()
    # the person sets their title to the right one elsewhere (the profile page, another chat)
    mem.save("default", copy.deepcopy(SAVED), {}, [])
    stale._persist()                                              # the old chat saves again
    saved = mem.load("default")["profile"]
    assert saved["experience"][0]["roles"][0]["title"] == TITLE
    stale.build_from_saved()
    assert f"\\textbf{{{TITLE}}}" in stale.assembled.tex_source
    fresh = _session(template_source, workdir, mem=mem)
    fresh.start()
    fresh.build_from_saved()
    assert f"\\textbf{{{TITLE}}}" in fresh.assembled.tex_source


def test_a_model_that_reorders_a_title_in_the_chat_is_not_believed(tmp_path):
    s = _session("x", tmp_path, llm=RewordingLLM())
    s.essentials = {"identity": dict(SAVED["identity"]), "education": [],
                    "experience": [{"org": ORG, "title": TITLE, "dates": DATES,
                                    "location": "Springfield, IL"}]}
    before = copy.deepcopy(s.essentials)
    s.essentials["experience"][0]["title"] = "AI Senior Data Platform Manager"
    s._keep_fact_words(before, "Nothing new, tailor what you have.")
    assert s.essentials["experience"][0]["title"] == TITLE
    assert s._fact_edits == []


# --------------------------------------------------------------------------- the person's own edits
@requires_latex
def test_the_persons_own_title_edit_still_applies(template_source, workdir):
    s = _session(template_source, workdir)
    s.start()
    s.build_from_saved()
    ref = next(k for k, (c, key) in s._field_slots().items()
               if key == "title" and c.get("title") == TITLE)
    st = s.edit_field(ref, "Lead Data Platform Manager")
    assert st["edit_ok"], st.get("edit_error")
    assert "\\textbf{Lead Data Platform Manager}" in s.assembled.tex_source
    saved = s.memory.load("default")["profile"]
    assert "Lead Data Platform Manager" in [r["title"] for e in saved["experience"]
                                            for r in (e.get("roles") or [e])]
    s._assemble()                                    # and a later rebuild keeps it
    assert "\\textbf{Lead Data Platform Manager}" in s.assembled.tex_source


@requires_latex
def test_a_title_typed_in_the_chat_applies(template_source, workdir):
    s = _session(template_source, workdir)
    s.start()
    s.build_from_saved()
    before = copy.deepcopy(s.essentials)
    for j in s.essentials["experience"]:
        if j.get("title") == TITLE:
            j["title"] = "Head of Data Platform"         # what the model read from the message
    s._keep_fact_words(before, f"Please change my title at {ORG} to Head of Data Platform.")
    assert s._fact_edits and s._fact_edits[0]["value"] == "Head of Data Platform"
    s._assemble()
    assert "\\textbf{Head of Data Platform}" in s.assembled.tex_source
    assert f"\\textbf{{{TITLE}}}" not in s.assembled.tex_source


@requires_latex
def test_a_confirmed_fact_edit_saved_from_outside_the_chat_shows_on_the_next_build(template_source, workdir):
    """'Also update my profile' from the phone review writes the saved profile directly; a chat
    already open shows it on its next build, not the copy it loaded."""
    s = _session(template_source, workdir)
    s.start()
    s.build_from_saved()
    saved = s.memory.load("default")
    prof = saved["profile"]
    prof["experience"][0]["roles"][0]["title"] = "Principal Data Platform Manager"
    s.memory.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
    s._assemble()
    assert "\\textbf{Principal Data Platform Manager}" in s.assembled.tex_source
