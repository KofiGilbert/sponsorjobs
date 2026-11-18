"""Apply setup (autoapply/setup.py, /api/apply-setup): every step saves into the store that already
owns the answer, the planner reads it back on a real form, the finish step's N of M counts real
saved answers, and the setup opens by itself only once and resumes where the person left.

All offline, no model: RESUME_AGENT_DEV + RESUME_AGENT_FAKE_LLM, a fresh data folder per test."""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import consents
from autoapply import setup as S
from autoapply.answers import normalize
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")

PROFILE = {"identity": {"name": "Ada Example", "email": "ada@example.com", "phone": "555-010-1234",
                        "linkedin": "https://www.linkedin.com/in/ada-example", "github": "In"},
           "experience": [{"org": "Initech", "location": "Chicago, IL",
                           "roles": [{"title": "Data Analyst", "dates": "Jan 2022 - Present", "bullets": ["Built dashboards."]}]}],
           "education": [], "skills": {}}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def _seed_profile(A, profile=PROFILE):
    mem = A._memory()
    try:
        mem.save("default", json.loads(json.dumps(profile)), A._essentials_from_profile(profile), [])
    finally:
        mem.close()


def _save(c, step, fields, nxt=None, code=200):
    r = c.post("/api/apply-setup/save", json={"step": step, "fields": fields, "next": nxt})
    assert r.status_code == code, r.get_json()
    return r.get_json()


CONTACT = {"name": "Ada Example", "email": "ada@example.com", "phone": "555-010-1234", "street": "1 Main St",
           "city": "Chicago", "state": "IL", "postal_code": "60601", "country": "United States",
           "linkedin": "https://www.linkedin.com/in/ada-example", "none": {"github": True, "website": True}}
WORKAUTH = {"work_auth": {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                                               "sponsor_future": "Yes", "auth_end": "2030-06-30"}}},
            "citizenship": "Ghana"}
WANTS = {"titles": "Data Analyst, BI Analyst", "locations": "Chicago", "level": ["entry"], "job_types": ["full_time"],
         "work_style": ["remote", "hybrid"], "radius": 25, "industries": "Fintech", "salary": "95000",
         "relocate": "Yes", "start_date": "Two weeks after an offer", "notice": "Two weeks", "avoid": "Acme Corp"}
FORMS = {"heard": "Job board", "over_18": "Yes", "covenants": "No", "marketing": "No", "forced": "ask",
         "terms": "always", "clearance": "None", "license": "Yes", "gpa": "3.7", "gpa_scale": "4.0"}
SELFID = {"gender": "female", "hispanic": "no", "race": "black", "veteran": "not", "disability": "decline",
          "transgender": "decline", "orientation": "decline", "first_gen": "decline"}


def _full_setup(c):
    _save(c, "contact", CONTACT, "workauth")
    _save(c, "workauth", WORKAUTH, "wants")
    _save(c, "wants", WANTS, "forms")
    _save(c, "forms", FORMS, "selfid")
    _save(c, "selfid", SELFID, "apply")
    return _save(c, "apply", {"autonomous": False, "daily_cap": 20, "autopilot_per_day": 3}, "finish")


# --------------------------------------------------------------------------- the keys
def test_each_standing_answer_is_saved_under_its_canonical_key():
    assert S.KEYS == {"start_date": "@start_date", "notice": "@notice_period", "heard": "@heard_about",
                      "over_18": "@over_18", "covenants": "@restrictive_covenants", "marketing": "@marketing_optin",
                      "forced": "@forced_consent", "relatives": "@relatives_at_employer", "other_names": "@other_names", "clearance": "@security_clearance", "license": "@drivers_license",
                      "gpa": "@gpa", "related_degree": "@related_degree", "current_salary": "@current_salary",
                      "current_bonus": "@current_bonus", "current_equity": "@current_equity"}
    from applicant import keys as K
    for k in S.KEYS.values():
        assert K.get(k) is not None, k


@pytest.mark.parametrize("q,key", [
    ("What is your current base salary (USD)?", "@current_salary"),
    ("Current compensation", "@current_salary"),
    ("What is your desired salary?", "@salary"),
    ("Do you currently hold an active security clearance?", "@security_clearance"),
    ("Do you have a valid driving licence?", "@drivers_license"),
    ("Cumulative GPA", "@gpa"),
    ("Bachelor's degree in Computer Science, Information Systems, Data Analytics, or a related field?", "@related_degree"),
])
def test_other_wordings_find_the_same_key(q, key):
    assert normalize(q) == key


@pytest.mark.parametrize("q", [
    "Are you able to obtain a security clearance?",
    "Are you willing to obtain a driver's license?",
    "Are you currently pursuing a degree in a related field?",
    "Do you have a GPA of 3.0 or higher?",
])
def test_a_different_question_keeps_its_own_key(q):
    assert not normalize(q).startswith("@"), normalize(q)


# --------------------------------------------------------------------------- round trips
def test_a_new_install_has_nothing_answered_and_starts_at_contact(app):
    _, c = app
    d = c.get("/api/apply-setup").get_json()
    assert d["state"] == {"seen": False, "step": "contact", "done": False}
    assert d["count"]["answered"] == 0 and d["count"]["total"] == len(S.ITEMS) == 20
    assert d["forms"]["marketing"] == "No" and d["forms"]["forced"] == "ask" and d["forms"]["terms"] == "ask"


def test_contact_prefills_from_the_resume_never_anchor_text(app):
    A, c = app
    _seed_profile(A)
    k = c.get("/api/apply-setup").get_json()["contact"]
    assert k["name"] == "Ada Example" and k["linkedin"] == "https://www.linkedin.com/in/ada-example"
    assert k["github"] == ""               # "In" from a resume's anchor text is not a link


def test_contact_saves_into_the_profile_and_the_agent_reads_it(app):
    A, c = app
    _seed_profile(A)
    d = _save(c, "contact", CONTACT)
    ident, declined = A._setup_identity()
    assert ident["city"] == "Chicago" and ident["postal_code"] == "60601" and ident["street"] == "1 Main St"
    assert {"github", "blog"} <= set(declined) and "linkedin" not in declined
    ap = A._applicant_for(A._saved_full_profile())
    assert ap["linkedin"] == "https://www.linkedin.com/in/ada-example"
    assert ap["city"] == "Chicago" and ap["state"] == "IL" and ap["postal_code"] == "60601"
    assert d["contact"]["none"] == {"linkedin": False, "github": True, "website": True}
    # a link given later takes it off the none list
    _save(c, "contact", {"github": "https://github.com/ada-example", "none": {"github": False}})
    assert "github" not in A._setup_identity()[1]


def test_a_half_written_link_is_refused(app):
    _, c = app
    r = c.post("/api/apply-setup/save", json={"step": "contact", "fields": {"linkedin": "In"}})
    assert r.status_code == 400 and "LinkedIn" in r.get_json()["error"]


def test_workauth_saves_the_profile_and_previews_the_sentence(app):
    A, c = app
    d = _save(c, "workauth", WORKAUTH)
    wa = A._work_auth()
    assert wa["countries"]["US"]["status"] == "f1_opt" and wa["countries"]["US"]["sponsor_future"] == "Yes"
    assert d["workauth"]["citizenship"] == "Ghana"
    assert "F-1 OPT" in d["workauth"]["preview"]["US"]
    p = c.post("/api/apply-setup/preview", json={"work_auth": {"countries": {"US": {"status": "us_citizen"}}}}).get_json()
    assert "citizen" in p["text"].lower()
    assert A._work_auth()["countries"]["US"]["status"] == "f1_opt"      # the preview saves nothing


def test_wants_saves_into_focus_companies_relocation_salary_and_dates(app):
    A, c = app
    _save(c, "wants", WANTS)
    p = A._prefs()
    assert p["aa_focus"]["titles"] == ["Data Analyst", "BI Analyst"] and p["aa_focus"]["locations"] == ["Chicago"]
    assert p["apply_wants"] == {"level": ["entry"], "job_types": ["full_time"], "work_style": ["remote", "hybrid"],
                                "radius": 25, "industries": ["Fintech"]}
    assert A._learn_view()["exclude"] == ["Acme Corp"]
    assert A._work_auth()["relocate"] == "Yes"
    assert A._brain_prefs().get("desired_salary") == "95000"
    lib = A._answer_lib()
    try:
        assert lib.best("@start_date")["answer"] == "Two weeks after an offer"
        assert lib.best("@notice_period")["answer"] == "Two weeks"
        assert lib.best("@salary")["answer"] == "95000"
    finally:
        lib.close()
    # a company taken off the list leaves "Never apply to"
    _save(c, "wants", {"avoid": ""})
    assert A._learn_view()["exclude"] == []


def test_forms_saves_standing_answers_and_clears_them(app):
    A, c = app
    _save(c, "forms", dict(FORMS, current_salary="80000", related_degree="Yes"))
    lib = A._answer_lib()
    try:
        got = {k: (lib.best(k) or {}).get("answer") for k in S.KEYS.values()}
    finally:
        lib.close()
    assert got["@heard_about"] == "Job board" and got["@over_18"] == "Yes"
    assert got["@restrictive_covenants"] == "No" and got["@marketing_optin"] == "No"
    assert got["@forced_consent"] is None                       # "ask" saves nothing
    assert got["@security_clearance"] == "No" and got["@drivers_license"] == "Yes"
    assert got["@gpa"] == "3.7 / 4.0" and got["@current_salary"] == "80000" and got["@related_degree"] == "Yes"
    assert A._terms_pref() == "always" and A._prefs()["terms_decided"] is True
    d = _save(c, "forms", {"related_degree": "", "current_salary": "", "forced": "always", "clearance": "Secret"})
    assert d["forms"]["related_degree"] == "" and d["forms"]["current_salary"] == ""
    assert d["forms"]["forced"] == "always" and d["forms"]["clearance"] == "Secret"


def test_a_refused_value_saves_nothing_from_its_step(app):
    A, c = app
    r = c.post("/api/apply-setup/save", json={"step": "forms", "fields": {"heard": "LinkedIn", "gpa": "5", "gpa_scale": "4"}})
    assert r.status_code == 400
    lib = A._answer_lib()
    try:
        assert lib.best("@heard_about") is None
    finally:
        lib.close()


def test_selfid_and_apply_save_into_their_stores(app):
    A, c = app
    d = _save(c, "selfid", SELFID)
    assert d["selfid"]["values"]["gender"] == "female" and d["selfid"]["values"]["first_gen"] == "decline"
    d = _save(c, "apply", {"autonomous": True, "daily_cap": 12, "autopilot_per_day": 4})
    assert A._autonomous_on() is True and A._prefs()["daily_cap"] == 12 and A._prefs()["autopilot_per_day"] == 4
    assert d["apply"]["auto_sites"] == sorted({e.ats for e in __import__("submit.allowlist", fromlist=["x"]).REGISTRY
                                               if e.auto_eligible})
    _save(c, "apply", {"autonomous": False})
    assert A._autonomous_on() is False
    assert c.post("/api/apply-setup/save", json={"step": "apply", "fields": {"daily_cap": "lots"}}).status_code == 400


# --------------------------------------------------------------------------- the planner reads it back
def test_after_setup_the_planner_answers_what_forms_ask(app):
    A, c = app
    _seed_profile(A)
    _full_setup(c)
    company = "Globex"
    lib = A._answer_lib()
    try:
        ctx = KnownContext(work_auth=A._work_auth(), library=lib, applicant=A._applicant_for(A._saved_full_profile()),
                           company=company, eeo_decline=A._eeo_decline(), accept_notices=A._accept_notices(),
                           **A._recall_ctx(company, {}))
        fields = [
            FormField("res", "Where in the US do you currently reside?", "text", required=True),
            FormField("sp", "Will you now or in the future require visa sponsorship?", "select", ["Yes", "No"], required=True),
            FormField("wh", "Have you previously worked for Globex?", "select", ["Yes", "No"], required=True),
            FormField("nc", "Do you have any restrictive covenants (non-compete, non-solicitation) with your current or prior employer?",
                      "select", ["Yes", "No"], required=True),
            FormField("mk", "Would you like to receive text messages about new jobs and newsletters?", "select",
                      ["Yes", "No"], required=False),
            FormField("bg", "If you are selected, a background check is a condition of employment. Do you acknowledge?",
                      "select", ["Yes, I acknowledge", "No"], required=True),
            FormField("hh", "How did you hear about us?", "text", required=True),
            FormField("o18", "Are you at least 18 years of age?", "select", ["Yes", "No"], required=True),
            FormField("li", "LinkedIn Profile", "text", required=True),
            FormField("sd", "When can you start?", "text", required=True),
            FormField("dl", "Do you have a valid driver's license?", "select", ["Yes", "No"], required=True),
        ]
        res = plan_known(fields, ctx)
    finally:
        lib.close()
    got = {a.ref: (a.value or a.source) for a in res.actions}
    assert got["res"] == "Chicago, IL"
    assert got["sp"] == "Yes"
    assert got["wh"] == "No"                         # Globex is not in the work history
    assert got["nc"] == "No"
    assert got["mk"] == "No"                         # marketing declined by the standing No
    assert got["bg"] == "Yes, I acknowledge"         # the standing consent to required notices
    assert got["hh"] == "Job board"
    assert got["o18"] == "Yes"
    assert got["li"] == "linkedin"                   # the profile link, resolved locally
    assert got["sd"] == "Two weeks after an offer"
    assert got["dl"] == "Yes"
    assert not [f.label for f in res.left]


def test_a_forced_consent_is_asked_unless_the_person_said_accept(app):
    A, c = app
    f = FormField("tx", "By selecting Yes, you agree to receive text messages from us in relation to this job application.",
                  "select", ["Yes"], required=True)
    lib = A._answer_lib()
    try:
        assert consents.standing_answer(f.label, f.options, "select", True, library=lib)[0] == consents.THEIRS
    finally:
        lib.close()
    _save(c, "forms", {"forced": "always"})
    lib = A._answer_lib()
    try:
        assert consents.standing_answer(f.label, f.options, "select", True, library=lib) == (consents.PICK, "Yes", "consent")
        # an optional opt-in is still declined: the forced choice is only for no-way-to-decline boxes
        opt = consents.standing_answer("Would you like to receive our newsletter?", ["Yes", "No"], "select", False, library=lib)
        assert opt == (consents.PICK, "No", "optin")
    finally:
        lib.close()


# --------------------------------------------------------------------------- the count
def test_the_count_follows_real_saved_answers(app):
    A, c = app
    _seed_profile(A)
    d = c.get("/api/apply-setup").get_json()
    before = d["count"]["answered"]
    assert {o["id"] for o in d["count"]["open"]} >= {"authorized", "sponsorship", "covenants", "links"}
    d = _full_setup(c)
    assert d["count"] == {"answered": 20, "total": 20, "open": []}
    assert before < 20
    # a standing decline answers the five self-identification questions too, as the card counts them
    lib = A._answer_lib()
    try:
        for k in ("@gender", "@race", "@hispanic_latino", "@veteran", "@disability"):
            for _ in range(5):
                row = lib.best(k)
                if row is None or not lib.delete(row["key"]):
                    break
    finally:
        lib.close()
    d = c.get("/api/apply-setup").get_json()
    assert {"gender", "race", "hispanic", "veteran", "disability"} <= {o["id"] for o in d["count"]["open"]}
    d = _save(c, "selfid", {"eeo_decline": True})
    assert d["count"]["answered"] == 20


def test_a_citizen_needs_no_sponsorship_answer():
    snap = {"work_auth": {"countries": {"US": {"status": "us_citizen"}}}}
    got = S.answered(snap)
    assert got["authorized"] and got["sponsorship"]


def test_each_open_item_names_its_step():
    assert {step for _i, _l, step in S.ITEMS} <= set(S.STEPS)
    assert len({i for i, _l, _s in S.ITEMS}) == len(S.ITEMS)


# --------------------------------------------------------------------------- first run, resume
def test_first_run_is_shown_once_and_resumes_at_its_step(app):
    _, c = app
    assert c.get("/api/apply-setup").get_json()["state"]["seen"] is False
    c.post("/api/apply-setup/state", json={"seen": True})
    _save(c, "contact", {"name": "Ada Example"}, "wants")
    st = c.get("/api/apply-setup").get_json()["state"]
    assert st == {"seen": True, "step": "wants", "done": False}
    c.post("/api/apply-setup/state", json={"step": "nowhere"})     # an unknown step is ignored
    assert c.get("/api/apply-setup").get_json()["state"]["step"] == "wants"
    c.post("/api/apply-setup/state", json={"done": True})
    assert c.get("/api/apply-setup").get_json()["state"]["done"] is True


def test_the_ui_opens_it_once_after_sign_in_and_from_two_doors():
    i = APP_JS.index("async function suMaybeFirstRun")
    body = APP_JS[i:APP_JS.index("\n  }\n", i)]
    assert "s.email" in body and "d.state.seen || d.state.done" in body
    assert APP_JS.count("suMaybeFirstRun();") == 2            # boot, and right after sign-in
    assert 'id="memSetup"' in HTML and 'id="aaSetup"' in HTML and 'id="view-setup"' in HTML
    assert 'openApplySetup({ origin: "profile" })' in APP_JS and 'openApplySetup({ origin: "autoapply" })' in APP_JS


def test_unknown_step_is_refused(app):
    _, c = app
    assert c.post("/api/apply-setup/save", json={"step": "nope", "fields": {}}).status_code == 400


# --------------------------------------------------------------------------- copy
def test_setup_strings_are_in_every_language_without_dashes():
    loc = ROOT / "ui" / "static" / "locales"
    en = json.loads((loc / "en.json").read_text(encoding="utf-8"))
    keys = [k for k in en if k.startswith("setup.")]
    assert len(keys) > 100
    for code in ("de", "es", "fr", "pt"):
        d = json.loads((loc / f"{code}.json").read_text(encoding="utf-8"))
        assert all(d.get(k) for k in keys), code
        assert not [k for k in keys if re.search("[–—]", d[k])], code
    assert not [k for k in keys if re.search(r"\bCV\b|résumé", en[k])]
    for _i, label, _s in S.ITEMS:
        assert f"setup.q.{_i}" in en


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses():
    subprocess.run(["node", "--check", str(ROOT / "ui" / "static" / "app.js")], check=True)
