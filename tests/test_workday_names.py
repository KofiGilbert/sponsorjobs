"""The person's name and street on a Workday "My Information" page (2026-11-19).

A Workday candidate account greeted the owner as "Welcome, Kofi Kofi Gilbert Gilbert" (Home
Depot), and Sidley's review page the day before read "Legal Name: Kofi Kofi Gilbert Gilbert" and
the street twice. The cause: the fill engine read Workday's Middle Name box (id
name--legalName--middleName, label "Middle Name") as a full-name box (``\\bname\\b``, and the id
holds "legalName"), so it took "Kofi Gilbert" between First and Last; Address Line 2 matched
``address.?line`` and took the street again. Text fills already replace a box's value (the
native setter; the shell's real typing selects all and deletes first), and a box that already
holds text is never filled again, so nothing was appended.

Pinned here, in headless Chromium (tests/js/forms_harness.js) and in the planner and the shell:
First and Last Name get the first and last name only; Middle, preferred, former and other names
stay empty; the street goes in the first address line only; "I have a preferred name" is never
ticked (the profile keeps no preferred name); a second fill changes nothing. Made-up data.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_form_widgets import _playwright_dir, ops_js, run

ROOT = Path(__file__).resolve().parents[1]

PAYLOAD = {"fields": {"name": "Avery Quinn", "first_name": "Avery", "last_name": "Quinn", "street": "12 Elm Street",
                      "address": "12 Elm Street, Springfield, IL 62701", "city": "Springfield", "zip": "62701",
                      "phone": "555-010-2000", "email": "avery@example.com"},
           "screening": {}, "eeo": {"decline": True}}
FILL = f"window.TailorAutofill.fillAsync({json.dumps(PAYLOAD)})"

needs_browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                                   reason="needs node + playwright (shell-electron devDependency)")


@needs_browser
@pytest.mark.parametrize("query", ["", "?filled=1"])
def test_workday_legal_name_and_address_go_in_their_own_boxes_only(tmp_path, query):
    r = run(tmp_path, "workday_names.html", [
        {"do": "inject"},
        {"do": "eval", "as": "rep", "js": FILL},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "rep2", "js": FILL},                       # the agent fills a page more than once
        {"do": "eval", "as": "values2", "js": "window.fxValues()"},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractForm({onlyEmpty: false})"},
    ], query=query)
    v = r["values"]
    assert v["legalName--firstName"] == "Avery" and v["legalName--lastName"] == "Quinn"
    assert v["legalName--middleName"] == ""                             # never the full name
    assert v["addressLine1"] == "12 Elm Street" and v["addressLine2"] == ""
    assert v["city"] == "Springfield" and v["postalCode"] == "62701"
    assert v["preferredCheck"] is False and v["preferredShown"] is False
    assert v["preferredName--firstName"] == "" and v["preferredName--lastName"] == ""
    assert r["values2"] == v                                            # a second fill adds nothing
    # the planner is never offered the preferred-name box to tick
    assert not any("preferred name" in (f.get("label") or "").lower() for f in r["fields"])


@needs_browser
@pytest.mark.parametrize("query, middle, line2, cleared", [("?stale=1", "", "", 2), ("?mine=1", "J.", "Apt 4", 0)])
def test_a_name_or_street_left_in_the_wrong_box_is_cleared_and_the_persons_own_stays(tmp_path, query, middle, line2, cleared):
    r = run(tmp_path, "workday_names.html", [
        {"do": "inject"},
        {"do": "eval", "as": "rep", "js": f"window.TailorAutofill.fill({json.dumps(PAYLOAD)})"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ], query=query)
    v = r["values"]
    assert v["legalName--middleName"] == middle and v["addressLine2"] == line2
    assert v["legalName--firstName"] == "Avery" and v["legalName--lastName"] == "Quinn" and v["addressLine1"] == "12 Elm Street"
    assert r["rep"]["cleared"] == cleared


@needs_browser
def test_a_planned_tick_on_i_have_a_preferred_name_is_refused(tmp_path):
    r = run(tmp_path, "workday_names.html", [
        {"do": "inject"},
        {"do": "eval", "as": "ops", "js": "(async () => { const T = window.TailorAutofill;"
                                          " const el = document.getElementById('name--preferredCheck');"
                                          " el.setAttribute('data-tailor-ref', 'pc');"
                                          " return T.applyOpsAsync([{ref: 'pc', op: 'check', text: 'Yes'}]); })()"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ])
    assert r["values"]["preferredCheck"] is False


def test_the_planner_gives_the_names_and_street_to_their_own_boxes_only():
    from autoapply.planner import contact_key as k
    assert (k("First Name"), k("Last Name"), k("Legal name"), k("Address Line 1")) == ("first_name", "last_name", "full_name", "address")
    for label in ("Middle Name", "Preferred First Name", "Preferred Last Name", "Maiden name", "Previous Last Name",
                  "Other names you have used", "Name Suffix", "Address Line 2", "Apartment / Suite"):
        assert k(label) == "", label
    assert k("Street address (apt, suite)") == "address"


def test_the_shell_refuses_the_brain_a_name_or_street_in_the_wrong_box():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const { boxRefusal } = require(process.argv[1]);
const t = (line, a) => boxRefusal(line, a);
const out = {
  middle: t('[7]<input type=text label="Middle Name" value="">', { op: "type", placeholder: "full_name" }),
  first_full: t('[6]<input type=text label="First Name" value="" required>', { op: "type", placeholder: "full_name" }),
  first_ok: t('[6]<input type=text label="First Name" value="" required>', { op: "type", placeholder: "first_name" }),
  last_ok: t('*[8]<input type=text label="Last Name" value="" required>', { op: "type", placeholder: "last_name" }),
  pref_first: t('[9]<input type=text label="Preferred First Name" value="">', { op: "type", placeholder: "first_name" }),
  line2: t('[12]<input type=text label="Address Line 2" value="">', { op: "type", placeholder: "address" }),
  line1: t('[11]<input type=text label="Address Line 1" value="" required>', { op: "type", placeholder: "address" }),
  tick: t('[10]<checkbox label="I have a preferred name" checked=false>', { op: "click" }),
  untick: t('[10]<checkbox label="I have a preferred name" checked=true>', { op: "click" }),
  other: t('[5]<input type=text label="Middle Name" value="">', { op: "type", placeholder: "answer_1" }),
};
console.log(JSON.stringify(out));
"""
    out = subprocess.run(["node", "-e", js, str(ROOT / "shell-electron" / "agent.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert "Middle Name" in r["middle"] and "first_name" in r["first_full"] and "Preferred First Name" in r["pref_first"]
    assert "Address Line 2" in r["line2"] and "preferred name" in r["tick"]
    assert r["first_ok"] == r["last_ok"] == r["line1"] == r["untick"] == r["other"] == ""
