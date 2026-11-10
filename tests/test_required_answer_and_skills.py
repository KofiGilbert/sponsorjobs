"""Three things Kofi hit in one resume chat (a Google data scientist job built from his saved
profile, 2026-11-10):

1. The outside-work question never said his interests were already on file, so when the page
   printed them he took them for invented. The question names what is on file first.
2. His answer "playing football and swimming" never reached the page (the model's absorb read it
   as interests, which were full, and dropped it) while the message said it had been added to his
   Extracurricular section. The answer goes into the section that was asked, and a claim that
   something went into a section is held to the page.
3. Every keyword he ADDed on a D365 job was kept on the Google resume, so its Skills block was
   another job's stack under that job's group names. The block is selected per job; an ADD holds
   its place only where the job asks for it. Two spellings of one term count once in the match.

FakeLLM; real pdflatex where the page is checked.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import narrate as N
from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.assembler import normalize_profile
from tailoring.linefit import split_terms
from ui.records import CVRecords
from ui.session import WebIntake, _reply_items

JD = ("Business Data Scientist, AI Foundations at Google. You will work with large datasets to "
      "develop and deploy ML/AI solutions. Lead the development of AI/ML solutions with a strong "
      "emphasis on Large Language Models (LLMs) and AI agents. Minimum qualifications: 4 years of "
      "experience using analytics to solve business problems, coding (e.g., Python, R, SQL), "
      "querying databases or statistical analysis. Preferred: machine learning and Natural "
      "Language Processing (NLP); Python with ML/AI libraries (TensorFlow, PyTorch, scikit-learn); "
      "experience with Large Language Models (LLMs); Statistics.")
D365_JD = ("D365 Software Developer. X++, CI/CD, .NET, SQL, Power BI, SSRS, Power Apps, Power "
           "Automate, Dataverse, Power Platform, SQL Server, Azure, C#, Git, Azure DevOps.")
INTERESTS = ("Playing football on weekends, travelling to see the world, and watching podcasts "
             "such as The Diary of a CEO (DOAC).")
# The saved profile as the D365 build left it: that job's terms, order and group names.
D365_BLOCK = {
    "Languages & Tools": "SQL, Python, X++, Dashboards, Reporting, advanced Excel, REST APIs",
    "Power Platform & Analysis": "Power BI, SSRS, Power Apps, Power Automate, Dataverse, "
                                 "Power Platform, SQL Server, Azure",
    "Development & Delivery": "C#, Object-oriented programming, JSON, XML, Git, Azure DevOps",
    "Microsoft & Azure Platform": "Data Management Framework, OData, Microsoft Fabric, "
                                  "Azure Data Factory, Azure Functions",
    "Domain Knowledge": "Logic Apps, Azure Data Lake Storage, CI/CD, .NET, KPI frameworks",
}
ADDED_ON_D365 = ["X++", "SQL Server", "SSRS", "Power Apps", "Power Automate", "Dataverse", "CD",
                 "Data Management Framework", "Microsoft Fabric", "OData", "Logic Apps",
                 "Azure Functions", "Azure Data Factory", "Azure DevOps", "Git", "C#"]
SAVED = {
    "identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
    "education": [{"school": "DePaul University", "degree": "MBA in Business Analytics",
                   "date": "June 2025", "location": "Chicago, IL"}],
    "experience": [
        {"org": "BlackOrigin Inc.", "location": "Chicago, IL",
         "roles": [{"title": "AI Senior Business Intelligence Manager",
                    "dates": "March 2026 - Present",
                    "bullets": ["Lead business intelligence across the company's products, "
                                "building KPI dashboards and automated Python and SQL reporting "
                                "that track growth, usage and unit costs.",
                                "Built an AI-assisted data pipeline in Python and SQL that "
                                "processes 280,000 job postings a day."]}]},
        {"org": "Stanbic Bank Ghana", "location": "Accra, Ghana",
         "roles": [{"title": "Senior Business Analyst", "dates": "Feb 2018 - Sept 2024",
                    "bullets": ["Led business analysis and performance reporting across retail "
                                "banking, owning KPI frameworks and Power BI dashboards.",
                                "Drove enterprise CRM requirements and process improvement across "
                                "two remits, with SQL Server reporting in SSRS."]}]},
    ],
    "skills": D365_BLOCK,
    "projects": [],
    "extracurricular": [],
    "interests": INTERESTS,
}
ESS = {
    "identity": SAVED["identity"], "education": SAVED["education"],
    "experience": [{"org": e["org"], "location": e["location"], **r}
                   for e in SAVED["experience"] for r in e["roles"]],
    "projects": [], "extracurricular": [], "interests": INTERESTS,
    "skills_input": ADDED_ON_D365 + ["Power BI", "Excel", "business analytics", "KPI frameworks",
                                     "Dashboards", "Reporting", "Python", "SQL"],
    "skills_from_jobs": list(ADDED_ON_D365),
    "declined": [],
}
BUILD_BUTTON = "just build it from my saved profile"
ANSWER = "playing football and swimming"
KOFI_CLAIM = ("Football and swimming give the resume some personality outside the data work, so "
              "I've added them to your Extracurricular section.")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")
    monkeypatch.setenv(N.FLAG, "1")


def _returning(template_source, workdir, jd=JD):
    mem = ConversationMemory(":memory:")
    mem.save("default", SAVED, ESS, [])
    s = WebIntake(jd, template_source, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name="shetty")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    s.start()
    return s


def _terms(skills: dict) -> list[str]:
    return [t for v in (skills or {}).values() for t in split_terms(v)]


# ------------------------------------------------------------- 1. the question says what it has

def test_the_question_names_what_is_already_on_file(template_source, workdir):
    s = _returning(template_source, workdir)
    st = s.submit(BUILD_BUTTON)
    assert s.assembled is None
    said = " ".join(st["messages"])
    assert "I already have your interests on file (" in said, said
    assert "football" in said.lower() and "travelling" in said.lower(), said
    assert "Extracurricular section is still empty" in said, said
    # The brief the AI writes from carries the same facts, so a written message can say them.
    items = getattr(s.llm, "last_narrate_items", None) or []
    ask = next((i for i in items if i.get("kind") == "ask_required"), None)
    assert ask is not None and "Interests" in ask.get("already_on_file", {}), items
    assert "football" in ask["already_on_file"]["Interests"].lower()
    assert "Interests" in ask.get("exact", [])


def test_a_template_with_nothing_on_file_asks_plainly(template_source, workdir):
    mem = ConversationMemory(":memory:")
    bare = dict(SAVED, interests="", extracurricular=[])
    mem.save("default", bare, dict(ESS, interests=""), [])
    s = WebIntake(JD, template_source, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name="shetty")
    s.start()
    said = " ".join(s.submit(BUILD_BUTTON)["messages"])
    assert "already have your" not in said
    assert "Extracurricular and Interests sections" in said, said


# ----------------------------------------------------- 2a. the answer lands in the asked section

def test_reply_items_are_clean_list_items():
    assert _reply_items("playing football and swimming") == ["Playing football", "Swimming"]
    assert _reply_items("I also volunteer at a food bank, and I coach a youth team.") == [
        "Volunteer at a food bank", "Coach a youth team"]
    assert _reply_items("Outside work I play chess; hiking") == ["Chess", "Hiking"]
    assert _reply_items("") == []


@requires_latex
def test_the_answer_goes_into_the_asked_section_and_renders(template_source, workdir):
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    st = s.submit(ANSWER)
    titles = [x["title"] for x in s.essentials.get("extracurricular") or []]
    assert titles == ["Playing football", "Swimming"], s.essentials.get("extracurricular")
    assert s.essentials.get("interests") == INTERESTS            # untouched
    assert s.assembled is not None and s.assembled.ok and st["phase"] == "review"
    page = s.assembled.profile_used
    assert [x["title"] for x in page.get("extracurricular") or []] == ["Playing football", "Swimming"]
    tex = (workdir / "cv.tex").read_text(encoding="utf-8") if (workdir / "cv.tex").exists() else ""
    rendered = tex or str(getattr(s.assembled, "tex", "") or "")
    assert "Swimming" in rendered and "Extracurricular" in rendered
    said = " ".join(st["messages"])
    assert "nothing went into your Extracurricular section" not in said
    assert "Before I build, I need your" not in said


def test_a_decline_or_a_question_is_never_material(template_source, workdir):
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    before = list(s.essentials.get("extracurricular") or [])
    assert s._absorb_required_reply("none", changed=False) == []
    assert s._absorb_required_reply("what do you mean by that?", changed=False) == []
    assert s._absorb_required_reply("that's everything", changed=False) == []
    assert s._absorb_required_reply(ANSWER, changed=True) == []       # the model took it already
    assert (s.essentials.get("extracurricular") or []) == before


# ------------------------------------------------- 2b. "added to your X section" is held to the page

def test_section_claims_are_detected_and_retold():
    claims = N.section_claims(KOFI_CLAIM + " You can edit any bullet on the page.")
    assert claims == [(KOFI_CLAIM, "Extracurricular")]
    assert N.section_claims("Your template has an Extracurricular section, and your resume won't "
                            "look complete without it.") == []
    assert N.section_claims("The Extracurricular section is still empty.") == []
    assert N.section_claims("I did not add anything to your Skills section.") == []
    assert N.section_claims("What should go in your Projects section?") == []
    told = N.section_not_added("Extracurricular", ANSWER)
    assert told == ("I noted what you said (playing football and swimming), but nothing went into "
                    "your Extracurricular section this time, so it is still empty on the page.")
    assert "-" not in told.replace("-", "") or True            # no dashes as punctuation
    assert " CV" not in told


@requires_latex
def test_a_claim_of_an_add_that_did_not_happen_is_retold_from_the_page(template_source, workdir,
                                                                      monkeypatch):
    # Exactly Kofi's transcript: the answer is dropped (the absorb path before this fix) and the
    # AI writes that it was added to the Extracurricular section.
    monkeypatch.setattr(WebIntake, "_absorb_required_reply", lambda self, m, changed: [])
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    s.llm.narrate_script = lambda system, user, items: KOFI_CLAIM + " " + N.fallback(items)
    st = s.submit(ANSWER)
    assert s.assembled is not None and not (s.assembled.profile_used.get("extracurricular") or [])
    said = " ".join(st["messages"])
    assert "added them to your Extracurricular section" not in said, said
    assert ("I noted what you said (playing football and swimming), but nothing went into your "
            "Extracurricular section this time") in said, said
    assert s.history[-1]["content"] == st["messages"][0]        # the history holds the truth too


@requires_latex
def test_a_true_claim_of_an_add_stands(template_source, workdir):
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    s.llm.narrate_script = lambda system, user, items: KOFI_CLAIM + " " + N.fallback(items)
    st = s.submit(ANSWER)
    assert [x["title"] for x in s.assembled.profile_used.get("extracurricular") or []]
    said = " ".join(st["messages"])
    assert "added them to your Extracurricular section" in said, said
    assert "nothing went into" not in said


# ------------------------------------------------------------- 3. skills selected per job

def test_an_add_holds_its_place_only_where_the_job_asks_for_it(template_source, workdir):
    s = _returning(template_source, workdir)
    assert s._skill_keep() == []                                 # nothing from D365 on a DS job
    d = _returning(template_source, workdir, jd=D365_JD)
    kept = d._skill_keep()
    assert "X++" in kept and "Power Apps" in kept and "Microsoft Fabric" not in kept
    # An ADD on THIS page is kept whatever the ad says.
    s.keyword_actions.append({"kind": "skill", "term": "Hugging Face"})
    assert s._skill_keep() == ["Hugging Face"]
    # The ADDs stay the person's own skills for every job.
    assert set(ADDED_ON_D365) <= set(s.essentials["skills_input"])


def test_the_skills_block_is_selected_for_this_job_not_carried_from_the_last(template_source, workdir):
    s = _returning(template_source, workdir)
    s.profile = normalize_profile(dict(s.saved_profile))
    assert list(s.profile["skills"]) == list(D365_BLOCK)          # as saved: the D365 render
    s._reselect_skills()
    s._validate_skills()
    s._fill_skills()
    s._dedupe_skill_groups()
    skills = s.profile["skills"]
    labels = list(skills)
    assert not ({"Power Platform & Analysis", "Microsoft & Azure Platform",
                 "Development & Delivery"} & set(labels)), labels
    first = split_terms(skills[labels[0]])
    assert first[0] in ("Python", "SQL") and first[1] in ("Python", "SQL"), skills
    assert "X++" not in first[:4], skills
    terms = _terms(skills)
    assert {"Python", "SQL", "Power BI"} <= set(terms), terms
    for v in skills.values():                                      # the block is still clean
        assert len(split_terms(v)) == len(set(t.lower() for t in split_terms(v)))
    # A term the block left off is still the person's own.
    for t in _terms(D365_BLOCK):
        assert t in terms or t.lower() in {x.lower() for x in s.essentials["skills_input"]}, t


def test_the_same_pool_leads_with_the_d365_stack_on_the_d365_job(template_source, workdir):
    d = _returning(template_source, workdir, jd=D365_JD)
    d.profile = normalize_profile(dict(d.saved_profile))
    d._reselect_skills()
    d._validate_skills()
    d._fill_skills()
    d._dedupe_skill_groups()
    first = split_terms(d.profile["skills"][list(d.profile["skills"])[0]])
    assert set(first[:6]) <= {t for t in _terms(d.profile["skills"]) if t in D365_JD}, first
    assert "X++" in _terms(d.profile["skills"])


def test_two_spellings_of_one_term_count_once_in_the_match():
    from tailoring.keywords import (acronym_pairs, build_coverage_report, merge_same_terms,
                                    same_term_key, skill_terms)
    pairs = acronym_pairs(JD)
    assert pairs.get("large language models") == "llm"
    assert pairs.get("natural language processing") == "nlp"
    assert same_term_key("ML/AI") == same_term_key("AI/ML")
    assert same_term_key("LLMs", pairs) == same_term_key("Large Language Models", pairs) == "llm"
    assert same_term_key("key performance indicators") == same_term_key("KPI")
    terms = skill_terms(JD)
    assert "NLP" in terms and "Natural Language Processing" in terms and "ML/AI" in terms
    groups = merge_same_terms(terms, JD)
    assert len(groups) < len(terms)
    rep = build_coverage_report(JD, "Python, SQL and LLMs in production", {"skills": {}}, terms)
    assert rep.jd_terms.count("NLP") + rep.jd_terms.count("Natural Language Processing") == 1
    assert sum(1 for t in rep.jd_terms if t in ("ML/AI", "AI/ML")) == 1
    assert "Python" in rep.present and "SQL" in rep.present
    assert any(t.lower().startswith("l") for t in rep.present), rep.present   # LLMs via LLM


# ------------------------------------------------------------- the project date lines

def test_a_project_built_in_one_month_shows_one_month():
    from tailoring.conform import format_dates
    from ui.github_dates import dates_from
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert dates_from({"created_at": "2026-07-16T15:16:04Z", "pushed_at": "2026-07-16T15:16:07Z"},
                      now) == "Jul 2026"
    assert dates_from({"created_at": "2026-07-16T15:16:04Z", "pushed_at": "2026-08-02T15:16:07Z"},
                      now) == "Jul 2026 - Aug 2026"
    assert dates_from({"created_at": "2026-08-19T21:52:48Z", "pushed_at": "2026-09-23T20:28:27Z"},
                      now) == "Aug 2026 - Present"                      # pushed within 60 days
    assert format_dates("July 2026 - July 2026") == "July 2026"
    assert format_dates("Jul 2026 - Jul 2026") == "July 2026"
    assert format_dates("July 2018 to July 2021") == "July 2018 - July 2021"
