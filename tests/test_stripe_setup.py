"""scripts/stripe_setup.py wires Stripe to the Worker in one command (2026-10-10). Offline: Stripe
and wrangler are faked."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("stripe_setup", Path("scripts/stripe_setup.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def test_prices_match_the_decided_plan_and_say_twenty_minutes():
    p = {x["env"]: x for x in S.products(2900)}
    assert {k: v["amount"] for k, v in p.items()} == {
        "STRIPE_PRICE_PASS30": 2900, "STRIPE_PRICE_PASS90": 6900, "STRIPE_PRICE_PACK_1": 900,
        "STRIPE_PRICE_PACK_3": 2400, "STRIPE_PRICE_PACK_5": 3900}
    assert all("15-minute" not in v["desc"] and "15 minute" not in v["desc"] for v in p.values())
    assert S.products(3500)[0]["amount"] == 3500


def test_form_encoding_is_stripe_s_nested_style():
    assert S._form({"a": {"b": [1, 2]}, "c": True, "d": None}) == [("a[b][0]", "1"), ("a[b][1]", "2"), ("c", "true")]


def _run(monkeypatch, key, argv):
    calls, secrets = [], {}

    def fake(k, method, path, params=None):
        calls.append((method, path))
        if path == "/account":
            return {"id": "acct_x"}
        if path == "/prices" and method == "GET":
            return {"data": []}
        if path == "/webhook_endpoints" and method == "GET":
            return {"data": [{"id": "we_old", "url": S.WEBHOOK_URL}]}
        if path == "/webhook_endpoints":
            return {"secret": "whsec_new"}
        return {"id": f"id_{len(calls)}"}
    monkeypatch.setattr(S, "stripe", fake)
    monkeypatch.setattr(S, "put_secret", lambda n, v: secrets.__setitem__(n, v))
    monkeypatch.setattr(S.getpass, "getpass", lambda prompt="": key)
    monkeypatch.setattr("sys.argv", ["stripe_setup.py", *argv])
    S.main()
    return calls, secrets


def test_test_key_is_stored_as_the_test_key_and_limited_to_testers(monkeypatch):
    calls, secrets = _run(monkeypatch, "sk_test_abc", ["--testers", "acct_1"])
    assert secrets["STRIPE_TEST_SECRET_KEY"] == "sk_test_abc" and "STRIPE_SECRET_KEY" not in secrets
    assert secrets["STRIPE_TEST_ACCOUNTS"] == "acct_1" and secrets["STRIPE_WEBHOOK_SECRET"] == "whsec_new"
    assert all(k in secrets for k in ("STRIPE_PRICE_PASS30", "STRIPE_PRICE_PACK_5"))
    assert ("DELETE", "/webhook_endpoints/we_old") in calls      # its secret can't be read again


def test_test_mode_refuses_to_run_open_to_everyone(monkeypatch):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "sk_test_abc", [])


def test_live_key_sells_to_everyone(monkeypatch):
    _calls, secrets = _run(monkeypatch, "sk_live_abc", [])
    assert secrets["STRIPE_SECRET_KEY"] == "sk_live_abc" and "STRIPE_TEST_ACCOUNTS" not in secrets
