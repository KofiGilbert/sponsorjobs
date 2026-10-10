"""The extension reads a job ad's sponsorship stance with a port of the app's reader. The two must
never disagree about the same ad (2026-10-10): run both on the same sentences, including the U.S.
Bank posting where the employer badges said H-1B and the ad said no."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sourcing.adstance import ad_stance
from tests.test_adstance import NEITHER, NO, YES

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
CORE = Path("extension/adstance_core.js").resolve()

US_BANK = """Location expectations

This role requires working from a U.S. Bank location three (3) or more days per week.

This position is not eligible for visa sponsorship.

If there's anything we can do to accommodate a disability during any portion of the application or hiring process, please refer to our disability accommodations for applicants.

E-Verify

U.S. Bank participates in the U.S. Department of Homeland Security E-Verify program in all facilities located in the United States and certain U.S. territories. The E-Verify program is an Internet-based employment eligibility verification system operated by the U.S. Citizenship and Immigration Services."""

MIXED = [
    "Visa sponsorship: We do sponsor visas! However, this position is not eligible for visa sponsorship.",
    "Great team. This role is eligible for visa sponsorship. Hybrid in Austin.",
    "We participate in E-Verify. Candidates must be authorized to work in the US.",
    "• Sponsorship available for exceptional candidates\n• H-1B transfer welcome",
]


def js_stances(texts):
    h = Path(__file__).with_name("_adstance_harness.js")
    h.write_text("const A = require(process.argv[2]);\n"
                 "const texts = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
                 "console.log(JSON.stringify(texts.map(t => A.adStance(t))));\n")
    try:
        out = subprocess.run(["node", str(h), str(CORE)], input=json.dumps(texts),
                             capture_output=True, text=True, timeout=60)
    finally:
        h.unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_extension_and_the_app_read_every_ad_the_same_way():
    texts = NO + YES + NEITHER + MIXED + [US_BANK]
    js = js_stances(texts)
    for text, j in zip(texts, js):
        assert j == ad_stance(text), text


def test_e_verify_is_not_sponsorship_and_the_role_s_no_wins():
    [r] = js_stances([US_BANK])
    assert r == {"stance": "not_offered", "sentence": "This position is not eligible for visa sponsorship"}
    [r] = js_stances(["We participate in E-Verify for all new hires."])
    assert r["stance"] == "unknown"


def test_the_extension_loads_the_reader_before_the_page_script():
    m = json.loads(Path("extension/manifest.json").read_text())
    for cs in m["content_scripts"]:
        js = cs.get("js", [])
        if "content.js" in js:
            assert "adstance_core.js" in js and js.index("adstance_core.js") < js.index("content.js")
