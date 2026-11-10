"""Check first, connect inline, carry on (Kofi, test build 13, 2026-11-09).

A Home request that needs an integration (Telegram, GitHub, the application address, the
browser extension) checks it FIRST. Not connected: one line and a card with one Connect button,
never menu steps, and nothing promised that needs it. Once it is connected, the agent carries on
with that same request by itself (/api/agent/connected), with no new turn, and only once.
"""
from __future__ import annotations

import time
from pathlib import Path

import ui.app as app
from agent import home
from test_home_agent import FakeChannel, _ask, ag  # noqa: F401  (ag is the shared fixture)

ROOT = Path(__file__).resolve().parents[1]
REPLIES: list[str] = []


def _connected():
    d = app.app.test_client().post("/api/agent/connected", json={}).get_json()
    REPLIES.append(str(d.get("text") or ""))
    return d


def _say(text):
    d, code = _ask(text)
    assert code == 200
    REPLIES.append(str(d.get("text") or ""))
    return d


# --- Telegram ------------------------------------------------------------------------------

def test_telegram_not_connected_is_one_line_and_a_connect_card(ag):
    d = _say(home.EXAMPLES[1])
    assert d["text"] == "Telegram isn't connected yet."
    assert d["connect"] == {"integration": "telegram", "label": "Connect Telegram"}
    assert d["buttons"] == [] and "message you" not in d["text"].lower()
    assert app._AGENT["plan"] is None                       # nothing to Go ahead on yet
    assert app._AGENT["pending"]["need"] == "telegram"
    # Still not connected: it keeps waiting, nothing runs.
    w = _connected()
    assert w["resumed"] is False and w["waiting"] == "telegram" and ag["runs"] == []


def test_after_linking_the_request_carries_on_once_without_a_new_turn(ag):
    _say(home.EXAMPLES[1])
    ch = FakeChannel()
    ag["channel"] = ch                                      # the link went through
    d = _connected()
    assert d["resumed"] is True and d["integration"] == "telegram"
    assert d["text"].startswith("Connected. Tailoring ")
    assert d["text"].endswith("business analyst roles now; I'll message you on Telegram when "
                              "they're ready.")
    assert d["next"] == "go" and "connect" not in d
    # The page then runs the plan by itself (agentReply sees next: "go").
    c = app.app.test_client()
    g = c.post("/api/agent/go", json={}).get_json()
    assert g["ok"] and g["telegram"] is True and len(ch.sent) == 1
    assert len(ag["runs"]) == 1
    # Only once: a second check (focus, a second tab) finds nothing waiting.
    again = _connected()
    assert again["resumed"] is False
    assert c.post("/api/agent/go", json={}).status_code == 400     # no plan left to run twice
    assert len(ag["runs"]) == 1


def test_connected_at_once_gets_the_plan_and_no_card(ag):
    ag["channel"] = FakeChannel()
    d = _say(home.EXAMPLES[1])
    assert "connect" not in d and app._AGENT["pending"] is None
    assert "4. Message you on Telegram when they are done." in d["text"]
    assert [b["id"] for b in d["buttons"]] == ["go", "change"]


def test_a_plan_that_cannot_run_says_why_after_connecting(ag):
    _say(home.EXAMPLES[1])
    ag["channel"], ag["left"] = FakeChannel(), 0
    d = _connected()
    assert d["resumed"] is True and "next" not in d
    assert d["text"].startswith("Connected. No packages left")
    assert "I'll message you" not in d["text"] and ag["runs"] == []


def test_a_new_request_replaces_the_one_waiting(ag):
    _say(home.EXAMPLES[1])
    _say(home.EXAMPLES[2])                                 # What is waiting on me?
    ag["channel"] = FakeChannel()
    assert _connected()["resumed"] is False and ag["runs"] == []


# --- GitHub on Home ------------------------------------------------------------------------

def _github(monkeypatch, state):
    monkeypatch.setattr(app, "_github_connection",
                        lambda: {"connected": state["on"], "login": "kofi" if state["on"] else ""})
    monkeypatch.setattr(app, "_github_token", lambda: "t" if state["on"] else "")
    import ui.github_repos as gr
    monkeypatch.setattr(gr, "list_repos", lambda login, token="": {
        "ok": True, "login": login, "repos": [{"name": "churn-model"}, {"name": "sql-dashboards"}]})


def test_github_on_home_checks_first_then_carries_on(ag, monkeypatch):
    state = {"on": False}
    _github(monkeypatch, state)
    d = _say("check my github for projects")
    assert d["intent"]["intent"] == "github"
    assert d["text"] == "GitHub isn't connected yet."
    assert d["connect"] == {"integration": "github", "label": "Connect GitHub"}
    assert _connected()["resumed"] is False
    state["on"] = True
    r = _connected()
    assert r["resumed"] is True
    assert r["text"] == ("Connected as kofi. I can see 2 repos, newest first: churn-model and "
                         "sql-dashboards.")
    assert [b["id"] for b in r["buttons"]] == ["open_projects"]
    assert _connected()["resumed"] is False                  # once


def test_github_connected_on_home_has_no_card(ag, monkeypatch):
    _github(monkeypatch, {"on": True})
    d = _say("what's on my GitHub?")
    assert "connect" not in d
    assert d["text"].startswith("Your GitHub is connected as kofi. I can see 2 repos")


# --- the application address and the extension ---------------------------------------------

def test_email_address_checks_first_then_carries_on(ag, monkeypatch):
    state = {"alias": ""}
    monkeypatch.setattr(app, "_mail_alias", lambda: state["alias"])
    monkeypatch.setattr(app, "_current_mailbox", lambda: None)
    d = _say("get me an email address for applications")
    assert d["text"] == "You don't have an address for applications yet."
    assert d["connect"] == {"integration": "email", "label": "Get an address"}
    state["alias"] = "kwame.a7@mail.sponsorjobs.ai"
    r = _connected()
    assert r["resumed"] is True
    assert r["text"].startswith("Done. Applications use kwame.a7@mail.sponsorjobs.ai.")


def test_extension_checks_first_then_carries_on(ag, monkeypatch):
    monkeypatch.setattr(app, "_EXT_SEEN", {})
    d = _say("help me apply on LinkedIn")
    assert d["intent"]["intent"] == "extension"
    assert d["connect"] == {"integration": "extension", "label": "How to load"}
    assert d["text"] == "The browser extension isn't loaded yet."
    app._EXT_SEEN["at"] = time.time()                        # the extension called in
    r = _connected()
    assert r["resumed"] is True and r["text"].startswith("The extension is connected.")


# --- the words and the page ----------------------------------------------------------------

def test_no_reply_gives_menu_steps():
    texts = REPLIES + [home.connect_reply(n)["text"] for n in home.INTEGRATIONS] + [
        home.plan_text(n=2, titles=["a"], min_fit=60, auto=0, auto_companies="", auto_reason="off",
                       telegram=True, telegram_connected=False, packages_left=None)]
    for t in texts:
        assert "Settings, then" not in t and "Settings >" not in t, t
        assert "Connect it in" not in t, t
        assert "—" not in t and "–" not in t and " - " not in t, t


def test_the_page_renders_the_card_and_carries_on():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for needle in ("function agentConnectCard", "/api/agent/connected", 'r.next === "go"',
                   "/api/notify/telegram/link", "ghAppStart()", "/api/inbox/alias",
                   "/api/extension/status", 'addEventListener("tailor:github", () => agentConnected())'):
        assert needle in js, needle
    start = js.index("function agentConnectCard")
    block = js[start:js.index("// One turn: the person's words", start)]
    assert "Settings, then" not in block
