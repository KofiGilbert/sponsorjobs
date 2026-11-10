"""One brain, one voice per turn (Kofi, 2026-11-10, a D365 job built from his saved profile).

What he saw: the scripted tail after EVERY build ("I'll ask for a few more real things to fill the
page.", "Edit any bullet right on the page...") interleaved with the agent; the outside-work
question three times after he had answered it with his hobbies; a short-bullet question about
"Last-Mile Operations Simulator (Python" for a one-word last line; twenty "Updated your resume"
rows for twenty keyword ADDs; and his typed question running a whole rebuild and getting the
intake model's guess ("neither uses X++", though he had ADDed it) with "share your GitHub" in
plain text. FakeLLM and real pdflatex.
"""

from __future__ import annotations

import pytest

from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake

JD = ("Data Analyst. Required skills: SQL, Python, dashboards, statistics, data pipelines, "
      "machine learning, Tableau, Power BI, Excel, forecasting, ETL, data modeling.")
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."
SAVED = {
    "identity": {"name": "Kwame Asante", "email": "kwame@example.com"},
    "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                   "date": "June 2019", "location": "Accra, Ghana"}],
    "experience": [{"org": "BlackOrigin", "location": "Accra, Ghana",
                    "roles": [{"title": "Data Analyst", "dates": "Feb 2021 - Present",
                               "bullets": ["Built Tableau dashboards for the operations team.",
                                           "Wrote SQL pipelines feeding the weekly sales report."]}]},
                   {"org": "Stanbic Bank", "location": "Accra, Ghana",
                    "roles": [{"title": "Analyst", "dates": "Jan 2018 - Dec 2020",
                               "bullets": ["Reconciled branch ledgers in Excel every month."]}]}],
    "skills": {"Data & Analytics": "SQL, Python, Tableau", "Domain": "statistics, forecasting"},
}
BUILD_BUTTON = "just build it from my saved profile"
OUTSIDE_WORK = ("outside", "volunteer", "club", "hobb")


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")


def _s(template_source, fake_llm, workdir, memory=None):
    s = WebIntake(JD, template_source, fake_llm, memory or ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    return s


def _built(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    assert s.assembled is not None and s.assembled.ok
    return s, st


def _returning(template_source, fake_llm, workdir):
    mem = ConversationMemory(":memory:")
    mem.save("default", SAVED, {}, [])
    s = _s(template_source, fake_llm, workdir, memory=mem)
    s.start()
    return s


def _builds(s):
    return [t for t in s.history if isinstance(t, dict) and t.get("build")]


def _questions(msgs):
    return [m for m in msgs if "?" in m]


# ------------------------------------------------------------------ the build's tail

@requires_latex
def test_after_a_build_one_question_at_most_and_the_edit_tip_once(template_source, fake_llm, workdir):
    s, st = _built(template_source, fake_llm, workdir)
    assert st["messages"].count(WebIntake.EDIT_TIP) == 1
    assert len(_questions(st["messages"])) <= 1, st["messages"]
    assert "a few more real things" not in " ".join(st["messages"])
    # A real answer rebuilds: the tip is not said again, and still one question at most.
    st2 = s.submit("At BlackOrigin I rebuilt the monthly reporting in SQL and cut close time "
                   "from five days to two.")
    assert len(_builds(s)) == 2
    assert WebIntake.EDIT_TIP not in st2["messages"]
    assert len(_questions(st2["messages"])) <= 1, st2["messages"]


@requires_latex
def test_an_answer_with_nothing_new_does_not_rebuild(template_source, fake_llm, workdir):
    s, _st = _built(template_source, fake_llm, workdir)
    n = len(_builds(s))
    st = s.submit("thanks, I don't have any certifications")
    assert len(_builds(s)) == n and "build" not in st
    assert len(st["messages"]) == 1
    assert WebIntake.EDIT_TIP not in st["messages"]


# ------------------------------------------------------------------ never re-ask a topic

@requires_latex
def test_hobbies_answer_the_outside_work_question_once_and_the_saved_build_runs(
        template_source, fake_llm, workdir):
    s = _returning(template_source, fake_llm, workdir)
    st1 = s.submit(BUILD_BUTTON)
    assert s.assembled is None
    assert any(w in " ".join(st1["messages"]).lower() for w in OUTSIDE_WORK)
    # His answer went into Interests only: that answers the question; the build carries on with
    # exactly what he clicked (his saved profile), not a fresh chat draft.
    st2 = s.submit("I enjoy football at weekends, travelling and podcasts like DOAC.")
    assert "podcasts" in str(s.essentials.get("interests") or "").lower()
    assert s.assembled is not None and st2["phase"] == "review"
    assert s._change_base is not None, "built as a chat draft, not from the saved profile"
    said = " ".join(st2["messages"]).lower()
    assert not any(w in said for w in ("volunteer", "club")), said
    # Later turns never bring it back.
    st3 = s.submit("At BlackOrigin I also trained two junior analysts on SQL.")
    later = " ".join(st3["messages"]).lower()
    assert not any(w in later for w in ("volunteer", "club", "outside work")), later
    assert "extracurricular" not in (s.essentials.get("declined") or [])   # covered, not declined


def test_pre_build_reask_is_the_short_nudge_not_the_question_again(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    first = " ".join(s.submit(SKELETON)["messages"])
    st = s.submit("My manager at BlackOrigin was great.")
    second = " ".join(st["messages"])
    assert s.assembled is None and second != first
    assert "None, leave it off" in second


def test_the_model_is_only_thanked_for_what_was_really_added(template_source, fake_llm, workdir):
    seen = []
    real = fake_llm.ask_enrich

    def spy(topics, answered, *a, **kw):
        seen.append(list(answered))
        return real(topics, answered, *a, **kw)

    fake_llm.ask_enrich = spy
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Meridian", "title": "Analyst", "dates": "2021 - Present",
                                   "location": "Chicago, IL", "bullets": ["Real work done"]}]
    s.essentials["skills_input"] = ["SQL"]
    s.essentials["declined"] = ["extracurricular", "interests"]
    assert s._enrich_invite()                     # asks projects and skills_more
    assert s._enrich_invite()                     # moves on (certifications); nothing answered
    assert seen == [[], []], seen
    s.essentials["certifications"] = ["AWS Certified"]
    s.essentials["enrich_last_ask"] = ["certifications"]
    s.essentials["enrich_asked"] = sorted(set(s.essentials["enrich_asked"]) - {"certifications"})
    s._enrich_invite(lambda k: True)
    assert seen[-1] in ([], ["certifications"])    # only a topic that really got filled


def test_each_enrichment_topic_is_asked_once(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Meridian", "title": "Analyst", "dates": "2021 - Present",
                                   "location": "Chicago, IL", "bullets": ["Real work done"]}]
    s.essentials["required_asked"] = ["extracurricular", "interests"]
    asks = []
    while True:
        q = s._enrich_invite()
        if not q:
            break
        asks.append(q)
        assert len(asks) < 6, "the same topics keep coming back"
    keys = s.essentials["enrich_asked"]
    assert "extracurricular" not in keys and "interests" not in keys
    assert len(keys) == len(set(keys))


# ------------------------------------------------------------------ the short-bullet question

class _Page:
    def __init__(self, gaps):
        self.line_gaps = gaps


def test_a_one_word_last_line_is_a_fit_issue_not_a_question(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    label = "Last-Mile Operations Simulator (Python, simulation, OR-Tools)"
    s.assembled = _Page({"short_bullets": [
        {"label": label, "text": "Showed a demand-shaped roster beat a flat one by 12 percent.",
         "fill": 0.08, "lines": 2}]})
    assert s._line_question() == ""
    s.assembled = _Page({"short_bullets": [
        {"label": "Last-Mile Operations Simulator (Python, simulation",
         "text": "Showed a demand-shaped roster beat a flat one.", "fill": 0.4, "lines": 1}]})
    # A thin bullet is not asked about either (Kofi, 2026-11-10): the line fit writes it out.
    assert s._line_question() == ""
    s.assembled = _Page({"short_skills": [{"label": "Tools"}], "skills_needed": 1})
    assert "needs a little more" not in s._line_question()


# ------------------------------------------------------------------ keyword ADD rows

@requires_latex
def test_a_run_of_keyword_adds_is_one_row_that_updates_in_place(template_source, fake_llm, workdir):
    s, _st = _built(template_source, fake_llm, workdir)
    first = _builds(s)
    st1 = s.keyword_add("Excel")
    assert st1["ok"], st1.get("reason")
    st2 = s.keyword_add("Power BI")
    assert st2["ok"], st2.get("reason")
    rows = _builds(s)
    assert len(rows) == len(first) + 1, [t["content"] for t in rows]
    rec = rows[-1]["build"]
    assert st2["build"]["id"] == st1["build"]["id"] == rec["id"]      # the chat replaces it
    assert rec["summary"].startswith("Updated your resume · 2 keywords added")
    assert rec["added"] == ["Excel", "Power BI"]
    assert any("Excel, Power BI" in i for i in rec["items"])
    # Undo inside the run takes it back out of the same row.
    st3 = s.keyword_undo("Power BI")
    assert st3["ok"] and len(_builds(s)) == len(first) + 1
    assert _builds(s)[-1]["build"]["added"] == ["Excel"]
    assert "Excel added" in _builds(s)[-1]["build"]["summary"]
    # Something said in between starts a new row.
    s.history.append({"role": "user", "content": "thanks"})
    s.keyword_add("Power BI")
    assert len(_builds(s)) == len(first) + 2


def test_the_chat_replaces_the_row_in_place():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "if (same) { same.replaceWith(m); live?.remove(); }" in js


# ------------------------------------------------------------------ the agent knows the page

@requires_latex
def test_a_typed_question_after_the_build_is_answered_not_rebuilt(template_source, fake_llm, workdir):
    s, _st = _built(template_source, fake_llm, workdir)
    s.keyword_add("Excel")
    n = len(_builds(s))
    calls = fake_llm.agent_calls if hasattr(fake_llm, "agent_calls") else 0
    st = s.submit("1. if we need projects to demonstrate that skill, why have you already listed "
                  "projects on the resume ? 2. Also why is there so much space under the additional "
                  "information, looks like an incomplete resume")
    assert getattr(fake_llm, "agent_calls", 0) > calls, "the agent did not answer it"
    assert len(_builds(s)) == n and "build" not in st, "the question ran a rebuild"
    said = " ".join(st["messages"])
    assert WebIntake.EDIT_TIP not in said and "a bit thin" not in said
    assert len(st["messages"]) == 1
    # The agent was told what is on the page: the ADDed term among the job terms on it.
    assert "Excel" in fake_llm.last_agent_system.split("Job terms on the page now")[1].split("\n")[0]
    assert "fill_percent" not in fake_llm.last_agent_system      # plain lines, not a dump


@requires_latex
def test_fill_page_acts_and_reports(template_source, fake_llm, workdir, monkeypatch):
    s, _st = _built(template_source, fake_llm, workdir)
    ran = []
    real = s._fill_page_now
    monkeypatch.setattr(s, "_fill_page_now", lambda: ran.append(1) or real())
    fake_llm.agent_script = [
        {"tool_calls": [{"id": "t1", "name": "fill_page", "input": {}}]},
        {"text": "I filled the page from your own material."},
    ]
    pdf = s.workdir / f"{s.jobname}.pdf"
    before = pdf.read_bytes()
    v0, pct0 = s._pdf_v, s._page_fill_pct()
    st = s.submit("why is there so much white space at the bottom")
    assert ran, "the agent did not act on the white space"
    if s._pdf_v > v0:        # kept: fuller, a row in the chat, and the claim stands
        assert s._page_fill_pct() > pct0 or s._written
        assert st.get("build") and st["build"]["kind"] == "fill"
        assert st["messages"] == ["I filled the page from your own material."]
    else:                    # no fuller: the page and its PDF stay exactly as they were
        assert pdf.read_bytes() == before and "build" not in st
    out = s._fill_page_now()
    assert out["ok"] and "fill_after_percent" in out
    if out.get("still_short"):
        assert out["why_still_short"] and out["real_material_that_would_fill_it"]


def test_state_lines_say_what_is_on_the_page():
    lines = "\n".join(rc.state_lines({
        "resume_built": True, "page": {"one_page": True, "fill_percent": 83, "short": True},
        "job_terms_on_page": ["X++", "Azure Data Factory"], "job_terms_not_on_page": ["Fabric"],
        "keywords_the_person_added": ["X++"],
        "projects_on_page": [{"name": "Gate Check", "github_link": False}],
        "github": {"connected": False}, "telegram_connected": True}))
    assert "Job terms on the page now (2): X++, Azure Data Factory." in lines
    assert "their own fact" in lines and "X++" in lines.split("ADD in the keyword panel")[1]
    assert "83% full" in lines and "Gate Check (no GitHub link)" in lines
    assert "GitHub: not connected and no username known." in lines
    assert "Telegram: connected." in lines
    assert "—" not in lines and "–" not in lines


def test_share_your_github_becomes_the_connect_button(fake_llm):
    ctx = rc.AppContext(llm=fake_llm, resume_snapshot=lambda: {}, upload_summary=lambda: None,
                        github_status=lambda: {"connected": False, "login": "", "resume_login": ""},
                        list_repos=lambda login: {})
    fake_llm.agent_script = [{"text": "Want to build one together, or share your GitHub so I can "
                                      "check for a fit?"}]
    out = rc.run_turn(None, ctx, "what now?")
    assert "share your GitHub" not in out["text"]
    assert "connect your GitHub with the button below" in out["text"]
    assert any(b.get("nav") == "github" for c in out["cards"] for b in c.get("buttons") or [])
    # Connected: left alone.
    ctx.github_status = lambda: {"connected": True, "login": "kofi"}
    fake_llm.agent_script = [{"text": "Or share your GitHub link."}]
    assert rc.run_turn(None, ctx, "what now?")["text"] == "Or share your GitHub link."


@pytest.mark.parametrize("msg,want", [
    ("1. why have you listed projects ? 2. Also why is there so much space", True),
    ("why is there so much space under additional information", True),
    ("What about my certifications", True),
    ("Yes, I have used Looker at the bank", False),
    ("Can you add Docker to my skills?", False),
    ("I volunteer at the food bank on weekends", False),
    ("https://github.com/kofi/route-planner", False),
    ("no", False),
])
def test_asks_something(msg, want):
    assert rc.asks_something(msg) is want


# ------------------------------------------------------------------ white space is never "done"

def test_missing_sections_are_asked_once_together_and_said_plainly(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    st = s.submit(SKELETON)
    said = " ".join(st["messages"])
    assert "won't look complete without them" in said
    assert "Extracurricular" in said and "Interests" in said     # both, in one message
    assert sum("won't look complete" in m for m in st["messages"]) == 1


@requires_latex
def test_thats_everything_over_a_short_page_fills_it_never_calls_it_done(
        template_source, fake_llm, workdir, monkeypatch):
    s, _st = _built(template_source, fake_llm, workdir)
    assert s.assembled.underfull                        # this thin profile builds a short page
    ran = []
    real = s._fill_page_now
    monkeypatch.setattr(s, "_fill_page_now", lambda: ran.append(1) or real())
    st = s.submit("that's everything")
    said = " ".join(st["messages"])
    assert ran, "the page fill did not run"
    assert s.essentials.get("enrich_done") is True
    assert len(st["messages"]) == 1
    if s.assembled.underfull:
        assert "isn't finished yet" in said and "Accept & Save" not in said
    else:
        assert "Accept & Save" in said


def test_a_later_add_never_pushes_an_earlier_add_off_its_line():
    from tailoring.linefit import place_term
    width = lambda label, terms: 10.0 * len(terms)          # noqa: E731 - 5 terms fit
    skills = {"Tools": "SQL, Python, Azure Data Factory, Tableau, Excel"}
    jd = "SQL Python Azure Data Factory Tableau Excel Power BI"
    pl = place_term(skills, "Power BI", width_fn=width, avail=50, jd_text=jd,
                    keep=["Azure Data Factory"])
    assert "Azure Data Factory" in pl.skills["Tools"] and "Power BI" in pl.skills["Tools"]
    assert "Azure Data Factory" not in pl.shed


def test_the_change_list_names_a_project_cleanly():
    from tailoring.changes import readable
    ch = {"reworded": [{"label": "Last-Mile Operations Simulator (Python, simulation, operations analytics)",
                        "before": "a", "after": "b"}]}
    assert readable(ch)[0] == "Reworded a Last-Mile Operations Simulator bullet toward the job."


def test_a_page_claim_without_a_change_is_not_said():
    assert rc.honest("I filled the page from your material.", did=[]) == rc.NOT_CHANGED
    assert rc.honest("I filled the page.", did=["fill_page"]) == "I filled the page."
