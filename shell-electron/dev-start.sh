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

# Pick the engine's Python: TAILOR_PYTHON if set, else the repo's .venv (a real venv or a symlink
# to one), else a python3 that can import Flask. Fail loudly here: a missing interpreter used to
# start the window, fail to start the engine, and quit with nothing in the terminal.
if [ -z "${TAILOR_PYTHON:-}" ]; then
  repo_py="$(cd .. && pwd)/.venv/bin/python"
  if [ -x "$repo_py" ]; then
    TAILOR_PYTHON="$repo_py"
  elif command -v python3 >/dev/null 2>&1 && python3 -c "import flask" >/dev/null 2>&1; then
    TAILOR_PYTHON="$(command -v python3)"
  else
    echo "SponsorJobs dev: no Python with the engine's packages found." >&2
    echo "  Create one:  python3 -m venv ../.venv && ../.venv/bin/pip install -r ../requirements.txt" >&2
    echo "  or point at one:  TAILOR_PYTHON=/path/to/python ./dev-start.sh" >&2
    exit 1
  fi
fi
if [ ! -x "$TAILOR_PYTHON" ]; then
  echo "SponsorJobs dev: TAILOR_PYTHON is not runnable: $TAILOR_PYTHON" >&2
  exit 1
fi
export TAILOR_PYTHON
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
# Default ON (Kofi, 2026-11-09): the dev app must look exactly like the installed app, which never
# asks for an AI key. TAILOR_LIVE_BROKER=0 gives the open-source bring-your-own-key edition.
if [ "${TAILOR_LIVE_BROKER:-1}" = "1" ]; then
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
# Own ports, so the dev app runs beside an installed SponsorJobs (which owns 57000 and 9223) instead of
# silently attaching to the installed engine and showing the installed code.
export TAILOR_PORT="${TAILOR_PORT:-57010}"
export TAILOR_CDP_PORT="${TAILOR_CDP_PORT:-9233}"
export PATH="/opt/homebrew/bin:$PATH"          # pdflatex (brew texlive)

exec npm start
