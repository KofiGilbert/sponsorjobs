"""Home's "Today" strip never shows a bare empty list (issue #277).

The owner saw it on 2026-11-16: after a restart the page painted before the engine had its
feed, the request failed, and `loadHomeToday` blanked the strip. What was left was a heading,
a lone "See all jobs" link and nothing else, which reads as "there are no roles" rather than
"not yet"; the twelve cards appeared seconds later.

The behaviour is pinned in a real Chromium (tests/js/home_today_harness.js), with every
/api/jobs?home=1 answer stubbed, so the three states are exercised without the engine needing
a feed at all: loading to cards, loading to a real empty result, and loading to a failure.
Skipped where Playwright is not installed, the way tests/test_form_widgets.py is.

The source checks below run everywhere, so a regression that removes the skeleton or brings
back `el.innerHTML = ""` fails even without a browser.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "ui" / "static" / "app.js"
HARNESS = ROOT / "tests" / "js" / "home_today_harness.js"


def _playwright_dir() -> Path | None:
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"),
              ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ source-level (always runs)

def test_the_strip_paints_a_skeleton_before_it_asks():
    """A bare gap under the heading is the bug; the skeleton has to go up first."""
    src = APP_JS.read_text(encoding="utf-8")
    body = src.split("async function loadHomeToday", 1)[1].split("\n  }\n", 1)[0]
    assert "feedLoadingHtml()" in body, "loadHomeToday must paint the skeleton itself"
    assert body.index("feedLoadingHtml()") < body.index("await api("), \
        "the skeleton must go up BEFORE the request, not after it"


def test_a_failed_load_is_retried_and_then_explained_never_blanked():
    src = APP_JS.read_text(encoding="utf-8")
    body = src.split("async function loadHomeToday", 1)[1].split("\n  }\n", 1)[0]
    assert 'el.innerHTML = ""' not in body, \
        "blanking the strip is exactly what made it look like there were no roles"
    assert "HOME_TODAY_BACKOFF" in body and "setTimeout" in body, "a failure must be retried"
    assert "home.todayFailed" in body, "a failure that stays failed must say so"


def test_the_failure_line_is_translated_everywhere():
    import json
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        assert d.get("home.todayFailed"), f"{code}.json has no home.todayFailed"


def test_how_roles_are_chosen_did_not_change():
    """The issue asks for a loading state, not a different list."""
    body = APP_JS.read_text(encoding="utf-8").split("async function loadHomeToday", 1)[1] \
                 .split("\n  }\n", 1)[0]
    assert "rankForReachability" in body and ".slice(0, 12)" in body


# --------------------------------------------------------------------- in a real browser

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.skipif(_playwright_dir() is None,
                    reason="needs playwright (shell-electron devDependency)")
def test_loading_then_cards_empty_and_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    import importlib

    import ui.app as app
    importlib.reload(app)
    app.WORKDIR.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    server = threading.Thread(
        target=lambda: app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True)
    server.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):                       # wait for the port to answer
        try:
            import urllib.request
            urllib.request.urlopen(base, timeout=1).read(1)
            break
        except Exception:
            time.sleep(0.1)

    out = subprocess.run(["node", str(HARNESS), base, str(_playwright_dir())],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
