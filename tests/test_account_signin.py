"""App-side email sign-in, sign-out and the sign-in-required checkout (ui/app.py).

Offline: the broker is replaced by monkeypatching _broker_post/_broker_get with the exact contract
the broker exposes (POST /account/email/start, POST /account/email/verify, GET /account/me, and a
401 {reason: "sign_in_required"} from pass and pack checkout when the account has no email).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A.app.config.update(TESTING=True)
    return A


class Broker:
    """Records calls; answers from a {path: (status, json)} table."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def post(self, path, body):
        self.calls.append((path, body))
        return self.table.get(path, (404, None))

    def get(self, path):
        self.calls.append((path, None))
        return self.table.get(path, (404, None))


def _wire(A, monkeypatch, table):
    b = Broker(table)
    monkeypatch.setattr(A, "_broker_post", b.post)
    monkeypatch.setattr(A, "_broker_get", b.get)
    return b


def test_start_sends_the_code(A, monkeypatch):
    b = _wire(A, monkeypatch, {"/account/email/start": (200, {"sent": True})})
    r = A.app.test_client().post("/api/account/email/start", json={"email": " kofi@example.com "})
    assert r.status_code == 200 and r.get_json()["sent"] is True
    assert b.calls == [("/account/email/start", {"email": "kofi@example.com"})]


def test_start_rejects_a_bad_email_before_the_broker(A, monkeypatch):
    b = _wire(A, monkeypatch, {})
    r = A.app.test_client().post("/api/account/email/start", json={"email": "not-an-email"})
    assert r.status_code == 400 and r.get_json()["error_code"] == "bad_email"
    assert b.calls == []


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (503, "off"), (400, "bad_email"),
                                         (0, "unreachable")])
def test_start_maps_broker_failures(A, monkeypatch, status, code):
    _wire(A, monkeypatch, {"/account/email/start": (status, {"reason": "rate_limited"} if status == 429 else None)})
    r = A.app.test_client().post("/api/account/email/start", json={"email": "a@b.co"})
    body = r.get_json()
    assert body["error_code"] == code and body["error"]
    assert "reason" not in body          # never trips the app's full-screen outage overlay


def test_verify_adopts_the_returned_token(A, monkeypatch):
    b = _wire(A, monkeypatch, {"/account/email/verify": (200, {"token": "tok_email", "account_id": "acct_9",
                                                              "email": "kofi@example.com"})})
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    r = A.app.test_client().post("/api/account/email/verify",
                                 json={"email": "kofi@example.com", "code": "123 456"})
    assert r.status_code == 200 and r.get_json()["email"] == "kofi@example.com"
    assert saved == {"TAILOR_ACCOUNT_TOKEN": "tok_email"}            # the device is now that account
    assert b.calls == [("/account/email/verify", {"email": "kofi@example.com", "code": "123456"})]


@pytest.mark.parametrize("reason", ["bad_code", "expired"])
def test_verify_bad_or_expired_code_keeps_the_device_token(A, monkeypatch, reason):
    _wire(A, monkeypatch, {"/account/email/verify": (400, {"reason": reason})})
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    r = A.app.test_client().post("/api/account/email/verify", json={"email": "a@b.co", "code": "000000"})
    assert r.status_code == 400 and r.get_json()["error_code"] == reason
    assert saved == {}


def test_verify_needs_six_digits(A, monkeypatch):
    b = _wire(A, monkeypatch, {})
    r = A.app.test_client().post("/api/account/email/verify", json={"email": "a@b.co", "code": "12"})
    assert r.status_code == 400 and r.get_json()["error_code"] == "bad_code" and b.calls == []


def test_signout_forgets_the_token_and_registers_a_fresh_anonymous_account(A, monkeypatch):
    revoked, registered = [], []
    monkeypatch.setattr(A, "_revoke_cred", lambda k: revoked.append(k))
    monkeypatch.setattr(A, "_account_token", lambda: registered.append(1) or "tok_anon")
    r = A.app.test_client().post("/api/account/signout", json={})
    assert r.status_code == 200 and r.get_json()["email"] is None
    assert revoked == ["TAILOR_ACCOUNT_TOKEN"] and registered == [1]


def test_status_shows_the_email_and_plan(A, monkeypatch):
    _wire(A, monkeypatch, {"/account/me": (200, {"account_id": "acct_9", "email": "kofi@example.com"}),
                           "/me/usage": (200, {"tier": "pass30", "pass_until": 1}),})
    s = A.app.test_client().get("/api/account/status").get_json()
    assert s["email"] == "kofi@example.com" and s["plan"] == "pass30"


def test_status_signed_out_has_no_email(A, monkeypatch):
    _wire(A, monkeypatch, {"/account/me": (200, {"account_id": "acct_1", "email": None}),
                           "/me/usage": (200, {"tier": "free"})})
    s = A.app.test_client().get("/api/account/status").get_json()
    assert s["email"] is None and s["plan"] == "free"


def test_pass_checkout_401_surfaces_sign_in_required(A, monkeypatch):
    _wire(A, monkeypatch, {"/billing/passes/pass30/checkout": (401, {"reason": "sign_in_required"})})
    r = A.app.test_client().post("/api/billing/checkout", json={"pass": "pass30"})
    assert r.status_code == 401 and r.get_json()["reason"] == "sign_in_required"


def test_pack_checkout_401_surfaces_sign_in_required(A, monkeypatch):
    _wire(A, monkeypatch, {"/billing/packs/r2_3/checkout": (401, {"reason": "sign_in_required"})})
    r = A.app.test_client().post("/api/interview/round2/buy", json={"pack": "r2_3"})
    assert r.status_code == 401 and r.get_json()["reason"] == "sign_in_required"


def test_verify_maps_email_off_to_not_available(A, monkeypatch):
    _wire(A, monkeypatch, {"/account/email/verify": (503, {"error": "email sign-in is not configured"})})
    r = A.app.test_client().post("/api/account/email/verify", json={"email": "a@b.co", "code": "123456"})
    assert r.status_code == 503 and r.get_json()["error"] == "Email sign-in isn't available yet."


def test_google_done_page_handles_the_fragment_errors(A):
    body = A.app.test_client().get("/account/google/done?via=tab").get_data(as_text=True)
    assert 'h.get("error") === "cancelled"' in body and "/?signin=" in body
