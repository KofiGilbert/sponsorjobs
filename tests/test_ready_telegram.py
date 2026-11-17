"""The Ready to send notice on Telegram (docs/notify.md "Ready to send", notify/ready.py).

When Apply for me leaves an application on its review page, the shell posts the hand-over to
/api/autoapply/agent/ready with a notice (shell-electron/agent.js readyNotice: how many fields it
filled, how many lines deserve a look, and those lines, never a self-identification value). The
engine sends ONE message per run through the active channel:

* nothing when Telegram is not linked;
* nothing, ever, for that run when the channel is paused or "Application updates" is off;
* held through quiet hours and sent by the notify tick once they end, while the application still
  waits for Send (and is under 36 hours old);
* never twice for the same run; a new run is a new message;
* no Send button: notify/ has no sanctioned way to approve an assisted submission from Telegram.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import ui.app as app
from notify import ready as RN
from notify.alerts import NotifyState
from notify.channel import ChannelError
from ui.records import CVRecords

H = {"X-Tailor-Extension": "1"}
SELF = "Zyxwv Selfid Value"
NOTICE = {"run": "1763300000.5", "filled": 14, "files": 1, "flagged": 2, "more": 0,
          "lines": ["• Field of Study: Agricultural Economics (closest match, check it) ⚠",
                    "• Resume: Avery Example Resume.pdf (the form also holds an earlier file with the same "
                    "name: check which one to keep) ⚠"]}


class Chan:
    name = "official"

    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail

    def send(self, text, buttons=None):
        if self.fail:
            raise ChannelError(self.fail)
        self.sent.append((text, buttons))
        return {"ok": True}


def _at(hour):
    return datetime(2026, 11, 16, hour, 5).astimezone()


@pytest.fixture()
def st(tmp_path):
    return NotifyState(tmp_path / "notify_state.json")


# --------------------------------------------------------------------------- the message
def test_the_message_names_the_job_counts_and_flagged_lines():
    text = RN.ready_text("Data Analyst", "Acme Corp", NOTICE)
    assert text.splitlines()[0] == "Ready to send: Data Analyst at Acme Corp."
    assert text.splitlines()[1] == "I filled 14 fields. 2 need a look:"
    assert NOTICE["lines"][0] in text and NOTICE["lines"][1] in text
    assert text.splitlines()[-1] == ("Review it in SponsorJobs, Auto-apply (Review answers), then press Send there. "
                                     "Nothing is sent until you do.")
    assert "–" not in text and "—" not in text and "CV" not in text and len(text) < 4096
    clean = RN.ready_text("Data Analyst", "", {"filled": 1, "flagged": 0, "lines": []})
    assert clean.splitlines()[:2] == ["Ready to send: Data Analyst.", "I filled 1 field. Nothing is flagged."]
    more = RN.ready_text("", "", {"filled": 30, "flagged": 20, "lines": ["• x"] * 12, "more": 8})
    assert more.startswith("Ready to send: An application.") and "And 8 more in the app." in more


# --------------------------------------------------------------------------- the rules
def test_sent_once_per_run(st):
    ch = Chan()
    assert RN.deliver(st, ch, RN.notice_key(5, "r1"), 5, "hello", _at(12)) == "sent"
    assert RN.deliver(st, ch, RN.notice_key(5, "r1"), 5, "hello", _at(12)) == "dup"
    assert len(ch.sent) == 1 and ch.sent[0][1] is None             # no buttons
    assert RN.deliver(st, ch, RN.notice_key(5, "r2"), 5, "again", _at(12)) == "sent"   # a new run
    assert len(ch.sent) == 2


def test_no_telegram_sends_nothing(st):
    assert RN.deliver(st, None, "5:r1", 5, "hello", _at(12)) == "none"
    assert not st.load().get("ready_held")


def test_paused_or_updates_off_never_sends(st):
    ch = Chan()
    assert RN.deliver(st, ch, "5:r1", 5, "hello", _at(12), paused=True) == "paused"
    assert RN.deliver(st, ch, "5:r1", 5, "hello", _at(12)) == "dup"   # not later either
    st.set_prefs({"app_updates": False})
    assert RN.deliver(st, ch, "6:r1", 6, "hello", _at(12)) == "off"
    # the broker's own answer to /stop
    st.set_prefs({"app_updates": True})
    assert RN.deliver(st, Chan(fail="paused"), "7:r1", 7, "hello", _at(12)) == "paused"
    assert RN.deliver(st, ch, "7:r1", 7, "hello", _at(12)) == "dup"
    assert ch.sent == []


def test_quiet_hours_hold_until_morning_while_still_ready(st):
    ch = Chan()
    assert RN.deliver(st, ch, "5:r1", 5, "five", _at(23)) == "held"
    assert RN.deliver(st, ch, "6:r1", 6, "six", _at(23)) == "held"
    assert RN.deliver(st, ch, "5:r1", 5, "five", _at(23)) == "dup"
    assert ch.sent == []
    assert RN.flush(st, ch, _at(2), lambda rid: True) == 0           # still quiet
    # morning: 5 still waits for Send, 6 was sent meanwhile
    assert RN.flush(st, ch, _at(8), lambda rid: rid == 5) == 1
    assert [t for t, _ in ch.sent] == ["five"]
    assert not st.load().get("ready_held")
    assert RN.flush(st, ch, _at(9), lambda rid: True) == 0           # once


def test_a_held_notice_goes_stale_or_with_its_application(st):
    ch = Chan()
    RN.deliver(st, ch, "5:r1", 5, "five", _at(23))
    assert RN.flush(st, ch, _at(23) + timedelta(hours=40), lambda rid: True) == 0
    assert ch.sent == [] and not st.load().get("ready_held")
    RN.deliver(st, ch, "6:r1", 6, "six", _at(23))
    RN.drop(st, 6)                                                    # Send pressed, or a new run
    assert not st.load().get("ready_held")


def test_a_rate_limit_waits_for_the_next_tick(st):
    busy = Chan(fail="rate_limited")
    assert RN.deliver(st, busy, "5:r1", 5, "five", _at(12)) == "held"
    ch = Chan()
    assert RN.flush(st, ch, _at(12), lambda rid: True) == 1 and ch.sent[0][0] == "five"


# --------------------------------------------------------------------------- the route
@pytest.fixture()
def eng(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "_NOTIFY_STATE_FILE", tmp_path / "notify_state.json")
    box = {"ch": Chan()}
    monkeypatch.setattr(app, "_active_channel", lambda: box["ch"])
    monkeypatch.setitem(app._OFFICIAL_CACHE, "data", {"linked": True})
    recs = CVRecords(app.DB_PATH)
    try:
        box["rid"] = recs.add("Data Analyst", "Acme Corp", 70, "", data={"status": "approved"})
    finally:
        recs.close()
    # never quiet in these tests: quiet hours are an empty window
    NotifyState(tmp_path / "notify_state.json").set_prefs({"quiet_start": 0, "quiet_end": 0})
    return box


def _ready(rid, notice=NOTICE, **extra):
    body = {"record_id": rid, "sig": "1234:abcd", "run": {"url": "https://acme.example.com/apply"}, "notice": notice}
    body.update(extra)
    return app.app.test_client().post("/api/autoapply/agent/ready", json=body, headers=H).get_json()


def test_the_hand_over_sends_one_notice_per_run(eng):
    r = _ready(eng["rid"])
    assert r["ok"] and r["notice"] == "sent"
    assert len(eng["ch"].sent) == 1
    text, buttons = eng["ch"].sent[0]
    assert text.startswith("Ready to send: Data Analyst at Acme Corp.\nI filled 14 fields. 2 need a look:")
    assert buttons is None and SELF not in text
    assert _ready(eng["rid"])["notice"] == "dup" and len(eng["ch"].sent) == 1
    assert _ready(eng["rid"], notice=dict(NOTICE, run="1763309999.1"))["notice"] == "sent"


def test_no_notice_without_telegram_or_from_an_old_shell(eng):
    eng["ch"] = None
    assert _ready(eng["rid"])["notice"] == "none"
    eng["ch"] = Chan()
    r = _ready(eng["rid"], notice=None)
    assert r["ok"] and r["notice"] == "" and eng["ch"].sent == []


def test_paused_from_telegram_sends_nothing(eng, monkeypatch):
    monkeypatch.setitem(app._OFFICIAL_CACHE, "data", {"linked": True, "paused": True})
    assert _ready(eng["rid"])["notice"] == "paused" and eng["ch"].sent == []


def test_quiet_hours_hold_it_for_the_tick(eng, monkeypatch, tmp_path):
    h = datetime.now().astimezone().hour
    NotifyState(tmp_path / "notify_state.json").set_prefs({"quiet_start": h, "quiet_end": (h + 1) % 24})
    assert _ready(eng["rid"])["notice"] == "held" and eng["ch"].sent == []
    NotifyState(tmp_path / "notify_state.json").set_prefs({"quiet_start": 0, "quiet_end": 0})
    assert app._ready_notice_flush(eng["ch"]) == 1 and len(eng["ch"].sent) == 1
    # sent (or cleared) before the morning: the held one goes with it
    NotifyState(tmp_path / "notify_state.json").set_prefs({"quiet_start": h, "quiet_end": (h + 1) % 24})
    _ready(eng["rid"], notice=dict(NOTICE, run="2"))
    app.app.test_client().post("/api/autoapply/agent/ready", json={"record_id": eng["rid"], "clear": True}, headers=H)
    NotifyState(tmp_path / "notify_state.json").set_prefs({"quiet_start": 0, "quiet_end": 0})
    assert app._ready_notice_flush(eng["ch"]) == 0 and len(eng["ch"].sent) == 1
