"""What the SITE fills, put right (2026-11-26, measured on real Ashby, Workday and Lam forms).

A site's own resume parse ("Autofill with Resume", Ashby's parse after the upload) or a saved
draft filled the form before or after the engine did:
1. the email printed on the resume instead of the application email;
2. a phone with its country code next to the page's own Country Phone Code (Workday: "Enter a
   valid format for Phone Number", and the agent retyped it six steps in a row);
3. the street in County;
4. a project read as a job, the real job with the parse's title and dates, a course read as a
   school.
The engine puts each right against the profile, the final check before Ready to send names what
is still wrong, and the brain's typed contact values take the form their box takes. The fixture
(tests/fixtures/forms/workday_prefilled.html) holds made-up values only. The agent loop's side
(Apply Manually by the shell, the phone retyped once, the check at Ready to send) is
tests/js/test_agent_site_fills.js, run below.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply.brain import contact_form, resolve_actions
from autoapply.history import form_history

ROOT = Path(__file__).resolve().parents[1]

try:
    from tests.test_form_widgets import _playwright_dir, run
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir, run  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")

PROFILE = {
    "experience": [
        {"org": "Northwind Labs", "roles": [{"title": "Data Analyst", "dates": "March 2026 - Present"}]},
        {"org": "Contoso Bank", "roles": [{"title": "Business Analyst", "dates": "Feb 2018 - Sept 2024"}]},
    ],
    "education": [
        {"school": "Example State University", "degree": "MBA", "date": "2024 - 2025"},
        {"school": "Sample College", "degree": "Bachelor of Arts in Economics", "date": "2011 - 2015"},
    ],
}
APPLY_EMAIL = "apply.person@example.com"
PAYLOAD = {
    "fields": {"first_name": "Avery", "last_name": "Example", "email": APPLY_EMAIL, "phone": "5550102000",
               "street": "12 Sample Street", "city": "Springfield", "zip": "60601"},
    "other_emails": ["resume.person@example.org"],
    "screening": {}, "eeo": {"decline": False},
    "history": form_history(PROFILE)["history"], "education": form_history(PROFILE)["education"],
}
FILL = f"window.TailorAutofill.fillAsync({json.dumps(PAYLOAD)}, {{promptMs: 500}})"
ISSUES = f"window.TailorAutofill.prefillIssues({json.dumps(PAYLOAD)})"


# ------------------------------------------------------------------ the brain's typed values
def test_a_phone_box_takes_digits_and_an_email_box_the_application_email():
    me = {"email": APPLY_EMAIL, "phone": "5550102000"}
    assert contact_form("answer_1", "+1 (555) 010-2000", "Phone Number", me) == "5550102000"
    assert contact_form("phone", "555-010-2000", "Mobile phone", me) == "5550102000"
    # an extension, a device type and a country code are not the number
    assert contact_form("answer_2", "12", "Phone Extension", me) == "12"
    assert contact_form("answer_3", "Mobile", "Phone Device Type", me) == "Mobile"
    # a saved answer holding the resume's email, in the applicant's own email box
    assert contact_form("answer_4", "resume.person@example.org", "Email Address", me) == APPLY_EMAIL
    # someone else's email is theirs; the portal sign-in stays the portal's
    assert contact_form("answer_5", "friend@example.net", "Referrer Email", me) == "friend@example.net"
    assert contact_form("portal_email", "portal@example.com", "Email Address", me) == "portal@example.com"


def test_resolve_actions_applies_the_box_form():
    me = {"email": APPLY_EMAIL, "phone": "5550102000", "answer_1": "+1 (555) 010-2000"}
    acts = [{"op": "type", "index": 27, "text": "{{answer_1}}"}, {"op": "type", "index": 4, "text": "{{answer_1}}"}]
    out = resolve_actions(acts, me, None, {27: "Phone Number", 4: "Years of experience"}, [])
    assert out[0]["text"] == "5550102000" and out[0]["sensitive"] is True
    assert out[1]["text"] == "+1 (555) 010-2000"                # not a phone box: as saved


# ------------------------------------------------------------------ the engine, in a real page
@browser
def test_the_site_parse_is_put_right_against_the_profile(tmp_path):
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "inject"},
        {"do": "eval", "as": "before", "js": ISSUES},
        {"do": "eval", "as": "report", "js": FILL},
        {"do": "wait", "ms": 400},
        {"do": "eval", "as": "values", "js": "window.fxValues()"},
        {"do": "eval", "as": "after", "js": ISSUES},
    ])
    before, v, rep = r["before"], r["values"], r["report"]
    # the read-only check names each problem in words, never a value
    joined = " | ".join(before)
    assert "not your application email" in joined, before
    assert "Work Experience 2 is not a job in your profile" in joined, before
    assert "Education 2 is a school that is not in your profile" in joined, before
    assert "resume.person@example.org" not in joined and "Sample Street" not in joined
    # contact: the application email (read back), the referrer's own, the phone as digits next to
    # the page's country code, the extension empty, the street out of County
    assert v["email"] == APPLY_EMAIL
    assert v["referrer"] == "friend@example.net"
    assert v["phone"] == "5550102000"
    assert v["ext"] == ""
    assert v["county"] == "" and v["line1"] == "12 Sample Street" and v["city"] == "Springfield"
    assert rep["contact"]["email"] >= 1 and rep["contact"]["phone"] >= 1 and rep["contact"]["address"] >= 1
    # history: the project removed, the real job's title and From month the profile's, the course removed
    assert v["companies"] == ["Northwind Labs Inc.", "Contoso Bank"], v["companies"]
    assert v["titles"] == ["Data Analyst", "Business Analyst"], v["titles"]
    assert v["dates"][:2] == ["3", "2026"] and v["dates"][4:] == ["2", "2018", "9", "2024"], v["dates"]
    assert v["schools"] == ["Example State University"], v["schools"]
    assert rep["history_fixed"] >= 3
    # and the final check has nothing left to say
    assert r["after"] == [], r["after"]


@browser
def test_a_parse_after_the_upload_is_put_right_again(tmp_path):
    fields = json.dumps(PAYLOAD["fields"])
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "inject"},
        {"do": "eval", "as": "armed", "js": f"window.TailorAutofill.guardContact({fields}, 3000)"},
        {"do": "wait", "ms": 700},
        {"do": "eval", "as": "first", "js": "document.getElementById('email').value"},
        {"do": "eval", "js": "window.fxParseAgain()"},              # the site's parse lands later
        {"do": "wait", "ms": 900},
        {"do": "eval", "as": "second", "js": "document.getElementById('email').value"},
    ])
    assert r["armed"]["armed"] is True
    assert r["first"] == APPLY_EMAIL and r["second"] == APPLY_EMAIL


@browser
def test_the_phone_is_handed_to_the_shell_once_to_type(tmp_path):
    fields = json.dumps(PAYLOAD["fields"])
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "inject"},
        {"do": "eval", "as": "keys", "js": f"window.TailorAutofill.phoneRetype({fields})"},
        {"do": "eval", "as": "again", "js": f"window.TailorAutofill.phoneRetype({fields})"},
        {"do": "eval", "as": "box", "js": "document.getElementById('phone').value"},
        {"do": "eval", "as": "coded", "js": "window.TailorAutofill.phoneCodeOnPage()"},
    ])
    assert len(r["keys"]) == 1 and r["keys"][0]["text"] == "5550102000"
    assert r["again"] == []                                       # once per box
    assert r["box"] == "" and r["coded"] is True


# ------------------------------------------------------------------ the agent loop (no Electron)
def test_agent_site_fill_fixes_js():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_site_fills.js")],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]
