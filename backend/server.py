"""Broker entrypoint: wire the real providers + persistent store into the broker app.

    python -m backend.server        # serves on 127.0.0.1:57001

Keys are read from the git-ignored config/credentials.env (or the environment). When BOTH the
Tavus and Anthropic keys are present it runs the real CompositeProvider (avatar -> Tavus,
llm -> Anthropic); otherwise it falls back to FakeProvider so local dev still boots. Identity is
the X-Tailor-User header for now (real license-token auth is P6). Usage persists in a git-ignored
SQLite file under data/.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.broker import _default_identify, _default_period, create_app
from backend.metering import Meter
from backend.providers import CompositeProvider, FakeProvider
from backend.store_sqlite import SqliteUsageStore

_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACE_ID = "r9d30b0e55ac"   # 'Luna', a Tavus stock face; override with TAVUS_FACE_ID


def _cred(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    path = Path(os.environ.get("RESUME_AGENT_CRED_FILE") or (_ROOT / "config" / "credentials.env"))
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def build_provider():
    """Wire each service to a real provider where its OWN key allows it.

    The LLM and the avatar are independent services with independent keys, and they must
    degrade independently. Requiring BOTH keys before either went real meant a user with a
    perfectly good Anthropic key and no Tavus key silently got FakeProvider for TAILORING:
    the resume still compiled, still looked like a resume, and every bullet was the
    placeholder string "[fake:<model>] tailored from: ...". That violates CLAUDE.md §13 --
    the fake is a TEST DOUBLE, never shipped, never shown as product quality -- and it
    fails in the worst possible way, by looking like it worked.

    Returns (provider, llm_is_real). The flag tracks the LLM specifically, because that is
    what decides whether the app can produce real work.
    """
    tavus_key = _cred("TAVUS_API_KEY")
    anthropic_key = _cred("ANTHROPIC_API_KEY")
    fake = FakeProvider()

    if anthropic_key:
        from backend.provider_anthropic import AnthropicProvider
        llm = AnthropicProvider(anthropic_key)
    else:
        llm = fake

    if tavus_key:
        from backend.provider_tavus import TavusProvider
        avatar = TavusProvider(tavus_key, face_id=_cred("TAVUS_FACE_ID") or DEFAULT_FACE_ID,
                               pal_id=_cred("TAVUS_PAL_ID") or _cred("TAVUS_PERSONA_ID"))
    else:
        avatar = fake

    if llm is fake and avatar is fake:
        return fake, False
    return CompositeProvider(avatar=avatar, llm=llm), bool(anthropic_key)


def build_billing(meter):
    """Real Stripe billing when the secret key and at least one pass price are present; otherwise
    None (the broker still runs, and the checkout/webhook routes return 503). The key lives ONLY
    here, server-side. Passes: STRIPE_PRICE_PASS30 / STRIPE_PRICE_PASS90; extra-interview packs:
    STRIPE_PRICE_PACK_1 / _3 / _5. Any unset price is just not offered. All are one-time prices;
    nothing auto-renews. STRIPE_SECRET_KEY (live) wins over STRIPE_TEST_SECRET_KEY (sandbox)."""
    key = _cred("STRIPE_SECRET_KEY") or _cred("STRIPE_TEST_SECRET_KEY")
    from backend.billing import PACK_PRICE_ENV, PASS_PRICE_ENV, Billing, StripeClient
    pass_prices = {pid: _cred(env) for pid, env in PASS_PRICE_ENV.items()}
    if not key or not any(pass_prices.values()):
        return None
    pack_prices = {pid: _cred(env) for pid, env in PACK_PRICE_ENV.items()}   # unset -> not offered
    app_url = os.environ.get("TAILOR_APP_URL", "http://127.0.0.1:57000").rstrip("/")
    return Billing(
        StripeClient(key, webhook_secret=_cred("STRIPE_WEBHOOK_SECRET") or ""),
        meter, pass_prices,
        success_url=app_url + "/?checkout=success",
        cancel_url=app_url + "/?checkout=cancel",
        pack_prices=pack_prices)


def build_broker(db_path: str | None = None, provider=None):
    """Assemble the broker Flask app. Pass ``provider`` to inject a fake in tests; otherwise the
    real/fake provider is chosen from the available keys. The usage DB path can be overridden with
    TAILOR_BROKER_DB (point it at a persistent disk when hosted, so metering survives a redeploy)."""
    db = Path(db_path or os.environ.get("TAILOR_BROKER_DB") or (_ROOT / "data" / "broker_usage.db"))
    db.parent.mkdir(parents=True, exist_ok=True)
    meter = Meter(SqliteUsageStore(str(db)))
    # Dev convenience: put the local user on a pass so the avatar works on launch with no manual
    # setup (e.g. TAILOR_DEV_PLAN=pass30). Set by the Electron shell in dev only; NEVER in
    # production (real users get their pass from billing). Granted only when the local user has
    # no active pass, so a restart doesn't stack another one.
    dev_plan = os.environ.get("TAILOR_DEV_PLAN")
    if dev_plan:
        dev_user = os.environ.get("TAILOR_DEV_USER", "local")
        try:
            if not meter.pass_active(dev_user):
                meter.set_plan(dev_user, dev_plan)
        except ValueError:
            pass
    is_real = False
    if provider is None:
        provider, is_real = build_provider()
    billing = build_billing(meter)
    from backend.accounts import SqliteAccountStore
    accounts = SqliteAccountStore(str(db))   # per-account bearer-token identity, same DB file
    app_url = os.environ.get("TAILOR_APP_URL", "http://127.0.0.1:57000").rstrip("/")
    google = build_google()
    app = create_app(meter=meter, provider=provider, period_fn=_default_period,
                     identify=_default_identify, billing=billing, accounts=accounts,
                     google=google, app_url=app_url, telegram=build_telegram(str(db)))
    app.config["BROKER_REAL_PROVIDERS"] = is_real
    app.config["BROKER_BILLING"] = billing is not None
    app.config["BROKER_GOOGLE"] = google is not None
    # Central job feed (the shared "kitchen"): serves public jobs + runs the freshness robot,
    # riding this same service so there's no second bill. No-ops unless JOBS_AUTOUPDATE=1.
    try:
        from backend.feed import register_feed
        register_feed(app)
    except Exception:                                # noqa: BLE001 - the broker must still boot
        import traceback
        traceback.print_exc()
    return app


def build_google():
    """Real Google 'Sign in with Google' when the client id + secret are present, else None. The
    callback URL must EXACTLY match one registered in Google Cloud; it comes from GOOGLE_REDIRECT_URI,
    or is derived from Render's automatic RENDER_EXTERNAL_URL. The secret stays server-side."""
    cid = _cred("GOOGLE_CLIENT_ID")
    secret = _cred("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        return None
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if not redirect_uri and os.environ.get("RENDER_EXTERNAL_URL"):
        redirect_uri = os.environ["RENDER_EXTERNAL_URL"].rstrip("/") + "/account/google/callback"
    if not redirect_uri:
        return None   # no public callback URL -> can't do the OAuth loopback
    from backend.google_oauth import GoogleOAuth
    return GoogleOAuth(cid, secret, redirect_uri, state_secret=secret)


def build_telegram(db_path: str, transport=None, media_transport=None):
    """The official SponsorJobs Telegram bot relay when TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME
    and TELEGRAM_WEBHOOK_SECRET are all set; otherwise None (the /notify/telegram/* routes answer
    503 telegram_not_configured). The token stays server-side. See docs/notify.md."""
    token = _cred("TELEGRAM_BOT_TOKEN")
    username = _cred("TELEGRAM_BOT_USERNAME")
    secret = _cred("TELEGRAM_WEBHOOK_SECRET")
    if not token:
        return None
    if not username or not secret:
        print("telegram: TELEGRAM_BOT_TOKEN is set but TELEGRAM_BOT_USERNAME / "
              "TELEGRAM_WEBHOOK_SECRET is missing; the bot relay stays off")
        return None
    from backend.store_sqlite import SqliteTelegramStore
    from backend.telegram_relay import TelegramRelay, urllib_media_transport, urllib_transport
    return TelegramRelay(SqliteTelegramStore(db_path), transport or urllib_transport(token),
                         bot_username=username, webhook_secret=secret,
                         media_transport=media_transport or urllib_media_transport(token))


def create_broker_app():
    """Entry point for a production WSGI server when the broker is HOSTED (e.g. Render):

        gunicorn "backend.server:create_broker_app()" --bind 0.0.0.0:$PORT

    Builds the same app main() serves locally; the company keys and the DB path are read from the
    environment (never committed). Kept a factory so nothing is built on a bare import."""
    return build_broker()


def main() -> None:
    app = build_broker()
    real = app.config.get("BROKER_REAL_PROVIDERS")
    billing = "ON" if app.config.get("BROKER_BILLING") else "off"
    google = "ON" if app.config.get("BROKER_GOOGLE") else "off"
    print(f"Tailor broker on http://127.0.0.1:57001  "
          f"(providers: {'REAL' if real else 'FAKE'}, billing: {billing}, google: {google})")
    app.run(host="127.0.0.1", port=57001)


if __name__ == "__main__":
    main()
