"""Packaging hardening for the desktop shell: the electron-builder afterPack hook flips the
Electron fuses before the macOS ad-hoc signature (tests/js/test_after_pack.js, under node), and
the build config keeps what those fuses rely on."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = json.loads((ROOT / "shell-electron" / "package.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_after_pack_hook_under_node():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_after_pack.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "after_pack ok" in out.stdout


def test_build_runs_the_fuse_hook_and_packs_asar():
    build = PKG["build"]
    assert build["afterPack"] == "scripts/after-pack.js"
    assert (ROOT / "shell-electron" / "scripts" / "after-pack.js").is_file()
    # onlyLoadAppFromAsar and the asar integrity check need the app packed as app.asar (the
    # default); turning asar off would leave the app unable to start.
    assert build.get("asar", True) is not False
    assert not build.get("disableAsarIntegrity")
    # electron-builder's own fuse step runs AFTER afterPack and would break the ad-hoc signature.
    assert "electronFuses" not in build


def test_mac_signing_setup_is_unchanged():
    mac = PKG["build"]["mac"]
    assert mac["identity"] is None and mac["hardenedRuntime"] is False
