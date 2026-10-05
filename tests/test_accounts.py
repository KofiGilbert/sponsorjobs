"""Per-account identity (backend/accounts.py) + the broker's bearer-token auth.

Offline, no keys. Verifies tokens are stored hashed (never plaintext), a bearer token resolves to
its own account, unknown/absent tokens fall back to the dev header, and two accounts get separate
plans/usage through the broker.
"""

from __future__ import annotations

import pytest

from backend.accounts import (AlreadyClaimed, EmailTaken, InMemoryAccountStore,
                              SqliteAccountStore, bearer_identify)
from backend.broker import _default_identify, create_app
from backend.metering import InMemoryStore, Meter


def test_claim_attaches_an_email_to_an_anonymous_account_keeping_its_id_and_token():
    s = InMemoryAccountStore()
    account_id, token = s.register()                       # anonymous, from try-first
    assert s.email_of(account_id) is None
    s.claim(account_id, "kofi@example.com", "longenough1")
    assert s.email_of(account_id) == "kofi@example.com"
    assert s.resolve(token) == account_id                  # same id + token -> state/sub preserved
    assert s.login("kofi@example.com", "longenough1")[0] == account_id   # can now log in elsewhere


def test_claim_rejects_an_already_claimed_account_or_a_taken_email():
    s = InMemoryAccountStore()
    a1, _ = s.register()
    s.claim(a1, "taken@b.com", "longenough1")
    with pytest.raises(AlreadyClaimed):
        s.claim(a1, "other@b.com", "longenough1")          # a1 already has an email
    a2, _ = s.register()
    with pytest.raises(EmailTaken):
        s.claim(a2, "taken@b.com", "longenough1")          # email belongs to a1


def test_broker_claim_then_me_reflects_the_email():
    app = create_app(meter=Meter(InMemoryStore()), period_fn=lambda: "2026-07",
                     accounts=InMemoryAccountStore())
    app.config.update(TESTING=True)
    c = app.test_client()
    tok = c.post("/account/register").get_json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    assert c.get("/account/me", headers=H).get_json()["email"] is None
    assert c.post("/account/claim", json={"email": "u@b.com", "password": "longenough1"}, headers=H).status_code == 200
    assert c.get("/account/me", headers=H).get_json()["email"] == "u@b.com"
    # claiming again on the same account -> 409
    assert c.post("/account/claim", json={"email": "v@b.com", "password": "longenough1"}, headers=H).status_code == 409


def test_register_then_resolve_roundtrips_and_is_unique():
    s = InMemoryAccountStore()
    a1, t1 = s.register()
    a2, t2 = s.register()
    assert a1 != a2 and t1 != t2
    assert s.resolve(t1) == a1 and s.resolve(t2) == a2
    assert s.resolve("nope") is None and s.resolve("") is None and s.resolve(None) is None


def test_sqlite_store_persists_and_stores_only_hashes(tmp_path):
    db = str(tmp_path / "b.db")
    account_id, token = SqliteAccountStore(db).register()
    # a fresh store over the same file still resolves the token (persisted)
    assert SqliteAccountStore(db).resolve(token) == account_id
    # the raw token must NOT be anywhere in the DB file -- only its hash
    assert token.encode() not in (tmp_path / "b.db").read_bytes()


def test_bearer_identify_prefers_token_then_falls_back_to_header():
    s = InMemoryAccountStore()
    account_id, token = s.register()
    identify = bearer_identify(s, _default_identify)

    class Req:
        def __init__(self, headers): self.headers = headers
    assert identify(Req({"Authorization": f"Bearer {token}"})) == account_id      # token wins
    assert identify(Req({"Authorization": "Bearer wrong"})) is None               # bad token -> fallback (no header)
    assert identify(Req({"X-Tailor-User": "dev"})) == "dev"                       # no token -> header fallback


def test_broker_register_then_two_accounts_have_separate_plans():
    accounts = InMemoryAccountStore()
    meter = Meter(InMemoryStore())
    app = create_app(meter=meter, period_fn=lambda: "2026-07", accounts=accounts)
    app.config.update(TESTING=True)
    c = app.test_client()

    tok_a = c.post("/account/register").get_json()["token"]
    tok_b = c.post("/account/register").get_json()["token"]
    # upgrade only account A
    c.post("/billing/plan", json={"plan": "pass90"}, headers={"Authorization": f"Bearer {tok_a}"})

    ua = c.get("/me/usage", headers={"Authorization": f"Bearer {tok_a}"}).get_json()
    ub = c.get("/me/usage", headers={"Authorization": f"Bearer {tok_b}"}).get_json()
    assert ua["plan"] == "pass90"          # A is on the plan it bought
    assert ub["plan"] == "free"         # B is untouched -> accounts are isolated


def test_register_is_503_when_accounts_not_enabled():
    app = create_app(period_fn=lambda: "2026-07")   # no accounts store
    app.config.update(TESTING=True)
    assert app.test_client().post("/account/register").status_code == 503


# -- email accounts: signup / login / cross-device -----------------------------------------------
def test_signup_then_login_returns_a_token_for_the_same_account():
    s = InMemoryAccountStore()
    acct, tok1 = s.signup("kofi@example.com", "hunter2pass")
    assert s.resolve(tok1) == acct
    # logging in (a "new device") issues a DIFFERENT token that resolves to the SAME account
    acct2, tok2 = s.login("kofi@example.com", "hunter2pass")
    assert acct2 == acct and tok2 != tok1 and s.resolve(tok2) == acct


def test_login_rejects_wrong_password_and_unknown_email():
    s = InMemoryAccountStore()
    s.signup("a@b.com", "correcthorse")
    assert s.login("a@b.com", "wrongwrong") is None
    assert s.login("nobody@b.com", "correcthorse") is None


def test_duplicate_signup_raises_email_taken():
    s = InMemoryAccountStore()
    s.signup("dup@b.com", "password1")
    with pytest.raises(EmailTaken):
        s.signup("dup@b.com", "password2")


def test_sqlite_never_stores_a_raw_password(tmp_path):
    db = str(tmp_path / "b.db")
    SqliteAccountStore(db).signup("secret@b.com", "SuperSecret99")
    assert b"SuperSecret99" not in (tmp_path / "b.db").read_bytes()
    # a fresh store over the same file can still log the person in (persisted + verifiable)
    assert SqliteAccountStore(db).login("secret@b.com", "SuperSecret99") is not None


def _client_with_accounts():
    app = create_app(meter=Meter(InMemoryStore()), period_fn=lambda: "2026-07",
                     accounts=InMemoryAccountStore())
    app.config.update(TESTING=True)
    return app.test_client()


def test_broker_signup_validates_then_issues_a_working_token():
    c = _client_with_accounts()
    assert c.post("/account/signup", json={"email": "x", "password": "short"}).status_code == 400
    ok = c.post("/account/signup", json={"email": "u@b.com", "password": "longenough1"})
    assert ok.status_code == 200
    tok = ok.get_json()["token"]
    assert c.get("/me/usage", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_broker_duplicate_signup_is_409_and_login_flow_works():
    c = _client_with_accounts()
    c.post("/account/signup", json={"email": "dup@b.com", "password": "longenough1"})
    assert c.post("/account/signup", json={"email": "dup@b.com", "password": "longenough1"}).status_code == 409
    assert c.post("/account/login", json={"email": "dup@b.com", "password": "nope"}).status_code == 401
    good = c.post("/account/login", json={"email": "dup@b.com", "password": "longenough1"})
    assert good.status_code == 200 and good.get_json()["token"]
