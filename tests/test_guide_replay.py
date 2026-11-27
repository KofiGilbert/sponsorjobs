"""The guided ask and the guided walk on a real browser page (tests/js/test_guide_replay.js, which
says what it checks): a pick in a Greenhouse react-select closes the list and moves on by itself,
Next lands on the next field in under 600 ms, and a pointer label never runs past the pane's edge.
Needs node and Playwright (the shell's dev dependency); CI runs it in the replay job."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_replay_forms import _playwright_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None, reason="needs node + playwright")
def test_the_guide_moves_on_at_once_on_a_real_page():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_guide_replay.js")],
                         capture_output=True, text=True, timeout=300)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["ok"], got["failures"]
    assert got["checks"] >= 20


def test_ci_runs_it():
    job = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    assert "node tests/js/test_guide_replay.js" in job
