"""Stripe billing for the managed-AI broker: one-time passes and extra-interview packs.

Pricing (decided by the owner 2026-10-02): NOTHING auto-renews and no subscription is sold.

  * Job Hunt Pass (``pass30``): $29, one payment, 30 days, 3 live interviews, 60 packages.
  * Season Pass   (``pass90``): $69, one payment, 90 days, 9 live interviews, 150 packages.
  * Extra live interviews (packs): 1 for $9, 3 for $24, 5 for $35. Sold ONLY while a pass is
    active; prepaid seconds that never expire.

Everything is a one-time Stripe Checkout Session (``mode="payment"``). The webhook grants what was
bought on ``checkout.session.completed`` once the session is paid, exactly once per session id
(the store's processed-events table), so Stripe redelivering an event never grants twice. When a
pass ends the account is simply free again; there is nothing to cancel.

The Stripe SECRET key lives ONLY here on the server (never in the downloaded app), like the
Anthropic/Tavus keys. Prices are not hard-coded: each product's Stripe price id comes from env
(STRIPE_PRICE_PASS30, STRIPE_PRICE_PASS90, STRIPE_PRICE_PACK_1/_3/_5); an unset one is not
offered. The ``price_label`` strings are display copy and must match what is set in Stripe.

The Stripe calls go through a thin injectable client so the logic is unit-tested offline with a
fake. Nothing about a resume or history is ever sent to Stripe, only the account id and the
product.
"""

from __future__ import annotations

from backend.metering import INTERVIEW_SECONDS, PASSES, PLANS

# Extra-interview packs: id -> (whole interviews, display price).
PACKS = {"pack_1": 1, "pack_3": 3, "pack_5": 5}
PACK_PRICE_LABEL = {"pack_1": "$9", "pack_3": "$24", "pack_5": "$35"}
PACK_PRICE_ENV = {"pack_1": "STRIPE_PRICE_PACK_1", "pack_3": "STRIPE_PRICE_PACK_3",
                  "pack_5": "STRIPE_PRICE_PACK_5"}
PASS_PRICE_ENV = {"pass30": "STRIPE_PRICE_PASS30", "pass90": "STRIPE_PRICE_PASS90"}


class PassRequired(Exception):
    """A pack checkout was asked for by an account with no active pass."""


class StripeClient:
    """Thin wrapper over the stripe SDK so tests can inject a fake. Real network calls only."""

    def __init__(self, api_key: str, webhook_secret: str = "") -> None:
        import stripe
        self._stripe = stripe
        self._stripe.api_key = api_key
        self.webhook_secret = webhook_secret

    def create_checkout_session(self, **params):
        return self._stripe.checkout.Session.create(**params)

    def construct_event(self, payload: bytes, sig_header: str):
        """Verify the Stripe-Signature header against the webhook secret, then return the event as a
        PLAIN dict. We re-parse the raw payload rather than hand back the stripe.Event StripeObject,
        which does NOT support dict.get() -- returning it broke apply_event and made every webhook
        500. Raises stripe.error.SignatureVerificationError on a spoofed or tampered payload."""
        import json
        self._stripe.Webhook.construct_event(payload, sig_header, self.webhook_secret)  # verify (raises)
        return json.loads(payload or b"{}")


def pass_offer(pass_id: str) -> dict:
    p = PLANS[pass_id]
    return {"id": pass_id, "price_label": p.price_label, "days": p.days,
            "interviews": p.interviews, "packages": p.packages, "auto_renew": False}


def pack_offer(pack_id: str) -> dict:
    n = PACKS[pack_id]
    return {"id": pack_id, "price_label": PACK_PRICE_LABEL[pack_id], "interviews": n,
            "seconds": n * INTERVIEW_SECONDS}


class Billing:
    def __init__(self, client, meter, pass_prices: dict | None = None, *, success_url: str,
                 cancel_url: str, pack_prices: dict | None = None) -> None:
        self.client = client
        self.meter = meter
        # {"pass30": "price_...", "pass90": "price_..."}; unset/empty ones are simply not offered
        self.pass_prices = {k: v for k, v in (pass_prices or {}).items() if v and k in PASSES}
        # {"pack_1": "price_...", ...}; unset/empty packs are simply not offered
        self.pack_prices = {k: v for k, v in (pack_prices or {}).items() if v and k in PACKS}
        self.success_url = success_url
        self.cancel_url = cancel_url

    # -- what is on sale ---------------------------------------------------------------- #
    def offered_passes(self) -> list[dict]:
        return [pass_offer(pid) for pid in PASSES if pid in self.pass_prices]

    def offered_packs(self) -> list[dict]:
        """The packs on sale (those with a configured price), smallest first."""
        return [pack_offer(pid) for pid, _n in sorted(PACKS.items(), key=lambda kv: kv[1])
                if pid in self.pack_prices]

    # -- checkout ----------------------------------------------------------------------- #
    def _session(self, user: str, price: str, metadata: dict) -> str:
        session = self.client.create_checkout_session(
            mode="payment",                       # one-time: nothing renews, nothing to cancel
            line_items=[{"price": price, "quantity": 1}],
            client_reference_id=user,
            metadata=metadata,
            success_url=self.success_url,
            cancel_url=self.cancel_url,
        )
        return session["url"]

    def pass_checkout_url(self, user: str, pass_id: str) -> str:
        """A one-time Checkout Session for a pass; return its URL."""
        price = self.pass_prices.get(pass_id)
        if not price:
            raise ValueError(f"unknown pass: {pass_id}")
        p = PLANS[pass_id]
        return self._session(user, price, {
            "kind": "pass", "pass": pass_id, "days": p.days,
            "seconds": p.avatar_seconds_included, "packages": p.packages})

    def pack_checkout_url(self, user: str, pack: str) -> str:
        """A one-time Checkout Session for an extra-interview pack. Only while a pass is active."""
        price = self.pack_prices.get(pack)
        if not price:
            raise ValueError(f"unknown pack: {pack}")
        if not self.meter.can_buy_pack(user):
            raise PassRequired("extra interviews are sold only while a pass is active")
        return self._session(user, price, {
            "kind": "credit_pack", "pack": pack, "seconds": PACKS[pack] * INTERVIEW_SECONDS})

    # -- webhook ------------------------------------------------------------------------ #
    def _apply_payment(self, session: dict) -> None:
        """Grant a paid pass or pack ONCE. Stripe may deliver the same event more than once, so the
        session id is recorded in the store and a repeat delivery is a no-op."""
        if session.get("mode") != "payment" or session.get("payment_status") != "paid":
            return
        meta = session.get("metadata") or {}
        kind = meta.get("kind")
        user = session.get("client_reference_id") or ""
        sid = session.get("id") or ""
        if not user or not sid:
            return
        if kind == "pass":
            pass_id = meta.get("pass")
            if pass_id not in PASSES:
                return
            if not self.meter.store.mark_processed(f"checkout:{sid}"):
                return                            # already granted for this session
            self.meter.grant_pass(user, pass_id)
            return
        if kind == "credit_pack":
            pack = meta.get("pack")
            try:
                seconds = PACKS[pack] * INTERVIEW_SECONDS if pack in PACKS else int(meta.get("seconds") or 0)
            except (TypeError, ValueError):
                seconds = 0
            if seconds <= 0:
                return
            if not self.meter.store.mark_processed(f"checkout:{sid}"):
                return
            # Paid is paid: granted even if the pass happened to end between checkout and payment.
            self.meter.store.add_credit_seconds(user, seconds)

    def apply_event(self, event: dict) -> None:
        """React to a VERIFIED Stripe event. ``checkout.session.completed`` (and
        ``checkout.session.async_payment_succeeded`` for a delayed payment method) grant what was
        paid for; both carry the same session, so the session-id record keeps it to one grant.

        LEGACY: ``customer.subscription.*`` events (the retired monthly plans) are deliberately
        ignored; no subscription is sold any more, so they cannot change an account."""
        if event.get("type", "") in ("checkout.session.completed",
                                     "checkout.session.async_payment_succeeded"):
            self._apply_payment((event.get("data") or {}).get("object") or {})
