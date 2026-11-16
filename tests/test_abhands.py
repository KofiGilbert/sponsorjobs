"""The agent-browser adapter's converter (shell-electron/abhands.js), an experiment that is OFF
unless SJ_HANDS=agent-browser (docs/agent-hands-eval.md). The behaviour is pinned under node in
`tests/js/test_abhands.js`: our indices kept where both sides see an element, no typed or
self-identification value in the view, file boxes keep their words, the * mark, off by default.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_agent_browser_adapter_converter():
    if not shutil.which("node"):
        pytest.skip("needs node")
    env = {k: v for k, v in os.environ.items() if k != "SJ_HANDS"}
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_abhands.js")],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
