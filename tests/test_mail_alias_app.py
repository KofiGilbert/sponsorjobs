"""The SponsorJobs address in the app (docs/mail-alias.md): taken through the broker, applied
with everywhere the agent types an e-mail, read by the apply agent before any other mailbox."""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as app_mod
    importlib.reload(app_mod)
    app_mod._set_prefs({"mail_alias": "", "apply_with_alias": True})
    app_mod._RELAY["box"] = None
    monkeypatch.setattr(app_mod, "_account_token", lambda: "tok-1")
    monkeypatch.setattr(app_mod, "_saved_identity", lambda: {"name": "Kofi Test", "email": "kofi@example.com"})
    return app_mod


class FakeBroker:
    def __init__(self, alias="kofi.test@sponsorjobs.ai", fail=None):
        self.alias_taken = None
        self.alias = alias
        self.fail = fail
        self.events = []
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/mail/alias" and method == "POST":
            if self.fail:
                return {"error": self.fail, "status": 409}
            self.alias_taken = self.alias
            return {"alias": self.alias, "forward_to": "kofi@example.com"}
        if path == "/mail/alias" and method == "DELETE":
            self.alias_taken = None
            return {"ok": True}
        if path.startswith("/mail/inbox"):
            return {"events": self.events, "cursor": self.events[-1]["cursor"] if self.events else 0}
        return {"alias": self.alias_taken, "forward_to": "kofi@example.com" if self.alias_taken else None}


def _wire(A, monkeypatch, broker):
    import inbox.relay as relay
    monkeypatch.setattr(relay, "urllib_fetch", lambda url, tok, timeout=15.0: broker)


def test_taking_the_address_saves_it_and_it_becomes_the_application_email(A, monkeypatch):
    broker = FakeBroker()
    _wire(A, monkeypatch, broker)
    c = A.app.test_client()
    assert A._application_email() == "kofi@example.com"
    r = c.post("/api/inbox/alias", json={}, headers={"X-Tailor-Extension": "1"})
    assert r.status_code == 200 and r.get_json()["alias"] == "kofi.test@sponsorjobs.ai"
    assert broker.calls[0] == ("POST", "/mail/alias", {"name": "Kofi Test"})
    assert A._application_email() == "kofi.test@sponsorjobs.ai"
    st = c.get("/api/inbox/status", headers={"X-Tailor-Extension": "1"}).get_json()
    assert st["method"] == "alias" and st["email"] == "kofi.test@sponsorjobs.ai" and st["configured"]
    auto = c.get("/api/profile/autofill", headers={"X-Tailor-Extension": "1"}).get_json()
    assert auto["fields"]["email"] == "kofi.test@sponsorjobs.ai"
    r = c.delete("/api/inbox/alias", headers={"X-Tailor-Extension": "1"})
    assert r.get_json()["ok"] and A._application_email() == "kofi@example.com"
    assert broker.alias_taken is None


def test_taking_the_address_needs_a_signed_in_account(A, monkeypatch):
    _wire(A, monkeypatch, FakeBroker(fail="sign_in_first"))
    r = A.app.test_client().post("/api/inbox/alias", json={}, headers={"X-Tailor-Extension": "1"})
    assert r.status_code == 400 and "Sign in" in r.get_json()["error"]
    assert A._application_email() == "kofi@example.com"


def test_the_agent_reads_the_alias_before_any_other_mailbox(A, monkeypatch):
    broker = FakeBroker()
    _wire(A, monkeypatch, broker)
    c = A.app.test_client()
    c.post("/api/inbox/alias", json={}, headers={"X-Tailor-Extension": "1"})
    A._save_cred("IMAP_EMAIL", "kofi@gmail.com"); A._save_cred("IMAP_PASSWORD", "x" * 16)
    box = A._current_mailbox()
    assert getattr(box, "name", "") == "alias"
    broker.events = [{"cursor": 1, "kind": "code", "code": "482913", "subject": "Acme: your code",
                      "sender": "no-reply@acme.com", "at": 9_999_999_999}]
    r = c.post("/api/inbox/await", json={"kind": "code", "host": "acme.com", "company": "Acme", "since": 1, "timeout_s": 5},
               headers={"X-Tailor-Extension": "1"})
    d = r.get_json()
    assert d["ok"] and d["code"] == "482913"


def test_portal_accounts_are_created_with_the_alias(A, monkeypatch):
    broker = FakeBroker()
    _wire(A, monkeypatch, broker)
    c = A.app.test_client()
    c.post("/api/inbox/alias", json={}, headers={"X-Tailor-Extension": "1"})
    r = c.post("/api/autoapply/agent/portal-account", json={"host": "acme.myworkdayjobs.com", "create": True},
               headers={"X-Tailor-Extension": "1"})
    assert r.get_json()["ok"] and r.get_json()["email"] == "kofi.test@sponsorjobs.ai"


def test_sign_proxy_returns_the_three_headers_or_fails_open(A, monkeypatch):
    seen = {}

    def broker(method, path, body=None):
        seen["req"] = (method, path, body)
        return {"signature_input": 'sig2=("@authority" "signature-agent");created=1;expires=61;keyid="k";alg="ed25519";tag="web-bot-auth"',
                "signature": "sig2=:AAAA:", "signature_agent": '"https://sponsorjobs.ai"'}
    import inbox.relay as relay
    monkeypatch.setattr(relay, "urllib_fetch", lambda url, tok, timeout=15.0: broker)
    c = A.app.test_client()
    r = c.post("/api/autoapply/agent/sign", json={"method": "GET", "authority": "Acme.MyWorkdayJobs.com", "path": "/en-US/x?y=1"},
               headers={"X-Tailor-Extension": "1"}).get_json()
    assert r["ok"] and r["signature"] == "sig2=:AAAA:" and "web-bot-auth" in r["signature_input"]
    assert seen["req"] == ("POST", "/agent/sign", {"method": "GET", "authority": "acme.myworkdayjobs.com", "path": "/en-US/x?y=1"})
    monkeypatch.setattr(relay, "urllib_fetch", lambda url, tok, timeout=15.0: (lambda m, p, b=None: {"error": "not_configured", "status": 503}))
    r = c.post("/api/autoapply/agent/sign", json={"method": "GET", "authority": "acme.com", "path": "/"},
               headers={"X-Tailor-Extension": "1"}).get_json()
    assert r["ok"] is False and r["reason"] == "not_configured"     # unsigned, never blocked
