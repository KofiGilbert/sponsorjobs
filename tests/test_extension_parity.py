"""P6: the extension's pure decision logic (extension/parity_core.js), run under node.

Tests the honest gating that matters: visa badges respect role_in_us, the match pill never
fabricates a number without a profile, the Required/Optional highlight split, and the list filter
(sponsorship chips + match-rate slider). No DOM needed, parity_core exports for require().
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CORE = Path("extension/parity_core.js").resolve()

HARNESS = r"""
const P = require(require('path').resolve(process.argv[2]));
const out = {
  sponsor: {
    not_role: P.sponsorBadge({ ok: true, matched: true, sponsor_employer: true, role_in_us: false }),
    unclear:  P.sponsorBadge({ ok: true, matched: true, sponsor_employer: true, role_in_us: null }),
    visa:     P.sponsorBadge({ ok: true, matched: true, role_in_us: true, visa: [{ code: 'H-1B' }, { code: 'E-VERIFY' }] }),
    none:     P.sponsorBadge({ ok: true, matched: false }),
    off:      P.sponsorBadge({ ok: false, error: 'cant_reach_app' }),
  },
  match: {
    high: P.matchBadge({ has_profile: true, score: 82, skills_covered: 5, skills_total: 8 }),
    low:  P.matchBadge({ has_profile: true, score: 20 }),
    need: P.matchBadge({ has_profile: false }),
    noscore: P.matchBadge({ has_profile: true, score: null }),
  },
  highlight: P.highlightTerms({ skills: [
    { term: 'SQL', required: true, covered: true },
    { term: 'Python', required: true, covered: false },
    { term: 'Spark', required: false, covered: false },
  ] }),
  filter: {
    chip_pass:        P.cardPasses({ codes: ['H-1B'], role_in_us: true }, { chips: ['H-1B'] }),
    chip_fail_nocode: P.cardPasses({ codes: ['E-VERIFY'], role_in_us: true }, { chips: ['H-1B'] }),
    chip_fail_abroad: P.cardPasses({ codes: ['H-1B'], role_in_us: false }, { chips: ['H-1B'] }),
    slider_pass:      P.cardPasses({ matchPct: 80 }, { minMatch: 70 }),
    slider_fail:      P.cardPasses({ matchPct: 50 }, { minMatch: 70 }),
    slider_unscored:  P.cardPasses({ matchPct: null }, { minMatch: 70 }),
    no_filter:        P.cardPasses({ codes: [] }, {}),
  },
};
console.log(JSON.stringify(out));
"""


def _run():
    harness = Path("build/_parity_harness.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(CORE)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_sponsor_badge_respects_role_gating():
    s = _run()["sponsor"]
    assert s["not_role"]["kind"] == "sponsor_not_role"        # role abroad -> no visa claim
    assert s["unclear"]["kind"] == "sponsor_unclear"          # location unknown -> honest
    assert s["visa"]["kind"] == "visa" and s["visa"]["codes"] == ["H-1B", "E-VERIFY"]
    assert s["none"]["kind"] == "none" and s["off"]["kind"] == "off"


def test_match_pill_never_fabricates_without_a_profile():
    m = _run()["match"]
    assert m["high"]["pct"] == 82 and m["high"]["cls"] == "high"
    assert m["low"]["cls"] == "low"
    assert m["need"]["needProfile"] is True                   # no profile -> prompt, not a number
    assert m["noscore"] is None


def test_highlight_splits_required_optional_covered_missing():
    h = _run()["highlight"]
    assert [x["term"] for x in h["required"]] == ["SQL", "Python"]
    assert [x["term"] for x in h["optional"]] == ["Spark"]
    assert h["covered"] == ["SQL"] and set(h["missing"]) == {"Python", "Spark"}


def test_filter_chips_and_match_slider():
    f = _run()["filter"]
    assert f["chip_pass"] is True
    assert f["chip_fail_nocode"] is False                     # employer lacks the chip's code
    assert f["chip_fail_abroad"] is False                     # US visa chip can't apply abroad
    assert f["slider_pass"] is True and f["slider_fail"] is False
    assert f["slider_unscored"] is True                       # honest: unscored cards aren't hidden
    assert f["no_filter"] is True
