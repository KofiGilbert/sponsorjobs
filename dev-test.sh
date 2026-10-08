#!/bin/bash
# Test the newest pushed code in the REAL installed app (Kofi, 2026-10-08: see it as a user does).
# How it works: the installed SponsorJobs.app looks for an engine on port 57000 and reuses one if
# it is already running. This starts the newest code's engine there (official edition, its own
# data folder and free account, never the installed app's data), then opens the app, which
# connects to it. Quit the app (Cmd+Q) and run `./dev-test.sh stop` to go back to normal.
#
#   ./dev-test.sh            pull the latest code, start the test engine, open the app
#   ./dev-test.sh stop       stop the test engine (the app then runs its own again)
#   ./dev-test.sh reset      forget the test copy's free account (new 3 free packages)
#   ./dev-test.sh web        old behaviour: open in the browser on port 57100 instead
set -e
cd "$(dirname "$0")"
DATA="$HOME/Library/Application Support/SponsorJobs-dev"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="$HOME/code/resume-agent/.venv/bin/python"
APP="/Applications/SponsorJobs.app"

case "${1:-start}" in
  stop)  pkill -f "ui.app" 2>/dev/null && echo "Test engine stopped." || echo "Not running."; exit 0 ;;
  reset) rm -f "$DATA/credentials.env"; echo "Test account forgotten; a fresh one is created on next start."; exit 0 ;;
  web)   PORT=57100 ;;
  *)     PORT=57000 ;;
esac

# The installed app's own engine (and any earlier test engine) must not hold the port.
osascript -e 'tell application "SponsorJobs" to quit' 2>/dev/null || true
pkill -f "SponsorJobs.app/Contents/Resources/engine" 2>/dev/null || true
pkill -f "ui.app" 2>/dev/null || true
sleep 1
git pull -q && echo "Code: $(git log --oneline -1)"
mkdir -p "$DATA"
TAILOR_PORT=$PORT RESUME_AGENT_DATA_DIR="$DATA" RESUME_AGENT_CRED_FILE="$DATA/credentials.env" \
TAILOR_SERVER_ONLY=1 JOBS_FIRST_CRAWL=0 TAILOR_EDITION=official \
TAILOR_BROKER_URL=https://api.sponsorjobs.ai JOBS_FEED_URL=https://feed.sponsorjobs.ai/feed \
nohup "$PY" -m ui.app > "$DATA/engine.log" 2>&1 &
for i in $(seq 1 60); do curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" && break; sleep 1; done
if [ "$PORT" = "57000" ] && [ -d "$APP" ]; then
  # `open` right after a quit can be swallowed while macOS tears the old instance down, so wait
  # until no app process is left, then launch the binary directly (detached).
  for i in $(seq 1 20); do pgrep -f "SponsorJobs.app/Contents/MacOS/SponsorJobs" >/dev/null || break; sleep 0.5; done
  nohup "$APP/Contents/MacOS/SponsorJobs" >/dev/null 2>&1 &
  echo "SponsorJobs is opening on the newest code."
else
  open "http://127.0.0.1:$PORT"; echo "Test copy running at http://127.0.0.1:$PORT"
fi
