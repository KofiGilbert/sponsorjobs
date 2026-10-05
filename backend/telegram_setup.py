"""One-time setup for the official SponsorJobs Telegram bot (docs/notify.md).

    python -m backend.telegram_setup

Points the bot's webhook at this broker's public URL (BROKER_PUBLIC_URL, or Render's automatic
RENDER_EXTERNAL_URL) with TELEGRAM_WEBHOOK_SECRET, registers the /help /stop /resume /show /versions commands, and
prints getWebhookInfo so you can see it took. Safe to re-run (setWebhook is idempotent).
"""

from __future__ import annotations

import json
import os
import sys

from backend.server import _cred
from backend.telegram_relay import urllib_transport

COMMANDS = [
    {"command": "help", "description": "What this bot does"},
    {"command": "stop", "description": "Pause messages"},
    {"command": "resume", "description": "Turn messages back on"},
    {"command": "show", "description": "A picture of a tailored CV"},
    {"command": "versions", "description": "Saved versions of a CV"},
]


def run(transport=None, out=print) -> int:
    token = _cred("TELEGRAM_BOT_TOKEN")
    secret = _cred("TELEGRAM_WEBHOOK_SECRET")
    public = (os.environ.get("BROKER_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_WEBHOOK_SECRET", secret),
                              ("BROKER_PUBLIC_URL", public)) if not v]
    if missing:
        out("missing: " + ", ".join(missing))
        return 2
    if not public.startswith("https://"):
        out("BROKER_PUBLIC_URL must be https:// (Telegram only calls HTTPS webhooks)")
        return 2
    call = transport or urllib_transport(token)
    url = public + "/telegram/webhook"
    call("setWebhook", {"url": url, "secret_token": secret,
                        "allowed_updates": ["message", "callback_query"],
                        "drop_pending_updates": True})
    call("setMyCommands", {"commands": COMMANDS})
    out(f"webhook set: {url}")
    info = call("getWebhookInfo", {})
    out(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run())
