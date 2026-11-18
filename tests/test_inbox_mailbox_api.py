"""The in-app mailbox routes (/api/inbox/messages..., /api/inbox/sync, /api/inbox/notify): the
contract the Inbox screen is built on. A fake broker stands in for the Worker; nothing leaves
the machine. Made-up people and companies only."""
from __future__ import annotations

import importlib
import time

import pytest

ALIAS = "zoe.muller@sponsorjobs.ai"
H = {"X-Tailor-Extension": "1"}

ACME = {"source_id": "careers::acme-de", "source": "careers", "company": "Acme Robotics",
        "title": "Data Engineer", "location": "Boston", "remote": "", "posted_at": "2026-10-20",
        "url": "https://careers.acmerobotics.example/jobs/data-engineer", "salary": "", "jd_text": ""}

RECEIVED_TEXT = ("Hi Zoe,\n\nThank you for applying to the Data Engineer role at Acme Robotics. We have received "
                 "your application.\nIf selected, we will reach out to schedule an interview.\n\nAcme Robotics Recruiting")
REJECTED_TEXT = ("Hi Zoe,\n\nThank you for your interest in Acme Robotics. Unfortunately, we have decided to "
                 "pursue other candidates for the Data Engineer position.\n\nAcme Robotics Recruiting")
INTERVIEW_TEXT = ("Hi Zoe,\n\nWe would like to invite you to a phone interview for the Data Engineer role. "
                  "Please pick a time: https://calendly.com/acme-robotics/phone-screen\n\nPriya")


def mail(cursor, subject, text, sender="jobs@acmerobotics.example", name="Acme Robotics Recruiting", **kw):
    return {"cursor": cursor, "sender": sender, "sender_name": name, "subject": subject, "text": text,
            "html": kw.pop("html", None), "at": kw.pop("at", int(time.time()) - 60 + cursor),
            "has_attachments": bool(kw.get("attachment_names")), "attachment_names": kw.pop("attachment_names", [])}


class FakeBroker:
    def __init__(self, messages=()):
        self.held = list(messages)
        self.calls = []
        self.mode = "forward"
        self.down = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise OSError("network is unreachable")
        if path.startswith("/mail/messages"):
            after = int(path.split("after=")[1])
            self.held = [m for m in self.held if m["cursor"] > after]
            out = self.held[:25]
            return {"messages": out, "cursor": out[-1]["cursor"] if out else after, "more": len(self.held) > 25}
        if path == "/mail/alias/notify":
            self.mode = body["mode"]
            return {"alias": ALIAS, "forward_to": "zoe@example.com", "notify_mode": self.mode}
        if path == "/mail/alias":
            return {"alias": ALIAS, "forward_to": "zoe@example.com", "notify_mode": self.mode}
        return {"error": "no such route", "status": 404}


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as app_mod
    importlib.reload(app_mod)
    app_mod._set_prefs({"mail_alias": "", "apply_with_alias": True})
    monkeypatch.setattr(app_mod, "_account_token", lambda: "tok-1")
    yield app_mod
    t = app_mod._MAILBOX["thread"]
    if t is not None:
        t.join(timeout=10)
    app_mod._mailbox_forget()
    app_mod._set_prefs({"mail_alias": "", "apply_with_alias": True})
    app_mod._RELAY["box"] = None


def wire(A, monkeypatch, broker, alias=ALIAS):
    import inbox.relay as relay
    monkeypatch.setattr(relay, "urllib_fetch", lambda url, tok, timeout=15.0: broker)
    A._set_prefs({"mail_alias": alias})


def put(A, job, stage):
    """Put one job on the Tracker board at ``stage`` (straight into the store: the move route
    also loads the bundled sponsor data, which these tests do not need)."""
    w = A._watchlist()
    try:
        w.upsert_jobs([job])
        w.track_job(job["source_id"], stage)
    finally:
        w.close()


def stage_of(A, sid):
    w = A._watchlist()
    try:
        return w.stage_of(sid)
    finally:
        w.close()


LIST_KEYS = {"id", "sender", "sender_name", "subject", "snippet", "at", "read", "archived", "label",
             "record_id", "job_id", "company", "role", "has_attachments"}


# ---- with no address, and offline -----------------------------------------------------------

def test_everything_answers_with_no_address_yet(A):
    c = A.app.test_client()
    d = c.get("/api/inbox/messages", headers=H).get_json()
    assert d == {"ok": True, "messages": [], "count": 0, "unread": 0, "alias": None, "syncing": False,
                 "labels": {"received": 0, "rejected": 0, "interview": 0, "action": 0, "other": 0}}
    assert c.post("/api/inbox/sync", headers=H).get_json() == {"ok": True, "new": 0, "unread": 0, "synced": False}
    assert c.post("/api/inbox/messages/read-all", headers=H).get_json() == {"ok": True, "unread": 0}
    assert c.get("/api/inbox/notify", headers=H).get_json() == {"ok": True, "mode": None}
    assert c.get("/api/inbox/messages/1", headers=H).status_code == 404
    assert c.post("/api/inbox/messages/1", json={"read": True}, headers=H).status_code == 404
    assert c.delete("/api/inbox/messages/1", headers=H).status_code == 404
    r = c.post("/api/inbox/notify", json={"mode": "notice"}, headers=H)
    assert r.status_code == 400 and r.get_json()["reason"] == "no_alias"


def test_offline_serves_the_local_mailbox_without_an_error(A, monkeypatch):
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT)])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    assert c.post("/api/inbox/sync", headers=H).get_json()["new"] == 1
    b.down = True
    out = c.post("/api/inbox/sync", headers=H)
    assert out.status_code == 200 and out.get_json() == {"ok": True, "new": 0, "unread": 1, "synced": False}
    d = c.get("/api/inbox/messages", headers=H).get_json()         # within a minute of a sync: no new one starts
    assert d["ok"] and d["count"] == 1 and d["syncing"] is False and d["alias"] == ALIAS
    assert c.get("/api/inbox/notify", headers=H).get_json() == {"ok": True, "mode": None}
    r = c.post("/api/inbox/notify", json={"mode": "off"}, headers=H)
    assert r.status_code == 503 and r.get_json()["reason"] == "offline"


# ---- sync, list, read -----------------------------------------------------------------------

def test_sync_downloads_labels_and_lists_newest_first(A, monkeypatch):
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT),
                    mail(2, "Your application to Acme Robotics", REJECTED_TEXT),
                    mail(3, "Interview with Acme Robotics", INTERVIEW_TEXT, attachment_names=["agenda.pdf"]),
                    mail(4, "Your verification code", "Your one-time code is 482913.", sender="noreply@myworkday.com", name="Workday"),
                    mail(5, "Weekly digest", "Five new roles near Boston.", sender="news@jobboard.example", name="Job Board")])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    out = c.post("/api/inbox/sync", headers=H).get_json()
    assert out == {"ok": True, "new": 5, "unread": 5, "synced": True}
    assert b.held == []                                            # downloaded, so gone from the broker
    d = c.get("/api/inbox/messages", headers=H).get_json()
    assert d["count"] == 5 and d["unread"] == 5 and d["alias"] == ALIAS
    assert [m["label"] for m in d["messages"]] == ["other", "action", "interview", "rejected", "received"]
    assert d["labels"] == {"received": 1, "rejected": 1, "interview": 1, "action": 1, "other": 1}
    first = d["messages"][-1]
    assert set(first) == LIST_KEYS
    assert first["sender"] == "jobs@acmerobotics.example" and first["sender_name"] == "Acme Robotics Recruiting"
    assert first["snippet"].startswith("Hi Zoe, Thank you for applying") and len(first["snippet"]) <= 143
    assert first["read"] is False and first["archived"] is False and first["has_attachments"] is False
    assert d["messages"][2]["has_attachments"] is True
    # filters
    assert [m["label"] for m in c.get("/api/inbox/messages?label=rejected", headers=H).get_json()["messages"]] == ["rejected"]
    assert c.get("/api/inbox/messages?q=calendly", headers=H).get_json()["count"] == 1
    page = c.get("/api/inbox/messages?per_page=2&page=3", headers=H).get_json()
    assert len(page["messages"]) == 1 and page["count"] == 5
    assert c.get("/api/inbox/messages?per_page=abc&page=-4", headers=H).get_json()["count"] == 5
    # a second sync finds nothing new and stores nothing twice
    assert c.post("/api/inbox/sync", headers=H).get_json()["new"] == 0
    assert c.get("/api/inbox/messages", headers=H).get_json()["count"] == 5


def test_opening_the_inbox_starts_a_background_sync_at_most_once_a_minute(A, monkeypatch):
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT)])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    d = c.get("/api/inbox/messages", headers=H).get_json()
    assert d["syncing"] is True
    A._MAILBOX["thread"].join(timeout=10)
    d = c.get("/api/inbox/messages", headers=H).get_json()
    assert d["count"] == 1 and d["syncing"] is False
    n = len(b.calls)
    c.get("/api/inbox/messages", headers=H)
    assert len(b.calls) == n                                       # no second download within the minute


def test_read_one_message_with_safe_html_and_update_it(A, monkeypatch):
    html = ('<div><p>Hi <b>Zoe</b></p><script>steal()</script><img src="https://track.example/o.gif">'
            '<a href="javascript:x()" onclick="y()">bad</a><a href="https://acme.example/status">Status</a></div>')
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT, html=html,
                         attachment_names=["offer.pdf"])])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    c.post("/api/inbox/sync", headers=H)
    mid = c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]["id"]
    m = c.get(f"/api/inbox/messages/{mid}", headers=H).get_json()["message"]
    assert set(m) == LIST_KEYS | {"text", "html_safe", "attachment_names", "application"}
    assert m["text"] == RECEIVED_TEXT and m["attachment_names"] == ["offer.pdf"] and m["application"] is None
    assert "<b>Zoe</b>" in m["html_safe"] and 'href="https://acme.example/status"' in m["html_safe"]
    for bad in ("script", "steal", "img", "track.example", "javascript", "onclick"):
        assert bad not in m["html_safe"]
    r = c.post(f"/api/inbox/messages/{mid}", json={"read": True, "archived": True, "label": "interview"}, headers=H).get_json()
    assert r["ok"] and (r["message"]["read"], r["message"]["archived"], r["message"]["label"]) == (True, True, "interview")
    assert c.get("/api/inbox/messages", headers=H).get_json()["count"] == 0
    arch = c.get("/api/inbox/messages?archived=1", headers=H).get_json()
    assert arch["count"] == 1 and arch["messages"][0]["label"] == "interview"
    assert c.post(f"/api/inbox/messages/{mid}", json={"label": "spam"}, headers=H).status_code == 400
    assert c.post(f"/api/inbox/messages/{mid}", json={"archived": False, "read": False}, headers=H).get_json()["message"]["read"] is False
    assert c.get("/api/inbox/messages?unread=1", headers=H).get_json()["count"] == 1
    assert c.post("/api/inbox/messages/read-all", headers=H).get_json() == {"ok": True, "unread": 0}
    assert c.get("/api/inbox/messages", headers=H).get_json()["unread"] == 0
    # A text-only message has no html to show.
    assert A._mailbox_detail({"html": None})["html_safe"] is None


def test_delete_is_local_and_final(A, monkeypatch):
    b = FakeBroker([mail(1, "Weekly digest", "Five new roles.")])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    c.post("/api/inbox/sync", headers=H)
    mid = c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]["id"]
    n = len(b.calls)
    assert c.delete(f"/api/inbox/messages/{mid}", headers=H).get_json() == {"ok": True}
    assert len(b.calls) == n                                       # nothing is asked of the broker
    assert c.get(f"/api/inbox/messages/{mid}", headers=H).status_code == 404
    assert c.delete(f"/api/inbox/messages/{mid}", headers=H).status_code == 404
    b.held = [mail(1, "Weekly digest", "Five new roles.")]         # a page delivered again
    c.post("/api/inbox/sync", headers=H)
    assert c.get("/api/inbox/messages", headers=H).get_json()["count"] == 0


# ---- matching and the application's status -------------------------------------------------

def test_a_confirmation_is_tied_to_its_application_and_moves_the_card_to_applied(A, monkeypatch):
    c = A.app.test_client()
    put(A, ACME, "saved")
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT)])
    wire(A, monkeypatch, b)
    c.post("/api/inbox/sync", headers=H)
    row = c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]
    assert (row["label"], row["job_id"], row["company"], row["role"]) == ("received", ACME["source_id"], "Acme Robotics", "Data Engineer")
    # The Tracker's own path made the move (a plain acknowledgement matched by domain), with Undo.
    assert stage_of(A, ACME["source_id"]) == "applied"
    shown = c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"]
    assert [(s["label"], s["status"], s["matched_by"]) for s in shown] == [("acknowledgement", "auto", "domain")]
    m = c.get(f"/api/inbox/messages/{row['id']}", headers=H).get_json()["message"]
    assert m["application"] == {"record_id": None, "job_id": ACME["source_id"], "company": "Acme Robotics",
                                "role": "Data Engineer", "status": "applied"}


def test_a_rejection_and_an_interview_become_suggestions_the_person_taps(A, monkeypatch):
    c = A.app.test_client()
    put(A, ACME, "applied")
    b = FakeBroker([mail(1, "Interview with Acme Robotics", INTERVIEW_TEXT)])
    wire(A, monkeypatch, b)
    c.post("/api/inbox/sync", headers=H)
    assert stage_of(A, ACME["source_id"]) == "applied"             # nothing moves by itself
    s = c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"]
    assert [(x["label"], x["stage"], x["status"]) for x in s] == [("interview_request", "interviewing", "pending")]
    b.held = [mail(2, "Your application to Acme Robotics", REJECTED_TEXT)]
    c.post("/api/inbox/sync", headers=H)
    s = c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"]
    rej = next(x for x in s if x["label"] == "rejection")
    assert rej["stage"] == "rejected" and rej["status"] == "pending"
    assert c.post(f"/api/inbox/suggestions/{rej['id']}/accept", headers=H).status_code == 200
    assert stage_of(A, ACME["source_id"]) == "rejected"
    row = c.get("/api/inbox/messages?label=rejected", headers=H).get_json()["messages"][0]
    assert c.get(f"/api/inbox/messages/{row['id']}", headers=H).get_json()["message"]["application"]["status"] == "rejected"


def test_a_rejection_older_than_a_later_positive_stage_suggests_nothing(A, monkeypatch):
    c = A.app.test_client()
    put(A, ACME, "interviewing")                                   # moved just now
    old = int(time.time()) - 7 * 86400
    b = FakeBroker([mail(1, "Your application to Acme Robotics", REJECTED_TEXT, at=old)])
    wire(A, monkeypatch, b)
    c.post("/api/inbox/sync", headers=H)
    row = c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]
    assert row["label"] == "rejected" and row["job_id"] == ACME["source_id"]    # still labelled and linked
    assert c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"] == []
    assert stage_of(A, ACME["source_id"]) == "interviewing"
    # A rejection that arrives AFTER the interview stage is news, and is suggested.
    b.held = [mail(2, "An update from Acme Robotics", REJECTED_TEXT, at=int(time.time()) + 5)]
    c.post("/api/inbox/sync", headers=H)
    s = c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"]
    assert [(x["label"], x["stage"]) for x in s] == [("rejection", "rejected")]


def test_a_label_the_person_set_survives_and_unmatched_mail_touches_no_application(A, monkeypatch):
    c = A.app.test_client()
    put(A, ACME, "applied")
    b = FakeBroker([mail(1, "Weekly digest", "Five new roles near Boston.", sender="news@jobboard.example", name="Job Board")])
    wire(A, monkeypatch, b)
    c.post("/api/inbox/sync", headers=H)
    row = c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]
    assert row["label"] == "other" and row["job_id"] is None and row["company"] == ""
    c.post(f"/api/inbox/messages/{row['id']}", json={"label": "action"}, headers=H)
    A._mailbox_file([row["id"]])                                   # the automatic pass runs again
    assert c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]["label"] == "action"
    assert stage_of(A, ACME["source_id"]) == "applied"
    assert c.get("/api/inbox/suggestions", headers=H).get_json()["suggestions"] == []


def test_mail_stored_by_a_sync_that_then_failed_is_still_filed_next_time(A, monkeypatch):
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT)])
    wire(A, monkeypatch, b)
    A._mailbox().add_page(b.held, 1, ALIAS)                        # committed, then the run died
    b.down = True
    c = A.app.test_client()
    out = c.post("/api/inbox/sync", headers=H).get_json()
    assert out["synced"] is False and out["new"] == 1
    assert c.get("/api/inbox/messages", headers=H).get_json()["messages"][0]["label"] == "received"


# ---- the notice setting ---------------------------------------------------------------------

def test_notify_mode_passes_through_to_the_broker(A, monkeypatch):
    b = FakeBroker()
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    assert c.get("/api/inbox/notify", headers=H).get_json() == {"ok": True, "mode": "forward"}
    assert c.post("/api/inbox/notify", json={"mode": "notice"}, headers=H).get_json() == {"ok": True, "mode": "notice"}
    assert ("POST", "/mail/alias/notify", {"mode": "notice"}) in b.calls
    assert c.post("/api/inbox/notify", json={"mode": "loud"}, headers=H).status_code == 400
    b.down = True                                                  # offline: the last known answer
    assert c.get("/api/inbox/notify", headers=H).get_json() == {"ok": True, "mode": "notice"}


def test_erase_removes_the_mailbox_file_and_export_includes_it(A, monkeypatch):
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT, html="<p>x</p>")])
    wire(A, monkeypatch, b)
    c = A.app.test_client()
    c.post("/api/inbox/sync", headers=H)
    exported = A._brain_export()["mailbox"]
    assert [m["subject"] for m in exported] == ["Thank you for applying to Acme Robotics"] and "html" not in exported[0]
    assert A._mailbox_path().exists()
    gone = A._erase_brain_and_answers()
    assert "mailbox.db" in gone and not A._mailbox_path().exists()
    assert c.get("/api/inbox/messages", headers=H).get_json()["count"] == 0


# ---- what the Inbox screen added: one application's mail, and sample mail for a dev session ----
def test_job_id_keeps_only_that_applications_mail(A, monkeypatch):
    c = A.app.test_client()
    put(A, ACME, "saved")
    b = FakeBroker([mail(1, "Thank you for applying to Acme Robotics", RECEIVED_TEXT),
                    mail(2, "Weekly digest", "Five new roles near Boston.", sender="news@jobboard.example", name="Job Board")])
    wire(A, monkeypatch, b)
    c.post("/api/inbox/sync", headers=H)
    d = c.get("/api/inbox/messages?job_id=" + ACME["source_id"], headers=H).get_json()
    assert d["count"] == 1 and [m["job_id"] for m in d["messages"]] == [ACME["source_id"]]
    assert d["unread"] == 2                                        # the badge still counts the whole mailbox
    assert c.get("/api/inbox/messages?job_id=careers::nobody", headers=H).get_json()["count"] == 0
    assert c.get("/api/inbox/messages", headers=H).get_json()["count"] == 2


def test_dev_mail_goes_into_the_local_mailbox_only_in_a_dev_session(A, monkeypatch):
    c = A.app.test_client()
    body = {"from": "jobs@acmerobotics.example", "name": "Acme Robotics Recruiting",
            "subject": "Interview with Acme Robotics", "text": INTERVIEW_TEXT,
            "html": "<p>Hello <b>Zoe</b></p><script>alert(1)</script>", "attachment_names": ["agenda.pdf"]}
    monkeypatch.delenv("RESUME_AGENT_FAKE_MAILBOX", raising=False)
    assert c.post("/api/inbox/dev/mail", json=body).status_code == 404
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    out = c.post("/api/inbox/dev/mail", json=body).get_json()
    assert out["ok"] is True and out["id"]
    d = c.get("/api/inbox/messages", headers=H).get_json()         # no address needed
    assert d["count"] == 1 and d["messages"][0]["label"] == "interview" and d["messages"][0]["has_attachments"] is True
    m = c.get(f"/api/inbox/messages/{out['id']}", headers=H).get_json()["message"]
    assert "<script" not in m["html_safe"] and "Zoe" in m["html_safe"] and m["attachment_names"] == ["agenda.pdf"]
    # a second one is a second message, and the broker's cursor has not moved
    assert c.post("/api/inbox/dev/mail", json={**body, "subject": "Another"}).get_json()["id"] != out["id"]
    assert A._mailbox().cursor("") == 0 and A._mailbox().cursor(ALIAS) == 0
