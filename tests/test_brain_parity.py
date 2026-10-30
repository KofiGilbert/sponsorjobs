"""Parity: the apply agent answers exactly as before when it reads the applicant brain
(docs/applicant-brain.md, Phase 1, "zero behaviour loss").

The same legacy data folder is read two ways: through AnswerLibrary and the prefs file (the
legacy stores), and through BrainLibrary and the brain's prefs view after the migration. The
planner (plan_known), the asks' known answers, _known_answers_for, the agent's ask route and the
fill route must give identical output: canonical keys, wording keys, the two salaries that
disagree, the Lam Research labels ("Error: The field What is your annual salary requirement?
..."), self-identification with and without the standing decline, citizenship and export
control, work authorization and sponsorship."""
from __future__ import annotations

import importlib
import json

import pytest

from applicant import migrate as M
from applicant import redact
from applicant.library import BrainLibrary, prefs_view
from applicant.store import open_store
from autoapply import asks as AK
from autoapply import workauth as W
from autoapply.answers import AnswerLibrary
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known
from brain_fixtures import CUBA_Q, LAM_EXPORT, LAM_SALARY, PREFS, write_legacy

H = {"X-Tailor-Extension": "1"}
GENDER = ["Male", "Female", "I don't wish to answer"]
RACE = ["Asian", "Black or African American", "White", "I don't wish to answer"]
VETERAN = ["I am not a protected veteran", "I am a protected veteran", "I don't wish to answer"]
YN = ["Yes", "No"]

FIELDS = [
    FormField("salary_lam", LAM_SALARY, "text"),
    FormField("salary_new", "What is your annual salary requirement?", "text"),
    FormField("salary_old", "What are your salary expectations?", "text"),
    FormField("heard", "How did you hear about us?", "select", ["LinkedIn", "Indeed", "Other"]),
    FormField("years", "How many years of experience do you have?", "text", required=True),
    FormField("start", "When can you start?", "text"),
    FormField("sql", "Do you have experience with SQL?", "textarea"),
    FormField("cert", "Which cloud certifications do you hold?", "text"),
    FormField("gender", "Gender", "select", GENDER),
    FormField("race", "Race", "select", RACE),
    FormField("veteran", "Veteran status", "select", VETERAN),
    FormField("ethnicity", "Ethnicity", "select", ["Hispanic or Latino", "Not Hispanic or Latino", "Decline to self identify"]),
    FormField("cit", "Country of citizenship", "select", ["Ghana", "United States", "Canada"]),
    FormField("export", LAM_EXPORT, "text"),
    FormField("cuba", CUBA_Q, "radio", YN),
    FormField("auth", "Are you legally authorized to work in the United States?", "radio", YN),
    FormField("sponsor", "Will you now or in the future require visa sponsorship?", "radio", YN),
    FormField("citizen", "Are you a US citizen?", "radio", YN),
    FormField("adult", "Are you at least 18 years of age?", "radio", YN),
    FormField("why", "Why do you want to work at Acme?", "textarea"),
]
APPLICANT = {"first_name": "Ama", "last_name": "Test", "email": "ama.test@example.com", "phone": "3125550100"}


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = write_legacy(tmp_path / "data")
    assert M.apply(d)["ok"]
    yield d
    M.forget_process_state(d)


def _sig(res):
    return {"actions": [(a.ref, a.op, a.source, a.value, a.origin, a.sensitive) for a in res.actions],
            "rest": [f.ref for f in res.rest], "left": [f.ref for f in res.left],
            "used": list(res.used_library), "drafted": list(res.drafted_used)}


def _both(data):
    st = open_store(data / "applicant.db")
    return AnswerLibrary(data / "answers.db"), BrainLibrary(st), PREFS, prefs_view(st)


@pytest.mark.parametrize("decline", [False, True])
@pytest.mark.parametrize("country", ["US", "UK"])
def test_the_planner_gives_identical_answers_from_both_libraries(data, decline, country):
    legacy, brain, prefs, view = _both(data)
    assert W.load(view) == W.load(prefs)
    out = []
    for lib, p in ((legacy, prefs), (brain, view)):
        ctx = KnownContext(work_auth=W.load(p), library=lib, drafted=[{"question": "Earliest start?", "answer": "November"}],
                           cover_letter="Dear team", applicant=APPLICANT, job_country=country, company="Acme",
                           eeo_decline=decline)
        out.append(_sig(plan_known(FIELDS, ctx)))
    legacy.close()
    assert out[0] == out[1]
    got = {a[0]: a[3] for a in out[0]["actions"]}
    # not a vacuous match: the answers that matter are there
    if country == "US":
        assert got["salary_lam"] == got["salary_new"] == got["salary_old"] == "130000"   # the newer salary
        assert got["cuba"] == "No" and got["cit"] == "Ghana" and got["sql"] == "Yes, 3 years in production."
        assert got["auth"] == "Yes" and got["sponsor"] == "Yes"
        assert ("gender" in got) == decline
        if not decline:
            assert got["race"] == "Asian"


QUESTIONS = [(LAM_SALARY, []), ("Desired salary", []), ("Gender", GENDER), ("Race", RACE), ("Veteran status", VETERAN),
             ("Country of citizenship", ["Ghana", "Canada"]), (LAM_EXPORT, []), (CUBA_Q, YN),
             ("Are you legally authorized to work in the United States?", YN),
             ("Will you now or in the future require visa sponsorship?", YN), ("Do you have experience with SQL?", []),
             ("What is your notice period?", []), ("How did you hear about us?", ["LinkedIn", "Other"])]


@pytest.mark.parametrize("decline", [False, True])
def test_the_asks_known_answers_are_identical(data, decline):
    legacy, brain, prefs, view = _both(data)
    for q, opts in QUESTIONS:
        a = AK.known_answer(q, opts, library=legacy, work_auth=W.load(prefs), eeo_decline=decline, job_country="US")
        b = AK.known_answer(q, opts, library=brain, work_auth=W.load(view), eeo_decline=decline, job_country="US")
        assert a == b, q
    legacy.close()


# --------------------------------------------------------------------------- through the app
@pytest.fixture()
def app(data, monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def _twice(monkeypatch, fn):
    """fn() with the brain on, then with SPONSORJOBS_BRAIN=0 (the legacy stores)."""
    monkeypatch.delenv(M.ENV, raising=False)
    on = fn()
    monkeypatch.setenv(M.ENV, "0")
    off = fn()
    monkeypatch.delenv(M.ENV)
    return on, off


def test_known_answers_for_is_identical(app, monkeypatch):
    A, c = app
    labels = [f.label for f in FIELDS]
    for country in ("US", "UK"):
        data = {"location": "London, UK"} if country == "UK" else {}
        on, off = _twice(monkeypatch, lambda: A._known_answers_for(labels, None, data, APPLICANT))
        assert on == off and on
    on, _ = _twice(monkeypatch, lambda: A._known_answers_for(labels, None, {}, APPLICANT))
    assert on[LAM_SALARY] == "130000" and on[CUBA_Q] == "No" and "Gender" not in on


@pytest.mark.parametrize("decline", [False, True])
def test_the_ask_route_is_identical(app, monkeypatch, decline):
    A, c = app
    A._merge_prefs({"eeo_decline": decline})
    body = {"known_only": True, "questions": [{"question": q, "options": o} for q, o in QUESTIONS]}
    def ask():
        r = c.post("/api/autoapply/agent/ask", json=body, headers=H).get_json()
        r.pop("id", None)                                            # a fresh ask id each time
        return r
    on, off = _twice(monkeypatch, ask)
    assert on == off
    assert on["answers"][LAM_SALARY] == "130000"


def test_the_fill_route_is_identical_and_mirrors_the_use_counts(app, monkeypatch):
    A, c = app
    sent = []
    import autoapply

    def capture(rest, llm, role="", company="", jd=""):
        sent.append({"fields": [{"label": f.label, "options": list(f.options)} for f in rest],
                     "role": role, "company": company, "jd": jd})
        return []
    monkeypatch.setattr(autoapply, "build_fill_plan", capture)
    body = {"url": "https://boards.greenhouse.io/acme/jobs/1",
            "fields": [{"ref": f.ref, "label": f.label, "type": f.type, "options": f.options, "required": f.required}
                       for f in FIELDS + [FormField("contact", "Preferred contact method", "select", ["Email", "Phone"])]]}
    st = open_store(A._DATA / "applicant.db")

    def uses():
        with AnswerLibrary(A._ANSWERS_DB) as legacy:
            return ({r["key"]: (r["uses"], r["last_used"]) for r in legacy.list()},
                    {r["key"]: (r["uses"], r["last_used"]) for r in BrainLibrary(st).list()})
    first = {}

    def plan():
        r = c.post("/api/autoapply/plan", json=body).get_json()
        if not first:
            first.update(dict(zip(("legacy", "brain"), uses())))     # the brain run, mirrored row for row
        return r
    on, off = _twice(monkeypatch, plan)
    assert on == off and on["ok"]
    assert first["legacy"] == first["brain"] and first["brain"]["@citizenship"][0] > 0
    A._answer_lib().close()                                          # the brain catches up on the legacy run
    legacy_now, brain_now = uses()
    assert legacy_now == brain_now and brain_now["@citizenship"][0] == 2 * first["brain"]["@citizenship"][0]
    # what the model was handed carries no stored self-identification or legal value beyond the
    # form's own labels and options
    vals = redact.sensitive_values(st)
    assert "Ghana" in vals and "f1_opt" in vals
    allowed = [f.label for f in FIELDS] + [o for f in FIELDS for o in f.options]
    assert sent and all(redact.leaks(s, vals, allowed) == [] for s in sent)


# --------------------------------------------------------------------------- Lam's Phenom form (PR #146)
# careers.lamresearch.com/careers/apply, read 2026-10-30 (tests/test_agent_lam_eeo.py)
LAM_GENDER = ["Choose not to disclose", "Male", "Female"]
LAM_ETHNICITY = ["American Indian/Alaskan Native", "Asian", "Black or African American", "Choose not to disclose",
                 "Hispanic or Latino", "Native Hawaiian or Other Pacific Island", "Two or More Races", "White"]
LAM_DISABILITY = ["Choose not to disclose", "No", "Yes"]
LAM_VETERAN = ["Choose not to disclose", "No", "Yes"]
LAM_VETERAN_NOTE = ("Veteran To meet U.S. Department of Labor requirements, your voluntary cooperation is appreciated. "
                    "Title 38, United States Code, Section 4212(d), PL 105-339 and PL, 107-228, require that federal "
                    "contractors report annually the number of employees who are: 1) special disabled veterans 2) "
                    "veterans of the Vietnam era and 3) other protected veterans (that is, who served on active duty "
                    "during ...")
LAM_QS = [{"question": "Gender", "options": LAM_GENDER}, {"question": "Ethnicity", "options": LAM_ETHNICITY},
          {"question": "Veteran", "options": LAM_VETERAN, "hint": LAM_VETERAN_NOTE},
          {"question": "Disability", "options": LAM_DISABILITY},
          {"question": "Gender", "options": ["Choose not to disclose"], "partial": True}]
CARD = {"gender": "male", "hispanic": "no", "race": "black", "veteran": "not", "disability": "no"}


@pytest.mark.parametrize("decline", [False, True])
def test_lams_self_identification_fill_from_saved_answers_is_identical(data, decline):
    from autoapply import basics
    st = open_store(data / "applicant.db")
    brain = BrainLibrary(st, mirror_path=data / "answers.db")
    basics.write(brain, CARD)                                         # the card, through the brain and its mirror
    legacy = AnswerLibrary(data / "answers.db")
    try:
        for q in LAM_QS:
            kw = {"partial": q.get("partial", False), "eeo_decline": decline}
            if q.get("hint"):
                kw["hint"] = q["hint"]
            a = AK.known_answer(q["question"], q["options"], library=legacy, **kw)
            b = AK.known_answer(q["question"], q["options"], library=brain, **kw)
            assert a == b, q["question"]
        if not decline:
            assert AK.known_answer("Ethnicity", LAM_ETHNICITY, library=brain) == ("Black or African American", "library")
            assert AK.known_answer("Veteran", LAM_VETERAN, library=brain, hint=LAM_VETERAN_NOTE) == ("No", "library")
        assert basics.read(legacy) == basics.read(brain)
    finally:
        legacy.close()


@pytest.mark.parametrize("decline", [False, True])
def test_the_known_only_pick_for_lam_is_identical(app, monkeypatch, decline):
    from autoapply import basics
    A, c = app
    lib = A._answer_lib()
    try:
        basics.write(lib, CARD)
    finally:
        lib.close()
    A._merge_prefs({"eeo_decline": decline})

    def ask():
        r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": LAM_QS}, headers=H).get_json()
        r.pop("id", None)
        return r
    on, off = _twice(monkeypatch, ask)
    assert on == off
    if not decline:
        assert on["select"] == {"Gender": "Male", "Ethnicity": "Black or African American", "Veteran": "No",
                                "Disability": "No"}


def test_the_terms_preference_reads_the_brain_and_is_mirrored(app, monkeypatch):
    A, c = app
    assert A._terms_pref() == "ask"
    A._merge_prefs({"accept_required_terms": "always"})
    st = open_store(A._DATA / "applicant.db")
    assert [f["value"] for f in st.facts(key="accept_required_terms", status="active")] == ["always"]
    assert json.loads((A._DATA / "app_prefs.json").read_text())["accept_required_terms"] == "always"
    on, off = _twice(monkeypatch, lambda: (A._terms_pref(), c.get("/api/answers/basics").get_json()))
    assert on == off and on[0] == "always" and on[1]["accept_required_terms"] == "always"
    # a whole-store save without the key (the extension's) keeps it
    A._save_prefs({"work_auth_us": "Yes"}, source="extension")
    assert A._terms_pref() == "always"
    c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "ask"})
    on, off = _twice(monkeypatch, A._terms_pref)
    assert on == off == "ask" and st.facts(key="accept_required_terms", status="active") == []
