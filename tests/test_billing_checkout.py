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
    offers = {"passes": [{"id": "pass30"}], "packs": [], "current": {"tier": "free"}}
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, offers) if path == "/billing/offers" else (404, None))
    assert c.get("/api/billing/offers").get_json() == {**offers, "reachable": True}
    monkeypatch.setattr(A, "_broker_get", lambda path: (0, None))
    assert c.get("/api/billing/offers").get_json() == {"passes": [], "packs": [], "current": None,
                                                        "reachable": False}


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
