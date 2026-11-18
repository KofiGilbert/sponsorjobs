"""The local mailbox (inbox/store.py) and its download from the broker (inbox/relay.sync):
stored once per cursor, acknowledged only after the rows are committed, and safe across a crash
between a fetch and its write. A fake broker, no network, made-up mail."""
from __future__ import annotations

import pytest

from inbox.relay import fetch_messages, notify_mode, set_notify_mode, sync
from inbox.store import MailboxStore, snippet_of

ALIAS = "zoe.muller@sponsorjobs.ai"


def msg(cursor, subject="Hello", text="Body text", **kw):
    return {"cursor": cursor, "sender": "hr@acme.example", "sender_name": "Acme Robotics", "subject": subject,
            "text": text, "html": None, "at": 1_800_000_000 + cursor, "has_attachments": False,
            "attachment_names": [], **kw}


class FakeBroker:
    """GET /mail/messages with the Worker's contract: after=N deletes everything up to N."""

    def __init__(self, messages=(), page=25):
        self.held = list(messages)
        self.page = page
        self.calls = []
        self.fail_at = None            # raise on the n-th call (1-based)
        self.mode = "forward"

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail_at == len(self.calls):
            raise OSError("network down")
        if path.startswith("/mail/messages"):
            after = int(path.split("after=")[1])
            self.held = [m for m in self.held if m["cursor"] > after]
            out = self.held[: self.page]
            return {"messages": out, "cursor": out[-1]["cursor"] if out else after, "more": len(self.held) > len(out)}
        if path == "/mail/alias" and method == "GET":
            return {"alias": ALIAS, "forward_to": "zoe@example.com", "notify_mode": self.mode}
        if path == "/mail/alias/notify":
            if body["mode"] not in ("forward", "notice", "off"):
                return {"error": "bad_mode", "status": 400}
            self.mode = body["mode"]
            return {"alias": ALIAS, "forward_to": "zoe@example.com", "notify_mode": self.mode}
        return {"error": "no such route", "status": 404}

    def afters(self):
        return [int(p.split("after=")[1]) for _m, p, _b in self.calls if p.startswith("/mail/messages")]


@pytest.fixture
def store(tmp_path):
    s = MailboxStore(tmp_path / "data" / "mailbox.db")
    yield s
    s.close()


# ---- the store ------------------------------------------------------------------------------

def test_a_page_is_stored_once_per_cursor(store):
    new = store.add_page([msg(5), msg(6)], 6, ALIAS)
    assert len(new) == 2 and store.cursor(ALIAS) == 6
    again = store.add_page([msg(5, subject="changed"), msg(6), msg(7)], 7, ALIAS)
    assert len(again) == 1                                   # only cursor 7 is new
    rows, total = store.list()
    assert total == 3 and [r["subject"] for r in rows] == ["Hello", "Hello", "Hello"]
    assert store.cursor(ALIAS) == 7
    store.add_page([], 3, ALIAS)                             # a cursor never moves backwards
    assert store.cursor(ALIAS) == 7


def test_the_cursor_is_kept_per_alias(store):
    store.add_page([msg(40)], 40, ALIAS)
    assert store.cursor("other.person@sponsorjobs.ai") == 0   # never acknowledges another account's mail
    assert store.cursor(ALIAS) == 40


def test_list_filters_search_paging_and_counts(store):
    store.add_page([msg(1, "Thank you for applying", "We received your application"),
                    msg(2, "Interview", "Pick a time", sender_name="Corvid"),
                    msg(3, "Update", "Unfortunately not", has_attachments=True, attachment_names=["a.pdf"])], 3, ALIAS)
    ids = {r["subject"]: r["id"] for r in store.list()[0]}
    store.set_auto(ids["Thank you for applying"], label="received")
    store.set_auto(ids["Interview"], label="interview")
    store.set_auto(ids["Update"], label="rejected")
    rows, total = store.list()
    assert [r["subject"] for r in rows] == ["Update", "Interview", "Thank you for applying"]   # newest first
    assert total == 3 and rows[0]["has_attachments"] is True and rows[0]["read"] is False
    assert store.list(label="interview")[1] == 1
    assert [r["subject"] for r in store.list(q="corvid")[0]] == ["Interview"]
    assert [r["subject"] for r in store.list(q="received your")[0]] == ["Thank you for applying"]
    assert store.list(q="100%_")[1] == 0                      # LIKE wildcards in a search are literal
    assert [r["subject"] for r in store.list(per_page=2, page=2)[0]] == ["Thank you for applying"]
    store.update(ids["Interview"], read=True)
    assert store.unread_count() == 2 and store.list(unread=True)[1] == 2
    store.update(ids["Update"], archived=True)
    assert store.list()[1] == 2 and store.list(archived=True)[1] == 1
    assert store.label_counts() == {"received": 1, "rejected": 0, "interview": 1, "action": 0, "other": 0}
    assert store.unread_count() == 1                          # archived mail is not counted as unread
    assert store.mark_all_read() == 2 and store.unread_count() == 0


def test_a_label_the_person_set_is_never_replaced_by_an_automatic_one(store):
    mid = store.add_page([msg(1)], 1, ALIAS)[0]
    store.set_auto(mid, label="received")
    assert store.get(mid)["label"] == "received" and store.get(mid)["label_source"] == "auto"
    store.update(mid, label="interview")
    store.set_auto(mid, label="rejected")
    got = store.get(mid)
    assert got["label"] == "interview" and got["label_source"] == "person"
    with pytest.raises(ValueError):
        store.update(mid, label="spam")


def test_delete_wipes_the_content_and_a_resent_page_does_not_bring_it_back(store):
    mid = store.add_page([msg(9, "Secret subject", "Secret body")], 9, ALIAS)[0]
    assert store.delete(mid) is True and store.delete(mid) is False
    assert store.get(mid) is None and store.list()[1] == 0
    raw = store._conn.execute("SELECT subject, text, sender, deleted FROM messages WHERE id=?", (mid,)).fetchone()
    assert (raw["subject"], raw["text"], raw["sender"], raw["deleted"]) == ("", "", "", 1)
    assert store.add_page([msg(9, "Secret subject", "Secret body")], 9, ALIAS) == []
    assert store.list()[1] == 0


def test_matches_and_the_snippet(store):
    mid = store.add_page([msg(1, text="word " * 100)], 1, ALIAS)[0]
    store.set_auto(mid, record_id=12, job_id="careers::acme-de", company="Acme Robotics", role="Data Engineer",
                   matched_by="domain")
    row = store.list()[0][0]
    assert (row["record_id"], row["job_id"], row["company"], row["role"]) == (12, "careers::acme-de", "Acme Robotics", "Data Engineer")
    assert len(row["snippet"]) <= 143 and row["snippet"].endswith("...")
    assert snippet_of("  a \n b  ") == "a b"
    assert store.export()[0]["subject"] == "Hello" and "html" not in store.export()[0]


# ---- sync -----------------------------------------------------------------------------------

def test_sync_pulls_every_page_and_acknowledges_only_what_is_committed(store):
    b = FakeBroker([msg(i) for i in range(1, 8)], page=3)
    out = sync(b, store, ALIAS)
    assert len(out["new"]) == 7 and store.list()[1] == 7
    # Each call acknowledges exactly the cursor committed by the call before it.
    assert b.afters() == [0, 3, 6, 7]
    assert b.held == []                                       # nothing downloaded stays on the broker
    out2 = sync(b, store, ALIAS)
    assert out2["new"] == [] and b.afters()[-1] == 7


def test_a_crash_between_fetch_and_write_loses_nothing(store, monkeypatch):
    b = FakeBroker([msg(1), msg(2), msg(3), msg(4)], page=2)
    real = store.add_page
    calls = {"n": 0}

    def crashing(messages, cursor, alias=""):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power cut")                   # page 2 was fetched, never written
        return real(messages, cursor, alias)

    monkeypatch.setattr(store, "add_page", crashing)
    with pytest.raises(RuntimeError):
        sync(b, store, ALIAS)
    assert store.list()[1] == 2 and store.cursor(ALIAS) == 2
    assert [m["cursor"] for m in b.held] == [3, 4]            # the unwritten page is still on the broker
    monkeypatch.setattr(store, "add_page", real)
    out = sync(b, store, ALIAS)
    assert len(out["new"]) == 2 and store.list()[1] == 4 and b.held == []


def test_a_page_delivered_twice_is_stored_once(store):
    b = FakeBroker([msg(1), msg(2)])
    b.fail_at = 2                                             # the acknowledging call never arrives
    with pytest.raises(OSError):
        sync(b, store, ALIAS)
    assert store.list()[1] == 2 and len(b.held) == 2          # committed here, still held there
    b.fail_at = None
    out = sync(b, store, ALIAS)
    assert out["new"] == [] and store.list()[1] == 2 and b.held == []


def test_a_broker_error_raises_and_stores_nothing(store):
    with pytest.raises(RuntimeError):
        fetch_messages(lambda m, p, body=None: {"error": "no user", "status": 401}, 0)
    with pytest.raises(RuntimeError):
        sync(lambda m, p, body=None: {"error": "http 500", "status": 500}, store, ALIAS)
    assert store.list()[1] == 0


def test_notify_mode_round_trip():
    b = FakeBroker()
    assert notify_mode(b) == {"ok": True, "mode": "forward"}
    assert set_notify_mode(b, "notice") == {"ok": True, "mode": "notice"}
    assert notify_mode(b)["mode"] == "notice"
    assert set_notify_mode(b, "loud")["ok"] is False
    assert notify_mode(lambda m, p, body=None: {"alias": None, "forward_to": None, "notify_mode": None}) == {"ok": True, "mode": None}
