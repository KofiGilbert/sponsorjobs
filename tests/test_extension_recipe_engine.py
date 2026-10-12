"""The fill engine's RECIPE layer (extension/autofill_core.js) on trimmed snippets of REAL
application pages (tests/fixtures/recipes/*.html, fetched 2026-10-11).

Runs the real engine under node in a small DOM (tests/js/recipe_harness.js) the way
tests/test_extension_sponsor_core.py runs the sponsor matcher: same code the browser runs, no
browser. What it pins: the recipe for a URL is found, its selectors fill the right controls, the
label patterns still cover what the recipe does not, the submit button is OUTLINED and never
clicked, multi-page sites get a hint instead of a click, the success marker is detected, and a
value the person changes afterwards is offered back as a correction.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "extension" / "autofill_core.js"
RECIPES = ROOT / "autoapply" / "recipes.json"
HARNESS = ROOT / "tests" / "js" / "recipe_harness.js"
FIX = ROOT / "tests" / "fixtures" / "recipes"

PAYLOAD = {
    "fields": {"first_name": "Kofi", "last_name": "Gilbert", "email": "kofi@example.com",
               "phone": "555-100-2000", "street": "1 Main St", "city": "Chicago", "state": "IL",
               "zip": "60601", "linkedin": "https://linkedin.com/in/kofi", "website": "https://kofi.dev"},
    "screening": {"work_auth_us": "Yes", "needs_sponsorship": "Future"},
    "eeo": {"decline": True},
}


def _run(tmp_path, fixture, url, actions, payload=PAYLOAD, html=None):
    sc = {"url": url, "payload": payload, "actions": actions}
    if html is not None:
        sc["html"] = html
    else:
        sc["fixture"] = str(FIX / fixture)
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(sc), encoding="utf-8")
    out = subprocess.run(["node", str(HARNESS), str(CORE), str(RECIPES), str(p)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


FILL = [{"op": "recipeFor", "as": "recipe"}, {"op": "fill", "as": "report"}]


def test_greenhouse_fills_by_recipe_and_outlines_submit(tmp_path):
    r = _run(tmp_path, "greenhouse.html", "https://job-boards.greenhouse.io/anthropic/jobs/5183044008", FILL + [
        {"op": "values", "as": "v", "of": {"first": "#first_name", "last": "#last_name", "email": "#email",
                                           "phone": "#phone", "q": "#question_15639912008"}},
        {"op": "classOf", "as": "submit", "sel": "form#application-form button[type='submit']"},
        {"op": "detectSuccess", "as": "success"},
    ])
    assert r["recipe"] == "greenhouse"
    rep = r["report"]
    assert rep["ok"] and rep["system"] == "greenhouse" and rep["via_recipe"] >= 4
    assert r["v"] == {"first": "Kofi", "last": "Gilbert", "email": "kofi@example.com", "phone": "555-100-2000", "q": ""}
    assert rep["submit_highlighted"] is True and "tailor-submit-hint" in r["submit"]
    assert rep["next_page"] is False and rep["resume_upload"] is True
    assert r["clicks"] == 0                          # nothing on the page was clicked
    assert r["success"] is False                     # the form is still open, not the thank-you page


def test_lever_recipe_fills_name_location_and_urls_then_labels_answer_screening(tmp_path):
    r = _run(tmp_path, "lever.html", "https://jobs.lever.co/abovelending/0adfabd0-e18b-495e-add5-52a2521043dd/apply", FILL + [
        {"op": "values", "as": "v", "of": {
            "name": "input[name='name']", "email": "input[name='email']", "phone": "input[name='phone']",
            "loc": "#location-input", "org": "input[name='org']", "li": "input[name='urls[LinkedIn]']",
            "web": "input[name='urls[Website]']",
            "spon_yes": "input[name='cards[b2f462e2-cf07-4a4d-ab65-6a7403d0af0b][field0]'][value='Yes']",
            "spon_no": "input[name='cards[b2f462e2-cf07-4a4d-ab65-6a7403d0af0b][field0]'][value='No']",
            "auth_yes": "input[name='cards[a52002df-7a12-41bf-ba86-d7c9ef1fcc88][field0]'][value='Yes']"}},
        {"op": "classOf", "as": "submit", "sel": "#btn-submit"},
    ])
    assert r["recipe"] == "lever"
    v = r["v"]
    assert v["name"] == "Kofi Gilbert"               # full name derived from the parts
    assert v["loc"] == "Chicago, IL"                 # location derived from city + state
    assert v["email"] == "kofi@example.com" and v["phone"] == "555-100-2000"
    assert v["li"] == "https://linkedin.com/in/kofi" and v["web"] == "https://kofi.dev"
    assert v["org"] == ""                            # current company: nothing saved, nothing guessed
    # Screening from the saved answers: "now or in the future" sponsorship -> Yes for Future,
    # work authorization -> Yes. Set through the radio, never a guess.
    assert v["spon_yes"] is True and v["spon_no"] is False and v["auth_yes"] is True
    assert r["report"]["screening"] == 2
    assert "tailor-submit-hint" in r["submit"] and r["clicks"] == 0


def test_future_sponsorship_leaves_a_present_tense_question_alone(tmp_path):
    html = """<form><div class="application-question"><div class="application-label">Do you currently require visa sponsorship?</div>
      <ul><li><label><input type="radio" name="q" value="Yes"/> Yes</label></li><li><label><input type="radio" name="q" value="No"/> No</label></li></ul></div></form>"""
    r = _run(tmp_path, None, "https://example.com/apply", FILL + [
        {"op": "values", "as": "v", "of": {"yes": "input[name='q'][value='Yes']", "no": "input[name='q'][value='No']"}}], html=html)
    assert r["v"] == {"yes": False, "no": False} and r["report"]["screening"] == 0
    r = _run(tmp_path, None, "https://example.com/apply", FILL + [
        {"op": "values", "as": "v", "of": {"yes": "input[name='q'][value='Yes']", "no": "input[name='q'][value='No']"}}],
        payload={"fields": {}, "screening": {"needs_sponsorship": "No"}}, html=html)
    assert r["v"] == {"yes": False, "no": True}


def test_recruitee_recipe_on_a_custom_domain(tmp_path):
    r = _run(tmp_path, "recruitee.html", "https://jobs.channable.com/o/talent-acquisition-partner/c/new", FILL + [
        {"op": "values", "as": "v", "of": {"name": "input[name='candidate.name']", "email": "input[name='candidate.email']",
                                           "phone": "input[name='candidate.phone']",
                                           "city": "input[name='candidate.openQuestionAnswers.7623253.content']"}},
        {"op": "classOf", "as": "submit", "sel": "button[data-testid='submit-application-form-button']"},
        {"op": "classOf", "as": "apply", "sel": "button[data-cy='apply-button']"},
    ])
    assert r["recipe"] == "recruitee"
    assert r["v"]["name"] == "Kofi Gilbert" and r["v"]["email"] == "kofi@example.com"
    assert r["v"]["phone"] == "+31"                  # the site pre-filled a country code: never overwritten
    assert r["v"]["city"] == "Chicago"               # "Where do you live? (City)": the label pattern's catch
    assert "tailor-submit-hint" in r["submit"] and "tailor-submit-hint" not in r["apply"]
    assert r["clicks"] == 0


def test_avature_is_recognised_by_its_markers_and_hints_continue(tmp_path):
    r = _run(tmp_path, "avature.html", "https://jobs.lenovo.com/en_US/careers/Register?jobId=82554", FILL + [
        {"op": "values", "as": "v", "of": {"first": "#353", "last": "#354", "mobile": "#355", "home": "#356",
                                           "email": "#12434", "zip": "#4111", "street": "#358", "city": "#361",
                                           "country": "#359"}},
    ])
    assert r["recipe"] == "avature"
    v = r["v"]
    assert v["first"] == "Kofi" and v["last"] == "Gilbert" and v["email"] == "kofi@example.com"
    assert v["mobile"] == "555-100-2000" and v["home"] == ""          # one phone, the first tel field
    assert v["street"] == "1 Main St" and v["city"] == "Chicago" and v["zip"] == "60601"
    assert v["country"] == ""                                          # no saved country: left alone
    rep = r["report"]
    assert rep["next_page"] is True and "press Fill again" in rep["continue_hint"]
    assert rep["submit_highlighted"] is False                          # the wizard's last page is not here
    assert r["clicks"] == 0                                            # Continue is a hint, not a click


def test_phenom_needs_its_dom_marker_and_hands_off(tmp_path):
    r = _run(tmp_path, "phenom.html", "https://jobs.cvshealth.com/us/en/home", FILL)
    assert r["recipe"] == "phenom" and r["report"]["handoff"] is True
    # The same careers.* URL WITHOUT the marker is nobody's recipe: the label patterns alone.
    r = _run(tmp_path, None, "https://careers.example.com/job/1", [{"op": "recipeFor", "as": "recipe"}],
             html="<form><label for='e'>Email</label><input id='e' type='email'/></form>")
    assert r["recipe"] is None


def test_icims_email_step_is_multipage(tmp_path):
    r = _run(tmp_path, "icims.html", "https://fdcnmcareers-ulta.icims.com/jobs/login?in_iframe=1", FILL + [
        {"op": "values", "as": "v", "of": {"email": "#email", "phone": "#phoneNumber"}}])
    assert r["recipe"] == "icims"
    assert r["v"] == {"email": "kofi@example.com", "phone": "555-100-2000"}
    assert r["report"]["next_page"] is True and r["clicks"] == 0


def test_no_recipe_falls_back_to_label_patterns(tmp_path):
    r = _run(tmp_path, "generic.html", "https://www.example.com/careers/apply", FILL + [
        {"op": "values", "as": "v", "of": {"fn": "#fn", "ln": "#ln", "em": "#em", "hd": "#hd"}},
        {"op": "remaining", "as": "remaining"},
    ])
    assert r["recipe"] is None and r["report"]["system"] == ""
    assert r["v"] == {"fn": "Kofi", "ln": "Gilbert", "em": "kofi@example.com", "hd": ""}
    assert r["report"]["via_recipe"] == 0 and r["report"]["filled"] == 3
    # What is still empty is exactly what the model-plan fallback would be asked about.
    assert r["remaining"] == ["How did you hear about us?"]


def test_success_marker_is_detected_after_the_person_submits(tmp_path):
    thanks = "<div class='application--container'><h1>Thank you for applying to Anthropic</h1></div>"
    r = _run(tmp_path, None, "https://job-boards.greenhouse.io/anthropic/jobs/5183044008",
             [{"op": "detectSuccess", "as": "success"}], html=thanks)
    assert r["success"] is True
    r = _run(tmp_path, None, "https://jobs.lever.co/acme/123/thanks", [{"op": "detectSuccess", "as": "success"}], html="<p>Done</p>")
    assert r["success"] is True                      # Lever: the /thanks URL is the marker
    r = _run(tmp_path, None, "https://jobs.lever.co/acme/123/apply", [{"op": "detectSuccess", "as": "success"}],
             html="<form id='application-form'><input name='name'/></form>")
    assert r["success"] is False


def test_success_text_beside_a_still_open_form_is_not_success(tmp_path):
    """The one path that marks a job applied must not fire on a page that merely SAYS thank you
    (a sidebar blurb, a previous application's note) while the submit button is still there."""
    html = ("<p>Thank you for applying, we read every application.</p>"
            "<form id='application-form'><input id='first_name'/><button type='submit'>Submit application</button></form>")
    r = _run(tmp_path, None, "https://job-boards.greenhouse.io/anthropic/jobs/5183044008",
             [{"op": "detectSuccess", "as": "success"}], html=html)
    assert r["success"] is False
    # No recipe: the same rule keyed on the file upload the application form carries.
    generic = "<p>We have received your application.</p><form><input type='file' name='resume'/></form>"
    r = _run(tmp_path, None, "https://www.example.com/careers/apply", [{"op": "detectSuccess", "as": "success"}], html=generic)
    assert r["success"] is False
    r = _run(tmp_path, None, "https://www.example.com/careers/apply", [{"op": "detectSuccess", "as": "success"}],
             html="<p>We have received your application.</p>")
    assert r["success"] is True


def test_a_changed_value_is_offered_back_as_a_correction(tmp_path):
    r = _run(tmp_path, "greenhouse.html", "https://job-boards.greenhouse.io/anthropic/jobs/5183044008", FILL + [
        {"op": "change", "sel": "#phone", "value": "555-999-0000"},
        {"op": "change", "sel": "#email", "value": "kofi@example.com"},   # unchanged -> not a correction
        {"op": "takeCorrections", "as": "c1"},
        {"op": "takeCorrections", "as": "c2"},
    ])
    assert r["c1"] == {"phone": "555-999-0000"}
    assert r["c2"] == {}                             # taken once, offered once
