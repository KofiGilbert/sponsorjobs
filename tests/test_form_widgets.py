"""The fill engine on the HARD parts of application forms, in a real (headless) Chromium.

Runs extension/autofill_core.js and the desktop shell's CDP frame path (shell-electron/frames.js)
against local fixtures that behave like the widgets real ATS pages use
(tests/fixtures/forms/widgets.js): react-select dropdowns (Greenhouse's new boards), Workday's
listbox buttons and prompt search boxes (data-automation-id), ARIA radio groups and checkboxes,
open shadow roots, an iCIMS-style form inside a cross-origin iframe (both in-process and
out-of-process), and a three-page single-page-app form. Never touches a real site: the harness
(tests/js/forms_harness.js) serves the fixtures from two local ports.

What it pins: custom widgets are opened, filtered, picked and VERIFIED; screening / EEO answers
reach custom widgets with the same rules as native controls; the model plan's ops land on custom
widgets; the frame path fills, uploads and detects success inside a cross-origin frame; a closed
shadow root stays untouched; "left for the person" is reported per page; and a new page after
the person's Next is told apart from a conditional question on the same page. Nothing here
clicks Next or Submit: the fixtures' buttons are pressed only by the test, playing the person.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "extension" / "autofill_core.js"
HARNESS = ROOT / "tests" / "js" / "forms_harness.js"


def _playwright_dir() -> Path | None:
    import os
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"),
              ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


pytestmark = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                                reason="needs node + playwright (shell-electron devDependency)")

PAYLOAD = {
    "fields": {"first_name": "Kofi", "last_name": "Gilbert", "email": "kofi@example.com",
               "phone": "555-100-2000", "city": "Chicago", "state": "IL"},
    "screening": {"work_auth_us": "Yes", "needs_sponsorship": "Future"},
    "eeo": {"decline": True},
}
FILL = f"window.TailorAutofill.fillAsync({json.dumps(PAYLOAD)})"


def run(tmp_path, page, steps, query="", core=CORE):
    sc = {"core": str(core), "page": page, "query": query, "steps": steps}
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(sc), encoding="utf-8")
    out = subprocess.run(["node", str(HARNESS), str(p)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])["results"]


def ops_js(pairs):
    """applyOpsAsync with ops targeted by field label, the way the planner's ops arrive."""
    sel = json.dumps(pairs)
    return ("(async () => { const T = window.TailorAutofill; const f = T.extractForm({onlyEmpty: true});"
            f" const ops = {sel}.map(([lab, op, text]) => ({{ref: (f.find(x => x.label.startsWith(lab)) || {{}}).ref, op, text}}));"
            " return T.applyOpsAsync(ops); })()")


# ------------------------------------------------------------------ react-select (Greenhouse)
def test_react_select_screening_eeo_location_then_plan(tmp_path):
    r = run(tmp_path, "react_select.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractFormAsync({onlyEmpty: true})"},
        {"do": "eval", "as": "ops", "js": ops_js([["Country", "select", "United States"],
                                                  ["How did you hear", "select", "LinkedIn"]])},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "first", "js": "document.getElementById('first_name').value"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])
    v = r["values"]
    assert r["first"] == "Kofi"
    assert v["auth"] == "Yes"                                  # saved work authorization
    assert v["sponsor"] == "Yes"                               # "Future" + now-or-future question
    assert v["gender"] == "Decline To Self Identify"           # the EEO decline the person opted into
    assert v["location"] == "Chicago, Illinois, United States"  # async options, state breaks the tie
    assert r["report"]["screening"] == 2 and r["report"]["eeo"] == 1
    # The plan sees custom dropdowns as selects WITH their options (read by opening the menu).
    country = next(f for f in r["fields"] if f["label"] == "Country")
    assert country["type"] == "select" and "United States" in country["options"]
    assert r["ops"]["filled"] == 2
    assert v["country"] == "United States" and v["source"] == "LinkedIn"
    assert v["privacy"] is False                               # a consent box is the person's
    assert r["left"] == ["I agree to the candidate privacy notice"]


def test_combined_question_and_no_answer_are_left_alone(tmp_path):
    payload = {"fields": {}, "screening": {}, "eeo": {"decline": False}}
    r = run(tmp_path, "react_select.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": f"window.TailorAutofill.fillAsync({json.dumps(payload)})"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ])
    assert r["values"]["auth"] is None and r["values"]["sponsor"] is None
    assert r["values"]["gender"] is None                       # no decline opt-in -> untouched


def test_a_pick_that_does_not_stick_is_not_counted(tmp_path):
    # Break the widget: option clicks do nothing. The engine must report it as not filled.
    r = run(tmp_path, "react_select.html", [
        {"do": "eval", "js": "document.addEventListener('click', e => { if (e.target.classList.contains('select__option')) e.stopImmediatePropagation(); }, true)"},
        {"do": "inject"},
        {"do": "eval", "as": "ops", "js": ops_js([["Country", "select", "Canada"]])},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ])
    assert r["values"]["country"] is None
    assert r["ops"]["filled"] == 0 and r["ops"]["skipped"] == 1


# ------------------------------------------------------------------ Workday
def test_workday_listbox_prompt_and_aria_radios(tmp_path):
    r = run(tmp_path, "workday.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractFormAsync({onlyEmpty: true})"},
        {"do": "eval", "as": "ops", "js": ops_js([["Country", "select", "United States"],
                                                  ["Phone Device Type", "select", "Mobile"],
                                                  ["How Did You Hear", "select", "LinkedIn"]])},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])
    v = r["values"]
    assert v["auth"] == "Yes"                                  # ARIA radio, saved answer
    assert v["country"] == "United States of America"         # synonym of the planned value
    assert v["device"] == "Mobile" and v["source"] == ["LinkedIn"]
    assert v["relocate"] is None                               # no saved answer -> left
    relocate = next(f for f in r["fields"] if f["type"] == "radio")
    assert relocate["label"] == "Are you willing to relocate?" and relocate["options"] == ["Yes", "No"]
    assert r["left"] == ["Are you willing to relocate?"]


# ------------------------------------------------------------------ shadow DOM
def test_open_shadow_roots_filled_closed_root_untouched(tmp_path):
    r = run(tmp_path, "shadow.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ])
    v = r["values"]
    assert (v["first"], v["last"], v["email"], v["phone"]) == ("Kofi", "Gilbert", "kofi@example.com", "555-100-2000")
    assert v["auth"] == "Yes"
    assert v["closed"] == ""


# ------------------------------------------------------------------ iframes
def test_same_origin_iframe_filled_from_the_top_page(tmp_path):
    r = run(tmp_path, "icims_outer.html", [
        {"do": "wait", "ms": 300},
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "xframes", "js": "window.TailorAutofill.crossOriginFrames()"},
        {"do": "frameValues", "as": "values"},
        {"do": "frameClick", "sel": "#icims-submit"},
        {"do": "eval", "as": "success", "js": "window.TailorAutofill.detectSuccess()"},
    ])
    assert r["xframes"] == 0
    assert r["values"]["first"] == "Kofi" and r["values"]["auth"] == "Yes"
    assert r["success"] is True                                # the thank-you is inside the frame


@pytest.mark.parametrize("host,oopif", [("localhost", True), ("127.0.0.1", False)])
def test_cross_origin_iframe_through_cdp(tmp_path, host, oopif):
    pdf = tmp_path / "Kofi_Gilbert_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%fixture\n")
    r = run(tmp_path, "icims_outer.html", [
        {"do": "wait", "ms": 300},
        {"do": "inject"},
        {"do": "eval", "as": "top", "js": FILL},
        {"do": "eval", "as": "xframes", "js": "window.TailorAutofill.crossOriginFrames()"},
        {"do": "frames", "as": "ctxs"},
        {"do": "framesEval", "as": "fill", "js": FILL},
        {"do": "framesEval", "as": "fields", "js": "window.TailorAutofill.extractFormAsync({onlyEmpty: true})"},
        {"do": "framesEval", "as": "ops", "js": "window.TailorAutofill.applyOpsAsync(window.TailorAutofill.extractForm({onlyEmpty: true}).filter(f => f.type === 'file').map(f => ({ref: f.ref, op: 'upload'})))"},
        {"do": "framesUpload", "refsFrom": "ops", "file": str(pdf), "as": "uploaded"},
        {"do": "framesEval", "as": "left", "js": "window.TailorAutofill.leftover()"},
        {"do": "frameValues", "as": "values"},
        {"do": "framesEval", "as": "before", "js": "window.TailorAutofill.detectSuccess()"},
        {"do": "frameClick", "sel": "#icims-submit"},
        {"do": "framesEval", "as": "after", "js": "window.TailorAutofill.detectSuccess()"},
        {"do": "route", "as": "route", "ops": [{"ref": "t3"}, {"ref": "PREFIXt1"}]},
    ], query=f"?inner=http://{host}:{{B}}/icims_inner.html")
    assert r["top"]["filled"] == 0 and r["xframes"] == 1       # the top page cannot reach it
    assert len(r["ctxs"]) == 1 and r["ctxs"][0]["oopif"] is oopif
    prefix = r["ctxs"][0]["prefix"]
    assert all(f["ref"].startswith(prefix) for f in r["fields"][0])
    v = r["values"]
    assert (v["first"], v["last"], v["email"], v["phone"], v["auth"]) == \
        ("Kofi", "Gilbert", "kofi@example.com", "555-100-2000", "Yes")
    assert r["uploaded"] == [1] and v["resume"] == "Kofi_Gilbert_Resume.pdf"
    assert r["left"] == [[]]
    assert r["before"] == [False] and r["after"] == [True]
    assert r["route"]["main"] == ["t3"]                        # unprefixed refs stay in the top page
    assert r["route"]["frames"] == [[prefix + "t1"]]


# ------------------------------------------------------------------ multi-page
def test_three_page_form_pages_detected_and_filled(tmp_path):
    sig = "window.TailorAutofill.pageSignature()"
    r = run(tmp_path, "multipage.html", [
        {"do": "inject"},
        {"do": "eval", "as": "p1", "js": FILL},
        {"do": "eval", "as": "s1", "js": sig},
        {"do": "eval", "as": "still1", "js": "fxPage()"},      # the fill never pressed Next
        {"do": "click", "sel": "#next"},                       # the PERSON presses Next
        {"do": "eval", "as": "s2", "js": sig},
        {"do": "isNewPage", "as": "new2", "prev": "s1", "now": "s2"},
        {"do": "eval", "as": "p2", "js": FILL},
        {"do": "eval", "as": "s2b", "js": sig},                # the conditional follow-up appeared
        {"do": "isNewPage", "as": "same2", "prev": "s2", "now": "s2b"},
        {"do": "eval", "as": "left2", "js": "window.TailorAutofill.leftover()"},
        {"do": "click", "sel": "#next"},
        {"do": "eval", "as": "s3", "js": sig},
        {"do": "isNewPage", "as": "new3", "prev": "s2b", "now": "s3"},
        {"do": "eval", "as": "p3", "js": FILL},
        {"do": "eval", "as": "values", "js": "({auth: fx.auth.value, sponsor: fx.sponsor.value, gender: fx.gender.value, vet: document.getElementById('vet').value, page: fxPage()})"},
    ])
    assert r["p1"]["filled"] == 4 and r["still1"] == 1
    assert r["new2"] is True and r["same2"] is False and r["new3"] is True
    assert r["p2"]["screening"] == 2
    assert r["left2"] == ["How many years of experience do you have?", "Anything else we should know?"]
    assert r["p3"]["eeo"] == 2
    v = r["values"]
    assert v == {"auth": "Yes", "sponsor": "Yes", "gender": "I don't wish to answer",
                 "vet": "I do not wish to self-identify", "page": 3}


def test_create_account_wall_is_detected_not_filled(tmp_path):
    r = run(tmp_path, "multipage.html", [
        {"do": "inject"},
        {"do": "eval", "as": "p1", "js": FILL},
        {"do": "click", "sel": "#next"},
        {"do": "eval", "as": "s2", "js": "window.TailorAutofill.pageSignature()"},
        {"do": "eval", "as": "p2", "js": FILL},
        {"do": "eval", "as": "email", "js": "document.getElementById('ae').value"},
    ], query="?wall=1")
    assert r["s2"]["blocker"] == "auth"
    assert r["p2"]["ok"] is False and r["p2"]["reason"] == "blocked"
    assert r["email"] == ""


def test_page_messages():
    out = subprocess.run(["node", "-e", (
        "const p = require(process.argv[1]);"
        "console.log(JSON.stringify([p.pageMessage(2, 9, []), p.pageMessage(3, 1, ['A', 'B']), p.wallMessage(2),"
        " p.isNewPage(new Set(['a','b']), {labels: ['a','b','c']}), p.isNewPage(new Set(['a']), {labels: ['x','y']})]))"),
        str(ROOT / "shell-electron" / "pagewatch.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    msgs = json.loads(out.stdout)
    assert msgs[0] == "Page 2 filled: 9 fields."
    assert msgs[1] == "Page 3 filled: 1 field. 2 left for you."
    assert "sign in or create an account" in msgs[2]
    assert msgs[3] is False and msgs[4] is True
    for m in msgs[:3]:
        assert "—" not in m and "–" not in m


def test_shell_frame_and_page_code_never_clicks():
    """Next and Submit stay the person's: the frame runner and the page watcher only fill."""
    for name in ("frames.js", "pagewatch.js"):
        src = (ROOT / "shell-electron" / name).read_text(encoding="utf-8")
        assert ".click(" not in src and '"click"' not in src
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    watch = main[main.index("function armPageWatch"):main.index("function disarmPageWatch")]
    assert "submitScript" not in watch and ".click(" not in watch
