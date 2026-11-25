"""scripts/stripe_setup.py wires Stripe to the Worker in one command (2026-10-10; renewing plans
2026-11-20). Offline: Stripe and wrangler are faked."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("stripe_setup", Path("scripts/stripe_setup.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def test_prices_match_the_decided_plans_and_say_twenty_minutes():
    # Only what is sold (2026-11-25): two monthly plans, a 1-pack and a 3-pack.
    p = {x["env"]: x for x in S.products()}
    assert {k: v["amount"] for k, v in p.items()} == {
        "STRIPE_PRICE_STUDENT_MONTHLY": 2900, "STRIPE_PRICE_STANDARD_MONTHLY": 4900,
        "STRIPE_PRICE_PACK_1": 1200, "STRIPE_PRICE_PACK_3": 3300}
    assert {k: (v.get("recurring") or {}).get("interval_count") for k, v in p.items()} == {
        "STRIPE_PRICE_STUDENT_MONTHLY": 1, "STRIPE_PRICE_STANDARD_MONTHLY": 1,
        "STRIPE_PRICE_PACK_1": None, "STRIPE_PRICE_PACK_3": None}
    assert not any("QUARTERLY" in k or "PACK_5" in k for k in p)
    # The every-3-months plans are behind one flag, unchanged.
    q = {x["env"]: x for x in S.products(sell_quarterly=True)}
    assert q["STRIPE_PRICE_STUDENT_QUARTERLY"]["amount"] == 6900
    assert q["STRIPE_PRICE_STANDARD_QUARTERLY"]["amount"] == 11900
    assert q["STRIPE_PRICE_STANDARD_QUARTERLY"]["recurring"] == {"interval": "month", "interval_count": 3}
    assert len(q) == len(p) + 2
    assert all(v["recurring"]["interval"] == "month" for v in p.values() if v.get("recurring"))
    assert all("Renews automatically" in v["desc"] for v in p.values() if v.get("recurring"))
    assert all("15-minute" not in v["desc"] and "15 minute" not in v["desc"] for v in p.values())
    # No one-time pass is created any more.
    assert not any("PASS" in k for k in p)


def test_form_encoding_is_stripe_s_nested_style():
    assert S._form({"a": {"b": [1, 2]}, "c": True, "d": None}) == [("a[b][0]", "1"), ("a[b][1]", "2"), ("c", "true")]
    assert ("recurring[interval_count]", "3") in S._form({"recurring": {"interval": "month", "interval_count": 3}})


class FakeStripe:
    """A small in-memory Stripe: products, prices (looked up by lookup_key), portal settings and
    webhook endpoints, so a second run can be checked against the first."""

    def __init__(self):
        self.calls, self.products, self.prices, self.portals, self.hooks = [], {}, [], [], []
        self.n = 0

    def _id(self, p):
        self.n += 1
        return f"{p}_{self.n}"

    def __call__(self, k, method, path, params=None):
        params = params or {}
        self.calls.append((method, path))
        if path == "/account":
            return {"id": "acct_x"}
        if path == "/prices" and method == "GET":
            key = params["lookup_keys"][0]
            live = [dict(p, product={"id": p["product"]}) for p in self.prices if p.get("lookup_key") == key]
            return {"data": live}
        if path == "/prices":
            if params.get("transfer_lookup_key"):
                for p in self.prices:
                    if p.get("lookup_key") == params["lookup_key"]:
                        p["lookup_key"] = None
            price = {"id": self._id("price"), "product": params["product"], "unit_amount": params["unit_amount"],
                     "lookup_key": params["lookup_key"], "recurring": params.get("recurring")}
            self.prices.append(price)
            return price
        if path == "/products":
            pid = self._id("prod")
            self.products[pid] = params
            return {"id": pid}
        if path.startswith("/products/"):
            return {"id": path.rsplit("/", 1)[1]}
        if path == "/billing_portal/configurations" and method == "GET":
            return {"data": self.portals}
        if path == "/billing_portal/configurations":
            c = {"id": self._id("bpc"), "metadata": params.get("metadata")}
            self.portals.append(c)
            return c
        if path.startswith("/billing_portal/configurations/"):
            return {"id": path.rsplit("/", 1)[1]}
        if path == "/webhook_endpoints" and method == "GET":
            return {"data": list(self.hooks)}
        if path.startswith("/webhook_endpoints/") and method == "DELETE":
            self.hooks = [h for h in self.hooks if h["id"] != path.rsplit("/", 1)[1]]
            return {"deleted": True}
        if path == "/webhook_endpoints":
            h = {"id": self._id("we"), "url": params["url"], "enabled_events": params["enabled_events"]}
            self.hooks.append(h)
            return {**h, "secret": f"whsec_{self.n}"}
        raise AssertionError(f"unexpected Stripe call {method} {path}")


def _run(monkeypatch, key, argv, fake=None):
    fake = fake or FakeStripe()
    secrets = {}
    monkeypatch.setattr(S, "stripe", fake)
    monkeypatch.setattr(S, "put_secret", lambda n, v: secrets.__setitem__(n, v))
    monkeypatch.setattr(S.getpass, "getpass", lambda prompt="": key)
    monkeypatch.setattr("sys.argv", ["stripe_setup.py", *argv])
    S.main()
    return fake, secrets


def test_test_key_is_stored_as_the_test_key_and_limited_to_testers(monkeypatch):
    fake = FakeStripe()
    fake.hooks.append({"id": "we_old", "url": S.WEBHOOK_URL})
    fake, secrets = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"], fake)
    assert secrets["STRIPE_TEST_SECRET_KEY"] == "sk_test_abc" and "STRIPE_SECRET_KEY" not in secrets
    assert secrets["STRIPE_TEST_ACCOUNTS"] == "acct_1" and secrets["STRIPE_WEBHOOK_SECRET"].startswith("whsec_")
    assert all(k in secrets for k in ("STRIPE_PRICE_STUDENT_MONTHLY", "STRIPE_PRICE_STANDARD_MONTHLY",
                                      "STRIPE_PRICE_PACK_1", "STRIPE_PRICE_PACK_3", "STRIPE_PORTAL_CONFIG"))
    # Nothing that is not sold is created or stored.
    assert not any("QUARTERLY" in k or "PACK_5" in k for k in secrets)
    assert not any(k.startswith("STRIPE_PRICE_PASS") for k in secrets)
    assert ("DELETE", "/webhook_endpoints/we_old") in fake.calls      # its secret can't be read again


def test_the_webhook_listens_for_every_event_the_worker_acts_on(monkeypatch):
    fake, _ = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"])
    events = set(fake.hooks[0]["enabled_events"])
    assert {"checkout.session.completed", "invoice.paid", "invoice.payment_failed", "invoice.upcoming",
            "customer.subscription.created", "customer.subscription.updated",
            "customer.subscription.deleted"} <= events


def test_a_second_run_reuses_every_price_and_the_portal(monkeypatch):
    fake, first = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"])
    prices, products, portals = len(fake.prices), len(fake.products), len(fake.portals)
    assert prices == 4 and products == 4 and portals == 1          # 2 monthly plans + 2 packs
    fake, second = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"], fake)
    assert (len(fake.prices), len(fake.products), len(fake.portals)) == (prices, products, portals)

    def ids(s):
        return {k: v for k, v in s.items() if k.startswith("STRIPE_PRICE_") or k == "STRIPE_PORTAL_CONFIG"}
    assert ids(second) == ids(first)
    assert len(fake.hooks) == 1                                   # replaced, not added


def test_a_changed_interval_makes_a_new_price_on_the_same_product(monkeypatch):
    fake, first = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"])
    old = next(p for p in fake.prices if p["id"] == first["STRIPE_PRICE_STANDARD_MONTHLY"])
    old["recurring"] = {"interval": "month", "interval_count": 2}     # as if someone edited it
    fake, second = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"], fake)
    new = next(p for p in fake.prices if p["id"] == second["STRIPE_PRICE_STANDARD_MONTHLY"])
    assert new["id"] != old["id"] and new["product"] == old["product"]
    assert new["recurring"] == {"interval": "month", "interval_count": 1} and old["lookup_key"] is None
    # A one-time price never matches a recurring spec (or the other way round).
    assert not S._same_recurring({"recurring": None}, {"recurring": {"interval": "month", "interval_count": 1}})
    assert S._same_recurring({"recurring": None}, {})


def test_test_mode_refuses_to_run_open_to_everyone(monkeypatch):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "sk_test_abc", [])


def test_live_key_sells_to_everyone(monkeypatch):
    _fake, secrets = _run(monkeypatch, "sk_live_abc", [])
    assert secrets["STRIPE_SECRET_KEY"] == "sk_live_abc" and "STRIPE_TEST_ACCOUNTS" not in secrets
