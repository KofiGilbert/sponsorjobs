"""The thin managed-AI broker (docs/bundled-api-backend.md).

In production this small service holds the company API keys (server-side only), meters every
call through ``backend.metering`` so no user can run the company's Anthropic/Tavus account past
their plan, and forwards to a ``Provider``. This skeleton runs FULLY OFFLINE with FakeProvider +
InMemoryStore; the real keys wire in at P2 (avatar) / P3 (LLM) by swapping the provider.

Stubbed for the skeleton, real versions later:
  * IDENTITY: the user id comes from an ``X-Tailor-User`` header. Real auth is a signed license
    token / JWT (P6) so a user cannot spend another user's quota.
  * BILLING: one-time passes and extra-interview packs bought through Stripe Checkout
    (backend/billing.py); ``/billing/plan`` and ``/billing/credits`` are DEV stubs, switched off
    whenever real billing is configured or the broker is hosted.

Privacy: nothing here persists resumes, job descriptions, or interview media. Only the meter
counts (seconds, tokens). Avatar video never flows through the broker (it streams peer-to-peer
between the user and Tavus); the broker only mints the session and counts minutes via heartbeats.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, redirect, request

from backend.metering import InMemoryStore, Meter, QuotaExceeded
from backend.providers import FakeProvider


def _default_period() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _default_identify(req) -> str | None:
    return req.headers.get("X-Tailor-User") or None


def create_app(meter=None, provider=None, period_fn=_default_period, identify=_default_identify,
               billing=None, accounts=None, google=None, app_url="http://127.0.0.1:57000",
               telegram=None, dev_billing=None):
    """Build the broker app. All collaborators are injected so tests wire in fakes and a fixed
    billing period; production passes the real Meter (SQLite-backed), the real Provider, and the
    datetime-based period. ``billing`` (a backend.billing.Billing) enables the Stripe routes; when
    None the checkout/webhook endpoints return 503 so the rest of the broker still runs. ``accounts``
    (a backend.accounts store) enables per-account bearer-token identity + /account/register; when
    None the broker stays on the X-Tailor-User header (dev/tests). ``telegram`` (a
    backend.telegram_relay.TelegramRelay) runs the official bot relay; when None its routes answer
    503 telegram_not_configured. ``dev_billing`` enables the /billing/plan and /billing/credits
    stubs that hand out passes and credits for free; by default only when real billing is off
    and the broker is not hosted (no RENDER env), so production can never be talked into it."""
    app = Flask(__name__)
    if dev_billing is None:
        dev_billing = billing is None and not os.environ.get("RENDER")
    meter = meter or Meter(InMemoryStore())
    provider = provider or FakeProvider()
    if accounts is not None:
        from backend.accounts import bearer_identify
        identify = bearer_identify(accounts, identify)   # Bearer token -> account_id, header fallback

    def _body() -> dict:
        return request.get_json(silent=True) or {}

    @app.route("/avatar/session/start", methods=["POST"])
    def avatar_start():
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        period = period_fn()
        try:
            meter.require_avatar(user, period)
        except QuotaExceeded as exc:
            return jsonify(error=str(exc), reason=exc.reason,
                           remaining=meter.avatar_seconds_left(user, period)), 402
        try:
            session = provider.start_avatar_session(user, _body().get("context"))
        except Exception:   # provider/network failure: fail clean, never leak the exception detail
            return jsonify(error="the interview service is unavailable"), 502
        return jsonify(remaining=meter.avatar_seconds_left(user, period), **session)

    @app.route("/avatar/heartbeat", methods=["POST"])
    def avatar_heartbeat():
        """The user's client pings this every few seconds with elapsed time; the broker deducts
        per-second and tells it to stop the moment the balance hits zero (hard stop, no overage)."""
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        try:
            seconds = max(0, int(_body().get("seconds", 0)))   # never negative; bad input is a 400, not a 500
        except (TypeError, ValueError):
            return jsonify(error="bad seconds"), 400
        r = meter.consume_avatar(user, period_fn(), seconds)
        return jsonify(remaining=r["remaining"], stop=r["remaining"] <= 0)

    @app.route("/llm/complete", methods=["POST"])
    def llm_complete():
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        period = period_fn()
        body = _body()
        prompt = str(body.get("prompt", ""))
        system = str(body.get("system", ""))
        # The bundled app calls carry a system prompt and, for the conversational flows, a full
        # message history instead of a single prompt. Accept both; estimate the input size from
        # whatever was sent so the cap is enforced on the real payload.
        messages = body.get("messages") if isinstance(body.get("messages"), list) else None
        est_chars = len(prompt) + len(system)
        if messages:
            est_chars += sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
        # Estimate input tokens up front so a single oversized prompt can't blow past a capped plan
        # on the company key (the plain check only sees usage AFTER the spend). ~1 token / 4 chars.
        try:
            meter.require_llm(user, period, est_tokens=max(1, est_chars // 4))
        except QuotaExceeded as exc:
            return jsonify(error=str(exc), reason=exc.reason, tier=meter.tier_for(user)), 402
        model = meter.llm_model_for(user)
        try:
            max_tokens = int(body.get("max_tokens") or 0) or None
            out = provider.complete(model, prompt, system=system, messages=messages,
                                    max_tokens=max_tokens, effort=body.get("effort"))
        except Exception as exc:
            # Log before flattening. Every upstream failure -- a rejected parameter, an
            # auth problem, a quota -- collapses into the same opaque 502 for the client,
            # and with nothing written down the only way to find the cause is to
            # reconstruct the call by hand. One line here is the difference between a
            # five-minute fix and an afternoon.
            app.logger.exception("llm/complete failed for model %s: %s", model, exc)
            return jsonify(error="the AI service is unavailable"), 502
        used = meter.record_llm(user, period, out["input_tokens"] + out["output_tokens"])
        return jsonify(text=out["text"], model=model, tokens=used)

    @app.route("/llm/package", methods=["POST"])
    def llm_package():
        """The app calls this ONCE at the start of a tailoring run on the bundled AI: one run is one
        "package" (the tailored resume and whatever it drafts with it). Counts it against the
        pass's balance, or the free plan's 3 a month; 402 package_limit when none is left, so the
        app can offer a pass or the person's own key before any model call is made."""
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        period = period_fn()
        try:
            r = meter.start_package(user, period)
        except QuotaExceeded as exc:
            return jsonify(error=str(exc), reason=exc.reason, tier=meter.tier_for(user),
                           packages_left=0), 402
        return jsonify(model=meter.llm_model_for(user), **r)

    @app.route("/me/usage", methods=["GET"])
    def me_usage():
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        st = meter.status(user, period_fn())
        return jsonify(plan=st["tier"], **st)

    # -- DEV billing stubs (never in production: see dev_billing) -------------------------- #
    @app.route("/billing/plan", methods=["POST"])
    def billing_plan():
        if not dev_billing:
            return jsonify(error="not available"), 403
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        try:
            meter.set_plan(user, _body().get("plan", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, plan=meter.tier_for(user))

    @app.route("/billing/credits", methods=["POST"])
    def billing_credits():
        if not dev_billing:
            return jsonify(error="not available"), 403
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        try:
            meter.add_credits(user, int(_body().get("seconds", 0)))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    # -- real Stripe billing: one-time passes + extra-interview packs ----------------------- #
    @app.route("/billing/offers", methods=["GET"])
    def billing_offers():
        """What is on sale and where the caller stands: {passes:[{id, price_label, days,
        interviews, packages, auto_renew}], packs:[{id, price_label, interviews, seconds}],
        current:{tier, pass_until, interviews_left, packages_left}}. Packs are listed only while
        the caller has an active pass (they are not sold otherwise). Without a user, current is
        null and packs are empty."""
        user = identify(request)
        passes = billing.offered_passes() if billing is not None else []
        current, packs = None, []
        if user:
            st = meter.status(user, period_fn())
            current = {k: st[k] for k in ("tier", "pass_until", "interviews_left", "packages_left")}
            if billing is not None and meter.can_buy_pack(user):
                packs = billing.offered_packs()
        return jsonify(passes=passes, packs=packs, current=current)

    @app.route("/billing/passes/<pass_id>/checkout", methods=["POST"])
    def billing_pass_checkout(pass_id):
        """Mint a one-time Checkout Session for a pass and hand back its URL for the app to open."""
        if billing is None:
            return jsonify(error="billing is not configured"), 503
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        try:
            url = billing.pass_checkout_url(user, str(pass_id))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:   # Stripe/network failure: fail clean, never leak the exception detail
            return jsonify(error="could not start checkout"), 502
        return jsonify(url=url)

    @app.route("/billing/packs", methods=["GET"])
    def billing_packs():
        """The extra-interview packs on sale (never expire): [{id, price_label, interviews,
        seconds}]. Empty when billing is off or no pack price is configured. Buying one still
        needs an active pass (the checkout route answers 402 pass_required otherwise)."""
        return jsonify(packs=billing.offered_packs() if billing is not None else [])

    @app.route("/billing/packs/<pack>/checkout", methods=["POST"])
    def billing_pack_checkout(pack):
        """Mint a one-time Checkout Session for an extra-interview pack and hand back its URL.
        402 pass_required when the caller has no active pass."""
        if billing is None:
            return jsonify(error="billing is not configured"), 503
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        from backend.billing import PassRequired
        try:
            url = billing.pack_checkout_url(user, str(pack))
        except PassRequired as exc:
            return jsonify(error=str(exc), reason="pass_required"), 402
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Exception:   # Stripe/network failure: fail clean, never leak the exception detail
            return jsonify(error="could not start checkout"), 502
        return jsonify(url=url)

    @app.route("/billing/webhook", methods=["POST"])
    def billing_webhook():
        """Stripe calls this when a pass or pack payment completes (checkout.session.completed).
        Authenticated by the signature (NOT a
        user header), so it verifies the raw body against the webhook secret before acting."""
        if billing is None:
            return jsonify(error="billing is not configured"), 503
        try:
            event = billing.client.construct_event(
                request.get_data(), request.headers.get("Stripe-Signature", ""))
        except Exception:   # bad/spoofed signature or unparseable body -> reject, do not act
            return jsonify(error="invalid signature"), 400
        billing.apply_event(event)
        return jsonify(received=True)

    @app.route("/account/register", methods=["POST"])
    def account_register():
        """Issue a fresh account_id + secret bearer token for a new install (an anonymous device
        account, used to try the app before paying). The token is returned ONCE (only its hash is
        stored) and the app keeps it (under App Lock). New accounts start on the free tier (3
        Haiku packages a month, no live interviews), so registration can't run up much AI cost."""
        if accounts is None:
            return jsonify(error="accounts are not enabled"), 503
        account_id, token = accounts.register()
        return jsonify(account_id=account_id, token=token)

    @app.route("/account/signup", methods=["POST"])
    def account_signup():
        """Create an EMAIL account so a paid plan follows the person to a new device. Returns a
        bearer token the app stores. Email + an 8+ char password; the password is never stored raw."""
        if accounts is None:
            return jsonify(error="accounts are not enabled"), 503
        from backend.accounts import EmailTaken
        email = str(_body().get("email", "")).strip().lower()
        password = str(_body().get("password", ""))
        if "@" not in email or "." not in email.split("@")[-1] or len(password) < 8:
            return jsonify(error="enter a valid email and a password of at least 8 characters"), 400
        try:
            account_id, token = accounts.signup(email, password)
        except EmailTaken:
            return jsonify(error="an account with that email already exists; log in instead"), 409
        return jsonify(account_id=account_id, token=token)

    @app.route("/account/login", methods=["POST"])
    def account_login():
        """Log in on a new device: email + password -> a fresh bearer token for that account. The
        error is deliberately identical for a wrong email vs a wrong password (no account probing)."""
        if accounts is None:
            return jsonify(error="accounts are not enabled"), 503
        email = str(_body().get("email", "")).strip().lower()
        result = accounts.login(email, str(_body().get("password", "")))
        if not result:
            return jsonify(error="wrong email or password"), 401
        account_id, token = result
        return jsonify(account_id=account_id, token=token)

    @app.route("/account/claim", methods=["POST"])
    def account_claim():
        """Attach an email + password to the CALLER'S existing anonymous account (try-first: they
        explored anonymously, now they create an account at checkout so the plan follows them). The
        account_id and its token stay the same, so their in-progress state and any pass
        remain tied to it. Authenticated by the caller's own bearer token."""
        if accounts is None:
            return jsonify(error="accounts are not enabled"), 503
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        from backend.accounts import AlreadyClaimed, EmailTaken
        email = str(_body().get("email", "")).strip().lower()
        password = str(_body().get("password", ""))
        if "@" not in email or "." not in email.split("@")[-1] or len(password) < 8:
            return jsonify(error="enter a valid email and a password of at least 8 characters"), 400
        try:
            accounts.claim(user, email, password)
        except AlreadyClaimed:
            return jsonify(error="this account already has an email"), 409
        except EmailTaken:
            return jsonify(error="an account with that email already exists; log in instead"), 409
        except KeyError:
            return jsonify(error="no user"), 401
        return jsonify(ok=True, email=email)

    @app.route("/account/me", methods=["GET"])
    def account_me():
        """The caller's account: id + email (null while still anonymous). Lets the app decide
        whether to show the create-account step at checkout."""
        if accounts is None:
            return jsonify(error="accounts are not enabled"), 503
        user = identify(request)
        if not user:
            return jsonify(error="no user"), 401
        return jsonify(account_id=user, email=accounts.email_of(user))

    # -- Sign in with Google (P6): loopback via the broker's HTTPS domain -------------------- #
    @app.route("/account/google/start", methods=["GET"])
    def google_start():
        """Send the browser to Google's consent screen (with a signed state for CSRF)."""
        if google is None or accounts is None:
            return jsonify(error="google sign-in is not configured"), 503
        return redirect(google.consent_url(google.sign_state()))

    @app.route("/account/google/callback", methods=["GET"])
    def google_callback():
        """Google returns here with a code; exchange it, find-or-create the account, and redirect to
        the LOCAL app carrying the bearer token (the shell intercepts it and stores it)."""
        if google is None or accounts is None:
            return jsonify(error="google sign-in is not configured"), 503
        if not google.valid_state(request.args.get("state", "")):
            return redirect(f"{app_url}/?signin=error")            # stale/forged state
        code = request.args.get("code", "")
        email = google.exchange_code(code) if code else None
        if not email:
            return redirect(f"{app_url}/?signin=error")
        _account_id, token = accounts.upsert_google(email)
        return redirect(f"{app_url}/?account={token}")             # the shell grabs this + stores it

    # -- the official SponsorJobs Telegram bot relay (docs/notify.md) ---------------------- #
    from backend.telegram_relay import register_telegram
    register_telegram(app, telegram, identify)

    @app.route("/health", methods=["GET"])
    def health():
        """Unauthenticated liveness check so a launcher can tell the broker is up.

        Reports whether the LLM is a REAL provider, not just whether the process is
        alive. A broker left running from an older session answers pings perfectly while
        serving FakeProvider placeholder text into people's resumes, and 'is it up?'
        cannot distinguish the two. The launcher reuses a running broker only when this
        says the model is real.
        """
        return jsonify(ok=True, real_providers=bool(app.config.get("BROKER_REAL_PROVIDERS")))

    return app
