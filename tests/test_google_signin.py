"""Sign in with Google (backend/google_oauth.py + broker routes).

Offline: the Google token exchange is injected, so the consent URL, signed-state CSRF check, code
exchange, account upsert, and the loopback redirect back to the app are all verified without
touching Google.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

from backend.accounts import InMemoryAccountStore
from backend.broker import create_app
from backend.google_oauth import GoogleOAuth, email_from_id_token
from backend.metering import InMemoryStore, Meter


def _id_token(email: str, verified: bool = True) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email, "email_verified": verified}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _fake_http(email):
    def http(url, data):
        assert url == "https://oauth2.googleapis.com/token"
        assert data["grant_type"] == "authorization_code" and data["code"]
        return {"id_token": _id_token(email)}
    return http


# -- helper unit -----------------------------------------------------------------------------------
def test_email_from_id_token_reads_a_verified_email():
    assert email_from_id_token(_id_token("Kofi@Example.com")) == "kofi@example.com"
    assert email_from_id_token(_id_token("x@y.com", verified=False)) is None
    assert email_from_id_token("garbage") is None


def test_signed_state_roundtrips_and_rejects_tampering():
    g = GoogleOAuth("cid", "secret", "https://b/cb", "statesecret")
    s = g.sign_state()
    assert g.valid_state(s)
    assert not g.valid_state(s + "x") and not g.valid_state("nonce.deadbeef") and not g.valid_state("")


def test_consent_url_has_client_id_redirect_and_state():
    g = GoogleOAuth("my-client", "secret", "https://b/cb", "statesecret")
    q = parse_qs(urlparse(g.consent_url("st8")).query)
    assert q["client_id"] == ["my-client"] and q["redirect_uri"] == ["https://b/cb"]
    assert q["response_type"] == ["code"] and q["state"] == ["st8"] and "email" in q["scope"][0]


def test_exchange_code_returns_the_verified_email():
    g = GoogleOAuth("cid", "secret", "https://b/cb", "statesecret", http=_fake_http("a@b.com"))
    assert g.exchange_code("auth-code") == "a@b.com"


# -- account upsert --------------------------------------------------------------------------------
def test_upsert_google_creates_then_reuses_the_same_account():
    s = InMemoryAccountStore()
    acct1, tok1 = s.upsert_google("kofi@x.com")
    assert s.resolve(tok1) == acct1 and s.email_of(acct1) == "kofi@x.com"
    acct2, tok2 = s.upsert_google("kofi@x.com")          # same email -> same account, new token
    assert acct2 == acct1 and tok2 != tok1


# -- broker routes ---------------------------------------------------------------------------------
def _app(http):
    accounts = InMemoryAccountStore()
    google = GoogleOAuth("cid", "secret", "https://broker/account/google/callback",
                         "statesecret", http=http)
    app = create_app(meter=Meter(InMemoryStore()), period_fn=lambda: "2026-07",
                     accounts=accounts, google=google, app_url="http://127.0.0.1:57000")
    app.config.update(TESTING=True)
    return app, accounts, google


def test_start_redirects_to_google_consent():
    app, _, _ = _app(_fake_http("a@b.com"))
    r = app.test_client().get("/account/google/start")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://accounts.google.com/")


def test_callback_signs_in_and_redirects_to_the_app_with_a_working_token():
    app, accounts, google = _app(_fake_http("kofi@gmail.com"))
    state = google.sign_state()
    r = app.test_client().get(f"/account/google/callback?state={state}&code=abc")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith("http://127.0.0.1:57000/?account=")
    token = parse_qs(urlparse(loc).query)["account"][0]
    assert accounts.resolve(token) is not None                     # a usable account token
    assert accounts.email_of(accounts.resolve(token)) == "kofi@gmail.com"


def test_callback_rejects_a_forged_state():
    app, _, _ = _app(_fake_http("a@b.com"))
    r = app.test_client().get("/account/google/callback?state=forged.deadbeef&code=abc")
    assert r.status_code == 302 and r.headers["Location"].endswith("/?signin=error")


def test_routes_503_when_google_not_configured():
    app = create_app(period_fn=lambda: "2026-07", accounts=InMemoryAccountStore())   # no google
    app.config.update(TESTING=True)
    assert app.test_client().get("/account/google/start").status_code == 503
    assert app.test_client().get("/account/google/callback?state=x").status_code == 503
