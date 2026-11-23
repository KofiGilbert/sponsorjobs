"""After the Submit, on a real Workday tenant (2026-11-19).

* The person pressed the site's own Submit while the bar said Ready to send; Workday showed
  "Application Submitted" at .../jobTasks/completed/application over Candidate Home, and the
  record stayed approved. The shell now watches a job left at Ready to send the same way as after
  its own Send, and the Workday recipe knows the page (a URL marker and an open-dialog marker).
* Right after, the Browser panel showed solid black: the shell had closed its pane while the
  page kept the panel open. The pane and the panel now always agree, and a pane coming into view
  has any size override cleared.

The shell parts run under node (tests/js/test_after_submit.js). Everyone here is made up.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_workday_recipe_knows_the_completed_page():
    recipes = json.loads((ROOT / "autoapply" / "recipes.json").read_text(encoding="utf-8"))
    wd = next(s for s in recipes["systems"] if s["id"] == "workday")
    kinds = {k for m in wd["success_markers"] for k in m}
    assert "url_re" in kinds and "dialog_text" in kinds
    assert any(m.get("dialog_text") == "Application Submitted" for m in wd["success_markers"])
    url_re = next(m["url_re"] for m in wd["success_markers"] if "url_re" in m)
    import re
    assert re.search(url_re, "https://acme.wd5.myworkdayjobs.com/en-US/External/jobTasks/completed/application", re.I)
    assert not re.search(url_re, "https://evil.example/jobTasks/completed/application", re.I)


def test_after_submit_shell_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_after_submit.js")],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
