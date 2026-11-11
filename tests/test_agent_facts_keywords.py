"""The resume chat states only what the page holds, puts the job's supported terms on it, and
decides about projects (Kofi's Google "Business Data Scientist, AI Foundations" chat, 2026-11-11).

He asked "are we done with this resume?" and was told his roles had no titles or dates (every
role on the page had both: they live under each employer's ``roles``), asked "if you've really
used statistical analysis, tell me which and where" over an MBA in Business Analytics, and
offered "I can check your GitHub if you'd like". The review footer then read "Answer a few
questions" with Accept & Save greyed out on a built page. FakeLLM only.
"""

from __future__ import annotations

import pytest

from agent import narrate as N
from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from tailoring import inferred_support as IS
from ui.records import CVRecords
from ui.session import WebIntake, page_roles

# His page's experience, exactly as the session holds it: titles and dates under ``roles``.
KOFI_EXPERIENCE = [
    {"org": "BlackOrigin Inc.", "location": "Chicago, IL", "roles": [
        {"title": "AI Senior Business Intelligence Manager", "dates": "March 2026 - Present",
         "bullets": ["Led business intelligence across the company's products, building KPI dashboards."],
         "drafted_bullets": False, "title_suggested": False, "title_original": ""}]},
    {"org": "Stanbic Bank Ghana", "location": "Accra, GH", "roles": [
        {"title": "Senior Business Analyst", "dates": "Feb 2018 - Sept 2024",
         "bullets": ["Led business analysis and performance reporting across retail and digital banking."]}]},
    {"org": "Viva Technologies Ghana", "location": "Accra, GH", "roles": [
        {"title": "Product and Operations Analyst", "dates": "Sept 2016 - Jan 2018",
         "bullets": ["Analyzed SQL and Excel operational data for a digital fulfillment platform."]}]},
    {"org": "Council for Scientific and Industrial Research (CSIR)", "location": "Accra, GH", "roles": [
        {"title": "Research Assistant", "dates": "Sept 2015 - Aug 2016",
         "bullets": ["Built SQL data management pipelines that supported structured querying."]}]},
]
KOFI_EDUCATION = [{"school": "DePaul University", "degree": "MBA - Business Analytics (STEM), With Distinction",
                   "date": "Sept 2024 - Dec 2025", "location": "Chicago, IL",
                   "courses": "Business Analytics Tools, Data Visualization, Database Management Systems"}]

THE_ANSWER = (
    "Nearly. The resume is built, fits on one page and is 95% full, so it's usable now. Three things "
    "could still make it stronger:\n\n"
    "1. Your roles have no titles or dates. Tell me them here, for example \"Data Analyst, BlackOrigin "
    "Inc., 2021 to 2023\", and recruiters will read them with more weight.\n"
    "2. Many of the Google job's terms aren't on the page yet, such as machine learning, statistical "
    "analysis and R. If you've really used any of them, tell me which and where.\n"
    "3. I can check your GitHub repos for ones that fit this role, if you'd like to add more projects.\n\n"
    "Do you want to start with the titles and dates?")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")


class _Page:
    ok = True
    compile = None

    def __init__(self, used):
        self.profile_used = used


def _session(template_source, fake_llm, workdir, used=None):
    s = WebIntake("Data Scientist. Python, SQL, statistical analysis.", template_source, fake_llm,
                  ConversationMemory(":memory:"), CVRecords(":memory:"), workdir, jobname="cv",
                  palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    if used is not None:
        s.assembled = _Page(used)
    return s


# ------------------------------------------------------------ 1. the page's facts, as printed

def test_every_role_is_read_with_its_title_and_dates_from_roles():
    roles = page_roles(KOFI_EXPERIENCE)
    assert [r["title"] for r in roles] == ["AI Senior Business Intelligence Manager", "Senior Business Analyst",
                                           "Product and Operations Analyst", "Research Assistant"]
    assert roles[0]["dates"] == "March 2026 - Present" and roles[0]["org"] == "BlackOrigin Inc."
    # A flat entry (title and dates on the entry itself) still reads.
    assert page_roles([{"org": "X", "title": "Analyst", "dates": "2020"}])[0]["title"] == "Analyst"


def test_the_agents_snapshot_and_state_carry_the_titles_and_dates(template_source, fake_llm, workdir, monkeypatch):
    s = _session(template_source, fake_llm, workdir, {"experience": KOFI_EXPERIENCE, "education": KOFI_EDUCATION})
    monkeypatch.setattr(s, "coverage_dict", lambda: {"present": ["SQL"], "missing": [], "missing_supported": []})
    snap = s._snapshot()
    assert snap["experience"][1] == {"org": "Stanbic Bank Ghana", "title": "Senior Business Analyst",
                                     "dates": "Feb 2018 - Sept 2024", "location": "Accra, GH"}
    lines = "\n".join(rc.state_lines(s._agent_state()))
    assert "AI Senior Business Intelligence Manager, BlackOrigin Inc., March 2026 - Present" in lines
    assert "Every role on the page has its title and dates" in lines


# ------------------------------------------------------------ 2. a false "missing" is never shown

def test_the_exact_answer_loses_its_false_claim_and_the_ask_on_it():
    claims = N.missing_claims(THE_ANSWER)
    assert {f for _s, f in claims} == {"titles", "dates"}
    out = N.drop_claims(THE_ANSWER, {"titles", "dates"}, {s for s, _f in claims})
    assert "titles" not in out and "dates" not in out and "Data Analyst, BlackOrigin" not in out
    assert "Two things could still make it stronger" in out
    assert out.count("\n1. ") == 1 and "\n2. I can check" in out and "\n3." not in out


def test_the_session_checks_a_missing_claim_against_the_page(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir, {"experience": KOFI_EXPERIENCE, "education": KOFI_EDUCATION,
                                                      "projects": [{"org": "Gate Check"}]})
    out = s._missing_claims_fix(THE_ANSWER)
    assert "no titles or dates" not in out and "start with the titles" not in out
    # Not a claim: "your projects don't show X" names the projects as the owner of the gap.
    assert s._missing_claims_fix("Your projects don't show machine learning.") == \
        "Your projects don't show machine learning."
    # A true one stays: a role really without dates.
    gap = [dict(KOFI_EXPERIENCE[0], roles=[dict(KOFI_EXPERIENCE[0]["roles"][0], dates="")])]
    s2 = _session(template_source, fake_llm, workdir, {"experience": gap})
    assert "no dates" in s2._missing_claims_fix("Your BlackOrigin role has no dates. Add them on the page.")


# ------------------------------------------------------------ 3. inferred support, checked in code

@pytest.mark.parametrize("term", ["R", "TensorFlow", "PyTorch", "scikit-learn", "Hugging Face", "SAS",
                                  "Power BI", "pandas"])
def test_a_named_tool_is_never_inferred(term):
    assert IS.is_named_tool(term)


@pytest.mark.parametrize("term", ["Statistical analysis", "Querying databases", "Machine learning",
                                  "AI agents", "Large Language Models", "Natural Language Processing",
                                  "Data visualization", "Generative AI"])
def test_a_method_may_be_inferred(term):
    assert not IS.is_named_tool(term)


def test_vet_keeps_only_verbatim_evidence_for_asked_methods():
    material = {"experience": KOFI_EXPERIENCE, "education": KOFI_EDUCATION}
    asked, tools = IS.candidates(["Statistical analysis", "Querying databases", "R", "TensorFlow",
                                  "Machine learning", "Master's degree in Statistics"])
    assert tools == ["R", "TensorFlow"] and "Master's degree in Statistics" not in asked
    answer = [{"term": "Statistical analysis", "supported": True, "evidence": "MBA - Business Analytics (STEM)"},
              {"term": "Querying databases", "supported": True, "evidence": "supported structured querying"},
              {"term": "Machine learning", "supported": True, "evidence": "trained neural networks"},   # not his
              {"term": "R", "supported": True, "evidence": "Business Analytics Tools"}]                 # never asked
    got = IS.vet(answer, asked, material)
    assert [g["term"] for g in got] == ["Statistical analysis", "Querying databases"]


# ------------------------------------------------------------ 4. "are we done?" is a page question

@pytest.mark.parametrize("m", ["are we done with this resume ?", "Is it ready to send?", "what's left?",
                               "are we done?", "Is this resume finished?", "can I send it now?"])
def test_asks_done(m):
    assert rc.asks_done(m)


@pytest.mark.parametrize("m", ["I'm done, that's everything", "done", "we are done with the dates",
                               "add Docker to my skills"])
def test_not_asks_done(m):
    assert not rc.asks_done(m)


# ------------------------------------------------------------ 5. the footer and a re-save

def test_a_saved_chat_reopened_offers_accept_and_save():
    from pathlib import Path
    js = Path(__file__).resolve().parents[1].joinpath("ui/static/app.js").read_text(encoding="utf-8")
    assert 'st.phase === "review" || (st.phase === "done" && st.pdf)' in js


SKELETON = (
    "I'm Kwame Asante, kwame@example.com. MBA in Business Analytics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021, where I analyzed "
    "survey datasets for trends. Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."
JD = ("Data Scientist. Required skills: SQL, Python, machine learning, TensorFlow, "
      "PyTorch, dashboards, forecasting.")


def _infer(jd, terms, material):
    from tailoring.keywords import profile_text
    text = profile_text(material)
    quote = "analyzed survey datasets for trends" if "analyzed survey datasets for trends" in text.lower() \
        else "Business Analytics"
    return [{"term": t, "supported": t.lower() == "dashboards", "evidence": quote} for t in terms]


@requires_latex
def test_are_we_done_answers_from_the_page_and_puts_supported_terms_on_it(template_source, fake_llm, workdir):
    fake_llm.infer_script = _infer
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                  workdir, jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    assert st["phase"] == "review" and s.assembled is not None
    # The first build already put the job's supported terms on the page, in its own words: one the
    # material clearly implies (inferred, with its evidence) and one it says outright.
    assert s._term_on_page("Dashboards") and s._term_on_page("Forecasting")
    assert [i["term"] for i in s._terms_result["inferred"]] == ["Dashboards"]
    assert s._terms_result["after"] > s._terms_result["before"]
    st = s.submit("are we done with this resume ?")
    text = st["messages"][-1]
    assert "Page: one page" in text and "Keywords:" in text
    assert "no titles" not in text and "tell me which and where" not in text
    assert "if you'd like" not in text and "—" not in text and "–" not in text and "CV" not in text
    chips = [c for c in st["cards"] if c.get("type") == "keyword_chips"]
    assert chips and {"TensorFlow", "PyTorch"} <= set(chips[0]["terms"])
    assert "dashboards" not in [t.lower() for t in chips[0]["terms"]]
    # Saved, then changed by the chat, then saved again: the same record, the new page.
    first = s.accept()
    s._place_job_terms(force=True)
    s._pdf_v += 1
    again = s.accept()
    assert again["record_id"] == first["record_id"] and len(s.records.list()) == 1
