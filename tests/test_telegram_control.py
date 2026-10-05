"""P3: Telegram two-way control of the apply engine.

Fully offline: the bot transport and the queue actions are injected fakes, the control store is a
real JSON file in a tmp dir, and Annalisa runs on FakeLLM. No token, no network, no camera.
"""

from __future__ import annotations

import importlib

import pytest

from notify.control import ControlState
from notify.service import NotifyService
from notify.telegram import TelegramBot


# ---- injected fakes ---------------------------------------------------------------------------
class FakeTransport:
    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []

    def __call__(self, url, payload=None):
        if url.endswith("/sendMessage"):
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        if url.endswith("/getUpdates"):
            out, self.updates = self.updates, []
            return {"ok": True, "result": out}
        return {"ok": True, "result": {}}


class FakeActions:
    """Records control calls and answers free text, so the command router is testable offline."""

    def __init__(self, rows=None):
        self.rows = {r["id"]: dict(r) for r in (rows or [{"id": 12, "role": "SWE", "company": "Acme"}])}
        self.paused = False
        self.skipped_current = False
        self.chats = []

    def pending(self):
        return [{"id": r["id"], "role": r["role"], "company": r.get("company", "")}
                for r in self.rows.values() if r.get("status", "ready") == "ready"]

    def approve(self, rid):
        r = self.rows.get(rid)
        return None if not r else {"id": rid, "role": r["role"], "company": r.get("company", "")}

    def status(self, rid):
        r = self.rows.get(rid)
        return None if not r else {"id": rid, "role": r["role"], "status": r.get("status", "ready")}

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def skip(self, rid=None):
        if rid is None:
            self.skipped_current = True
            return {"current": True}
        r = self.rows.get(rid)
        if not r:
            return None
        r["status"] = "skipped"
        return {"id": rid, "role": r["role"], "company": r.get("company", "")}

    def handle(self, rid, note):
        r = self.rows.get(rid)
        if not r:
            return None
        r["handling_note"] = note
        return {"id": rid, "role": r["role"], "company": r.get("company", "")}

    def chat(self, text):
        self.chats.append(text)
        return f"Annalisa: heard '{text}'."


def _update(update_id, text, chat_id="99"):
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": int(chat_id)}}}


def _svc(updates=None, chat_id="99"):
    bot = TelegramBot("tok", chat_id=chat_id, http=FakeTransport(updates))
    actions = FakeActions()
    return NotifyService(bot, actions), bot, actions


# ---- Task 1: control commands (owner-only via poll; routing here) ------------------------------
def test_stop_and_resume_ack_and_flip_pause():
    svc, _, actions = _svc()
    out = svc.handle_command("/stop")
    assert actions.paused is True and "pause" in out.lower() and "current" in out.lower()
    assert "resume" in svc.handle_command("/resume").lower() and actions.paused is False
    # /pause is an alias for /stop
    svc.handle_command("/pause")
    assert actions.paused is True


def test_skip_current_and_specific():
    svc, _, actions = _svc()
    assert "current" in svc.handle_command("/skip").lower() and actions.skipped_current is True
    out = svc.handle_command("/skip 12")
    assert "Skipped #12" in out and actions.rows[12]["status"] == "skipped"
    assert "No application #999" in svc.handle_command("/skip 999")


def test_handle_attaches_a_note():
    svc, _, actions = _svc()
    out = svc.handle_command("/handle 12 use my analyst profile")
    assert "#12" in out and actions.rows[12]["handling_note"] == "use my analyst profile"
    assert "how to handle" in svc.handle_command("/handle 12").lower()   # missing note prompts
    assert "Which one" in svc.handle_command("/handle")                  # missing id prompts


# ---- Task 3: free text -> Annalisa; commands still work ----------------------------------------
def test_free_text_routes_to_annalisa_and_commands_still_work():
    svc, _, actions = _svc()
    reply = svc.handle_command("how's it going tonight?")
    assert reply == "Annalisa: heard 'how's it going tonight?'." and actions.chats == ["how's it going tonight?"]
    # a real command after a chat still dispatches
    assert "waiting" in svc.handle_command("/list").lower() or "caught up" in svc.handle_command("/list").lower()
    # an UNKNOWN slash command is help, not Annalisa
    assert "SponsorJobs commands" in svc.handle_command("/wat")
    assert actions.chats == ["how's it going tonight?"]                  # /wat did not hit chat


def test_poll_routes_owner_free_text_to_annalisa():
    svc, bot, actions = _svc(updates=[_update(1, "focus on fintech roles tonight")])
    res = svc.poll_once(offset=0)
    assert res["handled"] == 1 and actions.chats == ["focus on fintech roles tonight"]
    assert "Annalisa" in bot._http.sent[0]["text"]


# ---- Task: owner-only / fail-closed still holds for the new surface ----------------------------
def test_poll_survives_a_malformed_update_id():
    """A malformed update_id in the batch must not abort the whole poll; a valid owner
    message right after it still gets handled."""
    bad = {"update_id": "not-an-int", "message": {"text": "/list", "chat": {"id": 99}}}
    svc, bot, actions = _svc(updates=[bad, _update(7, "focus on data roles")])
    res = svc.poll_once(offset=0)                       # must not raise
    assert actions.chats == ["focus on data roles"]    # the valid update was processed
    assert res["handled"] == 1


def test_non_owner_free_text_is_ignored():
    svc, bot, actions = _svc(updates=[_update(5, "stop everything", chat_id="55")], chat_id="99")
    res = svc.poll_once(offset=0)
    assert res["handled"] == 0 and actions.chats == [] and bot._http.sent == []


def test_unset_owner_answers_nobody():
    svc, bot, actions = _svc(updates=[_update(6, "/stop")], chat_id="")
    res = svc.poll_once(offset=0)
    assert res["handled"] == 0 and "No owner chat" in res["error"]
    assert actions.paused is False and bot._http.sent == []


# ---- Task 2: the persisted control store + the loop guard --------------------------------------
def test_control_store_pause_skip_roundtrip(tmp_path):
    ctrl = ControlState(tmp_path / "control.json")
    assert ctrl.paused() is False
    ctrl.set_paused(True); assert ctrl.paused() is True
    ctrl.set_paused(False); assert ctrl.paused() is False
    ctrl.request_skip(None)
    assert ctrl.take_skip_current() is True and ctrl.take_skip_current() is False   # one shot
    ctrl.request_skip(12)
    assert ctrl.is_skipped(12) is True and ctrl.is_skipped(13) is False


def test_stop_halts_the_loop_after_the_current_item(tmp_path):
    """The exact guard the auto-apply loop runs between items: /stop mid-run finishes the current
    application and then stops; /resume lets a later run continue."""
    ctrl = ControlState(tmp_path / "control.json")
    processed = []
    for item in ["a", "b", "c"]:
        if ctrl.paused():          # checked at the top of each iteration, like autoapply_run
            break
        processed.append(item)
        if item == "a":
            ctrl.set_paused(True)  # the person sends /stop while "a" is being handled
    assert processed == ["a"], "the loop did not halt after the current item"
    ctrl.set_paused(False)
    assert ctrl.paused() is False


# ---- Task 2b: engine reads the handling note; Annalisa persists to the diary -------------------
@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
    import ui.app as A
    importlib.reload(A)
    A.DB_PATH = str(tmp_path / "m.db")
    A.PALACE_DIR = tmp_path / "palace"
    return A


def test_handle_persists_note_on_the_record_and_builder_reads_it(app_client):
    A = app_client
    recs = A.CVRecords(A.DB_PATH)
    rid = recs.add("Data Analyst", "Acme", 70.0, "", data={"status": "ready"})
    recs.close()

    A._RecordActions().handle(rid, "use my analyst profile")
    rec = A._records().get(rid)
    assert A._handling_note(rec) == "use my analyst profile", "the builder's read path lost the note"


def test_skip_marks_the_record_and_the_control_store(app_client):
    A = app_client
    recs = A.CVRecords(A.DB_PATH)
    rid = recs.add("SWE", "Acme", 70.0, "", data={"status": "ready"})
    recs.close()
    A._RecordActions().skip(rid)
    assert A._record_data(A._records().get(rid)).get("status") == "skipped"
    assert A._control().is_skipped(rid) is True


def test_annalisa_reply_answers_and_persists_to_the_diary(app_client):
    A = app_client
    reply = A._annalisa_reply("how's it going tonight?")
    assert reply                                             # FakeLLM converse gives a reply
    history = A._palace().load_history()
    joined = " ".join(t["content"] for t in history)
    assert "how's it going tonight?" in joined              # the message was remembered
