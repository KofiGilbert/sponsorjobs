"""Email / inbox module — read-first classification, verification links, recruiter-reply
drafting, and the scan endpoint (CLAUDE.md §5).

Fully offline: messages are plain dicts, drafting uses the deterministic FakeLLM, and the
Gmail adapter is exercised only through injected fakes — no live mailbox, no Google libs.
"""

from __future__ import annotations

import ui.app as app
from inbox import detect, gmail
from inbox.service import draft_reply, scan_inbox
from intake.memory import ConversationMemory
from llm.base import FakeLLM

# --- sample messages --------------------------------------------------------- #

VERIFY_MSG = {
    "id": "m1",
    "from": "Greenhouse <no-reply@greenhouse.io>",
    "subject": "Please verify your email to complete your application",
    "body": "Welcome! Confirm your email to finish applying:\n"
            "https://boards.greenhouse.io/confirm?token=abc123\nThanks.",
    "date": "Mon, 14 Jul 2026 09:00:00 -0500",
}

RECRUITER_MSG = {
    "id": "m2",
    "from": "Dana Lopez <dana.lopez@acme.com>",
    "subject": "Next steps on your application",
    "body": "Hi! We reviewed your application and would love to schedule a phone screen. "
            "What is your availability next week?",
    "date": "Mon, 14 Jul 2026 10:00:00 -0500",
}

NEWSLETTER_MSG = {
    "id": "m3",
    "from": "Jobs Digest <newsletter@jobs.example>",
    "subject": "10 new jobs this week",
    "body": "Here are some roles you might like. Unsubscribe any time.",
    "date": "Mon, 14 Jul 2026 08:00:00 -0500",
}


# --- classification & extraction --------------------------------------------- #

def test_classify_verification():
    assert detect.classify(VERIFY_MSG) == "verification"


def test_classify_recruiter():
    assert detect.classify(RECRUITER_MSG) == "recruiter"


def test_classify_noise_is_other():
    assert detect.classify(NEWSLETTER_MSG) == "other"


def test_verification_needs_a_link():
    # Same verify wording but no link at all -> not actionable as a verification.
    no_link = {**VERIFY_MSG, "body": "Please verify your email in the portal."}
    assert detect.classify(no_link) == "other"


def test_noreply_is_never_a_recruiter():
    # Recruiter-ish wording from an automated sender must not become a 'recruiter'.
    automated = {**RECRUITER_MSG, "from": "noreply@acme.com"}
    assert detect.classify(automated) != "recruiter"


def test_verification_link_prefers_the_confirm_url():
    msg = {**VERIFY_MSG,
           "body": "News: https://acme.com/blog\nConfirm: https://acme.com/confirm?token=x\n"}
    assert detect.verification_link(msg) == "https://acme.com/confirm?token=x"


def test_verification_link_trims_trailing_punctuation():
    msg = {**VERIFY_MSG, "body": "Verify here: https://acme.com/verify?t=9)."}
    assert detect.verification_link(msg) == "https://acme.com/verify?t=9"


def test_sender_name_pulls_the_display_name():
    assert detect.sender_name(RECRUITER_MSG) == "Dana Lopez"
    assert detect.sender_name({"from": "plain@acme.com"}) == "plain@acme.com"


# --- scan orchestration ------------------------------------------------------ #

def test_scan_splits_and_drafts():
    profile = {"identity": {"name": "Sam Rivera"},
               "skills": ["Python", "SQL"], "experience": []}
    out = scan_inbox([VERIFY_MSG, RECRUITER_MSG, NEWSLETTER_MSG],
                     profile=profile, llm=FakeLLM())
    assert out["scanned"] == 3 and out["other"] == 1
    assert len(out["verifications"]) == 1 and len(out["recruiters"]) == 1
    v = out["verifications"][0]
    assert v["link"] == "https://boards.greenhouse.io/confirm?token=abc123"
    r = out["recruiters"][0]
    assert r["sender"] == "Dana Lopez"
    assert "Sam Rivera" in r["reply"] and r["reply"].strip()   # drafted from profile


def test_scan_without_llm_leaves_reply_blank():
    out = scan_inbox([RECRUITER_MSG], profile={}, llm=None)
    assert out["recruiters"][0]["reply"] == ""


def test_draft_failure_never_aborts_scan():
    class Boom:
        def draft_email_reply(self, *a, **k):
            raise RuntimeError("model down")
    out = scan_inbox([RECRUITER_MSG], profile={}, llm=Boom())
    assert out["recruiters"][0]["reply"] == ""     # swallowed, message still surfaced
    assert out["scanned"] == 1


def test_draft_reply_addresses_the_sender():
    reply = draft_reply(RECRUITER_MSG, {"identity": {"name": "Sam Rivera"}}, FakeLLM())
    assert reply.startswith("Hi Dana,")
    assert reply.rstrip().endswith("Sam Rivera")


# --- Gmail adapter (injected fakes, no Google libs) -------------------------- #

def test_parse_message_decodes_plaintext_body():
    import base64
    raw = {
        "id": "x1", "threadId": "t1", "snippet": "fallback",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": "a@b.com"},
                        {"name": "Subject", "value": "Hello"}],
            "body": {"data": base64.urlsafe_b64encode(b"Real body").decode()},
        },
    }
    msg = gmail.parse_message(raw)
    assert msg["from"] == "a@b.com" and msg["subject"] == "Hello"
    assert msg["body"] == "Real body" and msg["thread_id"] == "t1"


def test_parse_message_walks_multipart_for_plaintext():
    import base64
    raw = {
        "id": "x2", "snippet": "snip",
        "payload": {"mimeType": "multipart/alternative", "headers": [],
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<b>hi</b>").decode()}},
                        {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"plain wins").decode()}},
                    ]},
    }
    assert gmail.parse_message(raw)["body"] == "plain wins"


def test_fetch_messages_uses_injected_service():
    class FakeMessages:
        def list(self, **k):
            self._list = {"messages": [{"id": "m1"}, {"id": "m2"}]}
            return self
        def get(self, userId="me", id="", format=""):
            self._get = id
            return self
        def execute(self):
            if hasattr(self, "_list"):
                out, self._list = self._list, None
                del self._list
                return out
            return {"id": self._get, "snippet": f"snippet {self._get}",
                    "payload": {"headers": [{"name": "From", "value": "r@x.com"}]}}
    class FakeUsers:
        def messages(self):
            return FakeMessages()
    class FakeService:
        def users(self):
            return FakeUsers()
    msgs = gmail.fetch_messages(FakeService(), query="q", max_results=5)
    assert [m["id"] for m in msgs] == ["m1", "m2"]
    assert msgs[0]["from"] == "r@x.com"


def test_complete_verification_reports_status():
    ok = gmail.complete_verification("https://x/confirm?t=1", opener=lambda link: 200)
    assert ok["ok"] is True and ok["status"] == 200
    bad = gmail.complete_verification("https://x/confirm?t=1", opener=lambda link: 500)
    assert bad["ok"] is False and bad["status"] == 500
    empty = gmail.complete_verification("", opener=lambda link: 200)
    assert empty["ok"] is False and empty["status"] == 0


def test_complete_verification_swallows_errors():
    def boom(link):
        raise RuntimeError("no network")
    res = gmail.complete_verification("https://x/confirm", opener=boom)
    assert res["ok"] is False and res["link"] == "https://x/confirm"


def test_verification_refuses_a_redirect_into_a_private_host():
    """A public verification link can't 302 into cloud-metadata or this app: every redirect
    hop is re-validated, and an unsafe target is refused, not followed (SSRF defense)."""
    import urllib.error

    import pytest

    from inbox.gmail import _SafeRedirectHandler
    h = _SafeRedirectHandler()
    for bad in ("http://169.254.169.254/latest/meta-data/",
                "http://127.0.0.1:57000/api/lock/status", "http://localhost/x"):
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(None, None, 302, "Found", {}, bad)


# --- scan endpoint ----------------------------------------------------------- #

def _seed_profile(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    app._SESSION.pop("s", None)
    ident = {"name": "Sam Rivera", "email": "sam@example.com"}
    m = ConversationMemory(db)
    m.save("default",
           {"identity": dict(ident), "skills": {"Computing": "Python, SQL"},
            "experience": [{"org": "Acme", "location": "Chicago", "dates": "2021",
                            "roles": [{"title": "Engineer", "dates": "2021",
                                       "bullets": ["Built things."]}]}]},
           {"identity": dict(ident), "education": [], "experience": []},
           [{"role": "user", "content": "hi"}])
    m.close()
    return db


def test_scan_endpoint_classifies_and_drafts(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    _seed_profile(tmp_path, monkeypatch)
    body = {"messages": [VERIFY_MSG, RECRUITER_MSG, NEWSLETTER_MSG]}
    d = app.app.test_client().post("/api/inbox/scan", json=body).get_json()
    assert d["ok"] is True and d["scanned"] == 3 and d["other"] == 1
    assert d["verifications"][0]["link"].endswith("token=abc123")
    assert "Sam Rivera" in d["recruiters"][0]["reply"]


def test_scan_endpoint_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    _seed_profile(tmp_path, monkeypatch)
    r = app.app.test_client().post("/api/inbox/scan", json={"messages": []})
    assert r.headers.get("Cache-Control") == "no-store"


def test_draft_reply_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    _seed_profile(tmp_path, monkeypatch)
    body = {"from": RECRUITER_MSG["from"], "subject": RECRUITER_MSG["subject"],
            "body": RECRUITER_MSG["body"]}
    d = app.app.test_client().post("/api/inbox/draft_reply", json=body).get_json()
    assert d["ok"] is True and "Sam Rivera" in d["reply"]


# --- Gmail OAuth token storage is pluggable (so App Lock can encrypt it) ------ #

def test_token_present_reflects_the_installed_store(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail, "_TOKEN_PATH", tmp_path / "gt.json")
    monkeypatch.setattr(gmail, "_TOKEN_LOAD", None)   # default: file-based
    monkeypatch.setattr(gmail, "_TOKEN_SAVE", None)
    assert gmail.token_present() is False              # no file, no store
    gmail.set_token_store(lambda: '{"token":"x"}', lambda s: None)
    assert gmail.token_present() is True               # store holds a token
    gmail.set_token_store(lambda: None, lambda s: None)
    assert gmail.token_present() is False


def test_build_service_reads_the_token_from_the_injected_store(monkeypatch):
    """A valid token from the store builds a service without touching any file."""
    monkeypatch.setattr(gmail, "_TOKEN_LOAD", None)
    monkeypatch.setattr(gmail, "_TOKEN_SAVE", None)
    used: dict = {}
    gmail.set_token_store(lambda: '{"token":"t","refresh_token":"r"}',
                          lambda s: used.__setitem__("saved", s))

    class FakeCreds:
        valid = True
        @staticmethod
        def from_authorized_user_info(info, scopes):
            used["info"] = info
            return FakeCreds()

    monkeypatch.setattr(gmail, "_require_google",
                        lambda: (object(), FakeCreds, object(), lambda *a, **k: "SERVICE"))
    assert gmail.build_service() == "SERVICE"
    assert used["info"] == {"token": "t", "refresh_token": "r"}   # loaded from the store
    assert "saved" not in used                                    # valid creds -> no re-save
