#!/usr/bin/env bash
# Launch the SponsorJobs desktop shell for development.
#
# ELECTRON_RUN_AS_NODE must be cleared: when it is set (some terminals and IDE
# integrations export it), the electron binary boots as plain node, `app` is
# undefined, and main.js dies on the first app.commandLine call.
#
# TAILOR_PYTHON is required on this machine because the shell spawns the engine
# as `python`, which is not on PATH here -- only python3 -- and Flask lives in
# the repo venv rather than the system interpreter.
set -euo pipefail

cd "$(dirname "$0")"

unset ELECTRON_RUN_AS_NODE

export TAILOR_PYTHON="${TAILOR_PYTHON:-$(cd .. && pwd)/.venv/bin/python}"
export TAILOR_OWN_KEY="${TAILOR_OWN_KEY:-1}"   # run on the saved Anthropic key

# TAILOR_LIVE_BROKER=1 runs the dev app against the LIVE broker (https://api.sponsorjobs.ai), the
# same paid path the installed app uses: sign in with your account and Live interview runs on the
# company Tavus key through the broker, drawing on your pass. Off by default (the local dev broker
# on 127.0.0.1:57001, which main.js starts, exactly as before).
#
#   TAILOR_LIVE_BROKER=1 ./dev-start.sh
#
# The live account token is kept in its own git-ignored file (config/credentials.live.env), so it
# never overwrites the local dev broker's token in config/credentials.env and you stay signed in
# on both. Your saved AI keys are copied over once so tailoring keeps running on them.
if [ "${TAILOR_LIVE_BROKER:-0}" = "1" ]; then
  export TAILOR_EDITION=official
  export TAILOR_BROKER_URL="${TAILOR_BROKER_URL:-https://api.sponsorjobs.ai}"
  repo="$(cd .. && pwd)"
  live_creds="$repo/config/credentials.live.env"
  if [ ! -f "$live_creds" ]; then
    mkdir -p "$repo/config"
    ( umask 077; grep -E '^(ANTHROPIC_API_KEY|OPENAI_API_KEY)=' "$repo/config/credentials.env" \
        > "$live_creds" 2>/dev/null || : > "$live_creds" )
  fi
  export RESUME_AGENT_CRED_FILE="$live_creds"
  echo "SponsorJobs dev: live broker $TAILOR_BROKER_URL (sign in from the account menu)"
fi
export PATH="/opt/homebrew/bin:$PATH"          # pdflatex (brew texlive)

exec npm start
