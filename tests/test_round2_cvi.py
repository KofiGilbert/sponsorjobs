"""Round 2: ONE live Tavus CVI interview (ui/app.py /api/interviews/cvi/*, /api/interview/round2/*).

Offline: FakeLLM, no network. The managed broker (which holds the company Tavus key) is replaced
by monkeypatching _broker_post/_broker_get. We verify the gate (must pass Round 1 unless skipped),
that live interviews are PAID ONLY (a pass or a pack through the broker; no own Tavus key path,
2026-10-14), the briefing sent to the interviewer, the end -> transcript -> scored report path,
and that the retired three-round local simulation is gone.
"""

from __future__ import annotations

import importlib

import pytest

_GOOD = "At Acme I led a team of six and cut reporting time in half over eight weeks. " * 3


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    monkeypatch.delenv("AVATAR_API_KEY", raising=False)
    import ui.app as A
    importlib.reload(A)
    monkeypatch.setattr(A, "_TRANSCRIPT_WAIT_S", 0)
    # No broker in tests unless a test installs one: unreachable, never a real socket.
    monkeypatch.setattr(A, "_broker_reachable", lambda: True)
    monkeypatch.setattr(A, "_broker_get", lambda path: (0, None))
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (0, None))
    return A, A.app.test_client()


def _prep(c):
    return c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme",
                                      "jd": "SQL and Tableau dashboards for ops reporting."}).get_json()["prep"]


def _passed_screen(c, prep):
    s = c.post("/api/screens", json={"prep_id": prep["id"]}).get_json()
    for q in s["questions"]:
        c.post(f"/api/screens/{s['id']}/answers/{q['i']}/transcript", json={"transcript": _GOOD})
    assert c.post(f"/api/screens/{s['id']}/score").get_json()["screen"]["passed"] is True
    return s["id"]


_TRANSCRIPT_EVENTS = [
    {"event_type": "system.pal_joined"},
    {"event_type": "application.transcription_ready", "properties": {"transcript": [
        {"role": "assistant", "content": "Hi, I'm the hiring manager. Tell me about yourself.",
         "seconds_from_start": 0, "duration": 3},
        {"role": "user", "content": _GOOD, "seconds_from_start": 4, "duration": 40},
        {"role": "assistant", "content": "Tell me about a time you delivered a result relevant to Data Analyst.",
         "seconds_from_start": 50, "duration": 4},
        {"role": "user", "content": _GOOD, "seconds_from_start": 55, "duration": 45},
        {"role": "assistant", "content": "Thanks, that's all. You'll hear back soon.", "seconds_from_start": 110, "duration": 3},
        {"role": "user", "content": "Thank you, bye.", "seconds_from_start": 114, "duration": 2},
    ]}},
]


# The same dialogue as the broker hands it back (interviewer / candidate roles).
_BROKER_TURNS = [{**t, "role": "interviewer" if t["role"] == "assistant" else "candidate"}
                 for t in _TRANSCRIPT_EVENTS[1]["properties"]["transcript"]]


def _on_a_pass(A, monkeypatch, transcript=None, seconds=5400):
    """A pass with interviews left, and a broker that mints a room and hands back `transcript`
    (the scripted dialogue by default) on /avatar/session/end. Returns the list of broker calls."""
    calls = []
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": seconds}))

    def post(path, body):
        calls.append((path, body))
        if path == "/avatar/session/start":
            return 200, {"session_url": "https://tavus.daily.co/c1?t=tok", "provider_session_id": "c1"}
        if path == "/avatar/session/end":
            return 200, {"transcript": _BROKER_TURNS if transcript is None else transcript}
        if path == "/avatar/heartbeat":
            return 200, {"remaining": 5370, "stop": False}
        return 404, None
    monkeypatch.setattr(A, "_broker_post", post)
    return calls


# ---- round2 status: paid only (a pass or a pack), never an own key ----------------------------------------------------------
def test_round2_status_is_paid_only_and_the_own_key_routes_are_gone(client, monkeypatch):
    A, c = client
    st = c.get("/api/interview/round2/status").get_json()
    assert st == {"available": False, "reason": "unreachable", "source": None, "minutes_left": None,
                  "can_buy": False, "packs": [], "tier": None, "pass_until": None, "offer": None}
    # The Settings key box is gone: nothing can save or read a person's own Tavus key.
    assert c.post("/api/avatar/settings", json={"key": "tvs-own-key"}).status_code in (404, 405)
    assert c.get("/api/avatar/settings").status_code == 404
    for name in ("_TAVUS_HTTP", "_tavus_client", "_ensure_pal", "_start_own_key_conversation"):
        assert not hasattr(A, name)
    # An own key left on disk by an older version is ignored: no pass -> not available.
    monkeypatch.setenv("AVATAR_API_KEY", "tvs-left-over")
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "free", "avatar_seconds_left": 0}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["available"] is False and st["reason"] == "no_pass" and st["offer"] == "passes"
    assert "key_set" not in st and "tvs-left-over" not in str(st)


def test_round2_status_from_the_plan_and_out_of_minutes(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 1810}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st == {"available": True, "reason": "ok", "source": "plan", "minutes_left": 30,
                  "can_buy": False, "packs": [], "tier": "pass30", "pass_until": None, "offer": "packs"}
    # 10 minutes left is not a full 15-minute interview: never start one that cannot finish
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 610}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["available"] is False and st["reason"] == "no_minutes" and st["minutes_left"] == 10
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 0}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["available"] is False and st["reason"] == "no_minutes" and st["minutes_left"] == 0
    # a free account with no minutes has no pass, not 'used up': offer the passes
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "free", "avatar_seconds_left": 0}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["reason"] == "no_pass" and st["offer"] == "passes" and st["can_buy"] is False


def test_free_account_is_never_offered_packs(client, monkeypatch):
    """Extra interviews are sold only during a pass: a free account sees the passes instead,
    and the packs list is not even fetched."""
    A, c = client
    calls = []
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"tier": "free", "avatar_seconds_left": 0}, calls=calls))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["reason"] == "no_pass" and st["offer"] == "passes"
    assert st["packs"] == [] and st["can_buy"] is False and "/billing/packs" not in calls


def test_buy_without_a_pass_maps_to_402_with_the_passes_offer(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (402, {"error": "x", "reason": "pass_required"}))
    r = c.post("/api/interview/round2/buy", json={"pack": "pack_1"})
    assert r.status_code == 402 and r.get_json()["offer"] == "passes"




# ---- cvi/start: 403 gate, 402 unavailable, 200 through the broker ----------------------------
def test_cvi_start_is_gated_on_passing_round_one(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    _on_a_pass(A, monkeypatch)
    unpassed = c.post("/api/screens", json={"prep_id": prep["id"]}).get_json()["id"]
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "screen_id": unpassed})
    assert r.status_code == 403 and r.get_json()["error"] == "screen_not_passed"
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"]})            # no screen, no skip
    assert r.status_code == 403 and r.get_json()["error"] == "screen_not_passed"
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "screen_id": "ghost"})
    assert r.status_code == 403
    assert c.post("/api/interviews/cvi/start", json={"prep_id": "nope", "skip_screen": True}).status_code == 404


def test_cvi_start_402_when_round2_unavailable(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "free", "avatar_seconds_left": 0}))
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True})
    assert r.status_code == 402
    assert r.get_json()["error"] == "round2_unavailable" and r.get_json()["reason"] == "no_pass"
    assert r.get_json()["offer"] == "passes"








def test_cvi_start_via_the_plan_uses_the_broker(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 1800}))
    sent = {}

    def fake_post(path, body):
        sent["path"], sent["body"] = path, body
        return 200, {"session_url": "https://tavus/join?t=x", "provider_session_id": "c7", "remaining": 1800}
    monkeypatch.setattr(A, "_broker_post", fake_post)
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True})
    assert r.status_code == 200 and r.get_json()["source"] == "plan"
    assert r.get_json()["join_url"] == "https://tavus/join?t=x" and r.get_json()["conversation_id"] == "c7"
    assert sent["path"] == "/avatar/session/start" and "Data Analyst" in sent["body"]["context"]["prompt"]
    prompt = sent["body"]["context"]["prompt"]
    assert "Acme" in prompt and "Tableau" in prompt                             # company, JD
    assert "QUESTIONS TO COVER" in prompt and "4 to 6 questions" in prompt      # structure + plan
    # broker quota -> 402 round2_unavailable / no_minutes; unreachable -> 503
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (402, {"error": "out"}))
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True})
    assert r.status_code == 402 and r.get_json()["reason"] == "no_minutes" and r.get_json()["offer"] == "packs"
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (0, None))
    assert c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).status_code == 503


# ---- cvi/end: the broker ends it and hands the transcript back; score it, store the report -------------
def test_cvi_end_scores_the_tavus_transcript_into_a_report(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    calls = _on_a_pass(A, monkeypatch)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    r = c.post("/api/interviews/cvi/end", json={"interview_id": iid})
    assert r.status_code == 200, r.get_json()
    rep = r.get_json()["report"]
    assert calls[-1] == ("/avatar/session/end", {"conversation_id": "c1"})
    assert isinstance(rep["score"], int) and rep["passed"] is True and rep["why"] and rep["improvements"]
    assert rep["disclaimer"] == A.SIMULATION_DISCLAIMER and rep["is_practice"] is True
    assert rep["duration_s"] == 116 and rep["round"] == 2
    assert [t["role"] for t in rep["transcript"]][:2] == ["interviewer", "candidate"]
    # two substantive answers scored; the 'thank you, bye' is not
    assert len(rep["answers"]) == 2 and all(a["score"] >= 0 for a in rep["answers"])
    # the second question matched a planned question, so the report has a competency breakdown
    assert any(cb["name"] == "Ownership" for cb in rep["competencies"])
    stored = c.get(f"/api/interviews/{iid}").get_json()
    assert stored["report"]["score"] == rep["score"] and stored["interview"]["ended"] > 0
    # the live report counts toward readiness
    d = c.get("/api/interview/readiness").get_json()
    assert d["live"] == 1 and d["attempts"] == 1


def test_cvi_end_falls_back_to_a_supplied_transcript_and_needs_one(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    calls = _on_a_pass(A, monkeypatch, transcript=[])  # no transcript comes back
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    r = c.post("/api/interviews/cvi/end", json={"interview_id": iid})
    assert r.status_code == 400 and r.get_json()["error"] == "no_transcript"
    assert sum(1 for p, _ in calls if p == "/avatar/session/end") == 3       # retried, no sleeping
    r = c.post("/api/interviews/cvi/end", json={"interview_id": iid, "duration_s": 300,
               "transcript": f"Interviewer: Why Acme?\nMe: {_GOOD}\nInterviewer: Anything else?\nMe: {_GOOD}"})
    assert r.status_code == 200
    rep = r.get_json()["report"]
    assert rep["duration_s"] == 300 and len(rep["answers"]) == 2 and rep["passed"] is True
    assert c.post("/api/interviews/cvi/end", json={"interview_id": "nope"}).status_code == 404


def test_cvi_end_on_the_plan_path_scores_a_client_transcript_list(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass90", "avatar_seconds_left": 1200}))
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (200, {"session_url": "u", "provider_session_id": "c7"}))
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    r = c.post("/api/interviews/cvi/end", json={"interview_id": iid, "transcript": [
        {"role": "assistant", "content": "Tell me about yourself."}, {"role": "user", "content": "Short."}]})
    rep = r.get_json()["report"]
    assert r.status_code == 200 and rep["score"] == 0 and rep["passed"] is False   # too little said
    assert rep["answers"] == [] and "too little" in rep["why"].lower()


def test_cvi_end_on_the_plan_path_gets_the_transcript_from_the_broker(client, monkeypatch):
    """A paid interview runs on the company key, which only the broker holds: the broker ends the
    conversation and hands the transcript back, so a pass holder gets a scored report without
    pasting anything (2026-10-09)."""
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 2700}))
    calls = []

    def post(path, body):
        calls.append((path, body))
        if path == "/avatar/session/start":
            return 200, {"session_url": "https://tavus/join?t=x", "provider_session_id": "c9"}
        if path == "/avatar/session/end":
            # the transcript lags the end: empty on the first ask, there on the second
            n = sum(1 for p, _ in calls if p == "/avatar/session/end")
            return 200, {"transcript": [] if n == 1 else [
                {"role": "interviewer", "content": "Why do you want this role?"},
                {"role": "candidate", "content": _GOOD},
                {"role": "interviewer", "content": "Tell me about a time you took ownership."},
                {"role": "candidate", "content": _GOOD}]}
        return 404, None
    monkeypatch.setattr(A, "_broker_post", post)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    start_body = calls[0][1]["context"]
    assert A._PAL_SYSTEM_PROMPT[:40] in start_body["prompt"] and "Data Analyst" in start_body["prompt"]
    assert start_body["greeting"] == A._PAL_GREETING
    r = c.post("/api/interviews/cvi/end", json={"interview_id": iid})
    assert r.status_code == 200, r.get_json()
    assert [p for p, _ in calls].count("/avatar/session/end") == 2
    assert calls[-1][1] == {"conversation_id": "c9"}
    assert len(r.get_json()["report"]["answers"]) == 2


def test_dialogue_pairing_and_competency_tagging_are_pure():
    import ui.app as A
    plan = [{"q": "Tell me about a time you delivered a result relevant to Data Analyst.", "competency": "Ownership"}]
    turns = [{"role": "interviewer", "content": "Welcome."},
             {"role": "interviewer", "content": "Tell me about a time you delivered a result as a data analyst."},
             {"role": "candidate", "content": "I led " + "a big project with real numbers " * 4},
             {"role": "interviewer", "content": "Great, bye."}, {"role": "candidate", "content": "Bye."}]
    items = A._pair_dialogue(turns, plan)
    assert len(items) == 1 and items[0]["q"].startswith("Welcome. Tell me") and items[0]["competency"] == "Ownership"
    assert A._closest_competency("What is your favourite colour?", plan) == "General"
    assert A._pair_dialogue([], plan) == []


def test_heartbeat_is_metered_by_the_broker_and_fails_safe(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (200, {"remaining": 120, "stop": False}))
    assert c.post("/api/interviews/cvi/heartbeat", json={"seconds": 30}).get_json() == \
        {"remaining": 120, "stop": False, "source": "plan"}
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (0, None))
    r = c.post("/api/interviews/cvi/heartbeat", json={"seconds": 30})
    assert r.status_code == 200 and r.get_json()["stop"] is True       # never trapped in a paid session
    # an own key left on disk never turns the meter off
    monkeypatch.setenv("AVATAR_API_KEY", "tvs-left-over")
    assert c.post("/api/interviews/cvi/heartbeat", json={"seconds": 30}).get_json()["stop"] is True


def test_delete_removes_the_live_record(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    _on_a_pass(A, monkeypatch)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    assert c.post(f"/api/interviews/{iid}/delete").get_json()["ok"] is True
    assert c.get(f"/api/interviews/{iid}").status_code == 404


# ---- the retired three-round local simulation is gone -----------------------------------------------
def test_removed_local_rounds_routes_are_gone(client):
    A, c = client
    prep = _prep(c)
    assert c.post("/api/interviews", json={"prep_id": prep["id"], "skip_screen": True}).status_code == 404
    assert c.get("/api/interviews").status_code == 404
    for path in ("/api/interviews/x/rounds/0/ask/0", "/api/interviews/x/rounds/0/answers/0",
                 "/api/interviews/x/rounds/0/score"):
        assert c.post(path).status_code == 404
    assert c.get("/api/interviews/x/readiness").status_code == 404
    for name in ("_round_unlocked", "_avatar_tier", "_REALISM", "_WARMUP_Q", "_augment_essential_questions"):
        assert not hasattr(A, name)
    with pytest.raises(ImportError):
        importlib.import_module("interview.avatar")
    from llm.base import FakeLLM
    assert not hasattr(FakeLLM, "generate_interview_plan")


# ---- who pays: a pass, then prepaid packs -------------------------------------------------
_PACKS = [{"id": "pack_1", "interviews": 1, "seconds": 900, "price_label": "$9"},
          {"id": "pack_3", "interviews": 3, "seconds": 2700, "price_label": "$24"}]


def _fake_broker(usage, packs=_PACKS, calls=None):
    def get(path):
        if calls is not None:
            calls.append(path)
        if path == "/me/usage":
            return 200, usage
        if path == "/billing/packs":
            return 200, {"packs": packs}
        return 404, None
    return get


def test_status_offers_packs_when_the_plan_is_used_up_and_caches_them(client, monkeypatch):
    A, c = client
    calls = []
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"plan": "pass30", "avatar_seconds_left": 0}, calls=calls))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["available"] is False and st["reason"] == "no_minutes"
    assert st["can_buy"] is True and st["packs"] == _PACKS
    c.get("/api/interview/round2/status")
    assert calls.count("/billing/packs") == 1                          # cached for a few minutes
    # billing off -> no packs, nothing to buy
    A._PACKS_CACHE.update(at=0.0, packs=None)
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"plan": "pass30", "avatar_seconds_left": 0}, packs=[]))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["can_buy"] is False and st["packs"] == []


def test_paid_plan_with_credits_covering_an_interview_is_available(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"plan": "pass30", "avatar_seconds_left": 1200}))
    st = c.get("/api/interview/round2/status").get_json()
    assert st["available"] is True and st["source"] == "plan" and st["minutes_left"] == 20


def test_buy_proxies_to_the_broker_checkout(client, monkeypatch):
    A, c = client
    sent = {}

    def post(path, body):
        sent["path"] = path
        return 200, {"url": "https://checkout.stripe.test/s/cs_9"}
    monkeypatch.setattr(A, "_broker_post", post)
    r = c.post("/api/interview/round2/buy", json={"pack": "pack_3"})
    assert r.status_code == 200 and r.get_json() == {"url": "https://checkout.stripe.test/s/cs_9"}
    assert sent["path"] == "/billing/packs/pack_3/checkout"
    assert c.post("/api/interview/round2/buy", json={"pack": "../x"}).status_code == 400
    assert c.post("/api/interview/round2/buy", json={}).status_code == 400
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (400, {"error": "unknown pack"}))
    assert c.post("/api/interview/round2/buy", json={"pack": "pack_9"}).status_code == 400
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (503, {"error": "billing is not configured"}))
    assert c.post("/api/interview/round2/buy", json={"pack": "pack_1"}).status_code == 503
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (0, None))
    assert c.post("/api/interview/round2/buy", json={"pack": "pack_1"}).status_code == 503






def test_cvi_start_without_minutes_offers_packs(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"plan": "pass30", "avatar_seconds_left": 300}))
    r = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True})
    body = r.get_json()
    assert r.status_code == 402 and body["reason"] == "no_minutes" and body["can_buy"] is True
    assert [p["id"] for p in body["packs"]] == ["pack_1", "pack_3"]


def test_heartbeat_meters_a_plan_interview_even_with_an_old_own_key_on_disk(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    monkeypatch.setenv("AVATAR_API_KEY", "tvs-left-over")
    monkeypatch.setattr(A, "_broker_get", _fake_broker({"plan": "pass90", "avatar_seconds_left": 5400}))
    sent = []

    def post(path, body):
        sent.append(path)
        if path == "/avatar/session/start":
            return 200, {"session_url": "u", "provider_session_id": "c7"}
        return 200, {"remaining": 5370, "stop": False}
    monkeypatch.setattr(A, "_broker_post", post)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    r = c.post("/api/interviews/cvi/heartbeat", json={"seconds": 30, "interview_id": iid}).get_json()
    assert r == {"remaining": 5370, "stop": False, "source": "plan"} and sent[-1] == "/avatar/heartbeat"


def test_the_interviewer_keeps_time_and_the_marks_scale_for_a_short_test(client, monkeypatch):
    """The interviewer keeps time like a real one (2026-10-09): four main questions, one follow-up
    each, TIME NOTES from the app obeyed on the next turn. The app's marks come with the start
    response, and TAILOR_CVI_TEST_SCALE shrinks them so a 5-minute free Tavus call can walk the
    whole pattern."""
    A, _c = client
    p = A._PAL_SYSTEM_PROMPT
    assert "exactly 4 main questions" in p and "TIME NOTE" in p and "Sorry to jump in" in p
    t = A._cvi_timing()
    assert t["total"] == 1200 and t["minutes"] == 20 and t["answer_nudge"] < t["answer_step_in"] < t["total"] < t["hard_end"] < A.ROUND2_SAFETY_SECONDS
    # 0.22 fits a 20-minute interview into Tavus's 5-minute free calls (264 s), but the human
    # waits do not shrink: a person still gets 10 s to think and 8 s to finish a sentence.
    monkeypatch.setenv("TAILOR_CVI_TEST_SCALE", "0.22")
    s = A._cvi_timing()
    assert s["total"] == 264 and s["hard_end"] < 300 and s["answer_nudge"] == 39
    assert s["think_grace"] == 10 and s["pause_wait"] >= 8 and s["close_wait"] >= 10
    monkeypatch.setenv("TAILOR_CVI_TEST_SCALE", "nonsense")
    assert A._cvi_timing()["total"] == 1200


def test_time_notes_never_reach_the_scored_transcript(client, monkeypatch):
    """The timekeeper's notes come back in Tavus's transcript under the candidate's name (they are
    sent as if spoken). They must never be scored as the person's answers (2026-10-10)."""
    A, c = client
    prep = _prep(c)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"plan": "pass30", "avatar_seconds_left": 2700}))

    def post(path, body):
        if path == "/avatar/session/start":
            return 200, {"session_url": "u", "provider_session_id": "c11"}
        return 200, {"transcript": [
            {"role": "interviewer", "content": "Why do you want this role?"},
            {"role": "candidate", "content": _GOOD},
            {"role": "candidate", "content": "TIME NOTE: Time is nearly up. Ask whether they have one question."},
            {"role": "interviewer", "content": "Do you have a question for me?"},
            {"role": "candidate", "content": "time note: The time is up. Close the interview now."}]}
    monkeypatch.setattr(A, "_broker_post", post)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    rep = c.post("/api/interviews/cvi/end", json={"interview_id": iid}).get_json()["report"]
    assert not any("TIME NOTE" in t["content"].upper() for t in rep["transcript"])
    assert len(rep["answers"]) == 1


def test_a_reprompt_is_the_same_question_and_the_candidate_s_question_is_not_scored(client):
    """Kofi's second test call (2026-10-10): a false start, then "Take your time...", then the real
    answer were scored as two answers; and "How much am I getting paid?" after "Do you have a
    question for me?" is the candidate's question, never an answer."""
    A, _c = client
    long = "We rebuilt the inventory pipeline with ETL jobs and cut stale stock reporting time by half."
    turns = [
        {"role": "interviewer", "content": "Tell me about a time you led a complex analytics project."},
        {"role": "candidate", "content": "U.S. companies have, you know. More data centers, more advanced."},
        {"role": "interviewer", "content": "Take your time, Kofi. I am looking for a specific example."},
        {"role": "candidate", "content": long},
        {"role": "interviewer", "content": "No problem. I was just asking how you measured success."},
        {"role": "candidate", "content": "Sorry, come again. " + long},
        {"role": "interviewer", "content": "We are about out of time. Do you have one question for me?"},
        {"role": "candidate", "content": "How much am I getting paid for this role and what are the benefits like?"},
    ]
    items = A._pair_dialogue(turns, [])
    assert len(items) == 1
    assert "complex analytics project" in items[0]["q"]
    assert "More data centers" in items[0]["transcript"] and long in items[0]["transcript"]
