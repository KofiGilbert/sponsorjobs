"""The answer library and the planner's order: the person's own answers first, the model last,
and nothing invented.

Library: normalized keys (punctuation, filler and synonyms), "you" beats "drafted", per-job
questions are never saved, seeding from the older prefs happens once. Planner: work auth ->
library -> drafted -> cover letter -> profile -> model, with the fact gate on the model.
"""
from __future__ import annotations

import importlib

import pytest

from autoapply.answers import AnswerLibrary, is_per_job, normalize, seed_once
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_form, plan_known
from llm.base import FakeLLM


@pytest.mark.parametrize("a,b", [
    ("What are your salary expectations?*", "Desired compensation"),
    ("Expected salary (USD)", "Salary requirements"),
    ("What is your desired salary?", "Compensation expectations"),
    ("When can you start?", "Earliest start date"),
    ("How did you hear about us?", "Where did you hear about this position?"),
    ("How did you hear about this job?", "Referral source"),
    ("How many years of experience do you have?", "Years of relevant experience"),
    ("Are you at least 18 years of age?", "Are you 18 years or older?"),
    ("Do you have experience with Python?", "Do you have experience with Python? (required)"),
    ("Please describe your experience with SQL.", "Describe your experience with SQL"),
])
def test_same_question_same_key(a, b):
    assert normalize(a) == normalize(b) != ""


@pytest.mark.parametrize("a,b", [
    ("What is your current salary?", "What are your salary expectations?"),
    ("How many years of experience do you have with Python?", "How many years of experience do you have?"),
    ("Do you have experience with Python?", "Do you have experience with Java?"),
    ("What is your notice period?", "When can you start?"),
])
def test_different_questions_differ(a, b):
    assert normalize(a) != normalize(b)


def test_library_save_lookup_and_precedence(tmp_path):
    with AnswerLibrary(tmp_path / "a.db") as lib:
        assert lib.save("What are your salary expectations?", "$120,000", "you")["source"] == "you"
        assert lib.lookup("Desired compensation")["answer"] == "$120,000"
        assert lib.save("Expected salary", "$90,000", "drafted") is None     # never overwrites theirs
        assert lib.lookup("salary requirements")["answer"] == "$120,000"
        lib.save("Do you have experience with SQL?", "Yes, 3 years in production.", "drafted")
        row = lib.lookup("do you have experience with sql")
        assert row["source"] == "drafted"
        edited = lib.update(row["key"], answer="Yes, 4 years.")
        assert edited["source"] == "you" and edited["answer"] == "Yes, 4 years."
        assert [r["source"] for r in lib.list()] == ["you", "you"]
        assert len(lib.list("sql")) == 1
        lib.mark_used(edited["key"])
        assert lib.lookup("Do you have experience with SQL")["uses"] == 1
        assert lib.delete(edited["key"]) and lib.lookup("Do you have experience with SQL") is None


def test_per_job_questions_are_never_saved(tmp_path):
    assert is_per_job("Why do you want to work at Acme?")
    assert is_per_job("What interests you about Stripe's mission?", company="Stripe")
    with AnswerLibrary(tmp_path / "a.db") as lib:
        assert lib.save("Why do you want to join us?", "Because...", "you") is None
        assert lib.save("What do you know about Acme?", "A lot", "you", company="Acme") is None
        assert lib.count() == 0


def test_seed_once_from_prefs(tmp_path):
    prefs = {"desired_salary": "$100k", "earliest_start": "Two weeks", "hear_about_us": "LinkedIn"}
    with AnswerLibrary(tmp_path / "a.db") as lib:
        assert seed_once(lib, prefs) == 3
        assert lib.lookup("Desired compensation")["answer"] == "$100k"
        lib.delete(lib.lookup("When can you start?")["key"])
        assert seed_once(lib, prefs) == 0                       # a deleted answer stays deleted
        assert lib.lookup("When can you start?") is None


# --------------------------------------------------------------------------- planner order
OPT = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                            "sponsor_future": "Yes"}}}


def _fields():
    return [
        FormField("a", "First name", "text"),
        FormField("b", "Email", "email"),
        FormField("c", "Resume/CV", "file"),
        FormField("d", "Are you legally authorized to work in the United States?", "select", ["Yes", "No"]),
        FormField("e", "Will you now or in the future require sponsorship?", "radio", ["Yes", "No"]),
        FormField("f", "What are your salary expectations?", "text"),
        FormField("g", "Do you have experience with Kubernetes?", "textarea"),
        FormField("h", "Cover letter", "textarea"),
        FormField("i", "Are you a veteran?", "select", ["Yes", "No", "I prefer not to say"]),
        FormField("j", "Gender", "select", ["Male", "Female", "Decline"]),
        FormField("k", "Tell us about a project you are proud of", "textarea"),
        FormField("l", "Preferred office", "select", ["New York", "Remote"]),
        FormField("m", "Phone", "tel"),
    ]


def test_planner_order_and_sources(tmp_path):
    with AnswerLibrary(tmp_path / "a.db") as lib:
        lib.save("Desired compensation", "$130,000", "you")
        # a library answer that would also match a work-auth question loses to the profile
        lib.save("Will you now or in the future require sponsorship?", "No", "you")
        ctx = KnownContext(work_auth=OPT, library=lib,
                           drafted=[{"question": "Do you have experience with Kubernetes?",
                                     "answer": "Yes, I ran our clusters at Acme."},
                                    {"question": "What are your salary expectations?", "answer": "$1"}],
                           cover_letter="Dear team, ...", resume_path="/tmp/r.pdf",
                           applicant={"first_name": "Kofi", "email": "k@example.com"})
        res = plan_known(_fields(), ctx)
    by = {a.ref: a for a in res.actions}
    assert by["a"].origin == "profile" and by["a"].source == "first_name"
    assert by["b"].source == "email"
    assert by["c"].op == "upload"
    assert by["d"].origin == "workauth" and by["d"].value == "Yes"
    assert by["e"].origin == "workauth" and by["e"].value == "Yes"     # profile beats the library
    assert by["f"].origin == "library" and by["f"].value == "$130,000"  # library beats drafted
    assert by["g"].origin == "drafted" and "clusters" in by["g"].value
    assert by["h"].origin == "letter"
    left = {f.ref for f in res.left}
    assert {"i", "j", "k", "m"} <= left                 # facts and free text nobody answered: theirs
    assert [f.ref for f in res.rest] == ["l"]           # only a plain choice may go to the model


def test_model_never_answers_facts():
    """The model gets only the open fields, and even if it answers a fact question or writes
    free text, the fact gate drops it."""
    seen = {}

    class Eager(FakeLLM):
        def plan_form_fill(self, fields, available_keys, role="", company="", jd=""):
            seen["refs"] = [f["ref"] for f in fields]
            return {"actions": [{"ref": f["ref"], "op": "select" if f.get("options") else "fill",
                                 "value": (f.get("options") or ["I made this up"])[0]} for f in fields]}

    fields = _fields()
    res = plan_form(fields, KnownContext(work_auth={}, applicant={}), Eager())
    by = {a.ref: a for a in res.actions}
    assert seen["refs"] == ["c", "l"]                   # never the fact or free-text fields
    assert by["l"].value == "New York" and by["l"].origin == "ai"
    for ref in ("d", "e", "f", "g", "i", "j", "k"):
        assert ref not in by                            # no profile -> left, never invented
    assert "c" not in by                                # the model may not fill a file with text


def test_no_answer_text_reaches_the_model(tmp_path):
    seen = {}

    class Spy(FakeLLM):
        def plan_form_fill(self, fields, available_keys, role="", company="", jd=""):
            seen["blob"] = repr((fields, available_keys, role, company, jd))
            return {"actions": []}

    with AnswerLibrary(tmp_path / "a.db") as lib:
        lib.save("Desired compensation", "$130,000", "you")
        plan_form(_fields(), KnownContext(work_auth=OPT, library=lib, cover_letter="SECRET LETTER",
                                          applicant={"first_name": "Kofi"}), Spy(), jd="JD")
    assert "130,000" not in seen["blob"] and "SECRET" not in seen["blob"] and "Kofi" not in seen["blob"]


# --------------------------------------------------------------------------- the endpoints
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
                                 "experience": [{"org": "X"}]}, {}, [])
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={
        "jd_text": "We need Python.", "cover_letter": "Dear Acme team, I build things.",
        "screening": [{"question": "Do you have experience with Python?", "answer": "Yes, 4 years."},
                      {"question": "Why do you want to work at Acme?", "answer": "Your mission."}]})
    recs.close()
    return A, A.app.test_client(), rid


def test_workauth_save_syncs_old_answers(app):
    A, c, _ = app
    d = c.post("/api/answers/workauth", json={"work_auth": OPT}).get_json()
    assert d["ok"] and d["work_auth"]["countries"]["US"]["status"] == "f1_opt"
    assert d["answers"]["needs_sponsorship"] == "Future" and d["answers"]["work_auth_us"] == "Yes"
    # the older surface (profile/extension answers) writes through to the structured profile
    c.post("/api/profile/answers", json={"needs_sponsorship": "No"})
    wa = c.get("/api/answers").get_json()["work_auth"]
    assert wa["countries"]["US"]["status"] == "f1_opt" and wa["countries"]["US"]["sponsor_future"] == "No"


def test_answers_crud(app):
    _, c, _ = app
    row = c.post("/api/answers", json={"question": "Desired compensation", "answer": "$120k"}).get_json()["answer"]
    lst = c.get("/api/answers?q=120").get_json()
    assert lst["total"] == 1 and lst["answers"][0]["key"] == row["key"]
    c.post("/api/answers", json={"key": row["key"], "question": "Desired compensation", "answer": "$125k"})
    assert c.get("/api/answers").get_json()["answers"][0]["answer"] == "$125k"
    assert c.post("/api/answers/delete", json={"key": row["key"]}).get_json()["ok"]
    assert c.get("/api/answers").get_json()["total"] == 0
    assert c.post("/api/answers", json={"question": "Q", "answer": ""}).status_code == 400


def test_editing_a_screening_answer_reuses_it_everywhere(app):
    _, c, rid = app
    d = c.post(f"/api/record/{rid}/screening", json={"index": 0, "answer": "Yes, 5 years."}).get_json()
    assert d["reused"] is True and d["screening"][0]["answer"] == "Yes, 5 years."
    d = c.post(f"/api/record/{rid}/screening", json={"index": 1, "answer": "Your mission, still."}).get_json()
    assert d["reused"] is False                          # about this company: kept on this one only
    plan = c.post("/api/autoapply/plan", json={
        "url": "https://boards.greenhouse.io/other/jobs/1", "fields": [
            {"ref": "q", "label": "Do you have experience with Python?*", "type": "textarea"}]}).get_json()
    assert plan["ok"] and plan["ops"][0]["text"] == "Yes, 5 years." and plan["ops"][0]["from"] == "library"


def test_plan_uses_drafted_letter_and_workauth(app):
    _, c, rid = app
    c.post("/api/answers/workauth", json={"work_auth": OPT})
    d = c.post("/api/autoapply/plan", json={
        "url": "https://boards.greenhouse.io/acme/jobs/1", "record_id": rid, "fields": [
            {"ref": "f1", "label": "First name", "type": "text"},
            {"ref": "f2", "label": "Do you have experience with Python?", "type": "textarea"},
            {"ref": "f3", "label": "Cover Letter", "type": "textarea"},
            {"ref": "f4", "label": "Will you now or in the future require sponsorship?", "type": "select",
             "options": ["Yes", "No"]},
            {"ref": "f5", "label": "What is your race?", "type": "select", "options": ["A", "B"]},
        ]}).get_json()
    by = {o["ref"]: o for o in d["ops"]}
    assert by["f1"]["text"] == "Kofi" and by["f1"]["from"] == "profile"
    assert by["f2"]["text"] == "Yes, 4 years." and by["f2"]["from"] == "drafted"
    assert by["f3"]["text"].startswith("Dear Acme") and by["f3"]["from"] == "letter"
    assert by["f4"]["text"] == "Yes" and by["f4"]["from"] == "workauth"
    assert "f5" not in by and {"ref": "f5", "label": "What is your race?"} in d["left"]
