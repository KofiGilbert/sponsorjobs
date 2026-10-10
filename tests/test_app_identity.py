"""App-side account identity (ui/app.py): the app authenticates to the broker as its OWN account.

Offline: the broker's /account/register is mocked. Verifies the app reuses a stored token, registers
an anonymous account once when none exists, and falls back to the dev header when the broker can't
be reached (so dev/offline still works).
"""

from __future__ import annotations

import importlib

import pytest
from llm.broker_client import BROKER_USER_AGENT  # Cloudflare 403s Python's default agent


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    import ui.app as A
    importlib.reload(A)
    return A


def test_broker_headers_uses_a_stored_bearer_token(A, monkeypatch):
    monkeypatch.setattr(A, "_cred", lambda name: "tok_stored" if name == "TAILOR_ACCOUNT_TOKEN" else None)
    assert A._broker_headers() == {"Authorization": "Bearer tok_stored", "User-Agent": BROKER_USER_AGENT}


def test_account_token_registers_once_and_saves_it(A, monkeypatch):
    saved = {}
    monkeypatch.setattr(A, "_cred", lambda name: None)                    # nothing stored yet
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))

    class Resp:
        status_code = 200
        def json(self): return {"account_id": "acct_1", "token": "tok_new"}

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    assert A._account_token() == "tok_new"
    assert saved["TAILOR_ACCOUNT_TOKEN"] == "tok_new"                     # persisted for next time


def test_falls_back_to_the_dev_header_when_the_broker_is_unreachable(A, monkeypatch):
    monkeypatch.setattr(A, "_cred", lambda name: None)                    # no token, forced to register
    import requests

    def boom(*a, **k):
        raise requests.RequestException("broker down")

    monkeypatch.setattr(requests, "post", boom)
    assert A._broker_headers() == {"X-Tailor-User": "local", "User-Agent": BROKER_USER_AGENT}             # dev/offline still works


def _client(A):
    A.app.config.update(TESTING=True)
    return A.app.test_client()


def test_status_reports_the_email_from_the_broker(A, monkeypatch):
    monkeypatch.setattr(A, "_broker_get", lambda p: (200, {"account_id": "acct_1", "email": "u@b.com"}))
    assert _client(A).get("/api/account/status").get_json()["email"] == "u@b.com"


def test_password_routes_are_gone(A):
    """No passwords anywhere: sign-in is email code or Google only."""
    c = _client(A)
    for path in ("/api/account/claim", "/api/account/login"):
        assert c.post(path, json={"email": "u@b.com", "password": "longenough1"}).status_code in (404, 405)


def test_google_url_points_the_broker_back_at_the_app_callback(A):
    from urllib.parse import parse_qs, urlparse
    url = _client(A).get("/api/account/google/url?via=tab").get_json()["url"]
    u = urlparse(url)
    assert u.path.endswith("/account/google/start")
    redirect = parse_qs(u.query)["redirect"][0]
    assert redirect.endswith("/account/google/done?via=tab")


def test_google_done_page_reads_the_token_from_the_fragment(A):
    r = _client(A).get("/account/google/done?via=browser")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "location.hash" in body and "/api/account/adopt" in body


def test_adopt_saves_a_token_that_the_broker_verifies(A, monkeypatch):
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    r = _client(A).post("/api/account/adopt", json={"token": "tok_google"})
    assert r.status_code == 200 and saved["TAILOR_ACCOUNT_TOKEN"] == "tok_google"


def test_adopt_rejects_a_token_the_broker_does_not_accept(A, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {"status_code": 401})())
    assert _client(A).post("/api/account/adopt", json={"token": "bad"}).status_code == 400
