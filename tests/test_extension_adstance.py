"""The extension reads a job ad's sponsorship stance with a port of the app's reader. The two must
never disagree about the same ad (2026-10-10): run both on the same sentences, including the U.S.
Bank posting where the employer badges said H-1B and the ad said no."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sourcing.adstance import ad_stance, short_sentence
from tests.test_adstance import GREEN_CARD_YES, NEITHER, NO, RESTRICTED_ADS, YES

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
    "We do sponsor visas for many roles. This position is not eligible for visa sponsorship. "
    "Must be a U.S. citizen with an active security clearance.",
    "Benefits are great. U.S. citizenship is required for this role. Apply now.",
]


def js_run(texts, expr):
    """Run `expr` (a JS function of one text, with the reader as A) over `texts` in node."""
    h = Path(__file__).with_name("_adstance_harness.js")
    h.write_text("const A = require(process.argv[2]);\n"
                 "const texts = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
                 f"console.log(JSON.stringify(texts.map({expr})));\n")
    try:
        out = subprocess.run(["node", str(h), str(CORE)], input=json.dumps(texts),
                             capture_output=True, text=True, timeout=60)
    finally:
        h.unlink(missing_ok=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def js_stances(texts):
    return js_run(texts, "t => A.adStance(t)")


def test_the_extension_and_the_app_read_every_ad_the_same_way():
    texts = NO + YES + NEITHER + MIXED + RESTRICTED_ADS + GREEN_CARD_YES + [US_BANK]
    js = js_stances(texts)
    for text, j in zip(texts, js):
        assert j == ad_stance(text), text


def test_a_citizenship_or_clearance_bar_gets_its_own_chip_with_the_ad_s_sentence():
    chips = js_run(RESTRICTED_ADS + NO[:1] + YES[:1] + NEITHER[:1], "t => A.stanceChip(A.adStance(t))")
    for s, c in zip(RESTRICTED_ADS, chips):
        assert c["stance"] == "restricted" and c["label"] == "Citizenship or clearance required", s
        assert "tb-ad-no" in c["cls"] and c["quote"] == s.rstrip(".")
        assert c["title"].startswith("The ad says: “" + s.rstrip(".") + "”")
    no, yes, none = chips[-3:]
    assert no["label"] == "No sponsorship for this role" and no["cls"] == "tb-ad-no"
    assert yes["label"] == "Ad offers sponsorship" and yes["cls"] == "tb-ad-yes"
    assert none is None                                # unknown: no chip at all


def test_the_quote_is_shortened_the_same_way_in_both():
    long = ("Applicants must be legally authorized to work in the United States for any employer "
            "without requiring current or future visa sponsorship, for example employment-based "
            "visas such as H-1B or F-1 OPT and similar categories")
    texts = [long, "Short one.", "x" * 200, " spaced   out  words " * 20]
    js = js_run(texts, "t => A.shortSentence(t)")
    for t, j in zip(texts, js):
        assert j == short_sentence(t), t
    assert js[0].endswith("...") and len(js[0]) <= 144


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
