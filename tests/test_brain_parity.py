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
