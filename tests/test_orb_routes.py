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
    orb.state["cands"] = [_cand("ready", "r:1", "Resume ready for Acme"),
                          _cand("matches", "m:1", "3 new jobs for Data")]
    r = orb.get("/api/orb/nudge").get_json()
    assert r["nudge"]["text"] == "Resume ready for Acme" and r["count"] == 2
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
    st = OrbStore(app.DB_PATH)
    try:
        assert "followup" in st.snoozed(app._orb_now())     # the kind is off for a week
    finally:
        st.close()
    r = orb.get("/api/orb/nudge?peek=1").get_json()
    assert r["count"] == 2                      # asked, the list stays true (2026-11-27)
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
    assert orb.post("/api/agent/ask", json={"text": "find data analyst jobs"}).get_json()["mode"] == "ai"
    st = OrbStore(app.DB_PATH)
    try:
        for _ in range(rules.CHAT_PER_HOUR):
            st.log("chat", "ai")
    finally:
        st.close()
    r = orb.post("/api/agent/ask", json={"text": "find data analyst jobs"}).get_json()
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


# ---------------------------------------------------------------- (2026-11-27, QA)
def _waiting_data():
    fu = [{"type": "followup", "key": f"followup:s{i}:2026-11-2{i}", "text": f"Follow up with Co{i} (applied Nov {10 + i})",
           "action": {"do": "draft", "sid": f"s{i}", "label": "Draft email"}} for i in range(5)]
    pr = [{"type": "practice", "key": "practice:b1", "text": "Practice for your Barclays interview",
           "action": {"do": "practice", "sid": "b1", "label": "Practice"}}]
    rd = [{"type": "ready", "key": f"ready:{i}", "text": f"Resume ready for R{i}", "fit": f, "role": "Analyst",
           "company": f"R{i}", "action": {"do": "apply", "rid": i, "label": "Apply"}} for i, f in ((7, 90), (8, 60))]
    return fu + pr + rd


def _plain(text):
    return "\u2014" not in text and "\u2013" not in text and " - " not in text


def test_the_opening_lists_three_concrete_things_one_action_each(orb):
    orb.state["cands"] = _waiting_data()
    r = orb.get("/api/orb/nudge?peek=1").get_json()
    assert [w["text"] for w in r["waiting"]] == ["Follow up with Co0 (applied Nov 10)",
                                                 "Practice for your Barclays interview", "Resume ready for R7"]
    assert [w["action"]["label"] for w in r["waiting"]] == ["Draft email", "Practice", "Apply"]
    assert r["count"] == 8 and r["more"] == 5 and r["badge"] == 3      # the badge: the few worth a look
    assert not any("things wait" in w["text"] for w in r["waiting"])


@pytest.mark.parametrize("q,first,label,more", [
    ("What's waiting on me?", "Follow up with Co0 (applied Nov 10)", "Draft email", 5),
    ("Which jobs should I apply to today?", "Analyst at R7, 90% fit", "Apply", 0),
    ("Practice for my next interview", "Practice for your Barclays interview", "Practice", 0),
])
def test_the_orbs_questions_are_answered_here_short_and_true(orb, monkeypatch, q, first, label, more):
    monkeypatch.setattr(app, "_agent_parse", lambda text: (_ for _ in ()).throw(AssertionError("no AI")))
    orb.state["cands"] = _waiting_data()
    r = orb.post("/api/agent/ask", json={"text": q, "orb": True}).get_json()
    assert r["kind"] == "agent" and r["mode"] == "local"
    assert r["orb_items"][0]["text"] == first and r["orb_items"][0]["action"]["label"] == label
    assert r["more"] == more and len(r["orb_items"]) <= 3
    assert _plain(r["text"]) and len(r["text"].split()) <= 12
    # never counted against the chat's hourly cap
    st = OrbStore(app.DB_PATH)
    try:
        assert st.count_events("chat", app._orb_now().replace(year=2000)) == 0
    finally:
        st.close()


def test_how_and_the_empty_answers(orb, monkeypatch):
    monkeypatch.setattr(app, "_agent_parse", lambda text: (_ for _ in ()).throw(AssertionError("no AI")))
    r = orb.post("/api/agent/ask", json={"text": "How do you work?", "orb": True}).get_json()
    assert r["text"] == rules.HOW_TEXT
    for q in ("What's waiting on me?", "Which jobs should I apply to today?", "Practice for my next interview"):
        r = orb.post("/api/agent/ask", json={"text": q, "orb": True}).get_json()
        assert r["text"] and _plain(r["text"]) and len(r.get("orb_items") or []) <= 1


def test_a_day_on_a_fake_clock(orb, monkeypatch):
    """Polls every 10 minutes for three days: at most 3 bubbles a day, 30 minutes apart, none in
    quiet hours (22 to 8) or while typing or during Autofill, one of each kind before a second."""
    from datetime import datetime, timedelta
    clock = {"now": datetime(2026, 11, 27, 6, 0)}
    monkeypatch.setattr(app, "_orb_now", lambda: clock["now"])
    monkeypatch.setattr(app, "_orb_quiet", lambda now: rules.in_quiet_hours(now.hour, 22, 8))
    orb.state["cands"] = _waiting_data()
    shown: list[tuple] = []
    for step in range(3 * 24 * 6):
        clock["now"] = datetime(2026, 11, 27, 6, 0) + timedelta(minutes=10 * step)
        q = "typing=1" if step % 7 == 3 else "autofill=1" if step % 11 == 5 else ""
        r = orb.get("/api/orb/nudge?" + q).get_json()
        if r["nudge"]:
            assert not q
            shown.append((clock["now"], r["nudge"]["type"]))
    days: dict = {}
    for t, kind in shown:
        assert 8 <= t.hour < 22
        days.setdefault(t.date(), []).append((t, kind))
    assert len(days) == 3
    for d, items in days.items():
        assert len(items) == 3
        assert all(b[0] - a[0] >= timedelta(minutes=30) for a, b in zip(items, items[1:]))
    assert [k for _t, k in days[min(days)]] == ["followup", "practice", "ready"]


def test_dismissed_twice_off_for_a_week_then_back(orb, monkeypatch):
    from datetime import datetime, timedelta
    clock = {"now": datetime(2026, 11, 27, 9, 0)}
    monkeypatch.setattr(app, "_orb_now", lambda: clock["now"])
    orb.state["cands"] = [c for c in _waiting_data() if c["type"] == "followup"]
    for i in range(2):
        clock["now"] += timedelta(minutes=31)
        n = orb.get("/api/orb/nudge").get_json()["nudge"]
        assert n["type"] == "followup"
        orb.post(f"/api/orb/nudge/{n['id']}", json={"status": "dismissed"})
    for days in (1, 3, 6):
        clock["now"] = datetime(2026, 11, 27, 12, 0) + timedelta(days=days)
        assert orb.get("/api/orb/nudge").get_json()["nudge"] is None
    clock["now"] = datetime(2026, 11, 27, 12, 0) + timedelta(days=7, hours=1)
    assert orb.get("/api/orb/nudge").get_json()["nudge"]["type"] == "followup"
    # the chat still lists them when asked: the list stays true
    assert orb.get("/api/orb/nudge?peek=1").get_json()["count"] == 5


class _Recs:
    def __init__(self, rows): self.rows = rows
    def list(self, limit=None): return list(self.rows)
    def close(self): pass


class _Board:
    def __init__(self, states, due=(), interviews=()):
        self.states, self.due, self.interviews = states, list(due), list(interviews)
    def due_followups(self, today): return list(self.due)
    def board_jobs(self, stage=None): return list(self.interviews) if stage == "interviewing" else []
    def board_states(self, ids): return {i: self.states[i] for i in ids if i in self.states}
    def close(self): pass


def test_ready_means_still_to_apply_and_names_are_concrete(tmp_path, monkeypatch):
    """(2026-11-27) A resume counts as ready only while its job is To apply on the Tracker: a
    closed or archived job's resume is never "ready to apply"."""
    import json as _json
    monkeypatch.setattr(app, "_brain", lambda: None)
    app._ORB_MATCHES.clear()
    monkeypatch.setattr(app, "_orb_match_candidates", lambda now: [])
    rec = lambda i, co, fit, sid: {"id": i, "company": co, "role": "Business Analyst", "created_at": "2026-11-20 10:00:00",
                                   "data": _json.dumps({"status": "approved", "fit": fit, "source_job": {"source_id": sid}})}
    rows = [rec(1, "Acme Inc", 70, "s1"), rec(2, "Closed Co LLC", 99, "s2"), rec(3, "Gone Corp", 95, "s3"),
            rec(4, "Best Fit Corporation", 88, "s4")]
    states = {"s1": {"stage": "approved", "live": "open"}, "s2": {"stage": "archived", "live": None},
              "s3": {"stage": "approved", "live": "closed"}, "s4": {"stage": "approved", "live": None}}
    due = [{"source_id": "f1", "company": "Cambium Learning Group Inc", "stage": "applied",
            "applied_at": "2026-11-17T11:21:26-06:00", "follow_up_at": "2026-11-23"}]
    iv = [{"source_id": "b1", "company": "Barclays Services Corp", "stage": "interviewing"}]
    monkeypatch.setattr(app, "_records", lambda: _Recs(rows))
    monkeypatch.setattr(app, "_watchlist", lambda: _Board(states, due, iv))
    out = app._orb_candidates(app._orb_now())
    texts = [c["text"] for c in rules.rank(out)]
    assert texts == ["Follow up with Cambium Learning Group (applied Nov 17)", "Practice for your Barclays Services interview",
                     "Resume ready for Best Fit", "Resume ready for Acme"]
    assert all(_plain(t) for t in texts)


def test_celebrate_route_sizes_and_the_off_switch(orb, monkeypatch):
    import sourcing.board as board
    total = {"n": 1}
    monkeypatch.setattr(board, "funnel_metrics", lambda rows, today, goal: {"applied_total": total["n"]})
    assert orb.post("/api/orb/celebrate", json={"event": "submitted"}).get_json()["size"] == "big"
    total["n"] = 4
    assert orb.post("/api/orb/celebrate", json={"event": "submitted"}).get_json()["size"] == "light"
    assert orb.post("/api/orb/celebrate", json={"event": "submitted", "reduced_motion": True}).get_json()["size"] == ""
    assert orb.post("/api/orb/celebrate", json={"event": "offer"}).get_json()["size"] == "big"
    cfg = orb.post("/api/orb/settings", json={"celebrations": False}).get_json()
    assert cfg["celebrations"] is False
    assert orb.post("/api/orb/celebrate", json={"event": "offer"}).get_json()["size"] == ""
    st = OrbStore(app.DB_PATH)
    try:
        assert st.count_events("celebrate", app._orb_now().replace(year=2000)) == 3
    finally:
        st.close()


def test_the_confetti_layer_never_blocks_a_click_and_fires_once_per_submit():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    view = (root / "shell-electron" / "confetti_view.js").read_text()
    assert "setIgnoreMouseEvents(true)" in view and "focusable: false" in view and "transparent: true" in view
    assert "showInactive()" in view and "parent: win" in view
    js = (root / "ui" / "static" / "app.js").read_text()
    start = js.index("window.tailorShell.onApplied((info) => {")
    assert js[start:start + 1500].count('orbConfetti("submitted")') == 1
    assert "Date.now() - (ORB.confettiAt || 0) < 5000" in js
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    page = (root / "ui" / "static" / "confetti.js").read_text()
    assert "requestAnimationFrame" in page and "#7957FC" in page and "import " not in page


def test_the_confetti_page_is_served(orb):
    r = orb.get("/confetti")
    assert r.status_code == 200 and b'id="c"' in r.data and b"<script>" not in r.data


def test_without_the_orb_the_assistant_keeps_its_own_status_answer(orb, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_agent_parse", lambda text: calls.append(text) or {"intent": "status"})
    monkeypatch.setattr(app, "_agent_missing", lambda intent: None)
    r = orb.post("/api/agent/ask", json={"text": "What's waiting on me?"}).get_json()
    assert r["mode"] != "local" and calls == ["What's waiting on me?"]
