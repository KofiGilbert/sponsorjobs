"""The replay suite (2026-11-26): the guided Autofill against offline copies of the day's real
application forms in headless Chromium (tests/js/test_replay_forms.js, which says what it checks).

The forms: Greenhouse direct (Stripe, Axon, Meriton, Vestmark: their own questions, read from
Greenhouse's public job board API), Greenhouse inside an iframe (instacart.careers, Canada),
Ashby, Lever, Lam's Phenom form with its privacy window, Uline's Workday experience page and
iCIMS's email step; plus the Greenhouse confirmation and the Instacart frame reset, where the
sent detector must fire once, and a planted "check and submit with a required box empty" bug
the checks must catch. Needs node and Playwright (the shell's dev dependency); CI runs it in its
own job (.github/workflows/tests.yml, "replay"). No personal values: a made-up test profile."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "forms" / "replay"


def _playwright_dir() -> Path | None:
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"), ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


@pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None, reason="needs node + playwright")
def test_the_guided_autofill_replays_every_form():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_replay_forms.js")],
                         capture_output=True, text=True, timeout=600)
    try:
        got = json.loads(out.stdout)
    except ValueError:
        raise AssertionError(out.stderr[-3000:] or out.stdout[-3000:])
    assert not got["fails"], "\n".join(got["fails"])
    names = {r["name"] for r in got["results"]}
    assert {"greenhouse_instacart_frame", "greenhouse_confirmation", "phenom_lam_privacy", "workday_uline",
            "icims_email_step", "planted_check_and_submit_bug"} <= names
    assert out.returncode == 0


def test_the_fixtures_are_small_and_carry_no_personal_values():
    total = 0
    for p in FIX.iterdir():
        total += p.stat().st_size
        assert p.stat().st_size < 12_000, p.name
        if p.suffix in (".json", ".html"):
            text = p.read_text(encoding="utf-8")
            assert "@" not in text.replace("avery.example@example.com", ""), p.name     # no addresses
            assert "Kofi" not in text and "Gilbert" not in text, p.name
    assert total < 60_000
    for b in ("stripe", "axon", "meriton", "vestmark", "instacart"):
        d = json.loads((FIX / f"gh_{b}.json").read_text(encoding="utf-8"))
        assert d["questions"] and all({"label", "required", "fields"} <= set(q) for q in d["questions"])


def test_ci_runs_the_replay_in_real_chromium():
    wf = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    job = wf[wf.index("\n  replay:"):]
    assert "playwright install" in job and "node tests/js/test_replay_forms.js" in job
    assert "ELECTRON_SKIP_BINARY_DOWNLOAD" in job
    gate = wf[wf.index("\n  pytest:"):]
    assert "replay" in gate.split("steps:")[0]
