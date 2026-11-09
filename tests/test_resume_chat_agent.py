"""The resume chat agent, first piece (agent/resume_chat.py, flag TAILOR_CHAT_AGENT): a tool
loop that reads the resume, the last upload, the GitHub connection and the person's public
repositories, and answers only from what came back. FakeLLM and a fake GitHub only."""

from __future__ import annotations

import json

import pytest

import ui.app as app
from agent import resume_chat as rc
from intake.memory import ConversationMemory
from llm.agent_protocol import anthropic_messages, parse_json_reply
from ui import github_publish, github_repos
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Data Analyst at Northwind. Required: SQL, Python, dashboards, ETL, Tableau."
TOKEN = "ghp_SECRETTOKEN1234567890"


def _repo(name, **kw):
    r = {"name": name, "html_url": f"https://github.com/janedoe/{name}", "description": f"{name} desc",
         "topics": ["sql"], "language": "Python", "stargazers_count": 2, "pushed_at": "2026-10-01T00:00:00Z",
         "fork": False, "archived": False, "size": 10}
    r.update(kw)
    return r


REPOS = [_repo("route-planner"), _repo("forked-lib", fork=True), _repo("old-thing", archived=True),
         _repo("empty-repo", size=0), _repo("dock-scheduler")]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    github_repos.clear_cache()
    monkeypatch.delenv(rc.FLAG, raising=False)
    yield
    github_repos.clear_cache()


@pytest.fixture
def gh(monkeypatch):
    calls = []

    def fetch(method, url, token="", body=None, params=None):
        calls.append({"url": url, "token": token, "params": params})
        return 200, [dict(r) for r in REPOS]
    monkeypatch.setattr(github_publish, "FETCH", fetch)
    return calls


def _session(template_source, fake_llm, workdir):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                  workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.essentials["identity"].update({"name": "Jane Doe", "github": "https://github.com/janedoe"})
    s.essentials["projects"] = [{"org": "Route Planner", "bullets": ["Modeled a 40-truck fleet."]}]
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    return s


def _ctx(**kw):
    base = dict(llm=None, resume_snapshot=lambda: {"projects": [{"name": "Route Planner"}]},
                upload_summary=lambda: None,
                github_status=lambda: {"connected": False, "login": "", "resume_login": "janedoe"},
                list_repos=lambda login: github_repos.list_repos(login))
    base.update(kw)
    return rc.AppContext(**base)


# -- the loop ---------------------------------------------------------------------------- #

def test_the_loop_stops_after_four_tool_rounds(fake_llm, gh):
    fake_llm.agent_script = lambda s, m, t: {"text": "", "stop": "tool_use",
                                             "tool_calls": [{"id": "x", "name": "resume_snapshot", "input": {}}]}
    out = rc.run_turn(None, _ctx(llm=fake_llm), "what projects do I have?")
    assert out["tools"] == ["resume_snapshot"] * rc.MAX_ROUNDS
    assert fake_llm.agent_calls == rc.MAX_ROUNDS + 1        # the last call may only answer
    assert out["text"] == rc.POLITE_FAIL and out["did"] == []


def test_a_tool_that_raises_becomes_a_polite_line(fake_llm):
    def boom():
        raise RuntimeError("database is locked /Users/x/secret.db")
    seen = {}

    def script(system, messages, tools):
        if messages[-1]["role"] == "tool":
            seen["result"] = messages[-1]["results"][0]
            return {"text": "That lookup did not work just now.", "tool_calls": []}
        return {"text": "", "tool_calls": [{"id": "1", "name": "resume_snapshot", "input": {}}]}
    fake_llm.agent_script = script
    out = rc.run_turn(None, _ctx(llm=fake_llm, resume_snapshot=boom), "show my resume")
    assert seen["result"]["is_error"] and "secret.db" not in seen["result"]["content"]
    assert rc.TOOL_FAIL in seen["result"]["content"]
    assert "did not work" in out["text"]


def test_the_model_failing_is_a_polite_line(fake_llm):
    def fail(*a):
        raise TimeoutError("read timed out")
    fake_llm.agent_script = fail
    assert rc.run_turn(None, _ctx(llm=fake_llm), "github?")["text"] == rc.POLITE_FAIL


def test_the_system_prompt_lists_exactly_the_registered_tools(fake_llm, gh):
    rc.run_turn(None, _ctx(llm=fake_llm), "what's on my github?")
    listed = [ln[2:].split(":", 1)[0] for ln in fake_llm.last_agent_system.splitlines()
              if ln.startswith("- ") and ":" in ln and ln[2:].split(":", 1)[0] in rc.TOOLS]
    assert listed == list(rc.TOOLS)
    assert [t["name"] for t in fake_llm.last_agent_tools] == list(rc.TOOLS)
    only = {"github_status": rc.TOOLS["github_status"]}
    sysp = rc.system_prompt(only)
    assert "- github_status:" in sysp and "list_repos" not in sysp and "resume_snapshot" not in sysp
    assert "—" not in sysp and "–" not in sysp


def test_honest_removes_an_unbacked_claim():
    out = rc.honest("I added Route Planner to your resume. You have two repositories.", did=[])
    assert "I added" not in out and "two repositories" in out and rc.NOT_CHANGED in out
    assert rc.honest("I added it.", did=["add_projects"]) == "I added it."
    assert rc.honest("I've saved that.", did=[]) == rc.NOT_CHANGED
    listed = "Your repos:\n1. route-planner\n2. dock-scheduler\nI added both to your resume."
    assert rc.honest(listed, did=[]) == ("Your repos:\n1. route-planner\n2. dock-scheduler "
                                         + rc.NOT_CHANGED)
    assert rc.honest("Repos:\n1. a\n2. b", did=[]) == "Repos:\n1. a\n2. b"
    assert rc._clean("**Projects:** none\n## Repos") == "<b>Projects:</b> none\nRepos"


def test_cards_are_checked_and_merged():
    cards = rc.merge_cards([
        {"type": "buttons", "buttons": [{"label": "Open Projects", "nav": "projects"},
                                        {"label": "Evil", "nav": "javascript:alert(1)"}]},
        {"type": "buttons", "buttons": [{"label": "open projects", "nav": "projects"},
                                        {"label": "Ask", "send": "Which fit?"}]},
        {"type": "html", "html": "<script>"}])
    assert cards == [{"type": "buttons", "buttons": [{"label": "Open Projects", "nav": "projects"},
                                                     {"label": "Ask", "send": "Which fit?"}]}]


def test_routing_words():
    assert rc.wants_agent("what's on my github?")
    assert rc.wants_agent("which of my projects are relevant here?")
    assert rc.wants_agent("what can you do")
    assert not rc.wants_agent("Projects:\n1. Route Planner\n2. Dock Scheduler")
    assert not rc.wants_agent("add https://github.com/janedoe/x")
    assert not rc.wants_agent("I worked at Acme from 2020 to 2022")


# -- protocol ---------------------------------------------------------------------------- #

def test_native_messages_alternate_and_carry_tool_blocks():
    msgs = anthropic_messages([
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "github?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "list_repos", "input": {}}]},
        {"role": "tool", "results": [{"id": "t1", "name": "list_repos", "content": "{}"}]}])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[3]["content"][0]["type"] == "tool_use"
    assert msgs[4]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}


def test_json_fallback_parses_calls_and_replies():
    tools = [rc.TOOLS["list_repos"].spec()]
    got = parse_json_reply('```json\n{"tool_calls":[{"name":"list_repos","input":{}},{"name":"rm","input":{}}]}\n```', tools)
    assert got["stop"] == "tool_use" and [c["name"] for c in got["tool_calls"]] == ["list_repos"]
    assert parse_json_reply('{"reply": "You have 2."}', tools)["text"] == "You have 2."
    assert parse_json_reply("plain words", tools)["text"] == "plain words"


def test_broker_uses_the_json_fallback(monkeypatch):
    from llm.broker_client import BrokerLLM
    b = BrokerLLM(broker_url="http://127.0.0.1:1")
    sent = {}
    monkeypatch.setattr(b, "_post", lambda payload: sent.update(payload) or '{"reply": "ok"}')
    out = b.agent_turn("SYS", [{"role": "user", "content": "hi"}], [rc.TOOLS["github_status"].spec()])
    assert out["text"] == "ok" and "tools" not in sent and "github_status" in sent["system"]


# -- public repositories ----------------------------------------------------------------- #

def test_list_repos_keeps_only_owned_live_repos(gh):
    got = github_repos.list_repos("https://github.com/janedoe")
    assert got["ok"] and [r["name"] for r in got["repos"]] == ["route-planner", "dock-scheduler"]
    assert got["dropped"] == {"forks": 1, "archived": 1, "empty": 1}
    assert set(got["repos"][0]) == {"name", "url", "description", "topics", "language", "stars", "pushed_at",
                                    "created_at", "private"}
    assert gh[0]["url"].endswith("/users/janedoe/repos") and gh[0]["params"]["sort"] == "pushed"
    assert gh[0]["params"]["per_page"] == 100 and gh[0]["token"] == ""
    github_repos.list_repos("janedoe")
    assert len(gh) == 1                                   # cached per login


def test_list_repos_rate_limit_and_offline(monkeypatch):
    monkeypatch.setattr(github_publish, "FETCH", lambda *a, **k: (403, {"message": "rate limit"}))
    got = github_repos.list_repos("janedoe")
    assert not got["ok"] and got["reason"] == "rate_limited" and "60" in got["error"]
    github_repos.clear_cache()
    monkeypatch.setattr(github_publish, "FETCH", lambda *a, **k: (0, None))
    got = github_repos.list_repos("janedoe")
    assert not got["ok"] and got["reason"] == "offline" and "internet" in got["error"]
    assert github_repos.list_repos("github.com/janedoe/some-repo")["ok"] is False   # not a profile


def test_the_token_never_reaches_a_result_card_or_model(template_source, fake_llm, workdir, gh, monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")
    s = _session(template_source, fake_llm, workdir)
    s.github_status = lambda: {"connected": True, "login": "janedoe"}
    s.github_token = lambda: TOKEN
    seen = []

    def script(system, messages, tools):
        seen.append(json.dumps(messages) + system)
        if messages[-1]["role"] == "tool":
            return {"text": "You have route-planner and dock-scheduler.", "tool_calls": []}
        return {"text": "", "tool_calls": [{"id": "1", "name": "github_status", "input": {}},
                                           {"id": "2", "name": "list_repos", "input": {}}]}
    fake_llm.agent_script = script
    st = s.submit("what's on my github?")
    assert gh[0]["token"] == TOKEN                        # used for the call itself
    blob = json.dumps(st) + "".join(seen) + json.dumps(s.history)
    assert TOKEN not in blob
    assert "route-planner" in st["messages"][0]


# -- the session ------------------------------------------------------------------------- #

def test_flag_off_keeps_todays_reply(template_source, fake_llm, workdir, gh):
    s = _session(template_source, fake_llm, workdir)
    st = s.submit("can't you check my github for more projects?")
    assert getattr(fake_llm, "agent_calls", 0) == 0 and not gh
    assert st["cards"] == []
    assert "card" not in s.history[-1]


def test_flag_on_answers_from_the_repos_with_a_card(template_source, fake_llm, workdir, gh, monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")
    s = _session(template_source, fake_llm, workdir)
    st = s.submit("what projects are on my resume and what's on my github?")
    reply = st["messages"][0]
    assert "route-planner" in reply and "Route Planner" in reply and "forked-lib" not in reply
    labels = [b["label"] for b in st["cards"][0]["buttons"]]
    assert "Connect GitHub" in labels and len(labels) <= rc.MAX_BUTTONS
    assert s.history[-1]["card"] == st["cards"][0]


def test_cards_survive_session_state(template_source, fake_llm, workdir, gh, monkeypatch, tmp_path):
    monkeypatch.setenv(rc.FLAG, "1")
    monkeypatch.setattr(app, "_cred", lambda k: TOKEN if k == "GITHUB_TOKEN" else "janedoe")
    s = _session(template_source, fake_llm, workdir)
    s.github_status = s.github_token = None               # the app wires its own hooks
    app._SESSION["s"] = s
    try:
        client = app.app.test_client()
        st = client.post("/api/session/answer", json={"text": "what's on my github?"}).get_json()
        assert st["cards"] and st["cards"][0]["type"] == "buttons"
        hist = client.get("/api/session/state").get_json()["history"]
        assert hist[-1]["role"] == "agent" and hist[-1]["card"] == st["cards"][0]
        assert TOKEN not in json.dumps(hist) and TOKEN not in json.dumps(st)
    finally:
        app._SESSION.pop("s", None)
