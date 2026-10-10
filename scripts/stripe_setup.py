"""One command to wire Stripe to the SponsorJobs broker (the Cloudflare Worker).

    python3 scripts/stripe_setup.py

It asks for your Stripe SECRET key (typing is hidden; the key is never printed or saved to a file),
then, for the account that key belongs to:

1. creates the five products, each with a one-time USD price (re-running reuses them: prices are
   matched by lookup_key, never duplicated; a changed amount makes a new price and moves the key);
2. creates the webhook https://api.sponsorjobs.ai/billing/webhook for checkout.session.completed
   (an existing one for that URL is replaced, because Stripe shows a signing secret only once);
3. stores the key, the webhook secret and the five price ids as Worker secrets with
   `npx wrangler secret put` (piped, nothing echoed).

A TEST key (sk_test_...) is stored as STRIPE_TEST_SECRET_KEY, and billing then shows ONLY to the
account ids you give with --testers: Stripe's public test card would otherwise hand anyone a real
pass. A LIVE key (sk_live_...) is stored as STRIPE_SECRET_KEY and sells to everyone.

Pricing (decided 2026-10-02, five-pack $39 on 2026-10-09); one live interview is 20 minutes.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.stripe.com/v1"
WEBHOOK_URL = "https://api.sponsorjobs.ai/billing/webhook"
MINUTES = 20


def products(pass30_cents: int) -> list[dict]:
    return [
        {"lookup": "sponsorjobs_pass30", "name": "Job Hunt Pass", "amount": pass30_cents,
         "env": "STRIPE_PRICE_PASS30",
         "desc": "30 days: 3 live interviews and 60 tailored packages. One payment, no auto-renew."},
        {"lookup": "sponsorjobs_pass90", "name": "Season Pass", "amount": 6900,
         "env": "STRIPE_PRICE_PASS90",
         "desc": "90 days: 9 live interviews and 150 tailored packages. One payment, no auto-renew."},
        {"lookup": "sponsorjobs_pack_1", "name": "1 extra live interview", "amount": 900,
         "env": "STRIPE_PRICE_PACK_1", "desc": f"One {MINUTES}-minute live interview. Never expires."},
        {"lookup": "sponsorjobs_pack_3", "name": "3 extra live interviews", "amount": 2400,
         "env": "STRIPE_PRICE_PACK_3", "desc": f"Three {MINUTES}-minute live interviews. Never expire."},
        {"lookup": "sponsorjobs_pack_5", "name": "5 extra live interviews", "amount": 3900,
         "env": "STRIPE_PRICE_PACK_5", "desc": f"Five {MINUTES}-minute live interviews. Never expire."},
    ]


def _form(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        name = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _form(v, name)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _form(item, f"{name}[{i}]") if isinstance(item, dict) else [(f"{name}[{i}]", str(item))]
        elif v is not None:
            out.append((name, str(v).lower() if isinstance(v, bool) else str(v)))
    return out


def stripe(key: str, method: str, path: str, params: dict | None = None) -> dict:
    url = f"{API}{path}"
    data = None
    if params and method == "GET":
        url += "?" + urllib.parse.urlencode(_form(params))
    elif params:
        data = urllib.parse.urlencode(_form(params)).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        sys.exit(f"Stripe said no ({e.code}): {msg}")


def ensure_price(key: str, spec: dict) -> str:
    found = stripe(key, "GET", "/prices", {"lookup_keys": [spec["lookup"]], "active": "true",
                                           "expand": ["data.product"]}).get("data", [])
    if found and found[0]["unit_amount"] == spec["amount"] and not found[0].get("recurring"):
        return found[0]["id"]
    product = found[0]["product"]["id"] if found else stripe(key, "POST", "/products", {
        "name": spec["name"], "description": spec["desc"]})["id"]
    stripe(key, "POST", f"/products/{product}", {"name": spec["name"], "description": spec["desc"]})
    price = stripe(key, "POST", "/prices", {
        "product": product, "unit_amount": spec["amount"], "currency": "usd",
        "lookup_key": spec["lookup"], "transfer_lookup_key": True})
    return price["id"]


def ensure_webhook(key: str) -> str:
    for ep in stripe(key, "GET", "/webhook_endpoints", {"limit": 100}).get("data", []):
        if ep.get("url") == WEBHOOK_URL:
            stripe(key, "DELETE", f"/webhook_endpoints/{ep['id']}")
    ep = stripe(key, "POST", "/webhook_endpoints", {
        "url": WEBHOOK_URL, "description": "SponsorJobs: grant a pass or pack once paid",
        "enabled_events": ["checkout.session.completed", "checkout.session.async_payment_succeeded"]})
    return ep["secret"]


def put_secret(name: str, value: str) -> None:
    r = subprocess.run(["npx", "wrangler", "secret", "put", name], cwd=ROOT / "worker",
                       input=value, text=True, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"Could not store {name} on the Worker:\n{r.stderr[-600:]}")
    print(f"  stored {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass30", type=int, default=29, help="Job Hunt Pass price in dollars (default 29)")
    ap.add_argument("--testers", default="", help="test mode: comma-separated account ids that see billing")
    a = ap.parse_args()

    key = getpass.getpass("Paste your Stripe secret key (hidden, then press Enter): ").strip()
    if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        sys.exit("That doesn't look like a Stripe secret key (it starts with sk_test_ or sk_live_).")
    live = "_live_" in key
    if not live and not a.testers:
        sys.exit("Test mode needs --testers with at least one account id, so nobody else can buy.")
    acct = stripe(key, "GET", "/account")
    print(f"Stripe account: {acct.get('settings', {}).get('dashboard', {}).get('display_name') or acct.get('id')}"
          f"  ({'LIVE: real money' if live else 'TEST: fake money'})")

    prices = {}
    for spec in products(a.pass30 * 100):
        prices[spec["env"]] = ensure_price(key, spec)
        print(f"  {spec['name']:24s} ${spec['amount'] / 100:.0f}  ready")
    whsec = ensure_webhook(key)
    print(f"  webhook {WEBHOOK_URL}  ready")

    print("Storing on the Worker (nothing is printed):")
    put_secret("STRIPE_SECRET_KEY" if live else "STRIPE_TEST_SECRET_KEY", key)
    put_secret("STRIPE_WEBHOOK_SECRET", whsec)
    for name, pid in prices.items():
        put_secret(name, pid)
    if not live:
        put_secret("STRIPE_TEST_ACCOUNTS", a.testers)
    print("Done. Payments are wired" + ("." if live else ", visible only to the test accounts you named."))


if __name__ == "__main__":
    main()
