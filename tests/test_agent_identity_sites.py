"""The apply agent never signs in to or creates the person's own identity-provider account
(2026-11-24). Google's careers site sends Apply to accounts.google.com, which shares google.com
with the job page, so a run once typed the person's email there, pressed "Create account" and
typed their name into Google's sign-up form. Now any Google/Microsoft/Apple/... sign-in page
parks the run before anything is typed or clicked: the person signs in, Continue opens the job."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The apply agent is off by default (assisted apply only); these tests cover it, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "shell-electron" / "agent.js"

CASES = {
    "https://accounts.google.com/v3/signin/identifier?continue=https://www.google.com/about/careers/": "Google",
    "https://accounts.google.com/lifecycle/steps/signup/name?checkedDomains=youtube": "Google",
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize": "Microsoft",
    "https://signup.live.com/signup": "Microsoft",
    "https://appleid.apple.com/auth/authorize": "Apple",
    "https://github.com/login/oauth/authorize?client_id=x": "GitHub",
    "https://www.facebook.com/login.php": "Facebook",
    # the job's own pages and ordinary links are not sign-in walls
    "https://www.google.com/about/careers/applications/apply?jobId=1": "",
    "https://careers.google.com/jobs/results/1": "",
    "https://github.com/acme/careers": "",
    "https://www.facebook.com/acmejobs": "",
    "https://acme.wd5.myworkdayjobs.com/en-US/careers/login": "",
    "not a url": "",
}


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_identity_provider_pages_are_recognised():
    js = ("const a=require(%s);const out={};for(const u of %s) out[u]=a.idpName(u);"
          "console.log(JSON.stringify({out, line:a.IDP_LINE('Google')}))") % (json.dumps(str(AGENT)), json.dumps(list(CASES)))
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["out"] == CASES
    line = got["line"]
    assert "never sign in to or create Google accounts" in line and "Continue" in line
    assert "—" not in line and "–" not in line


def test_the_run_parks_at_an_identity_page_before_the_site_checks_and_any_fill():
    src = AGENT.read_text(encoding="utf-8")
    loop = src[src.index("const url = wc.getURL() || \"\";\n        const idp = idpName(url);"):]
    park_at = loop.index('return park("person", IDP_LINE(idp)')
    assert park_at < loop.index("if (!onSite(hosts, url))")
    assert park_at < loop.index("await ensureScripts(wc);")
    # Continue reopens the job's own page, not the identity provider's
    assert re.search(r"IDP_LINE\(idp\), \{ url, pageUrl: lastOn \|\| applyUrl", loop)
