"""The extension's assisted-apply panel acts only for the person.

* the panel lives in a closed shadow root and every button needs a real press (isTrusted):
  tests/js/test_extension_panel_guard.js runs extension/autofill.js in a tiny fake DOM;
* the content scripts run only on the application paths of hosts that also serve other
  content (Oracle's cloud, ADP, Rippling, iCIMS), never the whole domain, while every fill
  recipe's Oracle and ADP paths stay covered.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "extension" / "manifest.json"


def _matches() -> list[str]:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [h for cs in m.get("content_scripts", []) for h in cs.get("matches", [])]


@pytest.mark.parametrize("domain", ["oraclecloud.com", "adp.com", "rippling.com", "icims.com"])
def test_no_whole_domain_wildcard_for_shared_hosts(domain):
    assert f"https://*.{domain}/*" not in _matches()
    assert any(domain in h for h in _matches()), f"{domain} is still covered on its apply paths"


def test_the_apply_paths_the_fill_recipes_use_are_still_covered():
    hosts = _matches()
    assert "https://*.oraclecloud.com/hcmUI/CandidateExperience/*" in hosts
    assert "https://workforcenow.adp.com/mascsr/*" in hosts and "https://myjobs.adp.com/*" in hosts
    assert "https://*.icims.com/jobs/*" in hosts
    for h in ("https://*.myworkdayjobs.com/*", "https://boards.greenhouse.io/*", "https://jobs.lever.co/*",
              "https://jobs.ashbyhq.com/*", "https://www.linkedin.com/jobs/*"):
        assert h in hosts, h


def test_the_panel_styles_moved_into_the_shadow_root():
    css = (ROOT / "extension" / "content.css").read_text(encoding="utf-8")
    js = (ROOT / "extension" / "autofill.js").read_text(encoding="utf-8")
    assert ".tailor-af-panel {" not in css
    assert ".tailor-af-panel {" in js and 'attachShadow({ mode: "closed" })' in js


def test_the_panel_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_extension_panel_guard.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
