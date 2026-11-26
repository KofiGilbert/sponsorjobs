"""The Orb's engine routes (ui/app.py, "the Orb"): the three off switches, the nudge route's
pacing on the person's own data, mute handled without the AI, and the chat dropping to the
rules past its cap. Offline: no broker, no model."""
from __future__ import annotations

import pytest

import ui.app as app
from orb import rules
from orb.store import OrbStore


@pytest.fixture()
def orb(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "_NOTIFY_STATE_FILE", tmp_path / "notify_state.json")
    monkeypatch.delenv("SJ_ORB", raising=False)
    monkeypatch.delenv("SJ_ORB_NUDGES", raising=False)
    monkeypatch.delenv("TAILOR_BROKER_URL", raising=False)
    app._ORB_REMOTE.clear()
    app._ORB_MATCHES.clear()
    monkeypatch.setattr(app, "_orb_quiet", lambda now: False)
    state = {"cands": []}
    monkeypatch.setattr(app, "_orb_candidates", lambda now: list(state["cands"]))
    c = app.app.test_client()
    c.state = state
    return c


def _cand(kind, key, text):
    return {"type": kind, "key": key, "text": text, "action": {"nav": "tracker", "label": "Open Tracker"}}


def test_on_by_default_and_each_switch_turns_it_off(orb, monkeypatch):
    cfg = orb.get("/api/orb/config").get_json()
    assert cfg["enabled"] and cfg["nudges"] and not cfg["muted"]
    monkeypatch.setenv("SJ_ORB", "0")
    cfg = orb.get("/api/orb/config").get_json()
    assert not cfg["enabled"] and cfg["off_reason"] == "env" and cfg["env_off"]
    monkeypatch.delenv("SJ_ORB")
    cfg = orb.post("/api/orb/settings", json={"enabled": False}).get_json()
    assert not cfg["enabled"] and cfg["off_reason"] == "setting"
    orb.post("/api/orb/settings", json={"enabled": True})
    monkeypatch.setenv("SJ_ORB_NUDGES", "0")
    cfg = orb.get("/api/orb/config").get_json()
    assert cfg["enabled"] and not cfg["nudges"]


def test_the_broker_remote_switch(orb, monkeypatch):
    import io
    import json as _json
    monkeypatch.setenv("TAILOR_BROKER_URL", "https://broker.invalid")

    class R(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    body = {"features": {"orb": False}}
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2: R(_json.dumps(body).encode()))
    cfg = orb.get("/api/orb/config").get_json()
    assert not cfg["enabled"] and cfg["off_reason"] == "remote"
    # an unreachable broker never hides it on its own
    app._ORB_REMOTE.clear()
    def boom(*a, **k): raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert orb.get("/api/orb/config").get_json()["enabled"]


def test_the_orb_page_is_served_with_the_app_policy(orb):
    r = orb.get("/orb")
    assert r.status_code == 200 and b'id="orb"' in r.data
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert b"<script>" not in r.data          # no inline script


def test_one_nudge_then_the_30_minute_gap_and_the_waiting_list(orb):
    orb.state["cands"] = [_cand("ready", "r:1", "Your resume for Acme is ready to apply"),
                          _cand("matches", "m:1", "3 new jobs for Data")]
    r = orb.get("/api/orb/nudge").get_json()
    assert r["nudge"]["text"].startswith("Your resume for Acme") and r["count"] == 2
    again = orb.get("/api/orb/nudge").get_json()
    assert again["nudge"] is None and again["why"] == "gap"
    assert {w["type"] for w in again["waiting"]} == {"ready", "matches"}


@pytest.mark.parametrize("q,why", [("typing=1", "typing"), ("autofill=1", "autofill")])
def test_never_while_typing_or_during_autofill(orb, q, why):
    orb.state["cands"] = [_cand("ready", "r:1", "x")]
    r = orb.get("/api/orb/nudge?" + q).get_json()
    assert r["nudge"] is None and r["why"] == why and r["count"] == 1


def test_peek_never_counts_as_a_bubble(orb):
    orb.state["cands"] = [_cand("ready", "r:1", "x")]
    assert orb.get("/api/orb/nudge?peek=1").get_json()["nudge"] is None
    assert orb.get("/api/orb/nudge").get_json()["nudge"] is not None


def test_dismiss_twice_and_acted(orb):
    orb.state["cands"] = [_cand("followup", "f:1", "a"), _cand("followup", "f:2", "b")]
    first = orb.get("/api/orb/nudge").get_json()["nudge"]
    assert orb.post(f"/api/orb/nudge/{first['id']}", json={"status": "dismissed"}).get_json()["ok"]
    st = OrbStore(app.DB_PATH)
    try:
        other = next(n for n in st.nudges() if n["id"] != first["id"])
    finally:
        st.close()
    orb.post(f"/api/orb/nudge/{other['id']}", json={"status": "dismissed"})
    r = orb.get("/api/orb/nudge?peek=1").get_json()
    assert r["count"] == 0                      # the kind is off for a week
    assert orb.post("/api/orb/nudge/999", json={"status": "acted"}).status_code == 404
    assert orb.post(f"/api/orb/nudge/{first['id']}", json={"status": "nope"}).status_code == 400


def test_nothing_when_off(orb):
    orb.state["cands"] = [_cand("ready", "r:1", "x")]
    orb.post("/api/orb/settings", json={"enabled": False})
    r = orb.get("/api/orb/nudge").get_json()
    assert r["nudge"] is None and r["why"] == "off"


def test_mute_in_the_chat_needs_no_ai(orb, monkeypatch):
    monkeypatch.setattr(app, "_agent_parse", lambda text: (_ for _ in ()).throw(AssertionError("no AI")))
    r = orb.post("/api/agent/ask", json={"text": "mute"}).get_json()
    assert r["orb"] == {"muted": True} and r["text"] == "Sounds off."
    assert orb.get("/api/orb/config").get_json()["muted"]
    orb.post("/api/agent/ask", json={"text": "unmute"})
    assert not orb.get("/api/orb/config").get_json()["muted"]


def test_past_the_hourly_cap_the_chat_answers_from_the_rules(orb, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_agent_parse", lambda text: calls.append(text) or {"intent": "other"})
    monkeypatch.setattr(app, "_agent_answer", lambda intent: {"text": "ok", "buttons": []})
    monkeypatch.setattr(app, "_agent_missing", lambda intent: None)
    assert orb.post("/api/agent/ask", json={"text": "what is waiting on me"}).get_json()["mode"] == "ai"
    st = OrbStore(app.DB_PATH)
    try:
        for _ in range(rules.CHAT_PER_HOUR):
            st.log("chat", "ai")
    finally:
        st.close()
    r = orb.post("/api/agent/ask", json={"text": "what is waiting on me"}).get_json()
    assert r["mode"] == "rules" and len(calls) == 1


def test_close_drops_the_turns_without_a_model(orb, monkeypatch):
    def no_model():
        raise RuntimeError("offline")
    monkeypatch.setattr(app, "_make_llm", no_model)
    st = OrbStore(app.DB_PATH)
    try:
        st.add_turn("user", "what is waiting", app._orb_now())
    finally:
        st.close()
    assert orb.post("/api/orb/chat/close").get_json() == {"ok": True, "topics": False}
    st = OrbStore(app.DB_PATH)
    try:
        assert st.turns() == [] and st.memory() == []
    finally:
        st.close()


def test_close_keeps_one_topics_line_from_the_small_model(orb, monkeypatch):
    class M:
        def narrate(self, system, user, max_tokens=400):
            assert "TOPICS only" in system and "what is waiting" in user
            return "follow-ups due, interview practice"
    monkeypatch.setattr(app, "_make_llm", lambda: M())
    st = OrbStore(app.DB_PATH)
    try:
        st.add_turn("user", "what is waiting", app._orb_now())
    finally:
        st.close()
    assert orb.post("/api/orb/chat/close").get_json()["topics"] is True
    st = OrbStore(app.DB_PATH)
    try:
        assert st.memory()[0]["topics"] == "follow-ups due, interview practice"
    finally:
        st.close()


def test_candidates_come_from_real_records(tmp_path, monkeypatch):
    """With nothing saved, no nudge at all: nothing is made up to fill a quiet day."""
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "_brain", lambda: None)
    monkeypatch.setattr(app, "_watchlist", lambda: _EmptyBoard())
    app._ORB_MATCHES.clear()
    assert app._orb_candidates(app._orb_now()) == []


class _EmptyBoard:
    def due_followups(self, today): return []
    def board_jobs(self, stage=None): return []
    def close(self): pass
