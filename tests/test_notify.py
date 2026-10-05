"""Approve-while-away Telegram bot — formatting, command routing, owner-only guard, and
the poll/announce endpoints (CLAUDE.md §5).

Fully offline: the Telegram transport is a fake that records sends and replays canned
updates, and the queue actions are a small in-memory fake — no token, no network.
"""

from __future__ import annotations

import ui.app as app
from intake.memory import ConversationMemory
from notify.service import NotifyService, format_ready
from notify.telegram import TelegramBot
from ui.records import CVRecords


class FakeTransport:
    """Stand-in for the Bot API HTTP callable: queues updates, captures sends."""

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []

    def __call__(self, url, payload=None):
        if url.endswith("/sendMessage"):
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        if url.endswith("/getUpdates"):
            out, self.updates = self.updates, []      # consume once, like real acking
            return {"ok": True, "result": out}
        return {"ok": True, "result": {}}


class FakeActions:
    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}

    def pending(self):
        return [{"id": r["id"], "role": r["role"], "company": r.get("company", "")}
                for r in self.rows.values() if r.get("status", "ready") == "ready"]

    def approve(self, rid):
        r = self.rows.get(rid)
        if not r:
            return None
        r["status"] = "applied"
        return {"id": rid, "role": r["role"], "company": r.get("company", "")}

    def status(self, rid):
        r = self.rows.get(rid)
        if not r:
            return None
        return {"id": rid, "role": r["role"], "company": r.get("company", ""),
                "status": r.get("status", "ready")}


def _update(update_id, text, chat_id="99"):
    return {"update_id": update_id,
            "message": {"text": text, "chat": {"id": int(chat_id)}}}


def _svc(updates=None, rows=None, chat_id="99"):
    bot = TelegramBot("tok", chat_id=chat_id, http=FakeTransport(updates))
    actions = FakeActions(rows if rows is not None else
                          [{"id": 12, "role": "SWE", "company": "Acme"},
                           {"id": 14, "role": "Data Analyst", "company": "Beta"}])
    return NotifyService(bot, actions), bot


# --- formatting -------------------------------------------------------------- #

def test_format_ready_includes_role_company_and_commands():
    msg = format_ready({"id": 7, "role": "Analyst", "company": "Acme", "coverage": 72})
    assert "Analyst at Acme" in msg and "72% JD match" in msg
    assert "/approve 7" in msg


def test_format_ready_without_company_or_coverage():
    msg = format_ready({"id": 3, "role": "Engineer"})
    assert "Engineer" in msg and "/approve 3" in msg
    assert "match" not in msg


# --- command routing --------------------------------------------------------- #

def test_list_command():
    svc, _ = _svc()
    out = svc.handle_command("/list")
    assert "2 waiting" in out and "#12" in out and "Acme" in out


def test_list_when_empty():
    svc, _ = _svc(rows=[])
    assert "caught up" in svc.handle_command("/list").lower()


def test_approve_marks_applied():
    svc, _ = _svc()
    out = svc.handle_command("/approve 12")
    assert "Approved #12" in out and "applied" in out
    # second /list no longer shows the approved one
    assert "#12" not in svc.handle_command("/list")


def test_approve_unknown_id():
    svc, _ = _svc()
    assert "No application #999" in svc.handle_command("/approve 999")


def test_approve_without_id_prompts():
    svc, _ = _svc()
    assert "/approve <id>" in svc.handle_command("/approve")


def test_status_command():
    svc, _ = _svc()
    assert "SWE at Acme" in svc.handle_command("/status 12")


def test_group_syntax_and_hash_id():
    svc, _ = _svc()
    # /approve@TailorBot #12  — group mention + #-prefixed id both tolerated
    assert "Approved #12" in svc.handle_command("/approve@TailorBot #12")


def test_unknown_command_returns_help():
    svc, _ = _svc()
    assert "SponsorJobs commands" in svc.handle_command("/wat")
    assert "SponsorJobs commands" in svc.handle_command("")


# --- poll loop & owner-only guard ------------------------------------------- #

def test_poll_dispatches_and_replies():
    svc, bot = _svc(updates=[_update(100, "/list"), _update(101, "/approve 12")])
    res = svc.poll_once(offset=0)
    assert res["handled"] == 2 and res["offset"] == 102
    assert len(bot._http.sent) == 2
    assert "Approved #12" in bot._http.sent[1]["text"]


def test_poll_ignores_non_owner_chat():
    # An update from a stranger's chat must not be acted on or answered.
    svc, bot = _svc(updates=[_update(200, "/approve 12", chat_id="55")], chat_id="99")
    res = svc.poll_once(offset=0)
    assert res["handled"] == 0 and res["offset"] == 201
    assert bot._http.sent == []                    # no reply sent to the stranger


def test_announce_sends_ready_push():
    svc, bot = _svc()
    svc.announce_ready({"id": 5, "role": "SWE", "company": "Acme", "coverage": 60})
    assert bot._http.sent and "Package ready" in bot._http.sent[0]["text"]
    assert bot._http.sent[0]["chat_id"] == "99"


# --- endpoints --------------------------------------------------------------- #

def _seed_record(tmp_path, monkeypatch, status="ready"):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    monkeypatch.setattr(app, "_TG_OFFSET_FILE", tmp_path / "tg_offset")
    recs = CVRecords(db)
    rid = recs.add("SWE", "Acme", 70.0, "", data={"status": status})
    recs.close()
    return rid


def test_announce_endpoint_pushes(tmp_path, monkeypatch):
    rid = _seed_record(tmp_path, monkeypatch)
    transport = FakeTransport()
    monkeypatch.setattr(app, "_make_bot",
                        lambda: TelegramBot("tok", chat_id="99", http=transport))
    d = app.app.test_client().post(f"/api/notify/announce/{rid}").get_json()
    assert d["ok"] is True
    assert transport.sent and "Package ready" in transport.sent[0]["text"]


def test_announce_endpoint_404_for_missing(tmp_path, monkeypatch):
    _seed_record(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_make_bot",
                        lambda: TelegramBot("tok", chat_id="99", http=FakeTransport()))
    r = app.app.test_client().post("/api/notify/announce/9999")
    assert r.status_code == 404


def test_poll_endpoint_approves_and_persists_offset(tmp_path, monkeypatch):
    rid = _seed_record(tmp_path, monkeypatch)
    transport = FakeTransport(updates=[_update(300, f"/approve {rid}", chat_id="99")])
    monkeypatch.setattr(app, "_make_bot",
                        lambda: TelegramBot("tok", chat_id="99", http=transport))
    d = app.app.test_client().post("/api/notify/poll").get_json()
    assert d["ok"] is True and d["handled"] == 1 and d["offset"] == 301
    # offset persisted for next call
    assert app._tg_offset() == 301
    # the record is now applied
    recs = CVRecords(app.DB_PATH)
    try:
        rec = recs.get(rid)
    finally:
        recs.close()
    import json
    assert json.loads(rec["data"])["status"] == "applied"
