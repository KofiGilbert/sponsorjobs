"""Stripe billing (backend/billing.py) + the broker's billing routes.

Passes and extra-interview packs are ONE-TIME Checkout Sessions (mode="payment"); no subscription
is sold and nothing auto-renews. Fully offline: a fake Stripe client records the checkout params
and replays canned events, so the money logic (session shape, idempotent grants, the pass gate on
packs, the offers shape) is verified with no key and no network.
"""

from __future__ import annotations

import pytest

from backend.billing import PACKS, Billing, PassRequired, StripeClient
from backend.broker import create_app
from backend.metering import DAY_SECONDS, InMemoryStore, Meter

PASS_PRICES = {"pass30": "price_p30", "pass90": "price_p90"}
PACK_PRICES = {"pack_1": "price_k1", "pack_3": "price_k3", "pack_5": "price_k5"}
H = {"X-Tailor-User": "u1"}
T0 = 1_780_000_000


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def test_construct_event_returns_a_plain_dict_supporting_get():
    """Regression: Stripe's construct_event returns a StripeObject that does NOT support dict.get();
    handing it to apply_event 500'd every real webhook. StripeClient must return a plain dict."""
    import types
    client = StripeClient.__new__(StripeClient)          # skip __init__ (no real stripe/key)
    verified = {}
    client._stripe = types.SimpleNamespace(
        Webhook=types.SimpleNamespace(
            construct_event=lambda p, s, w: verified.update(sig=s, secret=w)))
    client.webhook_secret = "whsec_x"
    payload = b'{"type":"checkout.session.completed","data":{"object":{"mode":"payment"}}}'
    event = client.construct_event(payload, "t=1,v1=x")
    assert verified == {"sig": "t=1,v1=x", "secret": "whsec_x"}
    assert isinstance(event, dict)
    assert event.get("data", {}).get("object", {}).get("mode") == "payment"


class FakeStripe:
    def __init__(self, url="https://checkout.stripe.test/s/cs_123", event=None, raise_sig=False):
        self._url, self._event, self._raise_sig = url, event, raise_sig
        self.created = None
        self.webhook_secret = "whsec_test"

    def create_checkout_session(self, **params):
        self.created = params
        return {"url": self._url, "id": "cs_123"}

    def construct_event(self, payload, sig_header):
        if self._raise_sig:
            raise ValueError("bad signature")
        return self._event


def _billing(client=None, pass_prices=PASS_PRICES, pack_prices=PACK_PRICES, meter=None):
    meter = meter or Meter(InMemoryStore(), clock=Clock())
    b = Billing(client or FakeStripe(), meter, pass_prices, success_url="http://app/ok",
                cancel_url="http://app/no", pack_prices=pack_prices)
    return b, meter


def _paid(sid="cs_1", user="u1", mode="payment", payment_status="paid", **meta):
    return {"type": "checkout.session.completed", "data": {"object": {
        "id": sid, "mode": mode, "payment_status": payment_status, "client_reference_id": user,
        "metadata": meta}}}


def _pass_event(sid="cs_pass_1", user="u1", pass_id="pass30", **kw):
    return _paid(sid, user, kind="pass", **{"pass": pass_id}, days=30, seconds=2700, packages=150, **kw)


def _pack_event(sid="cs_pack_1", user="u1", pack="pack_1", seconds=900, **kw):
    return _paid(sid, user, kind="credit_pack", pack=pack, seconds=seconds, **kw)


# -- pass checkout --------------------------------------------------------------------------------
def test_pass_checkout_is_a_one_time_payment_with_pass_metadata():
    fake = FakeStripe()
    b, _ = _billing(fake)
    assert b.pass_checkout_url("u1", "pass90") == "https://checkout.stripe.test/s/cs_123"
    p = fake.created
    assert p["mode"] == "payment"                                      # never a subscription
    assert p["line_items"] == [{"price": "price_p90", "quantity": 1}]
    assert p["client_reference_id"] == "u1"
    assert p["metadata"] == {"kind": "pass", "pass": "pass90", "days": 90, "seconds": 8100,
                             "packages": 150}
    assert "subscription_data" not in p and "payment_method_collection" not in p
    assert p["success_url"] == "http://app/ok" and p["cancel_url"] == "http://app/no"


def test_pass_checkout_rejects_an_unknown_or_unpriced_pass():
    b, _ = _billing(pass_prices={"pass30": "price_p30"})
    for bad in ("pass90", "student", "pro", "free"):
        with pytest.raises(ValueError):
            b.pass_checkout_url("u1", bad)


def test_no_subscription_checkout_remains():
    b, _ = _billing()
    assert not hasattr(b, "checkout_url")
    import backend.billing as B
    assert not hasattr(B, "plan_for_subscription")
    app, _ = _app()
    assert app.test_client().post("/billing/checkout", json={"tier": "pro"}, headers=H).status_code == 404


# -- webhook: passes ------------------------------------------------------------------------------
def test_paid_pass_is_granted_exactly_once():
    b, meter = _billing()
    b.apply_event(_pass_event())
    b.apply_event(_pass_event())                                        # Stripe redelivers
    st = meter.status("u1", "2026-07")
    assert st["tier"] == "pass30" and st["pass_until"] == T0 + 30 * DAY_SECONDS
    assert st["interviews_left"] == 3 and st["packages_left"] == 60   # not doubled


def test_a_second_paid_pass_stacks():
    b, meter = _billing()
    b.apply_event(_pass_event())
    b.apply_event(_pass_event(sid="cs_pass_2", pass_id="pass90"))
    st = meter.status("u1", "2026-07")
    assert st["pass_until"] == T0 + 120 * DAY_SECONDS
    assert st["interviews_left"] == 12 and st["packages_left"] == 210


def test_async_payment_success_grants_once_too():
    b, meter = _billing()
    ev = _pass_event()
    ev["type"] = "checkout.session.async_payment_succeeded"
    b.apply_event(ev)
    b.apply_event(_pass_event())                                        # same session, completed event
    assert meter.status("u1", "2026-07")["packages_left"] == 60


def test_unpaid_or_malformed_pass_events_grant_nothing():
    b, meter = _billing()
    b.apply_event(_pass_event(payment_status="unpaid"))
    b.apply_event(_pass_event(sid="cs_x", mode="subscription"))
    b.apply_event(_pass_event(sid="cs_y", pass_id="pro"))
    b.apply_event(_pass_event(sid="cs_z", user=""))
    assert meter.tier_for("u1") == "free"
    # an unpaid delivery did not burn the session id: the later paid one still grants
    b.apply_event(_pass_event())
    assert meter.tier_for("u1") == "pass30"


def test_subscription_events_are_a_legacy_no_op():
    b, meter = _billing()
    for et in ("customer.subscription.created", "customer.subscription.updated",
               "customer.subscription.deleted", "invoice.paid"):
        b.apply_event({"type": et, "data": {"object": {"status": "active",
                                                       "metadata": {"tailor_user": "u1"}}}})
    assert meter.tier_for("u1") == "free"


def test_pass_grant_is_idempotent_on_sqlite_across_a_restart(tmp_path):
    from backend.store_sqlite import SqliteUsageStore
    b, m = _billing(meter=Meter(SqliteUsageStore(tmp_path / "u.db"), clock=Clock()))
    b.apply_event(_pass_event())
    b2, m2 = _billing(meter=Meter(SqliteUsageStore(tmp_path / "u.db"), clock=Clock()))
    b2.apply_event(_pass_event())
    st = m2.status("u1", "2026-07")
    assert st["tier"] == "pass30" and st["packages_left"] == 60 and st["interviews_left"] == 3


# -- packs ----------------------------------------------------------------------------------------
def test_packs_on_sale_include_the_five_pack_with_prices():
    assert PACKS == {"pack_1": 1, "pack_3": 3, "pack_5": 5}
    b, _ = _billing()
    assert b.offered_packs() == [
        {"id": "pack_1", "price_label": "$9", "interviews": 1, "seconds": 900},
        {"id": "pack_3", "price_label": "$24", "interviews": 3, "seconds": 2700},
        {"id": "pack_5", "price_label": "$35", "interviews": 5, "seconds": 4500}]
    b2, _ = _billing(pack_prices={"pack_1": "", "pack_5": "price_k5"})
    assert [p["id"] for p in b2.offered_packs()] == ["pack_5"]


def test_packs_require_an_active_pass():
    fake = FakeStripe()
    b, meter = _billing(fake)
    with pytest.raises(PassRequired):
        b.pack_checkout_url("u1", "pack_1")
    assert fake.created is None                                         # Stripe never called
    meter.grant_pass("u1", "pass30")
    assert b.pack_checkout_url("u1", "pack_5") == "https://checkout.stripe.test/s/cs_123"
    assert fake.created["mode"] == "payment"
    assert fake.created["metadata"] == {"kind": "credit_pack", "pack": "pack_5", "seconds": 4500}
    with pytest.raises(ValueError):
        b.pack_checkout_url("u1", "pack_99")


def test_paid_pack_adds_credit_seconds_exactly_once():
    b, meter = _billing()
    b.apply_event(_pack_event())
    b.apply_event(_pack_event())
    assert meter.store.get_credit_seconds("u1") == 900
    b.apply_event(_pack_event(sid="cs_pack_2", pack="pack_5", seconds=4500))
    assert meter.store.get_credit_seconds("u1") == 5400
    assert meter.tier_for("u1") == "free"                               # a pack never changes the tier


def test_unpaid_or_non_pack_checkouts_grant_nothing():
    b, meter = _billing()
    b.apply_event(_pack_event(payment_status="unpaid"))
    b.apply_event(_pack_event(sid="cs_sub", mode="subscription"))
    b.apply_event(_paid("cs_other", kind="something_else", seconds=900))
    b.apply_event(_pack_event(sid="cs_nouser", user=""))
    assert meter.store.get_credit_seconds("u1") == 0


# -- broker routes --------------------------------------------------------------------------------
def _app(client_stub=None, pass_prices=PASS_PRICES, pack_prices=PACK_PRICES):
    meter = Meter(InMemoryStore(), clock=Clock())
    b = Billing(client_stub or FakeStripe(), meter, pass_prices, success_url="http://app/ok",
                cancel_url="http://app/no", pack_prices=pack_prices)
    app = create_app(meter=meter, provider=None, period_fn=lambda: "2026-07", billing=b)
    app.config.update(TESTING=True)
    return app, meter


def test_offers_shape_for_a_free_user():
    app, _ = _app()
    body = app.test_client().get("/billing/offers", headers=H).get_json()
    assert body == {
        "passes": [
            {"id": "pass30", "price_label": "$29", "days": 30, "interviews": 3, "packages": 60,
             "auto_renew": False},
            {"id": "pass90", "price_label": "$69", "days": 90, "interviews": 9, "packages": 150,
             "auto_renew": False}],
        "packs": [],                                                    # not sold without a pass
        "current": {"tier": "free", "pass_until": None, "interviews_left": 0, "packages_left": 3}}


def test_offers_during_a_pass_list_the_packs_and_what_is_left():
    app, meter = _app()
    meter.grant_pass("u1", "pass30")
    body = app.test_client().get("/billing/offers", headers=H).get_json()
    assert [p["id"] for p in body["packs"]] == ["pack_1", "pack_3", "pack_5"]
    assert body["current"] == {"tier": "pass30", "pass_until": T0 + 30 * DAY_SECONDS,
                               "interviews_left": 3, "packages_left": 60}


def test_offers_without_billing_or_user():
    app = create_app(period_fn=lambda: "2026-07")
    app.config.update(TESTING=True)
    c = app.test_client()
    assert c.get("/billing/offers").get_json() == {"passes": [], "packs": [], "current": None}
    assert c.get("/billing/offers", headers=H).get_json()["current"]["tier"] == "free"


def test_pass_checkout_route():
    fake = FakeStripe()
    app, _ = _app(fake)
    c = app.test_client()
    r = c.post("/billing/passes/pass30/checkout", headers=H)
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://checkout.stripe.test")
    assert fake.created["metadata"]["kind"] == "pass"
    assert c.post("/billing/passes/pass30/checkout").status_code == 401
    assert c.post("/billing/passes/pro/checkout", headers=H).status_code == 400


def test_pack_checkout_route_needs_an_active_pass():
    app, meter = _app()
    c = app.test_client()
    r = c.post("/billing/packs/pack_1/checkout", headers=H)
    assert r.status_code == 402 and r.get_json()["reason"] == "pass_required"
    meter.grant_pass("u1", "pass30")
    r = c.post("/billing/packs/pack_1/checkout", headers=H)
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://checkout.stripe.test")
    assert c.post("/billing/packs/pack_1/checkout").status_code == 401
    assert c.post("/billing/packs/nope/checkout", headers=H).status_code == 400


def test_packs_route_lists_the_offered_packs():
    app, _ = _app()
    assert [p["id"] for p in app.test_client().get("/billing/packs").get_json()["packs"]] \
        == ["pack_1", "pack_3", "pack_5"]


def test_webhook_verifies_the_signature_then_grants_the_pass():
    app, meter = _app(FakeStripe(event=_pass_event()))
    c = app.test_client()
    for _ in range(2):                                                   # delivered twice
        r = c.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
        assert r.status_code == 200 and r.get_json()["received"] is True
    assert meter.status("u1", "2026-07")["interviews_left"] == 3
    assert c.post("/avatar/session/start", json={}, headers=H).status_code == 200


def test_webhook_rejects_a_bad_signature_and_does_not_act():
    app, meter = _app(FakeStripe(raise_sig=True))
    r = app.test_client().post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "bad"})
    assert r.status_code == 400
    assert meter.tier_for("u1") == "free"


def test_webhook_grants_a_paid_pack_and_reopens_the_avatar():
    app, meter = _app(FakeStripe(event=_pack_event()))
    meter.grant_pass("u1", "pass30")
    meter.consume_avatar("u1", "2026-07", 2700)                         # the pass's interviews used
    c = app.test_client()
    r = c.post("/avatar/session/start", json={}, headers=H)
    assert r.status_code == 402 and r.get_json()["reason"] == "no_interviews"
    for _ in range(2):
        assert c.post("/billing/webhook", data=b"{}", headers={"Stripe-Signature": "s"}).status_code == 200
    assert meter.avatar_seconds_left("u1", "2026-07") == 900
    assert c.post("/avatar/session/start", json={}, headers=H).status_code == 200


def test_billing_routes_without_billing():
    app = create_app(period_fn=lambda: "2026-07")
    app.config.update(TESTING=True)
    c = app.test_client()
    assert c.get("/billing/packs").get_json() == {"packs": []}
    assert c.post("/billing/packs/pack_1/checkout", headers=H).status_code == 503
    assert c.post("/billing/passes/pass30/checkout", headers=H).status_code == 503
    assert c.post("/billing/webhook", data=b"{}").status_code == 503


def test_dev_stubs_are_off_when_billing_is_configured_or_hosted(monkeypatch):
    app, meter = _app()
    c = app.test_client()
    assert c.post("/billing/plan", json={"plan": "pass90"}, headers=H).status_code == 403
    assert c.post("/billing/credits", json={"seconds": 900}, headers=H).status_code == 403
    assert meter.tier_for("u1") == "free" and meter.avatar_seconds_left("u1") == 0
    monkeypatch.setenv("RENDER", "true")
    hosted = create_app(period_fn=lambda: "2026-07")                    # no billing, but hosted
    hosted.config.update(TESTING=True)
    assert hosted.test_client().post("/billing/plan", json={"plan": "pass90"}, headers=H).status_code == 403
