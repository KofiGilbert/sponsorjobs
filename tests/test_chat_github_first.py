"""The resume chat checks GitHub before it offers a check, and carries on by itself once GitHub
is connected (Kofi, test build 13, 2026-11-09: "why didn't you check whether or not it is
connected before telling them you can check or before telling them to connect!").

- Not connected and no username: one line and a Connect GitHub button, never menu steps.
- "Check my GitHub" (the offer's button) runs the ranking itself and shows the repo_fit card.
- GitHub connected from anywhere (the chat's card, Settings) while the chat's last word on GitHub
  was "not connected": "Connected as <login>. Checking your repos for this <role> role." and
  the repo_fit card, with no turn from the person, once per connect. Earlier Connect GitHub
  buttons are marked connected so a restored chat shows them done.
FakeLLM and a fake GitHub only."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

import ui.app as app
from agent import resume_chat as rc
from intake.memory import ConversationMemory
from ui import github_publish, github_repos, repo_rank
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]
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


REPOS = [_repo("kpi-dashboard", description="Power BI KPI dashboards"),
         _repo("game-of-life", description="Conway in Rust", language="Rust")]
READMES = {"kpi-dashboard": "Built KPI dashboards in Power BI from SQL views. Python refreshes it.",
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


@pytest.fixture
def gh(monkeypatch):
    calls = []

    def fetch(method, url, token="", body=None, params=None):
        calls.append(url)
        if url.endswith("/users/janedoe/repos"):
            return 200, [dict(r) for r in REPOS]
        name = url.split("/repos/janedoe/")[-1].split("/")[0] if "/repos/janedoe/" in url else ""
        if url.endswith("/readme") and name in READMES:
            return 200, {"content": base64.b64encode(READMES[name].encode()).decode()}
        if url.endswith("/languages"):
            return 200, {"Python": 900} if name == "kpi-dashboard" else {}
        return 404, {}
    monkeypatch.setattr(github_publish, "FETCH", fetch)
    return calls


class _Github:
    """The app's GitHub hook, flipped to connected the way a connect in Settings would."""
    def __init__(self):
        self.on = False

    def status(self):
        return {"connected": True, "login": "janedoe"} if self.on else {"connected": False, "login": ""}

    def token(self):
        return TOKEN if self.on else ""


def _session(template_source, fake_llm, workdir):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                  workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.role = ROLE
    s.essentials["identity"].update({"name": "Jane Doe"})          # no GitHub username
    g = _Github()
    s.github_status, s.github_token = g.status, g.token
    return s, g


def _status_script(system, messages, tools):
    """The agent asked about GitHub while it is not connected: github_status, then one line."""
    if messages[-1]["role"] == "tool":
        return {"text": rc.CONNECT_LINE, "tool_calls": []}
    return {"text": "", "tool_calls": [{"id": "1", "name": "github_status", "input": {}}]}


def _no_model(*a):
    pytest.fail("the check and the carry-on make no agent call")


def _cards(s):
    return [c for t in s.history if isinstance(t, dict) for c in [t.get("card")] + list(t.get("cards") or [])
            if isinstance(c, dict)]


# -- the tools say one line and give the button ---------------------------------------------- #

def test_not_connected_status_gives_the_button_and_no_steps():
    ctx = rc.AppContext(llm=None, resume_snapshot=dict, upload_summary=lambda: None,
                        github_status=lambda: {"connected": False, "login": "", "resume_login": ""},
                        list_repos=lambda login: {})
    for name in ("github_status", "list_repos", "rank_repos_for_jd"):
        out = rc.TOOLS[name].run(ctx, {})
        assert out["card"] == rc.connect_card(), name
        assert "Settings" not in str(out) and "approve the code" not in str(out), name
    prompt = rc.system_prompt(rc.TOOLS)
    assert "Never describe menus" in prompt and "Settings, then Connections" not in prompt


def test_no_chat_text_carries_menu_steps():
    for path in ("agent/resume_chat.py", "ui/session.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "Settings, then Connections" not in src and "GITHUB_CONNECT_STEPS" not in src, path
        assert "approve the code on GitHub" not in src, path


# -- the check ----------------------------------------------------------------------------- #

def test_check_my_github_when_connected_runs_the_ranking(template_source, fake_llm, workdir, gh):
    s, g = _session(template_source, fake_llm, workdir)
    g.on = True
    fake_llm.agent_script = _no_model
    n_user = sum(t.get("role") == "user" for t in s.history)
    st = s.submit(rc.CHECK_GITHUB_SEND)
    card = st["cards"][0]
    assert card["type"] == "repo_fit" and card["token"]
    assert "kpi-dashboard" in [f["name"] for f in card["fits"]]
    assert "kpi-dashboard" in st["messages"][-1] and rc.ADD_LABEL in st["messages"][-1]
    assert sum(t.get("role") == "user" for t in s.history) == n_user + 1     # the click itself
    assert any(u.endswith("/users/janedoe/repos") for u in gh)


def test_check_my_github_when_not_connected_says_one_line_with_the_button(template_source, fake_llm,
                                                                          workdir, gh):
    s, _g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _no_model
    st = s.submit(rc.CHECK_GITHUB_SEND)
    assert st["cards"] == [rc.connect_card()]
    assert st["messages"] == [s.GITHUB_NOT_CONNECTED]
    assert "Settings" not in st["messages"][0] and "ask me again" not in st["messages"][0].lower()
    assert not gh


# -- carrying on once connected -------------------------------------------------------------- #

def test_connecting_in_settings_resumes_the_chat_once(template_source, fake_llm, workdir, gh):
    s, g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _status_script
    st = s.submit("Which of my GitHub projects fit this job?")
    assert st["messages"] == [rc.CONNECT_LINE]
    assert any(b.get("nav") == "github" for b in st["cards"][0]["buttons"])
    # Not connected yet: nothing happens.
    assert s.github_connected()["resumed"] is False
    # The person connects in Settings (not from the chat), then comes back.
    g.on = True
    fake_llm.agent_script = _no_model
    turns = len(s.history)
    users = [t for t in s.history if t.get("role") == "user"]
    st = s.github_connected()
    assert st["resumed"] is True and st["github_login"] == "janedoe"
    assert len(st["messages"]) == 1          # one voice: the lead and the takeaway, one message
    assert st["messages"][0].startswith(f"Connected as janedoe. Checking your repos for this {ROLE} role.")
    assert st["cards"][0]["type"] == "repo_fit"
    assert [t for t in s.history if t.get("role") == "user"] == users      # no turn from the person
    assert all(t["role"] == "agent" for t in s.history[turns:])
    # The earlier Connect GitHub button is marked connected (a restored chat shows it done).
    old = [c for c in _cards(s) if c.get("type") == "buttons"
           and any(b.get("nav") == "github" for b in c["buttons"])]
    assert old and all(c.get("connected") == "janedoe" for c in old)
    # A second status check (focus, view change) ranks nothing again.
    fits = sum(c.get("type") == "repo_fit" for c in _cards(s))
    again = s.github_connected()
    assert again["resumed"] is False and not again["messages"]
    assert sum(c.get("type") == "repo_fit" for c in _cards(s)) == fits == 1


def test_a_connect_already_answered_another_way_does_not_rank_again(template_source, fake_llm, workdir, gh):
    s, g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _status_script
    s.submit("Which of my GitHub projects fit this job?")
    g.on = True
    fake_llm.agent_script = _no_model
    s.submit(rc.CHECK_GITHUB_SEND)                     # ranked before the chat noticed the connect
    st = s.github_connected()
    assert st["resumed"] is False
    assert sum(c.get("type") == "repo_fit" for c in _cards(s)) == 1
    assert all(c.get("connected") for c in _cards(s) if c.get("type") == "buttons"
               and any(b.get("nav") == "github" for b in c["buttons"]))


def test_a_check_click_that_started_the_connect_resumes(template_source, fake_llm, workdir, gh):
    """An old offer card's "Check my GitHub", clicked while not connected, starts the connect
    (in the page); once connected the chat checks by itself (pending)."""
    s, g = _session(template_source, fake_llm, workdir)
    g.on = True
    fake_llm.agent_script = _no_model
    st = s.github_connected(pending=True)
    assert st["resumed"] and st["messages"][0].startswith("Connected as janedoe.")
    assert st["cards"][0]["type"] == "repo_fit"


def test_the_route_resumes_and_is_a_no_op_without_a_session(template_source, fake_llm, workdir, gh):
    client = app.app.test_client()
    assert client.post("/api/session/github_connected", json={}).get_json() == {"resumed": False}
    s, g = _session(template_source, fake_llm, workdir)
    fake_llm.agent_script = _status_script
    s.submit("Which of my GitHub projects fit this job?")
    g.on = True
    fake_llm.agent_script = _no_model
    app._SESSION["s"] = s
    st = client.post("/api/session/github_connected", json={}).get_json()
    assert st["resumed"] and st["cards"][0]["type"] == "repo_fit"
    hist = client.get("/api/session/state").get_json()["history"]
    assert any((t.get("card") or {}).get("connected") == "janedoe" for t in hist)
    assert client.post("/api/session/github_connected", json={}).get_json()["resumed"] is False


# -- the page ------------------------------------------------------------------------------ #

def test_the_page_connects_from_the_card_and_shows_stale_buttons_done():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    # a Connect GitHub button renders through chatGhBox, done when the card says connected
    assert 'if (b.nav === "github") { wrap.appendChild(chatGhBox(card.connected)); return; }' in js
    assert "Connected as ${connected}" in js
    # one click starts the App connect right in the chat, with the wait on the card
    body = js.split("async function connectFromChat()", 1)[1].split("\n  }\n", 1)[0]
    assert "ghAppStart()" in body
    assert "Finish in your browser" in js and "chatGhUi(waiting, text)" in js
    # any connect (here or in Settings) and coming back to the chat both check once
    assert 'document.addEventListener("tailor:github", () => chatGithubCheck())' in js
    assert 'window.addEventListener("focus", () => chatGithubCheck())' in js
    assert re.search(r'if \(name === "builder"\) setTimeout\(\(\) => chatGithubCheck\(\)', js)
    assert "/api/session/github_connected" in js
