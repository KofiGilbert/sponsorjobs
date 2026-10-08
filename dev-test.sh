#!/bin/bash
# Run SponsorJobs straight from the code, as the official edition, for testing fixes without an
# installer build (Kofi, 2026-10-08). Picks up the latest pushed code, uses its own data folder
# and free account (never the installed app's), and opens the app in the default browser.
#
#   ./dev-test.sh            start (pulls the latest code first)
#   ./dev-test.sh stop       stop it
#   ./dev-test.sh reset      forget this test copy's free account (new 3 free packages)
set -e
cd "$(dirname "$0")"
DATA="$HOME/Library/Application Support/SponsorJobs-dev"
PORT=57100
PY=".venv/bin/python"; [ -x "$PY" ] || PY="$HOME/code/resume-agent/.venv/bin/python"

case "${1:-start}" in
  stop)  pkill -f "ui.app" 2>/dev/null && echo "Stopped." || echo "Not running."; exit 0 ;;
  reset) rm -f "$DATA/credentials.env"; echo "Test account forgotten; a fresh one is created on next start."; exit 0 ;;
esac

pkill -f "ui.app" 2>/dev/null || true
git pull -q && echo "Code: $(git log --oneline -1)"
mkdir -p "$DATA"
TAILOR_PORT=$PORT RESUME_AGENT_DATA_DIR="$DATA" RESUME_AGENT_CRED_FILE="$DATA/credentials.env" \
TAILOR_SERVER_ONLY=1 JOBS_FIRST_CRAWL=0 TAILOR_EDITION=official \
TAILOR_BROKER_URL=https://api.sponsorjobs.ai JOBS_FEED_URL=https://feed.sponsorjobs.ai/feed \
nohup "$PY" -m ui.app > "$DATA/engine.log" 2>&1 &
for i in $(seq 1 60); do curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/" && break; sleep 1; done
echo "SponsorJobs (test copy) is running at http://127.0.0.1:$PORT"
open "http://127.0.0.1:$PORT"
