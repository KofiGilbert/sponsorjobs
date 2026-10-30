#!/usr/bin/env bash
# One-time owner setup for the official SponsorJobs Telegram bot on the Worker (docs/notify.md).
# The Worker port of backend/telegram_setup.py.
#
#   cd worker && ./scripts/telegram-setup.sh
#
# What it does:
#   1. asks for the @BotFather token (hidden while you type) and the bot's username;
#   2. checks the token with Telegram (getMe);
#   3. makes a fresh webhook secret;
#   4. stores TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME and TELEGRAM_WEBHOOK_SECRET as Worker
#      secrets with `npx wrangler secret put` (piped in, never printed or written to a file);
#   5. points Telegram at https://api.sponsorjobs.ai/telegram/webhook with that secret
#      (setWebhook, message and callback_query updates only);
#   6. registers the /help /stop /resume /show /versions commands (setMyCommands);
#   7. prints getWebhookInfo: the url and last_error_message only.
#
# Needs: node (wrangler already does) and a wrangler login to the BlackOrigin account
# (`npx wrangler login`). Run it after the Worker with the Telegram routes is deployed. Safe to
# re-run: it rotates the webhook secret and sets the webhook again.

set -euo pipefail

WEBHOOK_URL="${TELEGRAM_WEBHOOK_URL:-https://api.sponsorjobs.ai/telegram/webhook}"

cd "$(dirname "$0")/.."   # the worker directory, where wrangler.jsonc lives

command -v node >/dev/null || { echo "node is required (install Node.js 20 or newer)." >&2; exit 2; }

read -r -s -p "Bot token from @BotFather (hidden): " TG_TOKEN
echo
read -r -p "Bot username (e.g. SponsorJobsBot): " TG_USERNAME
TG_USERNAME="${TG_USERNAME#@}"

if [[ ! "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "That does not look like a bot token (it should be like 123456:ABC...)." >&2
  exit 2
fi
if [[ ! "$TG_USERNAME" =~ ^[A-Za-z][A-Za-z0-9_]{3,31}$ ]] || [[ ! "$(printf '%s' "$TG_USERNAME" | tr '[:upper:]' '[:lower:]')" == *bot ]]; then
  echo "A bot username is 5 to 32 letters, digits or _ and ends in 'bot'." >&2
  exit 2
fi

# Every Telegram call goes through node with the token in its environment, so the token never
# appears on a command line (where `ps` could show it) or in any output.
export TG_TOKEN
tg() {   # tg <method> <json payload>  -> prints the JSON result; exits non-zero on failure
  TG_METHOD="$1" TG_PAYLOAD="$2" node --input-type=module -e '
    const r = await fetch(`https://api.telegram.org/bot${process.env.TG_TOKEN}/${process.env.TG_METHOD}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: process.env.TG_PAYLOAD,
    }).catch((e) => { console.error(`${process.env.TG_METHOD}: ${e.name}`); process.exit(1); });
    const body = await r.json().catch(() => ({}));
    if (!body.ok) { console.error(`${process.env.TG_METHOD} failed: ${body.description || r.status}`); process.exit(1); }
    console.log(JSON.stringify(body.result));
  '
}

echo "Checking the token with Telegram..."
ME="$(tg getMe '{}')"
ACTUAL="$(ME="$ME" node -e 'console.log(JSON.parse(process.env.ME).username || "")')"
if [[ "$(printf '%s' "$ACTUAL" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$TG_USERNAME" | tr '[:upper:]' '[:lower:]')" ]]; then
  echo "That token belongs to @$ACTUAL, not @$TG_USERNAME. Check both and run this again." >&2
  exit 2
fi

TG_SECRET="$(node -e 'console.log(require("crypto").randomBytes(32).toString("base64url"))')"

echo "Storing the three Worker secrets..."
printf '%s' "$TG_TOKEN"    | npx wrangler secret put TELEGRAM_BOT_TOKEN >/dev/null
printf '%s' "$TG_USERNAME" | npx wrangler secret put TELEGRAM_BOT_USERNAME >/dev/null
printf '%s' "$TG_SECRET"   | npx wrangler secret put TELEGRAM_WEBHOOK_SECRET >/dev/null

echo "Pointing Telegram at $WEBHOOK_URL ..."
PAYLOAD="$(URL="$WEBHOOK_URL" SECRET="$TG_SECRET" node -e '
  console.log(JSON.stringify({ url: process.env.URL, secret_token: process.env.SECRET,
    allowed_updates: ["message", "callback_query"], drop_pending_updates: true }))')"
tg setWebhook "$PAYLOAD" >/dev/null
unset PAYLOAD TG_SECRET

tg setMyCommands '{"commands":[
  {"command":"help","description":"What this bot does"},
  {"command":"stop","description":"Pause messages"},
  {"command":"resume","description":"Turn messages back on"},
  {"command":"show","description":"A picture of a tailored resume"},
  {"command":"versions","description":"Saved versions of a resume"}]}' >/dev/null

INFO="$(tg getWebhookInfo '{}')"
INFO="$INFO" node -e '
  const i = JSON.parse(process.env.INFO);
  console.log(`url: ${i.url || "(none)"}`);
  console.log(`last_error_message: ${i.last_error_message || "(none)"}`);'
unset TG_TOKEN

echo "Done. Test it: in the app, Settings, Notifications, Connect Telegram, then press Start in Telegram."
