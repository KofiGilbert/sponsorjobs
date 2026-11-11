"""One proactive offer, the placeholder flow retired, and the session lock (one-brain plan PR 10).

With TAILOR_CHAT_AGENT on, the first build in a chat whose coverage shows missing job terms and
whose projects are weak for the job ends with ONE message carrying two buttons ("Check my GitHub",
"Build a project for this job"); no model call is made for it, and it never repeats in that
session. No path writes a "suggested" placeholder project with the flag on (flag off keeps the
old flow). Every session-changing route waits for WebIntake.lock. FakeLLM only.
"""

from __future__ import annotations

import threading
import time

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


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")


def _s(template_source, fake_llm, workdir):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    return s


class _Built:          # stands in for an assembled page: the offer only reads coverage
    profile_used: dict = {}


def _built(s, monkeypatch, missing=("ETL", "forecasting"), present=("SQL",), projects=None):
    s.assembled = _Built()
    s.assembled.profile_used = {"projects": list(projects or [])}
    monkeypatch.setattr(s, "coverage_dict", lambda: {"present": list(present), "missing": list(missing),
                                                     "missing_supported": []})
    return s


def _offers(s):
    return [t for t in s.history if (t.get("card") or {}).get("offer") == "projects"]


# -- the offer: projects decided, not offered (2026-11-11) ----------------------------------- #

def _repos(monkeypatch, repos):
    from ui import github_repos, repo_rank
    monkeypatch.setattr(github_repos, "list_repos", lambda login, token="": {
        "ok": True, "login": login, "repos": [dict(r, full_name=f"{login}/{r['name']}",
                                                   url=f"https://github.com/{login}/{r['name']}",
                                                   pushed_at="2026-09-01T00:00:00Z",
                                                   created_at="2026-08-01T00:00:00Z") for r in repos]})
    readmes = {f"{r['name']}": r.get("readme", "") for r in repos}
    monkeypatch.setattr(repo_rank, "fetch_details", lambda full, pushed_at, token="", now=None: {
        "readme": readmes.get(full.split("/")[-1], ""), "languages": [], "paths": []})


WEAK = [{"org": "Chess Club Site", "tools": "HTML", "bullets": ["Made a club website."]}]


def test_a_better_repo_is_named_with_a_swap_button_and_no_model_call_from_the_agent(
        template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=WEAK)
    s.github_status = lambda: {"connected": True, "login": "kwame"}
    _repos(monkeypatch, [{"name": "etl-forecasts", "description": "An ETL job and forecasting in Python",
                          "readme": "Built an ETL job and forecasting models in Python for daily sales."},
                         {"name": "notes", "description": "My notes"}])
    fake_llm.agent_script = lambda *a: pytest.fail("the verdict must not call the agent")
    msgs = []
    cards = s._project_offer(msgs)
    assert msgs[0].startswith("Your projects don't show ETL or forecasting, which this role asks for.")
    assert "Of your 2 public repos, etl-forecasts fits better" in msgs[0]
    assert "I can check your GitHub" not in msgs[0] and "if you'd like" not in msgs[0]
    (b,) = cards[0]["buttons"]
    assert b["label"] == "Swap it in" and b["choice"] == "swap" and b["token"] in s.action_tokens
    assert "—" not in msgs[0] and "–" not in msgs[0] and "CV" not in msgs[0]
    assert s.history[-1]["card"] is cards[0]
    # judged once per session: never again, even if the next build still has gaps
    assert s._project_offer([]) == [] and len(_offers(s)) == 1


def test_no_repo_fits_so_two_projects_are_offered_as_one_button(template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=WEAK)
    s.github_status = lambda: {"connected": False, "login": ""}
    s.essentials["identity"]["github"] = "https://github.com/kwame"   # public repos, no sign in
    s.start_build = lambda idea, cv: {"id": "p1", "title": idea["title"]}
    s.ideas_for = lambda: {"ok": True, "ideas": [{"title": "Forecast Lab"}, {"title": "ETL Kit"}]}
    _repos(monkeypatch, [{"name": "notes", "description": "My notes"}])
    msgs = []
    cards = s._project_offer(msgs)
    assert msgs[0] == ("None of your 1 public repos shows ETL or forecasting. I can build two small "
                       "projects for this role.")
    assert cards[0]["buttons"][0]["label"] == "Build 2 projects"
    monkeypatch.setattr(s, "_state", lambda msgs=None, extra=None, cards=None: {
        "phase": "review", "messages": list(msgs or []), "cards": list(cards or [])})
    st = s.apply_action(cards[0]["buttons"][0]["token"], "build_two")
    assert st["action_ok"] and st["projects"] == ["Forecast Lab", "ETL Kit"]


def test_projects_that_already_fit_say_nothing(template_source, fake_llm, workdir, monkeypatch):
    fit = [{"org": "Sales Pipeline", "tools": "Python, ETL, forecasting",
            "bullets": ["Built a nightly ETL job with forecasting."]}]
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=fit)
    s.github_status = lambda: {"connected": True, "login": "kwame"}
    _repos(monkeypatch, [{"name": "notes", "description": "My notes"}])
    assert s._project_offer([]) == []


def test_no_offer_when_nothing_is_missing_or_the_projects_fit(template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, missing=())
    assert s._project_offer([]) == []
    fit = [{"org": "Sales Pipeline", "tools": "Python, ETL", "bullets": ["Built a nightly ETL job."]}]
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=fit)
    assert s._project_offer([]) == []
    weak = [{"org": "Chess Club Site", "tools": "HTML", "bullets": ["Made a club website."]}]
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=weak)
    assert s._project_offer([])


def test_no_offer_with_the_flag_off_or_unattended(template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch)
    s.essentials["unattended"] = True
    assert s._project_offer([]) == []
    monkeypatch.delenv(rc.FLAG)
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch)
    assert s._project_offer([]) == []


def test_a_restored_chat_that_already_has_the_offer_does_not_repeat(template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch)
    s.history.append({"role": "agent", "content": "earlier", "card": {"type": "buttons", "offer": "projects"}})
    assert s._project_offer([]) == []


@requires_latex
def test_the_first_build_ends_with_the_offer_once(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    assert st["phase"] == "review"
    offer = [c for c in st["cards"] if c.get("offer") == "projects"]
    assert len(offer) == 1 and st["messages"][-1] == s.history[-1]["content"]
    st2 = s.draft()                                       # a rebuild in the same chat
    assert not [c for c in st2["cards"] if c.get("offer") == "projects"] and len(_offers(s)) == 1


# -- the placeholder flow is retired with the flag on --------------------------------------- #

IDEA_ASKS = ["suggest some projects I could build for this job", "give me a project idea",
             "what should i build", "Build me a project for this job"]


def test_flag_on_never_writes_a_suggested_project(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.ideas_for = lambda: {"ok": True, "ideas": []}
    for ask in IDEA_ASKS:
        s.submit(ask)
    s.suggest_projects_flow()                             # even a direct call goes to the agent
    bags = [s.essentials.get("projects") or [], (s.profile or {}).get("projects") or []]
    assert not any(p.get("suggested") for bag in bags for p in bag)
    assert "suggested_projects" not in s._state()


def test_flag_off_keeps_the_placeholder_flow(template_source, fake_llm, workdir, monkeypatch):
    monkeypatch.delenv(rc.FLAG)
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit("suggest some projects I could build for this job")
    assert any(p.get("suggested") for p in s.essentials.get("projects") or [])


# -- the session lock ---------------------------------------------------------------------- #

LOCKED = [("ingest_document", ("text", "f.pdf")), ("ingest_resume", ("text", "f.pdf")),
          ("ingest_transcript", ("text", "t.pdf")), ("set_title", ("r1", "keep")),
          ("decline_required", ()), ("keyword_apply", ("SQL", "")), ("keyword_undo", ("SQL",)),
          ("put_back", ("t1",)), ("remove_suggested_projects", ()), ("edit_bullet", ("b1", "x")),
          ("set_shown", ("e1", True)), ("accept", ())]


@pytest.mark.parametrize("name", [n for n, _ in LOCKED])
def test_session_changing_methods_take_the_lock(name):
    assert getattr(getattr(WebIntake, name), "_session_locked", False), name


@pytest.mark.parametrize("name,args", LOCKED[:4] + LOCKED[5:8], ids=[n for n, _ in LOCKED[:4] + LOCKED[5:8]])
def test_a_route_waits_while_a_chat_turn_holds_the_lock(template_source, fake_llm, workdir, name, args):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    done = threading.Event()

    def run():
        try:
            getattr(s, name)(*args)
        except Exception:      # noqa: BLE001 - only the waiting is under test
            pass
        done.set()

    s.lock.acquire()                                       # a chat turn on another request
    try:
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.2)
        assert not done.is_set()
    finally:
        s.lock.release()
    t.join(30)
    assert done.is_set()


# -- GitHub is checked before a check is offered (Kofi, test build 13, 2026-11-09) ------------ #

def test_offer_without_github_has_connect_and_no_check_promise(template_source, fake_llm, workdir, monkeypatch):
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch)    # not connected, no username
    msgs = []
    cards = s._project_offer(msgs)
    assert cards[0]["buttons"] == [{"label": "Connect GitHub", "nav": "github"}]
    assert "I can check your GitHub" not in msgs[0] and "Check my GitHub" not in str(cards)
    assert "Connect your GitHub and I'll check your repos" in msgs[0]
    assert "Settings" not in msgs[0] and "—" not in msgs[0] and "–" not in msgs[0]


def test_a_repo_with_no_bullet_to_show_is_never_swapped_in(template_source, fake_llm, workdir, monkeypatch):
    # Kofi's real run: the swap put "Data-Analytics-Projects" on the page as one bare "Built with
    # Jupyter Notebook, Python." line in place of Gate Check and its real bullet (2026-11-11).
    s = _built(_s(template_source, fake_llm, workdir), monkeypatch, projects=WEAK)
    s.github_status = lambda: {"connected": True, "login": "kwame"}
    _repos(monkeypatch, [{"name": "etl-forecasts", "description": "An ETL job and forecasting in Python"}])
    msgs = []
    cards = s._project_offer(msgs)
    assert "Swap it in" not in str(cards) and "fits better" not in msgs[0]
    assert "none of your 1 public repos describes one well enough" in msgs[0]
