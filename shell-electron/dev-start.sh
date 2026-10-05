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
export PATH="/opt/homebrew/bin:$PATH"          # pdflatex (brew texlive)

exec npm start
