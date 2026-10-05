"""The reusable-answers matcher (extension/autofill_core.js answerKeyForLabel): map a
form question's label to the saved short-text answer it asks for. Pure function, run in node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CORE = Path("extension/autofill_core.js")

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.HTMLInputElement = function () {};
global.HTMLTextAreaElement = function () {};
global.HTMLSelectElement = function () {};
const window = {};
const document = { querySelectorAll() { return []; }, querySelector() { return null; },
                  getElementById() { return null; }, body: { textContent: '' } };
const location = { hostname: 'boards.greenhouse.io' };
const CSS = { escape: (s) => s };
global.Event = function () {};
new Function('window', 'document', 'location', 'CSS', 'Event', 'setTimeout', src)(
  window, document, location, CSS, Event, (f) => {});
const A = window.TailorAutofill;
const label = (s) => A.answerKeyForLabel(s);
console.log(JSON.stringify({
  years1: label("Years of experience"),
  years2: label("How many years of experience do you have?"),
  salary1: label("Desired salary"),
  salary2: label("Expected compensation"),
  start1: label("When can you start?"),
  start2: label("Notice period"),
  hear1: label("How did you hear about us?"),
  hear2: label("Referral source"),
  none: label("Email address"),
  empty: label(""),
}));
"""


def _run():
    h = Path("build/_answers_harness.js")
    h.parent.mkdir(parents=True, exist_ok=True)
    h.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(["node", str(h), str(CORE)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_answer_key_for_label_maps_common_questions():
    c = _run()
    assert c["years1"] == "years_experience" and c["years2"] == "years_experience"
    assert c["salary1"] == "desired_salary" and c["salary2"] == "desired_salary"
    assert c["start1"] == "earliest_start" and c["start2"] == "earliest_start"
    assert c["hear1"] == "hear_about_us" and c["hear2"] == "hear_about_us"
    assert c["none"] is None and c["empty"] is None   # unknown / empty -> no guess
