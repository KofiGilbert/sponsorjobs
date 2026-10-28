"""The mail alias read through the broker (inbox/relay.py): events become the message dicts the
detectors understand, cursors acknowledge, and nothing older than the run is returned."""
from __future__ import annotations

from inbox import detect
from inbox.relay import RelayMailbox, alias_status, claim_alias, event_message


class FakeBroker:
    def __init__(self):
        self.calls = []
        self.events = []
        self.alias = None

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/mail/alias" and method == "GET":
            return {"alias": self.alias, "forward_to": "kofi@example.com" if self.alias else None}
        if path == "/mail/alias" and method == "POST":
            self.alias = self.alias or "kofi.gilbert@sponsorjobs.ai"
            return {"alias": self.alias, "forward_to": "kofi@example.com"}
        if path.startswith("/mail/inbox"):
            after = int(path.split("after=")[1])
            evs = [e for e in self.events if e["cursor"] > after]
            return {"events": evs, "cursor": evs[-1]["cursor"] if evs else after}
        return {"error": "no such route", "status": 404}


def test_alias_claim_and_status_round_trip():
    b = FakeBroker()
    assert alias_status(b)["alias"] is None
    r = claim_alias(b, "Kofi Gilbert")
    assert r["ok"] and r["alias"].endswith("@sponsorjobs.ai")
    assert alias_status(b)["alias"] == r["alias"]
    assert b.calls[1] == ("POST", "/mail/alias", {"name": "Kofi Gilbert"})


def test_events_become_messages_the_detectors_read():
    code = event_message({"cursor": 7, "kind": "code", "code": "482913", "subject": "Acme: your code",
                          "sender": "no-reply@acme.com", "at": 1_800_000_000})
    assert detect.otp_code(code) == "482913"
    assert detect.kind_of(code) == "code"
    reset = event_message({"cursor": 8, "kind": "reset", "link": "https://acme.com/reset?t=1",
                           "subject": "Reset your password", "sender": "no-reply@acme.com", "at": 1_800_000_000})
    assert detect.password_reset_link(reset) == "https://acme.com/reset?t=1"
    verify = event_message({"cursor": 9, "kind": "verify", "link": "https://acme.com/verify?t=2",
                            "subject": "Verify your email", "sender": "no-reply@acme.com", "at": 1_800_000_000})
    assert detect.verification_link(verify) == "https://acme.com/verify?t=2"


def test_relay_mailbox_acknowledges_with_the_cursor_and_drops_old_events():
    b = FakeBroker()
    b.events = [{"cursor": 1, "kind": "code", "code": "111111", "subject": "old", "sender": "x@acme.com", "at": 100},
                {"cursor": 2, "kind": "code", "code": "222222", "subject": "new", "sender": "x@acme.com", "at": 5000}]
    m = RelayMailbox(b, alias="kofi.gilbert@sponsorjobs.ai")
    rows = m.search(since_epoch=1000)
    assert [r["subject"] for r in rows] == ["new"]          # the old one is before the run
    assert not any(k.startswith("_") for k in rows[0])
    m.search(since_epoch=1000)
    assert b.calls[-1][1] == "/mail/inbox?after=2"          # acknowledged up to the last cursor
    b.events.append({"cursor": 3, "kind": "verify", "link": "https://acme.com/v", "subject": "verify", "sender": "x@acme.com", "at": 6000})
    rows = m.search(since_epoch=1000)
    assert [r["subject"] for r in rows] == ["verify", "new"]   # newest first, earlier ones kept


def test_relay_mailbox_raises_on_a_broker_error_so_await_reports_it():
    def broken(method, path, body=None):
        return {"error": "unauthorized", "status": 401}
    m = RelayMailbox(broken)
    try:
        m.search(0)
    except RuntimeError as e:
        assert "unauthorized" in str(e)
    else:
        raise AssertionError("expected a RuntimeError")
