"""The 2026-11-16 Workday (Uline) and Lever (CrossCountry) defects, each pinned at its cause.

Found on real jobs, re-checked here on local fixtures modelled on the real markup (no personal
values anywhere: the names, dates and schools below are made up):

1. Workday's My Experience fill hang: a Field of Study / Skills list the engine opened stayed open
   (it ignores Escape and blur and draws a radio in every option row), so about 100 option rows
   became "fields" and the model plan ran past the fill's cap. Now the list is closed with a press
   away from it, option rows are never fields, and the plan call is capped with a reason.
2. The CC-305 disability form: the signature "Date" is today's date (typed by the shell with real
   keys), and the checkbox group "Please check one of the boxes below:" takes the saved
   disability answer (its topic read from its choices), the decline box when nothing is saved.
3. Duplicate resume uploads into a Workday draft: a file the upload area already lists by name
   AND size is not uploaded again, and extra verified copies are removed; a same-name row with no
   size is not verified (tests/js/test_upload_identity.js has the full rule).
4. Lever: a custom question's label is its sibling div (with the "✱" that marks it required);
   the location and hybrid-location picks come from the profile's city, the visa-details box from
   the saved work authorization; the page is read again before asking (the agent JS test).
5. Workday's Apply Manually work history and degree: filled from the profile, a section per entry.
6. The loop and cost fixes are in tests/js/test_agent_workday_fixes.js (run below).
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import KnownContext, plan_known
from autoapply.fields import FormField
from autoapply.history import degree_level, field_of_study, form_history, form_skills, parse_span
from autoapply.planner import office_option, visa_detail_text

ROOT = Path(__file__).resolve().parents[1]

try:
    from tests.test_form_widgets import _playwright_dir, run
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir, run  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


# ------------------------------------------------------------------ 5. history from the profile
PROFILE = {
    "experience": [
        {"org": "Northwind Labs", "location": "Springfield, IL",
         "roles": [{"title": "Data Analyst", "dates": "March 2026 - Present",
                    "bullets": [{"text": "Built weekly dashboards."}, "Cut report time in half."]}]},
        {"org": "Contoso Bank", "location": "Riverton, NY",
         "roles": [{"title": "Business Analyst", "dates": "Feb 2018 – Sept 2024", "bullets": []}]},
    ],
    "education": [
        {"school": "Example State University", "degree": "MBA - Business Analytics (STEM), Downtown Campus",
         "date": "Sept 2024 - Dec 2025"},
        {"school": "Sample College", "degree": "Bachelor of Arts in Economics and Sociology", "date": "2011 - 2015"},
    ],
    "skills": {"Analytics": ["SQL", "Tableau"], "Tools": "Excel, Power BI"},
}


def test_dates_degrees_and_fields_are_read_from_the_profile():
    assert parse_span("March 2026 - Present") == {"start": {"m": 3, "y": 2026}, "end": None, "current": True}
    assert parse_span("Feb 2018 – Sept 2024") == {"start": {"m": 2, "y": 2018}, "end": {"m": 9, "y": 2024}, "current": False}
    assert parse_span("03/2019 to 11/2020")["end"] == {"m": 11, "y": 2020}
    assert parse_span("2011 - 2015")["start"] == {"m": 0, "y": 2011}
    assert degree_level("MBA - Business Analytics (STEM)") == "masters"
    assert degree_level("Bachelor of Arts in Economics") == "bachelors"
    assert degree_level("PhD in Physics") == "doctorate"
    assert field_of_study("MBA - Business Analytics (STEM), Downtown Campus") == "Business Analytics"
    assert field_of_study("Bachelor of Arts in Economics and Sociology") == "Economics and Sociology"
    assert field_of_study("Master of Science") == ""
    h = form_history(PROFILE)
    assert [j["title"] for j in h["history"]] == ["Data Analyst", "Business Analyst"]
    assert h["history"][0]["current"] is True and h["history"][0]["end"] is None
    assert h["history"][0]["description"] == "Built weekly dashboards.\nCut report time in half."
    assert [e["level"] for e in h["education"]] == ["masters", "bachelors"]
    assert h["skills"] == ["SQL", "Tableau", "Excel", "Power BI"]
    assert form_skills({}) == [] and form_history({}) == {"history": [], "education": [], "skills": []}


# ------------------------------------------------------------------ 4. Lever picks from the profile
def test_office_and_hybrid_location_come_from_the_profile_city():
    me = {"city": "Chicago", "state": "IL"}
    assert office_option("Which location are you applying for? ✱",
                         ["McLean, VA", "New York, NY", "Chicago, IL", "Dallas, TX"], me) == "Chicago, IL"
    hybrid = ("Are you able to work a hybrid schedule (2-3 days/week in office) from one of the locations "
              "below? If so, please select one.")
    assert office_option(hybrid, ["DC", "NYC", "Boston", "Chicago", "None of these locations"], me) == "Chicago"
    assert office_option(hybrid, ["DC", "NYC", "None of these locations"], me) == "None of these locations"
    assert office_option(hybrid, ["DC", "NYC"], me) is None                 # unclear: theirs
    assert office_option("Which location are you applying for?", ["Boston, MA"], {}) is None   # no city saved
    assert office_option("How did you hear about us?", ["Chicago Tribune"], me) is None


WA = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes"}}}


def test_visa_details_box_gets_the_status_sentence_never_a_bare_yes_or_no():
    q = ("If you require sponsorship or visa-related assistance, please specify your current visa type "
         "and any future sponsorship needs.")
    note = visa_detail_text(q, "textarea", WA)
    assert note and note not in ("Yes", "No") and "sponsorship" in note.lower()
    # a yes/no sponsorship question is not a details box
    assert visa_detail_text("Do you need sponsorship or visa-related assistance to work in the United States?",
                            "text", WA) is None
    assert visa_detail_text(q, "select", WA) is None


class _Lib:
    def __init__(self, rows):
        self.rows = rows

    def lookup(self, q):
        from autoapply.answers import normalize
        k = normalize(q)
        if k in self.rows:
            return {"answer": self.rows[k], "key": k}
        return None

    def mark_used(self, k):
        pass


def _ctx(**kw):
    base = dict(work_auth=WA, library=_Lib({}), applicant={"city": "Chicago", "state": "IL"}, job_country="US")
    base.update(kw)
    return KnownContext(**base)


def test_lever_custom_questions_are_answered_and_not_left():
    fields = [
        FormField(ref="t1", label="Which location are you applying for? ✱", type="select",
                  options=["McLean, VA", "New York, NY", "Chicago, IL"], required=True),
        FormField(ref="t2", label="If you require sponsorship or visa-related assistance, please specify your "
                                  "current visa type and any future sponsorship needs. ✱", type="textarea", required=True),
        FormField(ref="t3", label="Are you able to work a hybrid schedule (2-3 days/week in office) from one of the "
                                  "locations below? If so, please select one. ✱", type="select",
                  options=["DC", "NYC", "Chicago", "None of these locations"], required=True),
    ]
    res = plan_known(fields, _ctx())
    got = {a.ref: a for a in res.actions}
    assert got["t1"].value == "Chicago, IL" and got["t3"].value == "Chicago"
    assert got["t2"].op == "fill" and got["t2"].value not in ("Yes", "No") and len(got["t2"].value) > 20
    assert not res.left


# ------------------------------------------------------------------ 2. the CC-305 checkbox group
CC305 = ["Yes, I have a disability, or have had one in the past",
         "No, I do not have a disability and have not had one in the past",
         "I do not want to answer"]


def _cc305_fields():
    q = "Please check one of the boxes below:*"                  # the engine's label, mark and all
    return [FormField(ref=f"c{i}", label=f"{q}: {o}", type="checkbox") for i, o in enumerate(CC305)]


def test_cc305_group_takes_the_saved_disability_answer():
    lib = _Lib({"@disability": "No, I do not have a disability"})
    res = plan_known(_cc305_fields(), _ctx(library=lib))
    checked = [a.ref for a in res.actions if a.op == "check"]
    assert checked == ["c1"], res.actions                        # exactly the one box, the saved "No"
    assert all(a.origin == "library" for a in res.actions)


def test_cc305_group_with_nothing_saved_takes_the_decline_box():
    res = plan_known(_cc305_fields(), _ctx())
    assert [a.ref for a in res.actions if a.op == "check"] == ["c2"]
    res = plan_known(_cc305_fields(), _ctx(eeo_decline=True))
    assert [a.ref for a in res.actions if a.op == "check"] == ["c2"]


# ------------------------------------------------------------------ browser fixtures
PAYLOAD = {
    "fields": {"first_name": "Avery", "last_name": "Example", "email": "avery@example.com", "city": "Chicago"},
    "screening": {}, "eeo": {"decline": False},
    "history": form_history(PROFILE)["history"], "education": form_history(PROFILE)["education"],
    "skills": ["Excel", "SQL", "Python"],
}
FILL = f"window.TailorAutofill.fillAsync({json.dumps(PAYLOAD)})"


@browser
def test_workday_option_lists_never_become_fields_and_are_closed(tmp_path):
    r = run(tmp_path, "workday_experience.html", [
        {"do": "inject"},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractFormAsync({onlyEmpty: true})"},
        {"do": "eval", "as": "after", "js": "window.fxValues()"},
        {"do": "click", "sel": "[data-automation-id=searchBox]"},       # a person opens the list
        {"do": "eval", "as": "fields2", "js": "window.TailorAutofill.extractForm({onlyEmpty: true})"},
        {"do": "eval", "as": "left2", "js": "window.TailorAutofill.leftover()"},
    ])
    labels = [f["label"] for f in r["fields"]]
    assert not any("not checked" in l or l in ("Accounting", "Economics") for l in labels), labels
    assert r["after"]["open"] is False                          # the engine closed what it opened
    # with a list left open, its option rows are still never fields nor "left for you"
    labels2 = [f["label"] for f in r["fields2"]]
    assert not any("Accounting" in l or "Economics" in l for l in labels2), labels2
    assert not any("Accounting" in l for l in r["left2"])


@browser
def test_workday_work_history_and_education_filled_from_the_profile(tmp_path):
    r = run(tmp_path, "workday_experience.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "wait", "ms": 300},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
    ])
    v, rep = r["values"], r["report"]
    assert v["works"] == 2 and v["edus"] == 2                     # "Add Another" once per extra entry
    assert v["titles"] == ["Data Analyst", "Business Analyst"]
    assert v["companies"] == ["Northwind Labs", "Contoso Bank"]
    assert v["current"] == [True, False]
    assert v["schools"] == ["Example State University", "Sample College"]
    assert v["degrees"] == ["Masters", "Bachelors"]
    # the dates go to the shell to type with real keys: month then year, no To for the current job
    keys = [k["text"] for k in rep["keys"]]
    assert keys == ["3", "2026", "2", "2018", "9", "2024"], keys
    # Field of Study and Skills are searched by the shell (a real Enter), optional, never "left"
    kinds = [s["kind"] for s in rep["searches"]]
    assert kinds == ["field", "field", "skills"], kinds
    assert "business" in rep["searches"][0]["want"] and "administration" in rep["searches"][0]["want"]
    assert rep["searches"][1]["queries"][0] == "Economics and Sociology"
    assert not any(x in ("Field of Study", "Type to Add Skills", "Location", "Role Description") for x in r["left"])


@browser
def test_search_choice_picks_a_real_option_the_degree_supports(tmp_path):
    # MBA in Business Analytics: no such option; Business Administration is what an MBA says
    js = ("(async () => { const T = window.TailorAutofill; const el = document.querySelectorAll('[data-automation-id=searchBox]')[0];"
          " const ref = T.fieldRef(el); return T.searchChoice(ref, {kind: 'field', want: ['business', 'analytics', 'administration']}); })()")
    js2 = ("(async () => { const T = window.TailorAutofill; const el = document.querySelectorAll('[data-automation-id=searchBox]')[0];"
           " const ref = T.fieldRef(el); return T.searchChoice(ref, {kind: 'field', want: ['economics', 'sociology']}); })()")
    r = run(tmp_path, "workday_experience.html", [
        {"do": "inject"},
        {"do": "keys", "sel": "[data-automation-id=searchBox] >> nth=0", "text": "Business", "enter": True},
        {"do": "wait", "ms": 100},
        {"do": "eval", "as": "pick", "js": js},
        {"do": "eval", "as": "chosen", "js": "window.fx.fos[0].value"},
        {"do": "keys", "sel": "[data-automation-id=searchBox] >> nth=0", "text": "Economics", "enter": True},
        {"do": "eval", "as": "econ", "js": js2},
        {"do": "eval", "as": "chosen2", "js": "window.fx.fos[0].value.slice(1)"},
        {"do": "keys", "sel": "[data-automation-id=searchBox] >> nth=0", "text": "Agricultural", "enter": True},
        {"do": "eval", "as": "agri", "js": js2},
        {"do": "keys", "sel": "[data-automation-id=searchBox] >> nth=0", "text": "zzz", "enter": True},
        {"do": "eval", "as": "none", "js": js2},
    ])
    # "Agricultural Business and Management" shares "business" and was picked once (Uline,
    # 2026-11-16); it adds a subject the degree never names, so it never fits
    assert r["pick"]["ok"] is True and r["chosen"] == ["Business Administration/Management"]
    assert r["econ"]["ok"] is True and r["chosen2"] == ["Economics"]       # not "Agricultural Economics"
    assert r["agri"]["ok"] is False                               # only Agricultural choices listed: none fit
    assert r["none"]["ok"] is False                                # nothing fits: left empty, no ask


@browser
def test_cc305_date_is_today_and_the_styled_boxes_are_fields(tmp_path):
    r = run(tmp_path, "cc305_selfid.html", [
        {"do": "inject"},
        {"do": "eval", "as": "report", "js": "window.TailorAutofill.fillAsync({fields: {}, screening: {}, eeo: {}})"},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractForm({onlyEmpty: true})"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
        # the plan's op for the saved answer's box (planner test above): exactly that one box
        {"do": "eval", "as": "applied", "js": "(() => { const T = window.TailorAutofill; const f = T.extractForm({onlyEmpty: true});"
                                              " const no = f.find((x) => / No, I do not have/.test(x.label));"
                                              " return T.applyOps([{ref: no.ref, op: 'check', text: 'Yes'}]); })()"},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "left2", "js": "window.TailorAutofill.leftover()"},
    ])
    today = _dt.date.today()
    assert [k["text"] for k in r["report"]["keys"]] == [str(today.month), str(today.day), str(today.year)]
    # ONE question with three choices (the legend of the OUTER fieldset), never three questions
    boxes = [f["label"] for f in r["fields"] if f["type"] == "checkbox"]
    assert boxes == [f"Please check one of the boxes below:*: {o}" for o in CC305], boxes
    # (Name and Date are open here: no profile in this test, and the shell types the date)
    assert [x for x in r["left"] if x not in ("Name", "Date")] == ["Please check one of the boxes below:"], r["left"]
    assert not any(o in r["left"] for o in CC305)
    assert r["applied"]["filled"] == 1 and r["values"]["boxes"] == [False, True, False]
    assert "Please check one of the boxes below:" not in r["left2"]   # one box ticked answers the group


@browser
def test_cc305_signature_date_only_on_a_signature_page(tmp_path):
    r = run(tmp_path, "workday_experience.html", [
        {"do": "inject"},
        {"do": "eval", "as": "n", "js": "window.TailorAutofill.fillSignatureDate()"},
    ])
    assert r["n"] == 0


@browser
def test_a_file_the_area_already_lists_is_not_uploaded_again(tmp_path):
    copies = "window.TailorAutofill.fileCopies(document.getElementById('file'), 'Avery Example Resume.pdf', 108810)"
    r = run(tmp_path, "upload_listed.html", [
        {"do": "inject"},
        {"do": "eval", "as": "none", "js": copies},
        {"do": "eval", "js": "window.addFile('Avery Example Resume.pdf', '106.26 KB')"},
        {"do": "eval", "as": "one", "js": copies},
        {"do": "eval", "as": "other_size", "js": "window.TailorAutofill.fileCopies(document.getElementById('file'), 'Avery Example Resume.pdf', 512000)"},
        {"do": "eval", "as": "listed", "js": "window.TailorAutofill.fileListed('', 'Avery Example Resume.pdf', 108810)"},
        {"do": "eval", "js": "(window.addFile('Avery Example Resume.pdf', '106.26 KB'), window.addFile('Avery Example Resume.pdf', '106.26 KB'))"},
        {"do": "eval", "as": "three", "js": copies},
        {"do": "eval", "as": "dropped", "js": "window.TailorAutofill.dropDuplicateFiles('', 'Avery Example Resume.pdf', 108810)"},
        {"do": "wait", "ms": 50},
        {"do": "eval", "as": "left", "js": "window.fxCount()"},
        {"do": "eval", "as": "letter", "js": "window.TailorAutofill.fileCopies(document.getElementById('cl'), 'Avery Example Resume.pdf', 108810)"},
        # a row with no size is never "already there" (an earlier application's file has the same name)
        {"do": "eval", "js": "window.addFile('Avery Example Resume.pdf', '')"},
        {"do": "eval", "as": "listing", "js": "window.TailorAutofill.fileListing('', 'Avery Example Resume.pdf', 108810)"},
    ])
    assert r["none"] == 0 and r["one"] == 1 and r["listed"] is True
    assert r["other_size"] == 0                                  # same name, another file: not the same
    assert r["three"] == 3 and r["dropped"] == 2 and r["left"] == 1
    assert r["letter"] == 0                                      # the cover letter box's area is its own
    assert r["listing"] == {"copies": 2, "same": 1, "unsized": 1, "other": 0}


@browser
def test_lever_custom_question_labels_and_required_marks(tmp_path):
    r = run(tmp_path, "lever_custom.html", [
        {"do": "inject"},
        {"do": "eval", "as": "fields", "js": "window.TailorAutofill.extractForm({onlyEmpty: true})"},
        {"do": "eval", "as": "left", "js": "window.TailorAutofill.leftover()"},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "eval", "as": "org", "js": "document.querySelector('[name=org]').value"},
    ])
    # "Current company" is the job the profile marks current, never asked (CrossCountry 2026-11-16)
    assert r["org"] == "Northwind Labs"
    by = {f["label"].rstrip(" ✱"): f for f in r["fields"]}
    loc = by["Which location are you applying for?"]
    assert loc["required"] is True and "Chicago, IL" in loc["options"]
    visa = next(f for k, f in by.items() if k.startswith("If you require sponsorship"))
    hybrid = next(f for k, f in by.items() if k.startswith("Are you able to work a hybrid schedule"))
    assert visa["required"] is True and visa["type"] == "textarea"
    assert hybrid["required"] is True and "None of these locations" in hybrid["options"]
    assert by["Anything else you would like to share?"]["required"] is False
    assert "Anything else you would like to share?" not in r["left"]          # optional: never left


# ------------------------------------------------------------------ the agent loop (no Electron)
def test_agent_loop_fixes_js():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_workday_fixes.js")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]


# ------------------------------------------------------------------ 4b. "Current company" from the profile
def test_current_company_and_title_are_placeholders_from_the_saved_experience():
    from autoapply.fields import APPLICANT_KEYS, applicant_from_record, current_job
    assert "current_company" in APPLICANT_KEYS and "current_title" in APPLICANT_KEYS
    assert current_job(PROFILE) == {"current_company": "Northwind Labs", "current_title": "Data Analyst"}
    # no role marked Present: the most recent (first listed) one
    past = {"experience": [{"org": "Contoso Bank", "roles": [{"title": "Business Analyst", "dates": "2018 - 2024"}]},
                           {"org": "Fabrikam", "roles": [{"title": "Intern", "dates": "2016 - 2017"}]}]}
    assert current_job(past) == {"current_company": "Contoso Bank", "current_title": "Business Analyst"}
    assert current_job({}) == {}
    app = applicant_from_record({"profile": dict(PROFILE, identity={"name": "Avery Example"})})
    assert app["current_company"] == "Northwind Labs" and app["current_title"] == "Data Analyst"
    # Lever's "Current company ✱" (and the other wordings) resolve to the placeholder, never left
    fields = [FormField(ref="o1", label="Current company ✱", type="text", required=True),
              FormField(ref="o2", label="Most recent employer", type="text"),
              FormField(ref="o3", label="Current job title", type="text")]
    res = plan_known(fields, _ctx(applicant=dict(app, city="Chicago")))
    src = {a.ref: a.source for a in res.actions}
    assert src == {"o1": "current_company", "o2": "current_company", "o3": "current_title"}, res.actions
    assert not res.left
    # with no job in the profile it is the person's to answer, never a guess
    res = plan_known(fields[:1], _ctx(applicant={"city": "Chicago"}))
    assert not res.actions and [f.ref for f in res.left] == ["o1"]
