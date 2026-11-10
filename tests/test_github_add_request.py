"""A clear "add 2 github projects" is acted on with the person's GitHub, even while an intake
answer is pending (Kofi, 2026-11-09: GitHub connected, he typed "add 2 github projects" and the
intake answered "Send me the repo links (or names) plus a line on what each one does").

- An act on GitHub, repositories or projects (add, put, use, include, pull, import, show, rank,
  check, pick, find) leaves the funnel while an answer is pending; material (a pasted list or
  links, a described project) and declines ("no github, no projects") stay on it.
- Connected: the repos are ranked for this job and the top N that fit (strong or some fit) are
  added, the same add as the repo_fit card's button; the reply names them and the card stays.
- Fewer fit than asked: those are added and the reply says so, offering to build the rest.
- Not connected and no username: the Connect GitHub card, and connecting carries on with the
  same add, N included.
- The agent's prompt and tool: add from GitHub with the tool, never ask for repo links.
FakeLLM and a fake GitHub only."""

from __future__ import annotations

import base64

import pytest

import ui.app as app
from agent import resume_chat as rc
from intake.memory import ConversationMemory
from ui import github_publish, github_repos, repo_rank
from ui.records import CVRecords
from ui.session import WebIntake

JD = ("Business Intelligence Analyst. Required: SQL, Power BI, Tableau, Python, KPI dashboards, "
      "stakeholder management.")
TOKEN = "ghu_SECRETTOKEN1234567890"
ROLE = "Business Intelligence Analyst"


def _repo(name, **kw):
    r = {"name": name, "html_url": f"https://github.com/janedoe/{name}", "description": f"{name} tool",
         "topics": [], "language": "Python", "stargazers_count": 1, "pushed_at": "2026-10-01T00:00:00Z",
         "created_at": "2025-03-04T00:00:00Z", "fork": False, "archived": False, "size": 10}
    r.update(kw)
    return r


THREE_FIT = [_repo("kpi-dashboard", description="Power BI KPI dashboards"),
             _repo("sales-sql", description="SQL views for sales reporting"),
             _repo("churn-tableau", description="Tableau churn dashboard with Python"),
             _repo("game-of-life", description="Conway in Rust", language="Rust")]
ONE_FIT = [_repo("kpi-dashboard", description="Power BI KPI dashboards"),
           _repo("game-of-life", description="Conway in Rust", language="Rust")]
READMES = {"kpi-dashboard": "Built KPI dashboards in Power BI from SQL views. Python refreshes it.",
           "sales-sql": "SQL views and stored procedures for sales KPI reporting, loaded with Python.",
           "churn-tableau": "A Tableau dashboard of customer churn, data prepared in Python and SQL.",
           "game-of-life": "A Rust toy."}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    github_repos.clear_cache()
    repo_rank.clear_cache()
    monkeypatch.setenv(rc.FLAG, "1")
    yield
    github_repos.clear_cache()
    repo_rank.clear_cache()
    app._SESSION.pop("s", None)


def _github(monkeypatch, repos):
    calls = []

    def fetch(method, url, token="", body=None, params=None):
        calls.append(url)
        if url.endswith("/users/janedoe/repos"):
            return 200, [dict(r) for r in repos]
        name = url.split("/repos/janedoe/")[-1].split("/")[0] if "/repos/janedoe/" in url else ""
        if url.endswith("/readme") and name in READMES:
            return 200, {"content": base64.b64encode(READMES[name].encode()).decode()}
        if url.endswith("/languages"):
            return 200, {"Python": 900} if name != "game-of-life" else {"Rust": 900}
        return 404, {}
    monkeypatch.setattr(github_publish, "FETCH", fetch)
    return calls


class _Github:
    def __init__(self, on=True):
        self.on = on

    def status(self):
        return {"connected": True, "login": "janedoe"} if self.on else {"connected": False, "login": ""}

    def token(self):
        return TOKEN if self.on else ""


def _session(template_source, fake_llm, workdir, on=True):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                  workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.role = ROLE
    s.essentials["identity"].update({"name": "Jane Doe"})          # no GitHub username
    g = _Github(on)
    s.github_status, s.github_token = g.status, g.token
    return s, g


def _no_model(*a):
    pytest.fail("the add is decided without an agent call")


def _github_projects(s):
    return [p.get("org") for p in s.essentials.get("projects") or []
            if isinstance(p, dict) and p.get("source") == "github"]


def _ask_on_record(s):
    """Before the first build an intake answer is pending (the funnel has asked its question)."""
    assert s.assembled is None
    assert any(t.get("role") == "agent" for t in s.history)


# -- routing ------------------------------------------------------------------------------- #

ACTS = ["add 2 github projects", "Add two projects from my GitHub", "put my 3 best repos on it",
        "use my github", "pull my projects from GitHub", "check my github", "show my repos",
        "rank my repos for this job", "pick 2 repos from my github", "find projects on my github",
        "include my github projects", "import my repos"]
MATERIAL = ["https://github.com/janedoe/kpi-dashboard https://github.com/janedoe/sales-sql",
            "add these projects:\n1. Route Planner\n2. Dock Scheduler",
            "add these projects: Route Planner, Dock Scheduler",
            "add my project Route Planner, which modeled a 40-truck fleet in Python",
            "No github, no projects", "no github. no projects.", "I don't have projects"]


def test_acts_on_github_leave_the_funnel_while_an_answer_is_pending():
    for m in ACTS:
        assert rc.wants_agent(m, pending=True) and rc.wants_agent(m), m


def test_material_and_declines_stay_on_the_funnel():
    for m in MATERIAL:
        assert not rc.wants_agent(m, pending=True) and not rc.wants_agent(m), m
        assert rc.github_add_count(m) == 0, m


def test_the_count_is_read_from_the_request():
    assert rc.github_add_count("add 2 github projects") == 2
    assert rc.github_add_count("Add two projects from my GitHub") == 2
    assert rc.github_add_count("add the top 3 repos") == 3
    assert rc.github_add_count("add a project from my github") == 1
    assert rc.github_add_count("add 40 github projects") == rc.MAX_ADD
    for m in ("add my github projects", "add 2 projects", "check my github", "use my github"):
        assert rc.github_add_count(m) == 0, m        # the agent decides these


def test_add_request_during_a_pending_question_reaches_the_add(template_source, fake_llm, workdir,
                                                               monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)
    _ask_on_record(s)
    fake_llm.agent_script = _no_model
    fake_llm.extract_intake = lambda *a, **k: pytest.fail("the intake's extractor took the request")
    st = s.submit("add 2 github projects")
    assert "repo links" not in " ".join(st["messages"]).lower()
    assert st["cards"][0]["type"] == "repo_fit"


def test_a_use_my_github_request_during_intake_reaches_the_agent(template_source, fake_llm, workdir,
                                                                 monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)
    s.essentials["asked_required"] = True
    seen = []
    fake_llm.agent_script = lambda sys_, msgs, tools: seen.append(sys_) or {
        "text": "Here is what fits.", "tool_calls": [], "stop": "end_turn"}
    fake_llm.absorb_enrichment = lambda *a, **k: pytest.fail("enrichment took the request")
    s.submit("use my github")
    assert seen


# -- adding -------------------------------------------------------------------------------- #

def test_three_fitting_repos_add_exactly_two(template_source, fake_llm, workdir, monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _no_model
    st = s.submit("add 2 github projects")
    added = _github_projects(s)
    assert len(added) == 2 and set(added) <= {"kpi-dashboard", "sales-sql", "churn-tableau"}
    assert "game-of-life" not in added
    text = st["messages"][-1]
    assert all(name in text for name in added) and "I added" in text
    assert "Only" not in text and "repo links" not in text.lower()
    card = st["cards"][0]
    assert card["type"] == "repo_fit"
    on = {f["name"] for f in card["fits"] if f.get("on_resume")}
    assert on == set(added)                          # the card shows them on the resume
    left = [f for f in card["fits"] if not f.get("on_resume")]
    assert left and card["token"]                    # the third can still be added from the card
    # The same entries the card's button adds: the repo's link and source.
    p = next(p for p in s.essentials["projects"] if p.get("org") == added[0])
    assert p["link"] == f"https://github.com/janedoe/{added[0]}" and p["repo"] == f"janedoe/{added[0]}"


def test_one_fitting_repo_adds_one_and_says_so(template_source, fake_llm, workdir, monkeypatch):
    _github(monkeypatch, ONE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _no_model
    st = s.submit("add 2 github projects")
    assert _github_projects(s) == ["kpi-dashboard"]
    text = st["messages"][-1]
    assert text.startswith("Only one of your repos fits this job") and "kpi-dashboard" in text
    assert "build a project" in text and "game-of-life" not in text
    sends = [b.get("send") for c in st["cards"] if c["type"] == "buttons" for b in c["buttons"]]
    assert s.BUILD_ONE_SEND in sends


def test_after_the_build_the_add_rebuilds_the_page(template_source, fake_llm, workdir, monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)
    rebuilt = []
    s.assembled = object()                       # a draft exists
    monkeypatch.setattr(s, "_assemble", lambda *a, **k: rebuilt.append(1))
    monkeypatch.setattr(s, "_trims_note", lambda: "")
    monkeypatch.setattr(s, "_state", lambda messages=None, extra=None, cards=None: {
        "messages": list(messages or []), "cards": list(cards or [])})
    monkeypatch.setattr(s, "_resume_projects", lambda github_too=False: [
        p for p in s.essentials.get("projects") or [] if github_too or p.get("source") != "github"])
    fake_llm.agent_script = _no_model
    st = s.submit("add 2 github projects")
    assert rebuilt and len(_github_projects(s)) == 2
    assert "rebuilt your resume" in st["messages"][-1]


# -- not connected ------------------------------------------------------------------------- #

def test_not_connected_gives_the_connect_card_and_carries_on_with_the_add(template_source, fake_llm,
                                                                          workdir, monkeypatch):
    calls = _github(monkeypatch, THREE_FIT)
    s, g = _session(template_source, fake_llm, workdir, on=False)
    fake_llm.agent_script = _no_model
    st = s.submit("add 2 github projects")
    assert st["cards"] == [dict(rc.connect_card(), add=2)]
    assert "two repos" in st["messages"][-1] and not calls and not _github_projects(s)
    g.on = True
    st = s.github_connected()
    assert st["resumed"] is True
    assert len(st["messages"]) == 1          # one voice: the lead and what was added, one message
    assert st["messages"][0].startswith(f"Connected as janedoe. Adding the two repos that fit this {ROLE} role best.")
    assert len(_github_projects(s)) == 2
    assert st["cards"][0]["type"] == "repo_fit"
    again = s.github_connected()                  # once per connect
    assert again["resumed"] is False and len(_github_projects(s)) == 2


# -- the agent's tool and prompt ------------------------------------------------------------ #

def test_the_prompt_says_use_the_tools_never_ask_for_links():
    ctx = rc.AppContext(llm=None, resume_snapshot=dict, upload_summary=lambda: None,
                        github_status=lambda: {}, list_repos=lambda login: {}, add_top=lambda n, l: {})
    prompt = rc.system_prompt(rc.tools_for(ctx))
    assert "add_github_projects" in prompt
    assert "Never ask them to send repository links or names when GitHub is connected" in prompt
    bare = rc.AppContext(llm=None, resume_snapshot=dict, upload_summary=lambda: None,
                         github_status=lambda: {}, list_repos=lambda login: {})
    assert "add_github_projects" not in rc.tools_for(bare)


def test_the_agent_tool_adds_and_counts_as_a_change(template_source, fake_llm, workdir, monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, _g = _session(template_source, fake_llm, workdir)

    def script(system, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"text": "I added them to your resume.", "tool_calls": []}
        return {"text": "", "tool_calls": [{"id": "1", "name": "add_github_projects",
                                            "input": {"count": 1}}]}
    fake_llm.agent_script = script
    out = rc.run_turn(s, s._agent_ctx(), "put my best github project on my resume")
    assert out["did"] == ["add_github_projects"] and len(_github_projects(s)) == 1
    assert out["text"] == "I added them to your resume."     # not stripped: it really happened
    assert out["cards"][0]["type"] == "repo_fit"


def test_the_agent_tool_not_connected_remembers_the_add(template_source, fake_llm, workdir, monkeypatch):
    _github(monkeypatch, THREE_FIT)
    s, g = _session(template_source, fake_llm, workdir, on=False)

    def script(system, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"text": rc.CONNECT_LINE, "tool_calls": []}
        return {"text": "", "tool_calls": [{"id": "1", "name": "add_github_projects",
                                            "input": {"count": 3}}]}
    fake_llm.agent_script = script
    st = s.submit("use my github projects")
    assert st["cards"][0].get("add") == 3 and not _github_projects(s)
    g.on = True
    fake_llm.agent_script = _no_model
    st = s.github_connected()
    assert st["resumed"] is True and len(_github_projects(s)) == 3
