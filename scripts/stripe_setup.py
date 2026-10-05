"""Create (idempotently) SponsorJobs' Stripe Products + one-time Prices for the account the key
belongs to.

    python -m scripts.stripe_setup        # reads STRIPE_TEST_SECRET_KEY from config/credentials.env

Pricing decided 2026-10-02: two passes and three extra-interview packs, all ONE-TIME prices (no
recurring price, nothing auto-renews). Safe to re-run: prices are matched by their ``lookup_key``
and never duplicated. Prints the env lines to paste into config/credentials.env or the broker's
host. The amounts must match the ``price_label`` copy in backend/metering.py and backend/billing.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import stripe

ROOT = Path(__file__).resolve().parents[1]

# Amount in cents. 'env' is the broker variable that carries the price id.
PRODUCTS = [
    {"lookup": "sponsorjobs_pass30", "product": "Job Hunt Pass", "amount": 2900,
     "env": "STRIPE_PRICE_PASS30",
     "desc": "30 days: 3 live interviews and 60 tailored packages. One payment, no auto-renew."},
    {"lookup": "sponsorjobs_pass90", "product": "Season Pass", "amount": 6900,
     "env": "STRIPE_PRICE_PASS90",
     "desc": "90 days: 9 live interviews and 150 tailored packages. One payment, no auto-renew."},
    {"lookup": "sponsorjobs_pack_1", "product": "1 extra live interview", "amount": 900,
     "env": "STRIPE_PRICE_PACK_1", "desc": "One 15 minute live interview. Never expires."},
    {"lookup": "sponsorjobs_pack_3", "product": "3 extra live interviews", "amount": 2400,
     "env": "STRIPE_PRICE_PACK_3", "desc": "Three 15 minute live interviews. Never expire."},
    {"lookup": "sponsorjobs_pack_5", "product": "5 extra live interviews", "amount": 3500,
     "env": "STRIPE_PRICE_PACK_5", "desc": "Five 15 minute live interviews. Never expire."},
]


def _cred(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    path = ROOT / "config" / "credentials.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def ensure_price(spec: dict):
    """Return the existing price for this lookup_key, or create the product + one-time price."""
    found = stripe.Price.list(lookup_keys=[spec["lookup"]], limit=1)
    if found.data:
        return found.data[0]
    product = stripe.Product.create(name=spec["product"], description=spec["desc"])
    return stripe.Price.create(product=product.id, unit_amount=spec["amount"], currency="usd",
                               lookup_key=spec["lookup"])          # no `recurring`: one-time


def main() -> None:
    key = _cred("STRIPE_TEST_SECRET_KEY")
    if not key:
        sys.exit("No STRIPE_TEST_SECRET_KEY in env or config/credentials.env")
    stripe.api_key = key
    lines = []
    for spec in PRODUCTS:
        price = ensure_price(spec)
        print(f'{spec["lookup"]:22} {price.id}  (${spec["amount"] / 100:.0f} one-time)')
        lines.append(f'{spec["env"]}={price.id}')
    print("\n# Add these to config/credentials.env (or the broker host's env):")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
