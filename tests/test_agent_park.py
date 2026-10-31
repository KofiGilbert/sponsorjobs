"""The apply agent's asks wait longer and PARK instead of dying (live, Lam Research, 2026-10-31).

The agent met the Data Privacy Agreement, asked on the bar and Telegram, waited 5 minutes, and
ended the run while the owner was away. Now (docs/apply-agent.md):

* an ask waits up to an hour (agent_ask_wait_min; SJ_AGENT_ASK_WAIT_S in the tests), in chunks
  the shell asks for again, with ONE Telegram reminder at about 15 minutes;
* past the wait the job PARKS: the question stays open (bar, Telegram, the Auto-apply row
  "Answer and continue") for the park window, and an answer marks the job ready to re-run;
  "Just this one" is remembered on that application, "Accept for all" sets the standing choice;
* a batch never waits: its asks park at once, and ONE digest lists every parked job;
* a captcha or a step on the page parks with "Continue";
* the late-answer path (PR #143) never takes an answer meant for a parked job;
* the standing choice on terms is never made for the person (terms_decided).
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoapply import asks as AK

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
LAM_URL = "https://careers.lamresearch.com/careers/apply?pid=1099556233587&domain=lamresearch.com"
QUESTION = "Lam Research needs you to accept its Data Privacy Agreement to apply. Accept it?"
RUN = {"url": LAM_URL, "resume_path": "", "lane": "assisted", "autonomous": False, "company": "Lam Research"}


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("SJ_AGENT_ASK_WAIT_S", raising=False)
    monkeypatch.delenv("SJ_AGENT_PARK_S", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    sent = []

    class Chan:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Chan())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Business Operations Analyst 2", "Lam Research", 70, "", data={"status": "approved"})
    rid2 = recs.add("Data Analyst", "Acme Corp", 70, "", data={"status": "approved"})
    recs.close()
    return mod, sent, rid, rid2


def _post(mod, path, body):
    return mod.app.test_client().post(path, json=body, headers=H).get_json()


def _get(mod, path):
    return mod.app.test_client().get(path, headers=H).get_json()


def _park(rid, page=LAM_URL, batch=False):
    return {"page_url": page, "run": RUN, "batch": batch}


def _parked(mod):
    return {str(e["record_id"]): e for e in _get(mod, "/api/autoapply/agent/parked")["parked"]}


def _row(mod, rid):
    b = _get(mod, "/api/autoapply/board")
    for t in b["tiles"].values():
        for it in t:
            if it["id"] == rid:
                return it
    return None


# --------------------------------------------------------------------------- the wait
def test_the_wait_is_an_hour_by_default_configurable_and_the_reminder_about_15_minutes(A, monkeypatch):
    mod = A[0]
    assert mod._ask_wait_s() == 3600
    assert mod._ask_remind_s(3600) == 900
    assert mod._ask_remind_s(40) == 20                       # a short wait reminds half way
    mod._set_prefs({"agent_ask_wait_min": 30})
    assert mod._ask_wait_s() == 1800 and mod._ask_remind_s(1800) == 900
    mod._set_prefs({"agent_ask_wait_min": 999})
    assert mod._ask_wait_s() == 180 * 60                     # capped at three hours
    mod._set_prefs({"agent_ask_wait_min": 1})
    assert mod._ask_wait_s() == 5 * 60
    monkeypatch.setenv("SJ_AGENT_ASK_WAIT_S", "7")
    assert mod._ask_wait_s() == 7
    assert mod._park_window_s() == 7 * 24 * 3600


def test_a_shell_ask_waits_in_chunks_sends_one_reminder_then_parks(A, monkeypatch):
    mod, sent, rid, _ = A
    monkeypatch.setenv("SJ_AGENT_ASK_WAIT_S", "3")
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "w1", "question": "Years of experience?", "record_id": rid,
                                                "park": _park(rid), "chunk_s": 0.5})
    assert r == {"ok": False, "id": "w1", "reason": "waiting"}
    assert len(sent) == 1 and "Years of experience?" in sent[0][0]
    for _ in range(20):
        r = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "w1", "chunk_s": 1})
        if r["reason"] != "waiting":
            break
    assert r == {"ok": False, "id": "w1", "reason": "parked"}
    reminders = [t for t, _b in sent if t.startswith("Still waiting on: ")]
    assert len(reminders) == 1 and "Years of experience?" in reminders[0]
    assert "pause this application" in reminders[0] and "—" not in reminders[0] and "–" not in reminders[0]
    # parked: the question is still open, nothing more went to Telegram
    with mod._ASKS_LOCK:
        ask = mod._ASKS["w1"]
        assert ask["open"] and ask["parked"]
    assert len(sent) == 2
    e = _parked(mod)[str(rid)]
    assert e["state"] == "waiting" and e["kind"] == "questions" and e["questions"] == ["Years of experience?"]
    assert e["page_url"] == LAM_URL and e["run"]["url"] == LAM_URL
    assert e["ask_view"]["id"] == "w1"


def test_stop_ends_a_chunked_wait(A):
    mod, _sent, rid, _ = A
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "w2", "question": "Years of experience?", "record_id": rid,
                                                "park": _park(rid), "chunk_s": 0.2})
    assert r["reason"] == "waiting"
    assert _post(mod, "/api/autoapply/agent/ask/close", {"id": "w2"}) == {"ok": True}
    assert _post(mod, "/api/autoapply/agent/ask/wait", {"id": "w2", "chunk_s": 1})["reason"] == "stopped"
    assert str(rid) not in _parked(mod)


def test_an_answer_within_the_wait_carries_on_as_before(A):
    mod, _sent, rid, _ = A
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "w3", "question": "Years of experience?", "record_id": rid,
                                                "park": _park(rid), "chunk_s": 0.2})
    assert r["reason"] == "waiting"
    assert _post(mod, "/api/autoapply/agent/answer", {"id": "w3", "answer": "5", "exact": True, "index": 0})["done"]
    r = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "w3", "chunk_s": 1})
    assert r["ok"] and r["answers"] == {"Years of experience?": "5"}
    assert _post(mod, "/api/autoapply/agent/ask/wait", {"id": "w3", "chunk_s": 1}) == r     # taken once
    assert str(rid) not in _parked(mod)


# --------------------------------------------------------------------------- parked questions
def test_a_parked_question_shows_on_the_row_and_an_answer_on_the_bar_resumes_it(A, monkeypatch):
    mod, _sent, rid, _ = A
    monkeypatch.setenv("SJ_AGENT_ASK_WAIT_S", "2")
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "p1", "question": "Years of experience?", "record_id": rid,
                                                "park": _park(rid), "chunk_s": 5})
    assert r["reason"] == "parked"
    row = _row(mod, rid)
    assert row["tile"] == "needs" and row["action"] == "answer" and row["reason"] == "paused"
    assert row["stage_detail"] == "Paused: needs you. The application asks: Years of experience?"
    assert row["paused"]["questions"] == ["Years of experience?"]
    rec = mod._record_data(mod._get_record(rid))
    assert rec["agent_park"]["state"] == "waiting"
    # the bar answers it (Answer and continue): the job is ready to re-run
    d = _post(mod, "/api/autoapply/agent/answer", {"id": "p1", "answer": "5", "exact": True, "index": 0})
    assert d["done"] and d["parked"]
    assert d["reply"] == ("Got it: 5. I carry on with the application for Business Operations Analyst 2 at Lam Research "
                          "as soon as the browser is free.")
    e = _parked(mod)[str(rid)]
    assert e["state"] == "answered" and e.get("ask_view")
    lib = mod._answer_lib()
    try:
        assert lib.lookup("Years of experience?")["answer"] == "5"          # saved for next time
    finally:
        lib.close()
    assert _row(mod, rid)["action"] == "view"
    # the shell takes it to re-run: the park is gone, the row is back to normal
    t = _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {})
    assert t["ok"] and t["entry"]["state"] == "answered" and t["entry"]["run"]["url"] == LAM_URL
    assert str(rid) not in _parked(mod)
    assert mod._record_data(mod._get_record(rid)).get("agent_park") is None
    assert _row(mod, rid)["reason"] != "paused"


def test_a_waiting_park_is_not_taken_unless_dropped(A):
    mod, _sent, rid, _ = A
    _post(mod, "/api/autoapply/agent/ask", {"id": "p2", "question": "Years of experience?", "record_id": rid,
                                            "park": _park(rid, batch=True), "no_wait": True})
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {}) == {"ok": False, "reason": "waiting"}
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {"drop": True})["ok"]
    with mod._ASKS_LOCK:
        assert mod._ASKS["p2"]["open"] is False                  # a new run replaced the park
    assert _post(mod, "/api/autoapply/agent/answer", {"id": "p2", "answer": "5", "exact": True})["error"]


def test_a_job_only_answer_reaches_the_rerun_and_is_never_saved(A, monkeypatch):
    mod, _sent, rid, _ = A
    monkeypatch.setattr(mod, "_label_maps", lambda labels, company, lib: ({}, {}, {"Your ID at this company?"}))
    monkeypatch.setattr(mod, "_recall_ctx", lambda company, data: {"hold": frozenset()})
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "p3", "question": "Your ID at this company?", "record_id": rid,
                                                "park": _park(rid, batch=True), "no_wait": True})
    assert r["reason"] == "parked"
    _post(mod, "/api/autoapply/agent/answer", {"id": "p3", "answer": "A-1234", "exact": True})
    e = _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {})["entry"]
    assert e["answers"] == {"Your ID at this company?": "A-1234"}
    lib = mod._answer_lib()
    try:
        assert not lib.lookup("Your ID at this company?")
    finally:
        lib.close()


# --------------------------------------------------------------------------- parked consent
def test_a_batch_terms_ask_parks_at_once_and_the_digest_lists_it_with_buttons(A):
    mod, sent, rid, rid2 = A
    body = {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL, "id": "t1",
            "park": _park(rid, batch=True), "no_wait": True}
    r = _post(mod, "/api/autoapply/agent/terms", body)
    assert r == {"ok": False, "id": "t1", "reason": "parked"}
    assert sent == []                                          # a batch's asks go in the digest
    _post(mod, "/api/autoapply/agent/ask", {"id": "q2", "question": "Years of experience?", "record_id": rid2,
                                            "park": _park(rid2, batch=True), "no_wait": True})
    d = _post(mod, "/api/autoapply/agent/digest", {"only_new": True})
    assert d["sent"] and d["count"] == 1 and d["jobs"] == [str(rid), str(rid2)]
    assert len(sent) == 1
    text, buttons = sent[0]
    assert text.startswith("Paused: 2 applications need you.")
    assert "1. Business Operations Analyst 2 at Lam Research: Lam Research needs you to accept its Data Privacy Agreement to apply." in text
    assert "2. Data Analyst at Acme Corp: The application asks: Years of experience?" in text
    assert [b[0] for b in buttons[0]] == ["1. Accept for all applications", "1. Just this one", "1. Stop"]
    assert all(len(b[0]) <= AK.LABEL_MAX and len(b[1]) <= 40 for row in buttons for b in row)
    assert len(buttons) == 1                                   # the free-text question has no buttons
    assert "—" not in text and "–" not in text
    # nothing new parked: the 30-minute digest sends nothing
    assert _post(mod, "/api/autoapply/agent/digest", {"only_new": True})["sent"] is False
    assert len(sent) == 1
    assert _get(mod, "/api/autoapply/agent/digest")["last"]["jobs"] == [str(rid), str(rid2)]
    # two jobs parked: a typed reply is not guessed at, it goes on (to a late answer, the chat)
    assert mod._agent_ask_event("text", "5") is None
    # the digest's button answers the parked job: "Just this one" is remembered on THIS application
    got = mod._agent_ask_event("button", "ask:t1:1")
    assert got == ("Got it: Just this one. I carry on with the application for Business Operations Analyst 2 at "
                   "Lam Research as soon as the browser is free.")
    e = _parked(mod)[str(rid)]
    assert e["state"] == "answered" and e["decision"] == "accept" and e["via"] == "once"
    assert mod._terms_pref() == "ask"
    chk = {"title": "Data Privacy Agreement", "url": LAM_URL, "check_only": True}
    assert _post(mod, "/api/autoapply/agent/terms", dict(chk, record_id=rid)) == {"ok": True, "decision": "accept", "via": "once"}
    assert _post(mod, "/api/autoapply/agent/terms", dict(chk, record_id=rid2))["decision"] == "ask"
    # one parked question left, and the digest went out: a typed reply answers it
    assert mod._agent_ask_event("text", "5").startswith("Got it: 5. I carry on with the application for Data Analyst")
    assert _parked(mod)[str(rid2)]["state"] == "answered"


def test_accept_for_all_from_a_parked_ask_sets_the_standing_choice(A):
    mod, _sent, rid, _ = A
    _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                              "id": "t2", "park": _park(rid, batch=True), "no_wait": True})
    assert _post(mod, "/api/autoapply/agent/answer", {"id": "t2", "answer": AK.TERMS_ALL, "exact": True})["parked"]
    assert mod._terms_pref() == "always" and mod._terms_decided()
    assert _parked(mod)[str(rid)]["via"] == "all"


def test_stop_on_a_parked_terms_ask_declines_the_job(A):
    mod, _sent, rid, _ = A
    _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                              "id": "t3", "park": _park(rid, batch=True), "no_wait": True})
    d = _post(mod, "/api/autoapply/agent/answer", {"id": "t3", "answer": AK.TERMS_STOP, "exact": True})
    assert d["done"] and d["reply"] == "Got it: Stop."
    e = _parked(mod)[str(rid)]
    assert e["state"] == "declined"
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {})["entry"]["state"] == "declined"
    assert mod._terms_pref() == "ask"


def test_a_single_parked_terms_ask_takes_a_typed_choice_and_lets_other_text_through(A, monkeypatch):
    mod, sent, rid, _ = A
    monkeypatch.setenv("SJ_AGENT_ASK_WAIT_S", "1")
    r = _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                                  "id": "t4", "park": _park(rid), "chunk_s": 5})
    assert r["reason"] == "parked" and sent[0][0].startswith(QUESTION)
    # chat text that is not a choice is not consent, and not swallowed: it goes on to the chat
    assert mod._agent_ask_event("text", "what new jobs are there today?") is None
    assert _parked(mod)[str(rid)]["state"] == "waiting"
    got = mod._agent_ask_event("text", "2")
    assert got.startswith("Got it: Just this one. I carry on with the application for")
    assert _parked(mod)[str(rid)]["via"] == "once"


# --------------------------------------------------------------------------- no double handling
def test_a_late_answer_never_takes_a_parked_jobs_question(A, monkeypatch):
    """PR #143's late answers read a reply to an ask that stopped waiting. A parked ask is still
    open: its reply resumes the job, and the late path never saves it a second time or says the
    application already stopped."""
    mod, sent, rid, _ = A
    monkeypatch.setenv("SJ_AGENT_ASK_WAIT_S", "1")
    r = _post(mod, "/api/autoapply/agent/ask", {"id": "l1", "question": "Gender", "options": ["Male", "Female"],
                                                "record_id": rid, "park": _park(rid), "chunk_s": 5})
    assert r["reason"] == "parked"
    assert [x["id"] for x in mod._recent_asks_load()] == ["l1"]     # kept for a late reply as before
    assert mod._late_answer("text", "Male") is None                  # an open (parked) ask is never late
    got = mod._agent_ask_event("text", "Male")
    assert got.startswith("Got it: Male. I carry on with the application for")
    assert "already stopped" not in got
    assert mod._recent_asks_load() == []                             # answered: nothing left to be late
    assert mod._agent_ask_event("text", "Male") is None              # a second "Male" goes to the chat
    assert _parked(mod)[str(rid)]["state"] == "answered"


def test_slash_stop_ends_a_live_wait_but_not_a_parked_job_and_skip_id_ends_it(A):
    mod, _sent, rid, rid2 = A
    _post(mod, "/api/autoapply/agent/ask", {"id": "s1", "question": "Years of experience?", "record_id": rid,
                                            "park": _park(rid, batch=True), "no_wait": True})
    _post(mod, "/api/autoapply/agent/ask", {"id": "s2", "question": "Years of experience?", "record_id": rid2,
                                            "park": _park(rid2), "chunk_s": 0.1})
    assert mod._agent_ask_event("text", "/stop") is None
    with mod._ASKS_LOCK:
        assert mod._ASKS["s1"]["open"] and not mod._ASKS["s2"]["open"]
    assert mod._agent_ask_event("text", f"/skip {rid}") is None
    assert _parked(mod)[str(rid)]["state"] == "stopped"


# --------------------------------------------------------------------------- restart, expiry
def test_a_parked_question_survives_a_restart(A, monkeypatch):
    mod, _sent, rid, _ = A
    _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                              "id": "r1", "park": _park(rid, batch=True), "no_wait": True})
    importlib.reload(mod)                                   # the app restarted: the registry is empty
    monkeypatch.setattr(mod, "_active_channel", lambda: None)
    with mod._ASKS_LOCK:
        assert "r1" not in mod._ASKS
    assert mod._agent_ask_event("button", "ask:r1:1").startswith("Got it: Just this one. I carry on")
    e = _parked(mod)[str(rid)]
    assert e["state"] == "answered" and e["via"] == "once"


def test_past_the_window_the_question_closes_and_the_job_leaves_the_list(A, monkeypatch):
    mod, _sent, rid, _ = A
    monkeypatch.setenv("SJ_AGENT_PARK_S", "5")
    _post(mod, "/api/autoapply/agent/ask", {"id": "x1", "question": "Years of experience?", "record_id": rid,
                                            "park": _park(rid, batch=True), "no_wait": True})
    assert _parked(mod)[str(rid)]["state"] == "waiting"
    rows = mod._parked_load()
    rows[str(rid)]["expires_at"] = time.time() - 1
    mod._parked_save(rows)
    assert _parked(mod) == {}
    with mod._ASKS_LOCK:
        assert mod._ASKS["x1"]["open"] is False
    assert mod._record_data(mod._get_record(rid)).get("agent_park") is None


# --------------------------------------------------------------------------- captcha, sign-in
def test_a_captcha_parks_with_continue(A):
    mod, _sent, rid, _ = A
    r = _post(mod, "/api/autoapply/agent/park", {"record_id": rid, "kind": "captcha", "page_url": LAM_URL, "run": RUN,
                                                 "message": "This page has a captcha. Solve it on the page, then press Continue."})
    assert r["ok"]
    row = _row(mod, rid)
    assert row["action"] == "continue" and "captcha" in row["stage_detail"]
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {})["reason"] == "waiting"
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/continue", {})["ok"]
    assert _parked(mod)[str(rid)]["state"] == "answered"
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {})["ok"]


def test_continue_never_skips_a_question(A):
    mod, _sent, rid, _ = A
    _post(mod, "/api/autoapply/agent/ask", {"id": "c1", "question": "Years of experience?", "record_id": rid,
                                            "park": _park(rid, batch=True), "no_wait": True})
    r = mod.app.test_client().post(f"/api/autoapply/agent/parked/{rid}/continue", json={}, headers=H)
    assert r.status_code == 400


# --------------------------------------------------------------------------- the standing choice
def test_the_terms_choice_is_never_made_for_the_person(A):
    mod, _sent, rid, _ = A
    c = mod.app.test_client()
    assert c.get("/api/answers/basics", headers=H).get_json()["terms_decided"] is False
    # asking, parking, answering "Just this one" decides nothing for good
    _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                              "id": "d1", "park": _park(rid, batch=True), "no_wait": True})
    _post(mod, "/api/autoapply/agent/answer", {"id": "d1", "answer": AK.TERMS_ONCE, "exact": True})
    _get(mod, "/api/autoapply/agent/parked")
    assert c.get("/api/answers/basics", headers=H).get_json()["terms_decided"] is False
    # the person's own "No, ask me first" is a decision, and so is "Yes"
    d = c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "ask"}, headers=H).get_json()
    assert d["terms_decided"] is True and d["accept_required_terms"] == "ask"


def test_the_shell_helpers_ask_in_chunks_and_resume_parked_jobs():
    """agent.js: askLong goes on through "waiting" chunks, Stop closes the ask; the poller takes
    an answered parked job and re-runs it (re-opening the job when the pane moved on); a batch
    runs every job, sends ONE digest at the end and says how it went."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_park.js")], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


# --------------------------------------------------------------------------- Ready to send, page gone
def test_a_job_left_ready_to_send_offers_send_on_its_row_and_clears_when_sent(A):
    """After an unattended batch the person comes back to several Ready to send jobs whose pages
    are gone: each row offers Send (the shell fills it again and submits it in one click, or
    parks it when anything new comes up), with the review page's fingerprint to compare."""
    mod, _sent, rid, _ = A
    r = _post(mod, "/api/autoapply/agent/ready", {"record_id": rid, "sig": "1234:abcd", "accepted": ["Data Privacy Agreement"],
                                                  "run": RUN})
    assert r["ok"] and r["ready"]["sig"] == "1234:abcd"
    row = _row(mod, rid)
    assert row["action"] == "send" and row["ready_sig"] == "1234:abcd" and row["tile"] == "waiting"
    assert row["stage_detail"].startswith("Ready to send.")
    # a paused job is never offered Send
    _post(mod, "/api/autoapply/agent/ask", {"id": "rs1", "question": "Years of experience?", "record_id": rid,
                                            "park": _park(rid, batch=True), "no_wait": True})
    assert _row(mod, rid)["action"] == "answer"
    _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {"drop": True})
    assert _row(mod, rid)["action"] == "send"
    assert _post(mod, "/api/autoapply/agent/ready", {"record_id": rid, "clear": True})["ok"]
    assert _row(mod, rid)["action"] != "send"
    assert _post(mod, "/api/autoapply/agent/ready", {"sig": "x"})["reason"] == "no-record"
