"""The apply agent's page view and in-page actions (extension/agent_view.js) in a real headless
Chromium, on the local form fixtures (docs/apply-agent.md, section 11).

The checks themselves live in tests/test_agent_view.js (node + playwright, the same setup as
tests/js/forms_harness.js); this wrapper runs that file so pytest reports it with everything
else. Skipped without node or the shell's playwright, like test_form_widgets.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "tests" / "test_agent_view.js"


def _playwright_dir() -> Path | None:
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"),
              ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


pytestmark = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                                reason="needs node + playwright (shell-electron devDependency)")


def test_agent_view_node_suite():
    out = subprocess.run(["node", str(NODE_TEST)], capture_output=True, text=True, timeout=300)
    summary = (out.stdout.strip().splitlines() or [""])[-1]
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-2000:]
    assert summary.endswith(", 0 failed"), summary
    assert not summary.startswith("0 passed"), summary
