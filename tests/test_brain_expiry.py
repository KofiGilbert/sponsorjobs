"""The applicant brain, Phase 4 (docs/applicant-brain.md, sections 7.1 and 7.2): a saved value
that expired, or does not fit the job, is asked, never sent.

An OPT or work permit whose end date passed no longer answers "Are you authorized to work?";
a salary older than six months asks "Still $90,000?" once on a required field; a start date in
the past is never filled; a US dollar salary is never sent to a UK job, and the salary the
person gives for that job is kept for UK jobs only. Offline: FakeLLM only."""
from __future__ import annotations

import datetime as dt
import importlib
import threading
import time

import pytest

from applicant import expiry as E
from applicant import migrate as M
from autoapply import asks as AK
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known

H = {"X-Tailor-Extension": "1"}
DAY = dt.date(2026, 10, 30)
AUTH = "Are you legally authorized to work in the United States?"
SPONSOR_LATER = "Will you require visa sponsorship in the future?"
CITIZEN = "Are you a U.S. citizen?"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Test", "email": "kofi@example.com"}}, {}, [])
    assert A._brain() is not None
    yield A, A.app.test_client()
    M.forget_process_state(data)


def _save(A, q, a, ago_days=0):
    lib = A._answer_lib()
    try:
        lib.save(q, a, "you")
    finally:
        lib.close()
    if ago_days:
        st = A._brain()
        then = time.time() - ago_days * 86400
        st.conn.execute("UPDATE fact SET updated_at=? WHERE store='answers.db' AND question=?", (then, q))
        from autoapply.answers import AnswerLibrary
        legacy = AnswerLibrary(A._ANSWERS_DB)
        legacy.conn.execute("UPDATE answers SET updated_at=? WHERE question=?", (then, q))
        legacy.conn.commit()
        legacy.close()


def _ask(c, aid, question, answer, rid=None, options=None):
    out = {}

    def run():
        body = {"question": question, "timeout_s": 10, "id": aid, "record_id": rid}
        if options:
            body["options"] = options
        out["r"] = c.post("/api/autoapply/agent/ask", json=body, headers=H).get_json()
    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.2)
    c.post("/api/autoapply/agent/answer", json={"id": aid, "answer": answer, "exact": True, "index": 0}, headers=H)
    th.join(15)
    return out["r"]


def _record(A, location):
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Analyst", "Acme Corp", 70, "", data={"source_job": {"url": "https://x.test/1", "location": location}})
    recs.close()
    return rid


# --------------------------------------------------------------------------- the pure rules
def test_dates_people_save():
    assert E.parse_date("2026-05-01") == dt.date(2026, 5, 1)
    assert E.parse_date("2026-02") == dt.date(2026, 2, 28)
    assert E.parse_date("05/01/2026") == dt.date(2026, 5, 1)
    assert E.parse_date("May 1, 2026") == dt.date(2026, 5, 1) == E.parse_date("1st May 2026")
    assert E.parse_date("Immediately") is None and E.parse_date("Two weeks after an offer") is None
    assert E.start_date_passed("2026-05-01", DAY) == dt.date(2026, 5, 1)
    assert E.start_date_passed("2027-01-04", DAY) is None and E.start_date_passed("ASAP", DAY) is None


def test_a_work_authorization_that_ended():
    wa = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                               "sponsor_future": "Yes", "auth_end": "2026-06"}}}
    assert E.workauth_ended(wa, "US", DAY) == dt.date(2026, 6, 30)
    assert E.workauth_ended(wa, "US", dt.date(2026, 6, 1)) is None
    assert E.workauth_ended({"countries": {"US": {"status": "green_card", "auth_end": "2020-01"}}}, "US", DAY) is None
    ended = E.workauth_ended(wa, "US", DAY)
    fields = [FormField("a", AUTH, "radio", ["Yes", "No"], required=True),
              FormField("f", SPONSOR_LATER, "radio", ["Yes", "No"], required=True),
              FormField("c", CITIZEN, "radio", ["Yes", "No"], required=True)]
    res = plan_known(fields, KnownContext(work_auth=wa, wa_ended=ended))
    assert {a.ref: a.value for a in res.actions} == {"f": "Yes", "c": "No"}       # what the end date cannot change
    assert [f.ref for f in res.left] == ["a"]
    assert {a.ref for a in plan_known(fields, KnownContext(work_auth=wa)).actions} == {"a", "f", "c"}   # brain off
    assert AK.known_answer(AUTH, ["Yes", "No"], work_auth=wa, wa_ended=ended) is None
    assert AK.known_answer(SPONSOR_LATER, ["Yes", "No"], work_auth=wa, wa_ended=ended) == ("Yes", "workauth")


def test_an_answer_given_after_the_end_date_still_counts():
    wa = {"countries": {"US": {"status": "f1_opt", "authorized": "", "auth_end": "2026-06"}}}
    lib = type("L", (), {"lookup": lambda self, q: {"key": "x", "answer": "Yes", "updated_at": time.time()}})()
    old = type("L", (), {"lookup": lambda self, q: {"key": "x", "answer": "Yes", "updated_at": 1_600_000_000}})()
    f = [FormField("a", AUTH, "radio", ["Yes", "No"], required=True)]
    ended = E.workauth_ended(wa, "US", DAY)
    assert [a.value for a in plan_known(f, KnownContext(work_auth=wa, library=lib, wa_ended=ended)).actions] == ["Yes"]
    assert plan_known(f, KnownContext(work_auth=wa, library=old, wa_ended=ended)).actions == []


def test_salary_age_and_currency():
    now = time.time()
    assert E.salary_stale({"answer": "90000", "updated_at": now - 200 * 86400})
    assert not E.salary_stale({"answer": "90000", "updated_at": now - 30 * 86400})
    assert E.currency("90000") == "USD" == E.currency("$90,000") == E.currency("90,000 USD")
    assert E.currency("£60,000") == "GBP" and E.currency("60000 GBP") == "GBP" and E.currency("€55k") == "EUR"
    assert E.currency("C$80,000") == "CAD" and E.currency("80000 CAD") == "CAD"
    assert E.fits_job("90000", "US") and not E.fits_job("90000", "UK") and E.fits_job("£60,000", "UK")
    assert not E.fits_job("$80,000", "CA") and E.fits_job("CAD 80,000", "CA")


# --------------------------------------------------------------------------- the engine
def test_an_old_salary_asks_still_x_once(app):
    A, c = app
    _save(A, "Desired salary", "90000", ago_days=200)
    q = [{"question": "Desired salary"}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {} and k["reword"]["Desired salary"] == {
        "say": "Still $90,000? Tap it to keep it, or type a new amount.", "options": ["$90,000"]}
    # an optional salary field fills as before
    r = c.post("/api/autoapply/plan", json={"url": "https://x.test/1", "fields": [
        {"ref": "s", "label": "Desired salary", "type": "text"}]}, headers=H).get_json()
    assert r["ops"][0]["text"] == "90000"
    r = _ask(c, "s1", "Desired salary", "$90,000")
    assert r["ok"] and r["answers"] == {"Desired salary": "$90,000"}
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {"Desired salary": "$90,000"} and k["reword"] == {}    # kept, fresh again


def test_an_unanswered_still_x_is_asked_once_then_fills(app):
    A, c = app
    _save(A, "Desired salary", "90000", ago_days=200)
    r = c.post("/api/autoapply/agent/ask", json={"question": "Desired salary", "timeout_s": 5, "id": "s2"},
               headers=H).get_json()
    assert r["ok"] is False and r["reason"] == "timeout"
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [{"question": "Desired salary"}]},
               headers=H).get_json()
    assert k["answers"] == {"Desired salary": "90000"}                         # asked once; then as before


def test_a_start_date_in_the_past_is_never_filled(app):
    A, c = app
    _save(A, "When can you start?", "2026-05-01")
    fields = [{"ref": "d", "label": "When can you start?", "type": "text"}]
    r = c.post("/api/autoapply/plan", json={"url": "https://x.test/1", "fields": fields + [
        {"ref": "e", "label": "Email", "type": "email"}]}, headers=H).get_json()
    assert [o["ref"] for o in r["ops"]] == ["e"] and r["left"][0]["ref"] == "d"
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [{"question": "When can you start?"}]},
               headers=H).get_json()
    assert k["answers"] == {} and k["reword"]["When can you start?"]["say"] == \
        "Your saved start date (May 1, 2026) has passed. When can you start?"
    assert A._known_answers_for(["When can you start?"], None, {}, {}) == {}
    _save(A, "When can you start?", "2099-01-04")
    assert A._known_answers_for(["When can you start?"], None, {}, {}) == {"When can you start?": "2099-01-04"}


def test_a_dollar_salary_is_never_sent_to_a_uk_job(app):
    A, c = app
    _save(A, "Desired salary", "90000")
    uk, us = _record(A, "London, UK"), _record(A, "Chicago, IL")
    q = [{"question": "Desired salary"}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q, "record_id": uk}, headers=H).get_json()
    assert k["answers"] == {} and k["reword"]["Desired salary"]["say"] == \
        "This job is in the UK. What salary do you want for it?"
    r = _ask(c, "u1", "Desired salary", "£60,000", rid=uk)
    assert r["ok"] and r["answers"] == {"Desired salary": "£60,000"}
    st = A._brain()
    assert E.country_salary(st, "UK") == "£60,000"
    lib = A._answer_lib()
    try:
        assert lib.lookup("Desired salary")["answer"] == "90000"                   # the US salary is untouched
    finally:
        lib.close()
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q, "record_id": uk}, headers=H).get_json()
    assert k["answers"] == {"Desired salary": "£60,000"}
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q, "record_id": us}, headers=H).get_json()
    assert k["answers"] == {"Desired salary": "90000"}
    from applicant import facts as F
    assert F.salary_conflict(st) is None                                        # two countries, not a conflict


def test_an_expired_permit_is_asked_with_its_date(app):
    A, c = app
    A._merge_prefs({"work_auth": {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                                                       "sponsor_future": "Yes", "auth_end": "2025-06"}}}})
    q = [{"question": AUTH, "options": ["Yes", "No"]}, {"question": SPONSOR_LATER, "options": ["Yes", "No"]}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {SPONSOR_LATER: "Yes"}
    assert k["reword"][AUTH] == {"say": "Your saved work authorization ended on June 30, 2025. " + AUTH, "options": []}
    assert AK.prompt_text([{"question": AUTH, "options": ["Yes", "No"], "say": k["reword"][AUTH]["say"]}]).startswith(
        "Your saved work authorization ended on June 30, 2025.")


def test_the_brain_off_keeps_the_old_answers(app, monkeypatch):
    A, c = app
    _save(A, "When can you start?", "2026-05-01")
    monkeypatch.setenv(M.ENV, "0")
    assert A._recall_ctx("Acme") == {}
    assert A._known_answers_for(["When can you start?"], None, {}, {}) == {"When can you start?": "2026-05-01"}
