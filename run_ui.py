"""Launch the local web UI for the resume agent.

    python run_ui.py

Opens a local server at http://127.0.0.1:57000 (bound to this machine only) and
launches your browser. Everything stays local — no external requests.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ui.app import app, start_auto_updater

URL = "http://127.0.0.1:57000"


def main() -> None:
    print(f"SponsorJobs UI running locally at {URL}  (Ctrl+C to stop)")
    start_auto_updater()   # keep the job feed fresh on its own (only if RESUME_AGENT_AUTOUPDATE=1)
    threading.Timer(0.8, lambda: webbrowser.open(URL)).start()
    app.run(host="127.0.0.1", port=57000, debug=False)


if __name__ == "__main__":
    main()
