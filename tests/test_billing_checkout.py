"""The app-side pass checkout proxy (ui/app.py /api/billing/checkout) and offers.

Offline: the managed broker is replaced by monkeypatching _broker_post/_broker_get, so we verify
the app hands the chosen PASS to the broker's one-time checkout, returns the Checkout URL, rejects
anything else (including the retired subscription tiers) before calling the broker, and maps
broker outages to clean statuses, without a real broker or any Stripe key.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


class _FakeBroker:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.calls = status, data, []

    def __call__(self, path, body):
        self.calls.append((path, body))
        return self.status, self.data


def test_checkout_proxies_the_pass_and_returns_the_url(client, monkeypatch):
    A, c = client
    fake = _FakeBroker(200, {"url": "https://checkout.stripe.com/c/pay/cs_test_123"})
    monkeypatch.setattr(A, "_broker_post", fake)
    r = c.post("/api/billing/checkout", json={"pass": "pass90"})
    assert r.status_code == 200
    assert r.get_json()["url"].startswith("https://checkout.stripe.com/")
    assert fake.calls == [("/billing/passes/pass90/checkout", {})]


def test_unknown_pass_and_old_tiers_are_rejected_before_touching_the_broker(client, monkeypatch):
    A, c = client
    fake = _FakeBroker(200, {"url": "x"})
    monkeypatch.setattr(A, "_broker_post", fake)
    for body in ({"pass": "platinum"}, {"tier": "pro"}, {"tier": "student"}, {"pass": "../x"}, {}):
        assert c.post("/api/billing/checkout", json=body).status_code == 400
    assert fake.calls == []                         # never reached the broker


def test_offers_proxy_and_unreachable_fallback(client, monkeypatch):
    A, c = client
    offers = {"plans": [{"id": "student_monthly", "auto_renew": True}], "price_tier": "student",
              "student": {"verified": True, "eligible": True}, "subscription": None,
              "passes": [{"id": "pass30"}], "packs": [], "current": {"tier": "free"}}
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, offers) if path == "/billing/offers" else (404, None))
    assert c.get("/api/billing/offers").get_json() == {**offers, "reachable": True}
    monkeypatch.setattr(A, "_broker_get", lambda path: (0, None))
    assert c.get("/api/billing/offers").get_json() == {
        "plans": [], "price_tier": "standard", "student": None, "subscription": None,
        "passes": [], "packs": [], "current": None, "reachable": False}


def test_broker_unreachable_maps_to_503(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(0, None))
    assert c.post("/api/billing/checkout", json={"pass": "pass30"}).status_code == 503


def test_billing_off_maps_to_503(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(503, {"error": "billing is not configured"}))
    assert c.post("/api/billing/checkout", json={"pass": "pass30"}).status_code == 503


def test_a_broker_failure_maps_to_502(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(500, None))
    assert c.post("/api/billing/checkout", json={"pass": "pass30"}).status_code == 502


# -- plans that renew (2026-11-20) ---------------------------------------------------------------

def test_subscribe_sends_only_the_interval_and_the_consent(client, monkeypatch):
    """The price tier is the broker's decision: whatever else the page sends is dropped."""
    A, c = client
    fake = _FakeBroker(200, {"url": "https://checkout.stripe.com/c/pay/cs_sub", "plan": "standard_monthly"})
    monkeypatch.setattr(A, "_broker_post", fake)
    r = c.post("/api/billing/subscribe", json={"interval": "monthly", "consent": True, "tier": "student",
                                               "plan": "student_monthly"})
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://checkout.stripe.com/")
    assert fake.calls == [("/billing/subscribe", {"interval": "monthly", "consent": True})]


def test_subscribe_needs_a_ticked_consent_and_a_real_interval(client, monkeypatch):
    A, c = client
    fake = _FakeBroker(200, {"url": "x"})
    monkeypatch.setattr(A, "_broker_post", fake)
    for body in ({"interval": "monthly"}, {"interval": "monthly", "consent": "true"},
                 {"interval": "monthly", "consent": 1}, {"interval": "yearly", "consent": True}, {}):
        assert c.post("/api/billing/subscribe", json=body).status_code == 400, body
    assert fake.calls == []
    assert c.post("/api/billing/subscribe", json={"interval": "monthly"}).get_json()["reason"] == "consent_required"


def test_subscribe_maps_the_broker_answers(client, monkeypatch):
    A, c = client
    for status, data, want, reason in [
        (401, {"reason": "sign_in_required"}, 401, "sign_in_required"),
        (409, {"reason": "already_subscribed"}, 409, "already_subscribed"),
        (503, {"error": "billing is not configured"}, 503, None),
        (0, None, 503, None),
        (500, None, 502, None),
    ]:
        monkeypatch.setattr(A, "_broker_post", _FakeBroker(status, data))
        r = c.post("/api/billing/subscribe", json={"interval": "quarterly", "consent": True})
        assert r.status_code == want, status
        assert r.get_json().get("reason") == reason


def test_cancel_resume_and_portal_proxy_the_broker(client, monkeypatch):
    A, c = client
    view = {"subscription": {"plan": "standard_monthly", "renews": False, "ends_at": 1_900_000_000}}
    fake = _FakeBroker(200, view)
    monkeypatch.setattr(A, "_broker_post", fake)
    assert c.post("/api/billing/cancel").get_json() == view
    assert c.post("/api/billing/resume").get_json() == view
    assert c.post("/api/billing/portal").status_code == 200
    assert [p for p, _ in fake.calls] == ["/billing/subscription/cancel", "/billing/subscription/resume", "/billing/portal"]
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(404, {"reason": "no_subscription"}))
    assert c.post("/api/billing/cancel").status_code == 404


def test_student_check_proxies_and_explains_refusals(client, monkeypatch):
    A, c = client
    fake = _FakeBroker(200, {"sent": True, "school_domain": "example.edu"})
    monkeypatch.setattr(A, "_broker_post", fake)
    r = c.post("/api/account/student/start", json={"email": "a@example.edu", "grad_month": "2027-06"})
    assert r.status_code == 200 and fake.calls == [("/account/student/start",
                                                     {"email": "a@example.edu", "grad_month": "2027-06"})]
    assert c.post("/api/account/student/start", json={"email": "a@example.edu", "grad_month": "June"}).status_code == 400
    assert c.post("/api/account/student/verify", json={"code": "12"}).status_code == 400
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(400, {"reason": "not_school"}))
    r = c.post("/api/account/student/start", json={"email": "a@gmail.com", "grad_month": "2027-06"})
    assert r.status_code == 400 and ".edu" in r.get_json()["error"]
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(429, {"reason": "rate_limited"}))
    assert c.post("/api/account/student/start", json={"email": "a@x.edu", "grad_month": "2027-06"}).status_code == 429
    monkeypatch.setattr(A, "_broker_post", _FakeBroker(200, {"verified": True, "eligible": True}))
    assert c.post("/api/account/student/verify", json={"code": "123 456"}).get_json()["eligible"] is True


def test_a_renewing_plan_counts_as_paid_for_live_interviews(client, monkeypatch):
    A, _c = client
    assert {"student_monthly", "standard_quarterly", "pass30"} <= set(A._PASS_TIERS)
