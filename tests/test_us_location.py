"""usLocation (ui/static/app.js): the one location formatter every card uses, Jobs and
Auto-apply's Up next alike. QA, Oct 2026: Up next showed "Redmond, WA,US, US" raw, the feed's
own spelling, because the formatter only stripped a trailing "United States" / "USA".
Run in node (skipped without it)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CASES = {
    "Redmond, WA,US, US": "Redmond, WA",
    "San Diego, CA,US, US": "San Diego, CA",
    "Redmond, WA,US, US; Mountain View, CA,US, US": "Redmond, WA",
    "San Jose, California, United States of America": "San Jose, California",
    "New York, New York, United States": "New York, New York",
    "Seattle, WA, US": "Seattle, WA",
    "Mountain View, CA, USA": "Mountain View, CA",
    "San Mateo, CA United States": "San Mateo, CA",
    "United States": "United States",
    "USA": "United States",
    "Remote - United States": "Remote (US)",
    "Remote-USA": "Remote (US)",
    "USA - Remote": "Remote (US)",
    "United States | Remote": "Remote (US)",
    "Remote, US": "Remote (US)",
    "USA - California – Irvine": "California – Irvine",
    "Columbus, OH": "Columbus, OH",
    "Austin, TX": "Austin, TX",
    "London, UK; Austin, TX": "Austin, TX",
    "": "Not listed",
}


def _run(cases):
    src = Path("ui/static/app.js").read_text(encoding="utf-8")
    start = src.index("  const US_WORD")
    end = src.index("\n  }\n", src.index("function usLocation(")) + 4
    h = Path("build/_usloc_harness.js")
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text(src[start:end] + "\nconst cases = JSON.parse(process.argv[2]);\n"
                 "console.log(JSON.stringify(cases.map(usLocation)));\n", encoding="utf-8")
    out = subprocess.run(["node", str(h), json.dumps(cases)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_us_location_drops_the_country_in_every_feed_spelling():
    got = dict(zip(CASES, _run(list(CASES))))
    assert got == CASES


def test_up_next_and_jobs_cards_share_the_formatter():
    src = Path("ui/static/app.js").read_text(encoding="utf-8")
    assert len(re.findall(r"function usLocation\(", src)) == 1
    assert 'esc(usLocation(j.location))' in src
