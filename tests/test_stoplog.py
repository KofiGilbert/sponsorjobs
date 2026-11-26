"""Where Autofill and the agent stop (autoapply/stoplog.py): the event builder, the local log, the
background send, and the engine route. An event says where and why, never who: no saved profile
value, email address, phone, link or typed value gets through, whatever the shell sends. The agent
loop's side (which stops make an event) is tests/js/test_stoplog_events.js, run below. Every value
here is made up.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import stoplog

ROOT = Path(__file__).resolve().parents[1]
ME = {"full_name": "Avery Example", "email": "avery.example@mail.test", "phone": "5550102000",
      "city": "Springfield", "street": "12 Sample Street", "answer": "Northwind Labs Fellowship"}


def test_only_the_listed_fields_survive_and_texts_are_scrubbed():
    raw = {"type": "asked_person", "url": "https://acme.wd1.myworkdayjobs.com/en-US/x/apply?token=abc",
           "page_kind": "form", "question": "Is avery.example@mail.test still right? We call (555) 010-2000.",
           "options": ["Yes, Avery Example", "No", "x" * 200] + ["more"] * 10,
           "reason": "Typed 12 Sample Street into Address; see https://acme.test/help",
           "steps": 7, "cost": 0.04123, "version": "0.9.1",
           "name": "Avery Example", "answers": {"Gender": "Male"}, "resume": "my whole resume", "typed": "5550102000"}
    e = stoplog.build(raw, ME)
    assert set(e) == {"at", "host", "ats", "page_kind", "type", "question", "options", "reason", "steps", "cost", "version"}
    flat = json.dumps(e)
    for bad in ("Avery", "avery.example", "555", "010-2000", "12 Sample Street", "acme.test", "token=abc", "whole resume", "Male"):
        assert bad not in flat, bad
    assert e["host"] == "acme.wd1.myworkdayjobs.com" and e["ats"] == "workday"
    assert len(e["options"]) == 8 and len(e["options"][2]) == stoplog.MAX_OPTION
    assert e["question"] == "Is [x] still right? We call [x]."
    assert e["cost"] == 0.0412 and e["steps"] == 7


@pytest.mark.parametrize("host,ats", [("job-boards.greenhouse.io", "greenhouse"), ("jobs.lever.co", "lever"),
                                      ("jobs.ashbyhq.com", "ashby"), ("careers-x.icims.com", "icims"),
                                      ("careers.lamresearch.com", "careersite"), ("", "")])
def test_the_hiring_system_by_host(host, ats):
    assert stoplog.ats_of(host) == ats


def test_an_unknown_type_is_no_event():
    assert stoplog.build({"type": "drop table"}, ME) is None
    assert stoplog.build({"type": "captcha", "page_kind": "weird"}, ME)["page_kind"] == "other"


def test_the_local_log_and_the_background_send(tmp_path):
    sent = []
    ok = {"v": False}

    def send(evs):
        if not ok["v"]:
            return False                                   # offline: kept for the next batch
        sent.append(list(evs))
        return True

    stoplog.configure(tmp_path / "stoplog.jsonl", send=send)
    try:
        evs = [stoplog.build({"type": "captcha", "host": "a.test"}, ME) for _ in range(stoplog.BATCH + 5)]
        assert stoplog.record(evs) == stoplog.BATCH + 5
        assert stoplog.flush_once() == 0 and len(stoplog.pending()) == stoplog.BATCH + 5
        ok["v"] = True
        assert stoplog.flush_once() == stoplog.BATCH
        assert stoplog.flush_once() == 5
        assert stoplog.pending() == [] and [len(b) for b in sent] == [stoplog.BATCH, 5]
        # the log keeps the newest KEEP_LINES
        stoplog.record([evs[0]] * (stoplog.KEEP_LINES + 10))
        lines = (tmp_path / "stoplog.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == stoplog.KEEP_LINES
    finally:
        stoplog.configure(None, None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_the_route_keeps_scrubbed_events_and_sends_them_in_the_background(client, monkeypatch):
    A, c = client
    posted = []
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (posted.append((path, body)) or (200, {"ok": True})))
    monkeypatch.setattr(A, "_stoplog_values", lambda: {"email": "avery.example@mail.test", "name": "Avery Example"})
    r = c.post("/api/autoapply/stoplog", json={"events": [
        {"type": "cost_cap", "url": "https://job-boards.greenhouse.io/acme/jobs/1", "question": "Avery Example, why us?",
         "reason": "budget", "steps": 12, "cost": 0.31, "email": "avery.example@mail.test"},
        {"type": "nonsense"}]})
    assert r.get_json() == {"ok": True, "kept": 1}
    import time
    for _ in range(50):
        if posted:
            break
        time.sleep(0.05)
    assert posted and posted[0][0] == "/v1/autofill/events"
    body = json.dumps(posted[0][1])
    assert "Avery" not in body and "avery.example" not in body
    assert posted[0][1]["events"][0]["ats"] == "greenhouse" and posted[0][1]["events"][0]["type"] == "cost_cap"


def test_agent_stop_events_js():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_stoplog_events.js")],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]
