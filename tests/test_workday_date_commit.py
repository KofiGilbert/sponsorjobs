"""Workday's date that shows digits while the form's own state of it is empty (2026-11-19).

Measured on the CC-305 "Date" at Uline: the Month / Day / Year boxes showed 11/19/2026 (the box,
aria-valuetext, the display text), yet Workday said "Error: Enter today's date", and the agent
parked after four tries. What commits a segment, measured on that page: focus; the native setter
with "" and an input event of type deleteContentBackward; the native setter with the value and
an input event of type insertText carrying it; change; blur; a bubbling focusout; 300 ms between
segments. ``setSpin`` now does exactly that, and a date that shows digits while the page reports
a date error beside it is committed once again, as it shows, instead of being skipped.

The fixture (tests/fixtures/forms/workday_date_commit.html) reproduces it: the state takes a
segment only from an insertText input event, and the error is checked on a bubbling focusout.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _playwright_dir():
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"), ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


pytestmark = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                                reason="needs node + playwright (shell-electron devDependency)")


def run(tmp_path, steps, query=""):
    sc = {"core": str(ROOT / "extension" / "autofill_core.js"), "page": "workday_date_commit.html", "query": query, "steps": steps}
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(sc), encoding="utf-8")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "forms_harness.js"), str(p)], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])["results"]


V = {"do": "eval", "js": "window.fxValues()"}


def test_a_date_that_shows_digits_with_an_error_beside_it_is_committed_once(tmp_path):
    r = run(tmp_path, [
        {"do": "inject"}, dict(V, **{"as": "before"}),
        {"do": "eval", "as": "n", "js": "window.TailorAutofill.fillSignatureDate()"},
        dict(V, **{"as": "after"}),
        {"do": "eval", "as": "n2", "js": "window.TailorAutofill.fillSignatureDate()"},
    ], "?shown")
    b, a = r["before"], r["after"]
    assert b["shown"] == b["today"] and b["state"] == "//" and "Enter today's date" in b["error"]   # the bug, reproduced
    assert r["n"] == 1 and a["state"] == a["today"] and a["error"] == "" and a["shown"] == a["today"]
    assert r["n2"] == 0                                                   # done: nothing more to do


def test_a_complete_date_with_no_error_is_the_persons_and_left_alone(tmp_path):
    r = run(tmp_path, [{"do": "inject"},
                       {"do": "eval", "as": "n", "js": "window.TailorAutofill.fillSignatureDate()"},
                       dict(V, **{"as": "v"})], "?ok")
    assert r["n"] == 0 and r["v"]["inputs"] == 0 and r["v"]["error"] == ""


def test_a_date_the_page_never_takes_is_recommitted_only_once(tmp_path):
    r = run(tmp_path, [{"do": "inject"},
                       {"do": "eval", "as": "n", "js": "window.TailorAutofill.fillSignatureDate()"},
                       dict(V, **{"as": "v1"}),
                       {"do": "eval", "as": "n2", "js": "window.TailorAutofill.fillSignatureDate()"},
                       dict(V, **{"as": "v2"})], "?stuck")
    assert r["n"] == 0 and r["n2"] == 0
    assert r["v1"]["inputs"] == 6 and r["v2"]["inputs"] == 6           # one commit (two input events a segment), never again


def test_an_empty_date_is_committed_in_the_page_with_no_keys(tmp_path):
    r = run(tmp_path, [{"do": "inject"},
                       {"do": "eval", "as": "rep", "js": "window.TailorAutofill.fillAsync({fields: {}, screening: {}, eeo: {}})"},
                       dict(V, **{"as": "v"})])
    assert r["v"]["state"] == r["v"]["today"] and r["v"]["error"] == ""
    assert r["rep"]["keys"] == []                                       # nothing left for real keys


def test_the_brains_date_typing_goes_through_the_same_commit(tmp_path):
    # shell-electron/agent.js spinType calls TailorAutofill.setSpin first: one segment at a time
    js = ("(() => { const T = window.TailorAutofill; const y = document.querySelector('[data-automation-id=dateSectionYear-input]');"
          " T.setSpin(y, String(new Date().getFullYear())); return T.spinDigits(y); })()")
    r = run(tmp_path, [{"do": "inject"}, {"do": "eval", "as": "d", "js": js}, dict(V, **{"as": "v"})])
    assert r["v"]["state"].endswith("/" + r["d"]) and r["v"]["state"].split("/")[2] == r["d"]
    agent = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert "E.setSpin(el" in agent
