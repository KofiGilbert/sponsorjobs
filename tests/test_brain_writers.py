"""The applicant brain, Phase 3 (docs/applicant-brain.md): every writer saves facts with a source
ref, the readers ask the brain before asking the person, and two values that disagree become
one question.

Each writer lands its fact with the right source, confidence and ref: the details step, the
resume import (app and Telegram, with a document row), My profile, the builder's chat, the
chat's distilled facts, the extension's corrections, the agent's asks (bar and Telegram), the
late Telegram answers and the "Answers every form asks" card. Chat never writes a
self-identification value (the turn never reaches the model, and the store refuses it). A
lower-confidence value never replaces a confirmed one: it opens a conflict. The readers: the
fill gets city, state, ZIP and country from memory; the intake gate and the profile gate check
the brain first; tailoring reads the brain's preferences; the Telegram summary reads the brain.
"Have you worked here before?" is per company. The two disagreeing salaries are one question,
on Your answers and once in the agent's ask, never again. Offline: FakeLLM and stubs only."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from applicant import facts as F
from applicant import migrate as M
from applicant.store import ACTIVE, NEEDS_CHECK, REPLACED, RefusedWrite, open_store
from autoapply import asks as AK
from autoapply.fields import FormField, applicant_from_record
from autoapply.planner import KnownContext, plan_known
from brain_fixtures import LAM_SALARY, write_legacy

H = {"X-Tailor-Extension": "1"}
ROOT = Path(__file__).resolve().parents[1]


def _facts(st, key, subject=None, store=""):
    sql, args = "SELECT * FROM fact WHERE key=? AND store=?", [key, store]
    if subject is not None:
        sql += " AND subject=?"
        args.append(subject)
    return [dict(r) for r in st.q(sql + " ORDER BY id", args)]


def _active(st, key, subject="", store=""):
    return [r for r in _facts(st, key, subject, store) if r["status"] == ACTIVE]


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    d.mkdir()
    s = M.ensure(d)
    assert s is not None
    yield s
    M.forget_process_state(d)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """The engine on a fresh data folder with a saved profile (no legacy answers)."""
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Test", "email": "kofi@example.com", "phone": "312-555-0100",
                                              "location": "Chicago, IL"},
                                 "experience": [{"org": "Stanbic Bank", "roles": [{"title": "Analyst", "dates": "2020 - 2021"}]}],
                                 "education": [{"school": "University of Ghana", "degree": "BSc", "date": "2019"}]},
                     {"identity": {"name": "Kofi Test"}}, [])
    assert A._brain() is not None
    yield A, A.app.test_client()
    A._SESSION.pop("s", None)
    M.forget_process_state(data)


@pytest.fixture()
def legacy_app(tmp_path, monkeypatch):
    """The engine on the legacy fixture folder: two salaries that disagree (the library's
    $120,000 and a newer 130000 under the Lam label's old key, the prefs' 95000)."""
    monkeypatch.delenv(M.ENV, raising=False)
    data = write_legacy(tmp_path / "data")
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    assert A._brain() is not None
    yield A, A.app.test_client()
    M.forget_process_state(data)


# --------------------------------------------------------------------------- the store's rules
def test_a_lower_confidence_value_never_replaces_a_confirmed_one(st):
    F.put(st, "phone", "312-555-0100", source="you", source_ref="details")
    F.put(st, "phone", "312-555-9999", source="resume", source_ref="document:1")
    rows = _facts(st, "phone", "")
    assert [(r["value"], r["status"], r["check_reason"]) for r in rows] == [
        ("312-555-0100", ACTIVE, ""), ("312-555-9999", NEEDS_CHECK, "conflict")]
    # the person's own new value does replace it (kept as history), and settles the proposal
    F.put(st, "phone", "312-555-7777", source="you")
    assert [(r["value"], r["status"]) for r in _facts(st, "phone", "")] == [
        ("312-555-0100", REPLACED), ("312-555-9999", REPLACED), ("312-555-7777", ACTIVE)]
    # the same rule inside the store itself (add_fact)
    st.add_fact("relocate", "Yes", source="you")
    st.add_fact("relocate", "No", source="chat")
    assert [(r["value"], r["status"]) for r in _facts(st, "relocate", "")] == [("Yes", ACTIVE), ("No", NEEDS_CHECK)]


def test_resume_values_replace_each_other_and_an_unchanged_value_writes_nothing(st):
    assert F.put(st, "email", "a@x.com", source="resume") is not None
    assert F.put(st, "email", "a@x.com", source="resume") is None
    assert F.put(st, "email", "a@x.com", source="you") is None            # unchanged: no write...
    assert _active(st, "email")[0]["confidence"] == pytest.approx(0.9)
    F.put(st, "email", "a@x.com", source="you", confirm=True)             # ...unless the person kept it
    assert _active(st, "email")[0]["confidence"] == 1.0 and _active(st, "email")[0]["confirmed"] == 1
    F.put(st, "email", "b@x.com", source="resume")
    assert _active(st, "email")[0]["value"] == "a@x.com"                   # confirmed now: a conflict


def test_the_address_splits_into_its_parts_and_a_new_city_retires_the_old_zip(st):
    F.write_contact(st, {"address": "200 W Madison St, Chicago, IL 60606"}, source="you", source_ref="details")
    assert F.contact(st) == {"address": "200 W Madison St, Chicago, IL 60606", "street": "200 W Madison St",
                             "city": "Chicago", "state": "IL", "postal_code": "60606", "country": "United States"}
    F.write_contact(st, {"location": "Austin, TX"}, source="you")
    c = F.contact(st)
    assert (c["city"], c["state"], c.get("postal_code")) == ("Austin", "TX", None)
    assert _facts(st, "address.postal_code", "")[0]["status"] == REPLACED   # kept as history, never handed out
    assert F.split_address("", "Atlanta, Georgia") == {"city": "Atlanta", "state": "GA", "country": "United States"}
    assert F.split_address("Accra, Ghana") == {"city": "Accra", "country": "Ghana"}
    assert F.split_address("221B Baker Street") == {}


def test_chat_never_writes_a_self_identification_value(st):
    with pytest.raises(RefusedWrite):
        st.add_fact("@gender", "Male", source="chat")
    rows = [{"id": 1, "type": "fact", "key": "gender", "value": "male", "status": "active", "confidence": 0.9},
            {"id": 2, "type": "fact", "key": "desired_salary", "value": "$95,000", "status": "active",
             "confidence": 0.7, "provenance": "turn 3 · 2026-10-30 · diary:default.jsonl:12"},
            {"id": 3, "type": "fact", "key": "citizenship", "value": "Ghana", "status": "active", "confidence": 0.7},
            {"id": 4, "type": "constraint", "key": "avoid_viva", "value": "do not mention Viva", "status": "active",
             "confidence": 0.85}]
    n = F.sync_chat(st, rows)
    assert n["refused"] == 1 and not _facts(st, "@gender")
    sal = _facts(st, "@salary", "")[0]
    assert (sal["status"], sal["check_reason"], sal["source"], sal["confidence"], sal["source_ref"]) == (
        NEEDS_CHECK, "from_chat", "chat", pytest.approx(0.7), "diary:default.jsonl:12")
    assert _facts(st, "@citizenship", "")[0]["check_reason"] == "unconfirmed_legal"
    assert [x["value"] for x in F.notes(st)] == ["do not mention Viva"]
    # retracted: no longer read, kept as history; deleted by the person: forgotten
    F.sync_chat(st, [dict(rows[3], status="retracted")] + rows[1:3])
    assert F.notes(st) == [] and _facts(st, "constraint", "avoid_viva")[0]["status"] == REPLACED
    F.sync_chat(st, rows[1:3])
    assert _facts(st, "constraint", "avoid_viva") == []


def test_the_intake_chat_never_hands_a_self_identification_turn_to_the_model(tmp_path, monkeypatch):
    from intake.memory import ConversationMemory
    from llm.base import FakeLLM
    from ui.session import WebIntake
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    mem = ConversationMemory(str(d / "resume_agent.db"))
    st = M.ensure(d)
    seen = []

    class Spy(FakeLLM):
        def distill_memory(self, history, known_keys):
            seen.extend(t.get("content", "") for t in history)
            return super().distill_memory(history, known_keys)
    sess = WebIntake("Analyst at Acme. SQL.", "", Spy(), mem, None, tmp_path / "w", palace_dir=d / "palace")
    sess.history = [{"role": "user", "content": "1. Female 2. Black or African American 3. Not a veteran"},
                    {"role": "user", "content": "My salary expectation is $95,000 a year"}]
    sess._last_user_ref = "diary:default.jsonl:2"
    sess._distill_facts()
    assert seen and not any("veteran" in t.lower() for t in seen)
    sal = _facts(st, "@salary", "")
    assert sal and sal[0]["status"] == NEEDS_CHECK and sal[0]["confidence"] == pytest.approx(0.7)
    assert not _facts(st, "@gender") and not _facts(st, "@race") and not _facts(st, "@veteran")
    M.forget_process_state(d)


# --------------------------------------------------------------------------- the writers
class _Stub:
    def __init__(self, profile):
        self.profile = profile

    def extract_profile(self, text):
        return json.loads(json.dumps(self.profile))


RESUME = {"identity": {"name": "Kofi Test", "email": "kofi@example.com", "address": "200 W Madison St, Chicago, IL 60606"},
          "experience": [{"org": "Viva", "roles": [{"title": "Data Analyst", "dates": "2020 - 2022"}]}],
          "education": [{"school": "University of Ghana", "degree": "BSc", "date": "2019"}],
          "skills": {"Languages": "SQL, Python"}}


def _upload(A, c, tmp_path, monkeypatch):
    from docx import Document
    monkeypatch.setattr(A, "_make_llm", lambda: _Stub(RESUME))
    doc = Document()
    doc.add_paragraph("Kofi Test")
    cv = tmp_path / "resume.docx"
    doc.save(str(cv))
    with open(cv, "rb") as f:
        r = c.post("/api/profile/from_cv", data={"file": (f, "resume.docx")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()


def test_a_resume_import_writes_open_facts_at_0_9_with_a_document_row(app, tmp_path, monkeypatch):
    A, c = app
    _upload(A, c, tmp_path, monkeypatch)
    st = A._brain()
    docs = st.q("SELECT * FROM document")
    assert len(docs) == 1 and docs[0]["channel"] == "app" and docs[0]["kind"] == "resume" and docs[0]["facts"] > 0
    role = _active(st, "role", "role:viva|data analyst")[0]
    assert (role["source"], role["confidence"], role["source_ref"]) == ("resume", pytest.approx(0.9), f"document:{docs[0]['id']}")
    assert _active(st, "skill", "skill:sql") and _active(st, "school", "school:university of ghana")
    assert _active(st, "address.postal_code")[0]["value"] == "60606"
    # the same file on Telegram: a second document, on the telegram channel
    monkeypatch.setattr(A, "_make_llm", lambda: _Stub(RESUME))
    A._telegram_file_import({"name": "resume.txt", "mime": "text/plain"}, b"Kofi Test, Data Analyst at Viva")
    assert [d["channel"] for d in st.q("SELECT channel FROM document ORDER BY id")] == ["app", "telegram"]


def test_the_details_step_writes_the_persons_answers_as_their_own(app):
    A, c = app
    A._memory().save("default", {"identity": {"name": "Kofi Test"},
                                 "experience": [{"org": "Acme Corp", "roles": [{"title": "Engineer"}]}]}, {}, [])
    st = A._brain()
    for k in ("email", "phone"):                 # no contact in memory either (else it is not asked)
        st.forget(k, "", "")
    slots = c.get("/api/profile/missing").get_json()["missing"]
    vals = {"contact": "kofi@example.com", "dates": "2020 - 2024", "location": "Austin, TX"}
    answers = [{**{k: s[k] for k in ("sec", "key", "match")}, "value": vals[s["key"]]} for s in slots if s["key"] in vals]
    assert len(answers) == 3
    c.post("/api/profile/details", json={"answers": answers})
    em = _active(st, "email")[0]
    assert (em["value"], em["source"], em["confidence"], em["source_ref"]) == ("kofi@example.com", "you", 1.0, "details")
    role = _active(st, "role", "role:acme corp|engineer")[0]
    assert role["source"] == "you" and json.loads(role["value"])["dates"] == "2020 - 2024"


def test_my_profile_and_the_extension_corrections_write_contact_facts(app):
    A, c = app
    st = A._brain()
    assert _active(st, "phone")[0]["confidence"] == pytest.approx(0.9)        # from the saved profile
    c.post("/api/profile/identity", json={"name": "Kofi Test", "email": "kofi@example.com", "phone": "312-555-0100"})
    ph = _active(st, "phone")[0]
    assert (ph["confidence"], ph["confirmed"]) == (1.0, 1)                     # the person saw and kept it
    c.post("/api/profile/identity", json={"name": "Kofi Test", "email": "kofi@example.com", "phone": "312-555-0199"})
    ph = _active(st, "phone")[0]
    assert (ph["value"], ph["source"], ph["source_ref"]) == ("312-555-0199", "you", "profile")
    r = c.post("/api/profile/autofill", json={"fields": {"street": "1 Main St", "city": "Evanston", "state": "IL",
                                                          "zip": "60201"}}, headers=H)
    assert r.status_code == 200
    got = {k: (_active(st, "address." + k)[0]["value"], _active(st, "address." + k)[0]["source"])
           for k in ("street", "city", "state", "postal_code")}
    assert got == {"street": ("1 Main St", "extension"), "city": ("Evanston", "extension"),
                   "state": ("IL", "resume"),            # unchanged ("Chicago, IL"): keeps where it came from
                   "postal_code": ("60201", "extension")}


def test_the_builders_chat_writes_the_contact_the_person_typed(app):
    A, c = app
    assert c.post("/api/session/start", json={"jd": "Business Intelligence Analyst at Acme Corp. SQL."}).status_code == 200
    c.post("/api/session/answer", json={"text": "My home address is 200 W Madison St, Chicago, IL 60606"})
    st = A._brain()
    z = _active(st, "address.postal_code")[0]
    assert (z["value"], z["source"]) == ("60606", "you") and z["source_ref"].startswith("diary:")
    # the e2e's ?address=1 contact page: City, State and the required ZIP code fill from memory
    fields = [{"ref": r, "label": lab, "type": "text", "required": r == "zip"}
              for r, lab in (("fn", "First name"), ("city", "City"), ("st", "State"), ("zip", "ZIP code"))]
    r = c.post("/api/autoapply/plan", json={"url": "https://acme.example/apply", "fields": fields}, headers=H).get_json()
    got = {o["ref"]: o["text"] for o in r["ops"]}
    assert got == {"fn": "Kofi", "city": "Chicago", "st": "IL", "zip": "60606"} and not r["left"]


def test_asks_late_answers_and_the_card_keep_where_they_came_from(app):
    A, c = app
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"question": "What is your notice period?", "timeout_s": 10,
                                                             "id": "ab12cd34"}, headers=H).get_json()
    th = threading.Thread(target=ask)
    th.start()
    time.sleep(1.2)
    c.post("/api/autoapply/agent/answer", json={"id": "ab12cd34", "answer": "Two weeks", "exact": True, "index": 0}, headers=H)
    th.join(15)
    assert out["r"]["ok"]
    st = A._brain()
    f = _active(st, "@notice_period", "", "answers.db")[0]
    assert (f["value"], f["source"], f["source_ref"]) == ("Two weeks", "you", "ask_bar:ab12cd34")
    assert st.q("SELECT 1 FROM event WHERE kind='ask_answered' AND ref='ask:ab12cd34'")
    # a late Telegram answer
    msg = A._late_save({"id": "ef56", "items": [{"n": 1, "question": "When can you start?", "options": []}],
                        "company": "Acme"}, {0: "In a month"})
    assert msg and _active(st, "@start_date", "", "answers.db")[0]["source_ref"] == "late_telegram:ef56"
    # the card
    c.post("/api/answers/basics", json={"fields": {"citizenship": "Ghana"}})
    assert _active(st, "@citizenship", "", "answers.db")[0]["source_ref"] == "card"
    assert _active(st, "@citizenship", "", "answers.db")[0]["code"] == "Ghana"


# --------------------------------------------------------------------------- the readers
def test_the_fill_gets_city_state_zip_and_country_from_memory():
    mem = {"city": "Chicago", "state": "IL", "postal_code": "60606", "country": "United States"}
    a = applicant_from_record({"profile": {"identity": {"name": "Kofi Test"}}}, mem)
    assert {k: a[k] for k in mem} == mem
    # the record's own address line wins, and another place's ZIP is never mixed in
    b = applicant_from_record({"profile": {"identity": {"name": "K T", "address": "1 Main St, Boston, MA 02110"}}}, mem)
    assert (b["city"], b["state"], b["postal_code"]) == ("Boston", "MA", "02110")
    c = applicant_from_record({"profile": {"identity": {"name": "K T", "location": "Austin, TX"}}}, mem)
    assert (c["city"], c["state"], c.get("postal_code")) == ("Austin", "TX", None)


def test_the_profile_gate_and_the_intake_gate_check_the_brain_first(app):
    A, c = app
    A._memory().save("default", {"identity": {"name": "Kofi Test"},
                                 "experience": [{"org": "Acme Corp", "location": "Remote",
                                                 "roles": [{"title": "Engineer"}]}]}, {}, [])
    st = A._brain()
    for k in ("email", "phone"):
        st.forget(k, "", "")
    qs = [s["q"] for s in c.get("/api/profile/missing").get_json()["missing"]]
    assert "an email or phone number" in qs and any("employment dates" in q for q in qs)
    F.put(st, "email", "kofi@example.com", source="extension")
    F.put(st, "role", {"org": "Acme Corp", "title": "Engineer", "dates": "2020 - 2024"}, subject="role:acme corp|engineer",
          source="you")
    qs = [s["q"] for s in c.get("/api/profile/missing").get_json()["missing"]]
    assert "an email or phone number" not in qs and not any("employment dates" in q for q in qs)
    # the builder's gate: the brain's details land in the session's essentials (and the resume)
    assert c.post("/api/session/start", json={"jd": "Engineer at Acme Corp. Python."}).status_code == 200
    sess = A._SESSION["s"]
    assert not any("email" in m or "phone" in m for m in sess._missing_required())
    assert sess.essentials["identity"]["email"] == "kofi@example.com"


def test_tailoring_reads_the_brains_preferences(app):
    A, c = app
    assert c.post("/api/session/start", json={"jd": "Analyst at Acme. SQL."}).status_code == 200
    sess = A._SESSION["s"]
    sess.memory.upsert_fact("default", "constraint", "avoid_viva", "do not mention Viva", provenance="t", confidence=0.85)
    assert sess._avoid_token_sets() == [{"viva"}]
    assert [n["value"] for n in F.notes(A._brain())] == ["do not mention Viva"]
    fid = sess.memory.list_facts("default")[0]["id"]
    sess.memory.retract_fact("default", fid)
    assert sess._avoid_token_sets() == [] and F.notes(A._brain()) == []


def test_the_telegram_summary_reads_the_brain_and_never_a_value(app):
    A, c = app
    A._memory().save("default", {}, {}, [])
    st = A._brain()
    F.put(st, "name", "Kofi Test", source="resume")
    F.put(st, "role", {"org": "Viva", "title": "Data Analyst", "dates": "2020"}, subject="role:viva|data analyst",
          source="resume")
    A._merge_prefs({"desired_salary": "95000", "earliest_start": "2 weeks"})
    F.save_worked_here(st, "Acme Corp", "Yes")                     # a brain-only answer (per company)
    text = A._assistant_summary()
    assert "Name: Kofi Test" in text and "Data Analyst at Viva" in text
    assert "salary expectations" in text and "When can you start?" in text
    assert "Have you worked for this company before?" in text
    assert "95000" not in text and "2 weeks" not in text


# --------------------------------------------------------------------------- worked here before
def test_worked_here_before_is_per_company(st):
    # (Phase 4) a Yes read from the resume is confirmed once per company before it is used
    assert F.worked_here(st, "Stanbic Bank", ["Stanbic Bank Ghana", "Viva"]) == ("Yes", "confirm")
    assert F.worked_here(st, "Acme Corp", ["Stanbic Bank Ghana"]) is None                # never answered: asked
    F.save_worked_here(st, "Acme Corporation", "No", question="Have you worked for us before?")
    assert F.worked_here(st, "ACME Corp.", []) == ("No", "library")                        # the same company
    assert F.worked_here(st, "Globex", ["Stanbic Bank"]) == ("No", "derived")             # answers from history
    assert F.worked_here(st, "Stanbic", ["Stanbic Bank"]) == ("Yes", "confirm")
    F.save_worked_here(st, "Globex", "Yes, as an intern")
    assert F.worked_here(st, "Globex", []) == ("Yes", "library")
    assert F.worked_here(st, "Initech", []) == ("No", "derived")                           # a No exists elsewhere
    assert F.save_worked_here(st, "Initech", "Prefer not to say") is False


def test_a_yes_for_one_company_never_answers_another(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Have you worked for this company before?", "Yes", "you")      # a standing Yes (Your answers)
    finally:
        lib.close()
    q = [{"question": "Have you previously worked for this company?", "options": ["Yes", "No"]}]
    r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert r["answers"] == {}                                                    # asked, never the other Yes
    # the person's employer: Yes from their history
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Analyst", "Stanbic Bank", 70, "", data={})
    recs.close()
    r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q, "record_id": rid}, headers=H).get_json()
    # (Phase 4) the person's employer: the resume's Yes is a one-tap confirm, never sent unconfirmed
    assert r["answers"] == {}
    assert r["reword"][q[0]["question"]] == {"say": "Your resume lists Stanbic Bank. Have you worked for Stanbic Bank before?",
                                             "options": ["Yes", "No"]}
    ctx = KnownContext(per_company=True, worked_here=A._worked_here("Stanbic Bank"))
    fld = FormField("w", "Were you previously an employee of Stanbic Bank?", "radio", ["Yes", "No"])
    res = plan_known([fld], ctx)
    assert res.actions == [] and res.left == [fld]
    # once the person says Yes for this company, it is theirs and fills
    st = A._brain()
    F.save_worked_here(st, "Stanbic Bank", "Yes", question=q[0]["question"])
    ctx = KnownContext(per_company=True, worked_here=A._worked_here("Stanbic Bank"))
    assert [(a.value, a.origin) for a in plan_known([fld], ctx).actions] == [("Yes", "library")]


def test_an_asked_worked_here_answer_is_saved_for_that_company_only(app):
    A, c = app
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Analyst", "Initech", 70, "", data={})
    recs.close()
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"question": "Have you ever worked for this company before?",
                                                             "options": ["Yes", "No"], "timeout_s": 10, "id": "w1",
                                                             "record_id": rid}, headers=H).get_json()
    th = threading.Thread(target=ask)
    th.start()
    time.sleep(1.2)
    c.post("/api/autoapply/agent/answer", json={"id": "w1", "answer": "No", "exact": True, "index": 0}, headers=H)
    th.join(15)
    st = A._brain()
    assert _active(st, "@previously_worked", F.company_key("Initech"))[0]["source_ref"] == "ask_bar:w1"
    assert not _facts(st, "@previously_worked", "", "answers.db")                       # never every employer's
    assert A._worked_here("Globex") == ("No", "derived")
    assert A._worked_here("Stanbic Bank") == ("Yes", "confirm")


# --------------------------------------------------------------------------- one question for two salaries
def test_two_salaries_that_disagree_are_one_question_on_your_answers(legacy_app):
    A, c = legacy_app
    d = c.get("/api/answers/basics").get_json()
    [conf] = d["conflicts"]
    # the library's two wordings, the extension's and the one said in chat ("About 100k")
    assert conf["key"] == "@salary" and conf["text"] == "You saved 4 different salaries. Which one is right?"
    assert sorted(x["label"] for x in conf["choices"]) == ["$100,000", "$120,000", "$130,000", "$95,000"]
    d = c.post("/api/brain/conflicts/settle", json={"key": "@salary", "value": "120000"}).get_json()
    assert d["conflicts"] == [] and d["values"]["salary"] == "120000"
    assert json.loads((A._DATA / "app_prefs.json").read_text())["desired_salary"] == "120000"
    lib = A._answer_lib()
    try:
        assert lib.lookup(LAM_SALARY)["answer"] == "120000"
    finally:
        lib.close()


def test_the_two_value_wording(st):
    lib_t, pref_t = time.time() - 200 * 86400, time.time()
    st.add_fact("@salary", "90000", source="you", store="app_prefs.json", updated_at=lib_t)
    st.add_fact("@salary", "120000", source="you", store="answers.db", question="Desired salary", updated_at=pref_t)
    c = F.salary_conflict(st)
    month = time.strftime("%B", time.localtime(lib_t))
    assert c["text"] == f"Forms say $90,000 ({month}); you also saved $120,000. Which one?"
    assert [x["value"] for x in c["choices"]] == ["120000", "90000"] and not c["asked"]
    F.mark_asked(st, c)
    assert F.salary_conflict(st)["asked"]


def test_the_agent_asks_the_salary_question_once_then_fills_the_newest(legacy_app):
    A, c = legacy_app
    fields = [{"ref": "s", "label": LAM_SALARY, "type": "text", "required": True},
              {"ref": "o", "label": "Desired salary", "type": "text", "required": False}]
    r = c.post("/api/autoapply/plan", json={"url": "https://acme.example/apply", "fields": fields}, headers=H).get_json()
    assert {o["ref"]: o["text"] for o in r["ops"]} == {"o": "130000"}           # an optional field fills as before
    assert [x["ref"] for x in r["left"]] == ["s"]                               # the required one is asked
    assert A._known_answers_for([LAM_SALARY], None, {}, {}) == {}
    q = [{"question": LAM_SALARY, "options": []}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {} and sorted(k["reword"][LAM_SALARY]["options"]) == ["$100,000", "$120,000", "$130,000", "$95,000"]
    assert k["reword"][LAM_SALARY]["say"].endswith("Which one is right?")
    # the real ask: offered with the amounts, answered on the bar, settled everywhere
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"questions": q, "timeout_s": 10, "id": "sal1"},
                          headers=H).get_json()
    th = threading.Thread(target=ask)
    th.start()
    time.sleep(1.2)
    with A._ASKS_LOCK:
        assert "$120,000" in A._ASKS["sal1"]["items"][0]["options"]
    c.post("/api/autoapply/agent/answer", json={"id": "sal1", "answer": "$120,000", "exact": True, "index": 0}, headers=H)
    th.join(15)
    assert out["r"]["answers"] == {LAM_SALARY: "$120,000"}
    assert A._salary_conflict() is None
    assert json.loads((A._DATA / "app_prefs.json").read_text())["desired_salary"] == "120000"
    assert A._known_answers_for([LAM_SALARY], None, {}, {}) == {LAM_SALARY: "120000"}


def test_an_unanswered_salary_question_is_not_asked_again(legacy_app):
    A, c = legacy_app
    q = [{"question": LAM_SALARY, "options": []}]
    r = c.post("/api/autoapply/agent/ask", json={"questions": q, "timeout_s": 5}, headers=H).get_json()
    assert r["reason"] == "timeout"
    assert A._salary_conflict()["asked"]                                         # still on Your answers
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {LAM_SALARY: "130000"} and k["reword"] == {}         # the newest, as before


def test_a_salary_said_in_chat_opens_the_question_and_the_card_settles_it(app):
    A, c = app
    c.post("/api/answers/basics", json={"fields": {"salary": "90000"}})
    assert A._salary_conflict() is None
    st = A._brain()
    F.sync_chat(st, [{"id": 1, "type": "fact", "key": "desired_salary", "value": "$110k", "status": "active",
                      "confidence": 0.7}])
    conf = A._salary_conflict()
    assert conf and conf["text"] == "Forms say $90,000; you just said $110,000. Which one?"
    c.post("/api/answers/basics", json={"fields": {"salary": "110000"}})
    assert A._salary_conflict() is None
    assert _facts(st, "@salary", "")[0]["status"] == ACTIVE and _facts(st, "@salary", "")[0]["confirmed"] == 1


# --------------------------------------------------------------------------- Telegram
def test_telegram_buttons_carry_the_ask_id_and_the_choice_number_only():
    opts = ["Male", "Female", "I don't wish to answer"]
    _text, rows = AK.telegram_ask("ab12", [{"question": "Gender", "options": opts, "all_options": opts}])
    datas = [d for row in rows for (_label, d) in row]
    assert datas == ["ask:ab12:0", "ask:ab12:1", "ask:ab12:2"]
    assert not any(o.lower() in d.lower() for d in datas for o in opts)


def test_the_salary_question_reads_the_same_on_telegram():
    text, rows = AK.telegram_ask("s1", [{"question": "Desired salary", "options": ["$120,000", "$90,000"],
                                         "say": "Forms say $90,000 (March); you said $120,000 (Oct 28). Which one?"}])
    assert "Forms say $90,000 (March)" in text and [d for row in rows for _l, d in row] == ["ask:s1:0", "ask:s1:1"]


# --------------------------------------------------------------------------- the Phase 3 upgrade
def test_an_existing_brain_gets_the_address_parts_once(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    from intake.memory import ConversationMemory
    d = tmp_path / "data"
    ConversationMemory(str(d / "resume_agent.db")).save(
        "default", {"identity": {"name": "Ama", "address": "5 Main St, Springfield, Illinois 62701"}}, {}, [])
    assert M.apply(d)["ok"]
    st = open_store(d / "applicant.db")
    st.set_meta("phase3", None)
    with st.tx() as c:
        c.execute("DELETE FROM fact WHERE key LIKE 'address.%'")                 # as Phase 1 left it
    assert M.ensure(d) is st
    assert F.contact(st)["postal_code"] == "62701" and F.contact(st)["state"] == "IL"
    assert st.meta("phase3") == "1"
    M.forget_process_state(d)


# --------------------------------------------------------------------------- the shell
@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_shell_rewords_the_salary_question_and_never_names_an_answered_label():
    js = """
const a = require(%s);
const items = [{question: "Desired salary", options: [], all_options: []}, {question: "Notice period", options: []}];
a.applyReword(items, {"Desired salary": {say: "Forms say $90,000 (March); you said $120,000. Which one?", options: ["$120,000", "$90,000"]}});
const left = ["Desired salary", {label: "How many years of experience do you have?"}, "Notice period"];
const open = a.stillOpen(left, {"How many years of experience do you have?": "5"}, new Set(["Notice period"]));
console.log(JSON.stringify({items, open, msg: a.blockedMessage("needs-answers", open)}));
""" % json.dumps(str(ROOT / "shell-electron" / "agent.js"))
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60).stdout)
    it = out["items"][0]
    assert it["say"].startswith("Forms say") and it["options"] == ["$120,000", "$90,000"] and it["partial"] is True
    assert out["items"][1] == {"question": "Notice period", "options": []}
    assert out["open"] == ["Desired salary"] and "years" not in out["msg"]
