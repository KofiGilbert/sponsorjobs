"""One command to wire Stripe to the SponsorJobs broker (the Cloudflare Worker).

    python3 scripts/stripe_setup.py --testers <account id>     # test mode (a sk_test_ key)
    python3 scripts/stripe_setup.py                            # live mode (a sk_live_ key)

It asks for your Stripe SECRET key (typing is hidden; the key is never printed or saved to a file),
then, for the account that key belongs to:

1. creates the products and prices for what is SOLD: the two monthly plans that renew
   automatically (recurring USD prices) and the two extra-interview packs (one-time USD prices).
   Re-running reuses them: prices are
   matched by lookup_key, never duplicated; a changed amount or interval makes a new price and
   moves the key to it;
2. creates the customer portal settings the app's "Update card" link uses (update the card, see
   invoices, cancel at the period end; no plan switching), reusing them on a re-run;
3. creates the webhook https://api.sponsorjobs.ai/billing/webhook for the events the broker needs
   (an existing one for that URL is replaced, because Stripe shows a signing secret only once);
4. stores the key, the webhook secret, the portal settings id and the price ids as Worker secrets
   with `npx wrangler secret put` (piped, nothing echoed).

A TEST key (sk_test_...) is stored as STRIPE_TEST_SECRET_KEY, and billing then shows ONLY to the
account ids you give with --testers: Stripe's public test card would otherwise hand anyone a real
plan. A LIVE key (sk_live_...) is stored as STRIPE_SECRET_KEY and sells to everyone.

Pricing (decided 2026-11-25): Student $29 every month and Standard $49 every month, each with 60
tailored applications and 3 live interviews of 20 minutes a period; both renew until cancelled.
Extra live interviews (20 minutes each): 1 for $12, 3 for $33, one-time. Nothing else is created:
no every-3-months plan, no 5-pack, no one-time pass. Prices made by an earlier run are left alone
in Stripe (a subscription already on one keeps renewing), and their Worker secrets are not touched.

    python3 scripts/stripe_setup.py --sell-quarterly           # ALSO the every-3-months plans

The every-3-months plans ($69 / $119) are kept behind that one flag. Selling them again also needs
SELL_QUARTERLY set to "1" in worker/wrangler.jsonc and a Worker deploy.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.stripe.com/v1"
WEBHOOK_URL = "https://api.sponsorjobs.ai/billing/webhook"
MINUTES = 20

# Everything the broker's webhook acts on (worker/src/billing.ts and worker/src/subscriptions.ts).
WEBHOOK_EVENTS = [
    "checkout.session.completed",            # a pack paid, or a plan's checkout finished
    "checkout.session.async_payment_succeeded",
    "invoice.paid",                          # a plan's period is paid: grant it
    "invoice.payment_failed",                # a renewal failed: tell the person
    "invoice.upcoming",                      # a renewal is coming: send the reminder e-mail
    "customer.subscription.created",
    "customer.subscription.updated",         # cancel at period end, resume, status changes
    "customer.subscription.deleted",         # the plan ended
]
PORTAL_TAG = "sponsorjobs_portal"


def _plan(lookup: str, name: str, cents: int, months: int, env: str, who: str) -> dict:
    every = "every month" if months == 1 else f"every {months} months"
    allowance = "3 live interviews and 60 tailored packages" if months == 1 else \
        "9 live interviews and 150 tailored packages"
    return {"lookup": lookup, "name": name, "amount": cents, "env": env,
            "recurring": {"interval": "month", "interval_count": months},
            "desc": f"{who}. {allowance} {every}. Renews automatically until you cancel."}


def quarterly_products() -> list[dict]:
    """The every-3-months plans. NOT sold (owner decision 2026-11-25); made only with
    --sell-quarterly."""
    return [
        _plan("sponsorjobs_student_quarterly", "Student plan, every 3 months", 6900, 3,
              "STRIPE_PRICE_STUDENT_QUARTERLY", "For verified students and recent graduates"),
        _plan("sponsorjobs_standard_quarterly", "Standard plan, every 3 months", 11900, 3,
              "STRIPE_PRICE_STANDARD_QUARTERLY", "SponsorJobs Standard"),
    ]


def products(sell_quarterly: bool = False) -> list[dict]:
    """What is on sale, and nothing else. The amounts are the prices people pay; the app's labels
    must match (worker/src/metering.ts)."""
    return [
        _plan("sponsorjobs_student_monthly", "Student plan, monthly", 2900, 1, "STRIPE_PRICE_STUDENT_MONTHLY",
              "For verified students and recent graduates"),
        _plan("sponsorjobs_standard_monthly", "Standard plan, monthly", 4900, 1, "STRIPE_PRICE_STANDARD_MONTHLY",
              "SponsorJobs Standard"),
        *(quarterly_products() if sell_quarterly else []),
        {"lookup": "sponsorjobs_pack_1", "name": "1 extra live interview", "amount": 1200,
         "env": "STRIPE_PRICE_PACK_1", "desc": f"One {MINUTES}-minute live interview. Never expires."},
        {"lookup": "sponsorjobs_pack_3", "name": "3 extra live interviews", "amount": 3300,
         "env": "STRIPE_PRICE_PACK_3", "desc": f"Three {MINUTES}-minute live interviews. Never expire."},
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


def _same_recurring(price: dict, spec: dict) -> bool:
    """Whether an existing price renews exactly as the spec says (or both are one-time)."""
    want, have = spec.get("recurring"), price.get("recurring")
    if not want or not have:
        return not want and not have
    return (have.get("interval") == want["interval"]
            and int(have.get("interval_count") or 1) == int(want["interval_count"]))


def ensure_price(key: str, spec: dict) -> str:
    """The price for this spec, made once. A re-run with the same amount and interval reuses it."""
    found = stripe(key, "GET", "/prices", {"lookup_keys": [spec["lookup"]], "active": "true",
                                           "expand": ["data.product"]}).get("data", [])
    if found and found[0]["unit_amount"] == spec["amount"] and _same_recurring(found[0], spec):
        return found[0]["id"]
    product = found[0]["product"]["id"] if found else stripe(key, "POST", "/products", {
        "name": spec["name"], "description": spec["desc"]})["id"]
    stripe(key, "POST", f"/products/{product}", {"name": spec["name"], "description": spec["desc"]})
    params = {"product": product, "unit_amount": spec["amount"], "currency": "usd",
              "lookup_key": spec["lookup"], "transfer_lookup_key": True}
    if spec.get("recurring"):
        params["recurring"] = spec["recurring"]
    return stripe(key, "POST", "/prices", params)["id"]


def portal_settings() -> dict:
    """The billing portal the app's "Update card" link opens: card, invoices and cancel at the end
    of the period. Switching plans is off (a plan's price depends on the student check)."""
    return {
        "business_profile": {"headline": "SponsorJobs: manage your plan"},
        "features": {
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
            "subscription_update": {"enabled": False},
            "customer_update": {"enabled": False},
        },
        "metadata": {"tag": PORTAL_TAG},
    }


def ensure_portal(key: str) -> str:
    """Our portal settings, made once and kept up to date on a re-run."""
    for c in stripe(key, "GET", "/billing_portal/configurations", {"active": "true", "limit": 100}).get("data", []):
        if (c.get("metadata") or {}).get("tag") == PORTAL_TAG:
            stripe(key, "POST", f"/billing_portal/configurations/{c['id']}", portal_settings())
            return c["id"]
    return stripe(key, "POST", "/billing_portal/configurations", portal_settings())["id"]


def ensure_webhook(key: str) -> str:
    for ep in stripe(key, "GET", "/webhook_endpoints", {"limit": 100}).get("data", []):
        if ep.get("url") == WEBHOOK_URL:
            stripe(key, "DELETE", f"/webhook_endpoints/{ep['id']}")
    ep = stripe(key, "POST", "/webhook_endpoints", {
        "url": WEBHOOK_URL, "description": "SponsorJobs: plans, renewals and packs",
        "enabled_events": WEBHOOK_EVENTS})
    return ep["secret"]


def put_secret(name: str, value: str) -> None:
    r = subprocess.run(["npx", "wrangler", "secret", "put", name], cwd=ROOT / "worker",
                       input=value, text=True, capture_output=True)
    if r.returncode != 0:
        sys.exit(f"Could not store {name} on the Worker:\n{r.stderr[-600:]}")
    print(f"  stored {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--testers", default="", help="test mode: comma-separated account ids that see billing")
    ap.add_argument("--sell-quarterly", action="store_true",
                    help="also create the every-3-months plans ($69 / $119); they are not sold by default")
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
    for spec in products(a.sell_quarterly):
        prices[spec["env"]] = ensure_price(key, spec)
        print(f"  {spec['name']:30s} ${spec['amount'] / 100:.0f}  ready")
    portal = ensure_portal(key)
    print("  customer portal settings  ready")
    whsec = ensure_webhook(key)
    print(f"  webhook {WEBHOOK_URL}  ready")

    print("Storing on the Worker (nothing is printed):")
    put_secret("STRIPE_SECRET_KEY" if live else "STRIPE_TEST_SECRET_KEY", key)
    put_secret("STRIPE_WEBHOOK_SECRET", whsec)
    put_secret("STRIPE_PORTAL_CONFIG", portal)
    for name, pid in prices.items():
        put_secret(name, pid)
    if not live:
        put_secret("STRIPE_TEST_ACCOUNTS", a.testers)
    print("Done. Payments are wired" + ("." if live else ", visible only to the test accounts you named."))
    print("One step left in the Stripe dashboard (docs/stripe-setup.md): set 'Upcoming renewal events' to 7 days.")


if __name__ == "__main__":
    main()
