"""Guided fill (2026-11-26): top to bottom, visible, ends where the person acts.

The owner watched a live Autofill on a Greenhouse form and could not follow it: the pointer was
not seen (the fields it filled were off screen), Yes / No lists were left open, and Veteran Status
stayed "Select..." although it is saved (the form read stopped reading choices after 15 lists, and
that form has 23). Now the fill walks the form in page order with the pointer, closes every list,
reads every list's choices, and ends on the first field that needs the person, else on Submit.
The walk itself runs in a real Chromium: tests/js/test_guided_walk.js (below).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

try:
    from tests.test_form_widgets import _playwright_dir
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


@browser
def test_the_guided_walk_in_a_real_page():
    import os
    env = dict(os.environ, SJ_PLAYWRIGHT_DIR=str(_playwright_dir()))
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_guided_walk.js")], capture_output=True,
                         text=True, timeout=240, env=env)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]


def test_every_list_is_read_not_only_the_first_fifteen():
    core = (ROOT / "extension" / "autofill_core.js").read_text(encoding="utf-8")
    assert "n >= 40" in core and "n >= 15 " not in core
    assert re.search(r"EXTRACT_BUDGET_MS = 15000", core)


def test_the_shell_walks_every_autofill_and_ends_where_the_person_acts():
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    assert "runAutoApplyOnPane(recordId, { pointer: true, guided: true })" in main
    assert "return guideEnd(r);" in main                      # every Autofill ends with guideEnd
    agent = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert "guided: !quiet" in agent                          # the agent's pages are walked the same way
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function say(msg)" in app and "guideStep" in app   # one status line, Next / Previous / Done


def _bar_short(cases):
    """Run app.js's aaBarShort under node on ``cases`` (the function cut out of the file)."""
    import json
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    i = app.index("  function aaBarShort(info) {")
    j = app.index("\n  }\n", i) + 4
    src = "const T = (k, d) => d;\n" + app[i:j] + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(aaBarShort)));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_bar_says_one_short_line_and_keeps_the_rest_under_details():
    # (2026-11-26, Lam) the long "Ready to send. Accepted ... for you. Check it, then click Submit
    # on the site." stayed on the bar, over two lines, even after the owner submitted
    long_ready = ("Ready to send. Accepted Example Corp's Data Privacy Agreement for you. "
                  "Check it, then click Submit on the site.")
    ready, sent, wall, ask, short = _bar_short([
        {"state": "ready", "record_id": 7, "finish": True, "message": long_ready},
        {"state": "submitted", "record_id": 7, "message": "Sent, and the site confirmed it. Marked as applied in your Tracker."},
        {"state": "stopped", "message": "This site wants you to sign in first. Sign in in the Browser tab, then press Autofill this page again."},
        {"state": "ask", "message": "Are you willing to relocate to a city you have never lived in before for this role?"},
        {"state": "running", "message": "Reading the job page"},
    ])
    assert ready == {"line": "Your turn: check and submit", "details": long_ready, "one": True}
    assert sent["line"] == "Submitted. Marked as applied" and not sent["details"]
    assert wall["line"] == "This site wants you to sign in first." and wall["details"].startswith("This site")
    assert ask["line"].startswith("Are you willing") and not ask["details"]          # a question is never cut
    assert short == {"line": "Reading the job page", "details": "", "one": True}


def test_the_bar_clears_after_the_site_confirms_the_application():
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    i = app.index("window.tailorShell.onApplied((info) => {")
    block = app[i:i + 900]
    assert 'T("fill.submitted"' in block and "say(null)" in block and "5000" in block
    assert "const short = aaBarShort(info);" in app
