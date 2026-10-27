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


# ------------------------------------------------------------------ native radios via the plan
@pytest.mark.parametrize("fn", ["applyOps", "applyOpsAsync"])
def test_planned_answer_reaches_a_native_radio_group(tmp_path, fn):
    """A plan op for a radio group ("select" with the option's text) picks that option. It used to
    fall through fieldType() as a text box and never reach pickRadio."""
    js = ("(async () => { const T = window.TailorAutofill; const f = T.extractForm({onlyEmpty: true});"
          " const ref = (lab) => f.find(x => x.label.startsWith(lab)).ref;"
          f" const rep = await T.{fn}([{{ref: ref('What is your highest'), op: 'select', text: \"Master's degree\"}},"
          " {ref: ref('Are you willing'), op: 'select', text: 'Yes'}]);"
          " return {rep, types: f.map(x => x.type)}; })()")
    r = run(tmp_path, "native_radio.html", [
        {"do": "inject"},
        {"do": "eval", "as": "out", "js": js},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "again", "js": js.replace("onlyEmpty: true", "onlyEmpty: false")},
        {"do": "eval", "as": "values2", "js": "window.fxValues()"},
    ])
    assert r["out"]["types"] == ["radio", "radio"]
    assert r["out"]["rep"]["filled"] == 2 and r["out"]["rep"]["skipped"] == 0
    assert r["values"] == {"edu": "ms", "onsite": "y"}
    assert r["again"]["rep"]["filled"] == 0                    # an answered group is never changed
    assert r["values2"] == r["values"]


# ------------------------------------------------------------------ real-page patterns
# Each fixture below reproduces markup found on a REAL public application page in October 2026
# (field labels only); the engine was run there, read back, and these are the things it got
# wrong. See the fixture headers for which system and what pattern.
EXTRACT = "window.TailorAutofill.extractFormAsync({onlyEmpty: false})"
FILL_NAMED = ("window.TailorAutofill.fillAsync("
              + json.dumps(dict(PAYLOAD, fields=dict(PAYLOAD["fields"], name="Kofi Gilbert"))) + ")")


def _labels(fields):
    return {f["label"]: f for f in fields}


def test_greenhouse_live_labels_hidden_mirror_and_file_groups(tmp_path):
    r = run(tmp_path, "greenhouse_live.html", [
        {"do": "inject"},
        {"do": "eval", "as": "fields", "js": EXTRACT},
    ], query="?delay=50")
    by = _labels(r["fields"])
    # The phone's country picker is labelled by its own label, not the fieldset's "Phone" legend.
    assert by["Country"]["type"] == "select" and "United States +1" in by["Country"]["options"]
    # react-select's aria-hidden "required" mirror input is not a field (it showed up as "Phone").
    assert [f["label"] for f in r["fields"] if f["label"] == "Phone"] == ["Phone"]
    assert by["Phone"]["type"] == "tel"
    # A file input whose own label is its "Attach" button takes the group's question.
    assert by["Resume/CV*"]["type"] == "file" and by["Cover Letter"]["type"] == "file"
    assert "Attach" not in by and "Enter manually" not in by
    # One box of a multi-choice question says which question it answers.
    assert "How did you hear about us?*: LinkedIn" in by


def test_greenhouse_slow_location_search_is_waited_for_not_typed(tmp_path):
    r = run(tmp_path, "greenhouse_live.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])                                                         # options arrive after 3.2 s
    assert r["values"]["location"] == "Chicago, Illinois, United States"
    assert "Location (City)" not in r["left"]
    assert r["values"]["first"] == "Kofi" and r["values"]["phone"] == "555-100-2000"


def test_greenhouse_location_with_no_match_is_not_counted(tmp_path):
    # The search finds nothing: the typed text is NOT an answer in a select-style box (react-select
    # drops it on blur), so it must be left for the person, not counted as filled.
    r = run(tmp_path, "greenhouse_live.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
        {"do": "eval", "as": "shown", "js": "window.TailorAutofill.comboValue(document.getElementById('candidate-location'))"},
    ], query="?delay=50&empty=1")
    assert r["values"]["location"] is None and r["shown"] == ""
    assert r["report"]["widgets"] == 0
    assert "Location (City)" in r["left"]
    assert "How did you hear about us?" in r["left"]           # the group once, by its question
    assert not any(x in ("LinkedIn", "Referral", "Other", "Attach") for x in r["left"])


def test_lever_wrapped_labels_and_option_groups(tmp_path):
    r = run(tmp_path, "lever_apply.html", [
        {"do": "inject"},
        {"do": "eval", "as": "fields", "js": EXTRACT},
        {"do": "eval", "as": "report", "js": FILL_NAMED},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])
    labels = [f["label"] for f in r["fields"]]
    # Headings only: no hidden status text, no <select> options glued onto the label.
    assert "Resume/CV ✱" in labels or "Resume/CV✱" in labels
    assert "What is your location?" in labels
    assert not any("Couldn't" in l or "No location found" in l or "Afghanistan" in l or "Canada" in l
                   for l in labels)
    # Radio groups are named by their question, checkboxes by "question: option".
    radios = [f for f in r["fields"] if f["type"] == "radio"]
    assert [f["label"].rstrip("✱ ") for f in radios] == [
        "Are you legally authorized to work in the United States?", "Are you open to working on site?"]
    assert radios[0]["options"] == ["yes", "no"]
    assert "Pronouns: He/him" in labels
    v = r["values"]
    assert v["name"] == "Kofi Gilbert" and v["auth"] == "Yes" and v["onsite"] is None
    # Left for the person: each question once, by its heading (it used to say "Yes" and "No").
    assert "Have you worked here before?" in r["left"] and "Are you open to working on site?" in r["left"]
    assert not {"Yes", "No"} & set(r["left"])


def test_ashby_radio_question_from_heading_not_first_option(tmp_path):
    r = run(tmp_path, "ashby_application.html", [
        {"do": "inject"},
        {"do": "eval", "as": "fields", "js": EXTRACT},
        {"do": "eval", "as": "report", "js": FILL_NAMED},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])
    radios = [f for f in r["fields"] if f["type"] == "radio"]
    assert radios[0]["label"] == "What is your current geographic availability for this role?"
    assert radios[1]["label"] == "Are you legally authorized to work in the United States?"
    assert r["values"]["auth"] == "auth-0" and r["values"]["geo"] is None
    assert r["report"]["screening"] == 1
    assert r["left"] == ["What is your current geographic availability for this role?"]


def test_workable_essay_mentioning_address_is_not_given_the_street(tmp_path):
    payload = dict(PAYLOAD, fields=dict(PAYLOAD["fields"], address="100 Main St", street="100 Main St"))
    r = run(tmp_path, "workable_apply.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": f"window.TailorAutofill.fillAsync({json.dumps(payload)})"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "fields", "js": EXTRACT},
    ])
    v = r["values"]
    assert v["first"] == "Kofi" and v["address"] == "100 Main St"
    assert v["essay"] == ""                                    # "...how did you address them?"
    labels = [f["label"] for f in r["fields"]]
    assert "Address" in labels and not any("SVGs not supported" in l for l in labels)
    photo = next(f for f in r["fields"] if f["type"] == "file")
    assert photo["label"] == "Photo Choose file"


def test_workday_create_account_wall_and_robot_trap(tmp_path):
    payload = dict(PAYLOAD, fields=dict(PAYLOAD["fields"], website="https://example.com"))
    fill = f"window.TailorAutofill.fillAsync({json.dumps(payload)})"
    wall = run(tmp_path, "workday_account.html", [
        {"do": "inject"},
        {"do": "eval", "as": "blocker", "js": "window.TailorAutofill.pageBlocker()"},
        {"do": "eval", "as": "report", "js": fill},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ])
    assert wall["blocker"] == "auth"
    assert wall["report"]["ok"] is False and wall["report"]["reason"] == "blocked"
    assert wall["values"] == {"email": "", "website": ""}      # nothing behind the wall
    open_ = run(tmp_path, "workday_account.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": fill},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "fields", "js": EXTRACT},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ], query="?open=1")
    assert open_["values"] == {"email": "kofi@example.com", "website": ""}   # the trap stays empty
    assert not any("robots" in f["label"] for f in open_["fields"])
    assert not any("robots" in l for l in open_["left"])


def test_shell_says_captcha_when_a_bot_check_replaces_the_form():
    """SmartRecruiters served headless Chromium a full-page DataDome check instead of the form: no
    fields, a captcha frame. The shell reports it as a captcha for the person (the UI then says
    "Solve it, then click Fill this page again"), not as "no form on this page"."""
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    run_fn = main[main.index("async function runAutoApplyOnPane"):main.index("// ------------------------------------------------------------- multi-page forms")]
    branch = run_fn[run_fn.index("!fields.length && !left.length"):]
    assert branch.index('blocker === "captcha"') < branch.index('reason: "no-fields"')
    app_js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'r.blocker === "captcha"' in app_js


def test_fields_scrolled_above_the_fold_are_still_filled(tmp_path):
    """The person scrolled to the bottom, then pressed Fill: the top fields have a negative
    viewport rect but are on the page, unlike a honeypot parked at left:-9999px."""
    r = run(tmp_path, "lever_apply.html", [
        {"do": "eval", "js": "document.body.appendChild(Object.assign(document.createElement('div'), {style: 'height:4000px'})) && 1"},
        {"do": "eval", "js": "document.body.appendChild(Object.assign(document.createElement('input'), {type: 'text', name: 'email2', placeholder: 'Email', style: 'position:absolute;left:-9999px;top:0'})) && 1"},
        {"do": "eval", "js": "window.scrollTo(0, 3000) || window.scrollY"},
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL_NAMED},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "email", "js": "document.querySelector('[name=email]').value"},
        {"do": "eval", "as": "trap", "js": "document.querySelector('[name=email2]').value"},
        {"do": "eval", "as": "labels", "js": "window.TailorAutofill.extractForm({}).map(f => f.label)"},
    ])
    assert r["values"]["name"] == "Kofi Gilbert" and r["email"] == "kofi@example.com"
    assert r["trap"] == ""
    assert any(l.startswith("Full name") for l in r["labels"]) and "Email" not in r["labels"]


def test_recipe_never_types_into_a_dropdown_search_box(tmp_path):
    """Greenhouse's recipe lists #candidate-location, which on the current boards is a react-select
    search box: typing "Chicago, IL" there was counted as filled though nothing was chosen. The
    widget layer picks the option instead, and only that counts."""
    recipes = {"version": 1, "systems": [{"id": "greenhouse", "name": "Greenhouse",
               "url_patterns": ["http://127.0.0.1*"],
               "fields": {"first_name": ["#first_name"], "location": ["#candidate-location"]}}]}
    payload = dict(PAYLOAD, recipes=recipes)
    r = run(tmp_path, "greenhouse_live.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": f"window.TailorAutofill.fillAsync({json.dumps(payload)})"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
    ], query="?delay=50")
    rep = r["report"]
    assert rep["system"] == "greenhouse" and rep["via_recipe"] == 1          # first name only
    assert r["values"]["location"] == "Chicago, Illinois, United States"
    assert rep["widgets"] == 1


# ------------------------------------------------------------------ a job page is not a form
def test_job_page_search_boxes_are_never_filled(tmp_path):
    """Fill on a job description page (amazon.jobs, before Apply) once typed the person's city
    into the site's job search. The search boxes are never filled, the page is not an
    application form, and the site's own Apply button is named for the message."""
    r = run(tmp_path, "job_page.html", [
        {"do": "inject"},
        {"do": "eval", "as": "form", "js": "window.TailorAutofill.applicationForm()"},
        {"do": "eval", "as": "apply", "js": "window.TailorAutofill.applyButtonLabel()"},
        {"do": "eval", "as": "fill", "js": FILL},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractForm({}).map(f => f.label)"},
        {"do": "eval", "as": "values", "js": "[...document.querySelectorAll('header input')].map(i => i.value)"},
    ])
    assert r["form"] is False
    assert r["apply"] == "Apply now"
    assert r["values"] == ["", ""]
    assert not any("search" in (l or "").lower() or "city" in (l or "").lower() for l in r["fields"])


def test_real_application_forms_still_count_as_forms(tmp_path):
    for page in ("lever_apply.html", "react_select.html", "workday.html", "ashby_application.html",
                 "greenhouse_live.html", "workable_apply.html", "multipage.html", "native_radio.html"):
        r = run(tmp_path, page, [{"do": "inject"},
                                 {"do": "eval", "as": "form", "js": "window.TailorAutofill.applicationForm()"}])
        assert r["form"] is True, page
