"""The applicant brain's store (docs/applicant-brain.md, sections 5 and 9; Phase 1).

One active row per single-valued key; a replaced value is kept as history; forgetting is a
hard delete (checked with a raw SELECT); EEO values never come from chat, the resume, a draft
or a derivation; sensitivity is set by the key registry, never by the caller; the event log
holds no value; BrainLibrary mirrors every write into answers.db row for row; the redact guard
finds stored values outside the form's own text."""
from __future__ import annotations

import json
import sqlite3

import pytest

from applicant import keys as K
from applicant import redact
from applicant.library import BrainLibrary, catch_up_prefs, prefs_view
from applicant.store import RefusedWrite, Store, STORE_ANSWERS
from autoapply import answers as A


@pytest.fixture()
def st(tmp_path):
    s = Store(tmp_path / "applicant.db")
    yield s
    s.close()


def _raw(st, sql, args=()):
    con = sqlite3.connect(st.path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_the_whole_schema_is_created_at_version_1(st):
    names = {r[0] for r in _raw(st, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"fact", "rule", "application", "fill", "event", "document", "label_map", "company_class", "meta"} <= names
    assert st.meta("schema_version") == "1"
    assert _raw(st, "PRAGMA journal_mode")[0][0] == "wal"


def test_one_active_row_per_single_valued_key_and_history_is_kept(st):
    a = st.add_fact("@salary", "120000", source="you")
    b = st.add_fact("@salary", "130000", source="card")
    rows = st.facts(key="@salary")
    assert [(r["id"], r["status"]) for r in rows] == [(a, "replaced"), (b, "active")]
    assert rows[0]["replaced_by"] == b and rows[0]["value"] == "120000"
    # a multi-valued key keeps several
    st.add_fact("@citizenship", "Ghana", source="you")
    st.add_fact("@citizenship", "Canada", source="you")
    assert len(st.facts(key="@citizenship", status="active")) == 2
    # one active row per SUBJECT
    st.add_fact("workauth", "{}", source="you", subject="US")
    st.add_fact("workauth", "{}", source="you", subject="UK")
    assert len(st.facts(key="workauth", status="active")) == 2


def test_forget_is_a_hard_delete_with_history_and_fills(st):
    a = st.add_fact("@salary", "120000", source="you")
    b = st.add_fact("@salary", "130000", source="you")
    with st.tx() as c:
        c.execute("INSERT INTO fill (application_id, label, key, fact_id, origin, at) VALUES (1, 'Salary', '@salary', ?, 'library', 0)", (b,))
    assert st.forget("@salary") == 2
    assert _raw(st, "SELECT * FROM fact WHERE id IN (?, ?)", (a, b)) == []
    assert _raw(st, "SELECT * FROM fill") == []
    assert not any("120000" in str(r) or "130000" in str(r) for r in _raw(st, "SELECT * FROM event"))


@pytest.mark.parametrize("source", ["chat", "telegram_chat", "resume", "drafted", "derived", "model"])
def test_eeo_values_are_refused_from_anything_but_the_persons_own_choice(st, source):
    with pytest.raises(RefusedWrite):
        st.add_fact("@gender", "Female", source=source)
    assert st.facts(key="@gender") == []
    # a one-question key whose wording is a self-identification question is EEO too
    with pytest.raises(RefusedWrite):
        st.add_fact("q:orientation sexual", "Straight", source=source, question="What is your sexual orientation?")


@pytest.mark.parametrize("source", ["you", "card", "onboarding", "ask_bar", "ask_telegram", "extension"])
def test_the_persons_own_eeo_choice_is_kept_without_their_words(st, source):
    fid = st.add_fact("@gender", "Female", source=source, words="I'm a woman")
    r = st.q("SELECT * FROM fact WHERE id=?", (fid,))[0]
    assert r["sensitivity"] == "eeo" and r["words"] == "" and r["status"] == "active"


def test_sensitivity_is_set_by_the_registry_never_the_caller(st):
    fid = st.add_fact("@gender", "Male", source="you", sensitivity="normal")
    assert st.q("SELECT sensitivity FROM fact WHERE id=?", (fid,))[0][0] == "eeo"
    fid = st.add_fact("@citizenship", "Ghana", source="you", sensitivity="normal")
    assert st.q("SELECT sensitivity FROM fact WHERE id=?", (fid,))[0][0] == "legal"
    fid = st.add_fact("q:sponsorship visa", "Yes", source="you", sensitivity="eeo",
                      question="Will you require visa sponsorship?")
    assert st.q("SELECT sensitivity FROM fact WHERE id=?", (fid,))[0][0] == "legal"
    fid = st.add_fact("q:certifications cloud hold", "AWS", source="you", sensitivity="eeo",
                      question="Which cloud certifications do you hold?")
    assert st.q("SELECT sensitivity FROM fact WHERE id=?", (fid,))[0][0] == "normal"


def test_a_legal_value_from_the_resume_or_chat_waits_for_a_confirm(st):
    fid = st.add_fact("@citizenship", "Ghana", source="resume")
    r = st.q("SELECT status, check_reason, confidence FROM fact WHERE id=?", (fid,))[0]
    assert (r["status"], r["check_reason"], r["confidence"]) == ("needs_check", "unconfirmed_legal", 0.9)


def test_every_answer_library_canonical_key_is_registered():
    for k, _rx in A._CANON:
        assert K.get("@" + k) is not None, k
    for k in A._EEO_KEYS:
        assert K.sensitivity(k) == "eeo"


def test_the_event_log_keeps_no_value(st):
    st.log("fact_added", ref="answers.db:@gender", value="Female", key="@gender", n=1)
    detail = json.loads(st.q("SELECT detail FROM event")[0]["detail"])
    assert detail == {"key": "@gender", "n": 1}


# --------------------------------------------------------------------------- BrainLibrary and its mirror
def _legacy_rows(path):
    con = sqlite3.connect(path)
    try:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT key, question, answer, source, created_at, updated_at, last_used, uses FROM answers")}
    finally:
        con.close()


def test_the_mirror_copies_every_write_into_answers_db_row_for_row(st, tmp_path):
    st.set_meta("mirror_legacy", "1")
    legacy = tmp_path / "answers.db"
    lib = BrainLibrary(st, mirror_path=legacy)
    assert lib.save("What are your salary expectations?", "120000")["key"] == "@salary"
    lib.save("Do you have experience with SQL?", "Yes", "drafted")
    lib.mark_used("@salary")
    lib.update("experience have sql", answer="Yes, 3 years")
    brain = {r["key"]: (r["question"], r["answer"], r["source"], r["updated_at"], r["uses"]) for r in lib.list()}
    rows = _legacy_rows(legacy)
    assert {k: (v[0], v[1], v[2], v[4], v[6]) for k, v in rows.items()} == brain
    assert rows["@salary"][6] == 1 and rows["experience have sql"][2] == "you"
    # the replaced salary is history in the brain, never a second row in the library
    lib.save("Desired salary", "130000")
    assert len(st.facts(key="@salary", store=STORE_ANSWERS)) == 2 and lib.count() == 2
    assert lib.delete("@salary") and "@salary" not in _legacy_rows(legacy)
    assert st.facts(key="@salary") == []


def test_a_drafted_self_identification_answer_is_never_saved(st, tmp_path):
    lib = BrainLibrary(st, mirror_path=tmp_path / "answers.db")
    assert lib.save("Gender", "Male", "drafted") is None
    assert lib.save("Gender", "Male", "you")["answer"] == "Male"
    assert lib.save("Gender", "Female", "drafted") is None and lib.get_key("@gender")["answer"] == "Male"


def test_the_prefs_view_round_trips_the_prefs_answers(st):
    from autoapply import workauth as W
    from brain_fixtures import PREFS
    catch_up_prefs(st, PREFS)
    view = prefs_view(st)
    assert W.load(view) == W.load(PREFS) and view["eeo_decline"] is False
    assert view["desired_salary"] == "95000" and view["needs_sponsorship"] == "Future"
    # a cleared answer is forgotten, a changed one replaces
    catch_up_prefs(st, dict(PREFS, desired_salary="", eeo_decline=True))
    assert prefs_view(st)["desired_salary"] == "" and prefs_view(st)["eeo_decline"] is True
    assert st.facts(key="@salary", store="app_prefs.json") == []
    n = len(st.facts())
    catch_up_prefs(st, dict(PREFS, desired_salary="", eeo_decline=True))
    assert len(st.facts()) == n                                   # nothing to do the second time


# --------------------------------------------------------------------------- redact
def test_redact_finds_and_withholds_stored_values_outside_the_forms_own_text(st):
    st.add_fact("@gender", "Female", source="you")
    st.add_fact("@race", "Asian", source="you")
    st.add_fact("@citizenship", "Ghana", source="you")
    st.add_fact("@over_18", "Yes", source="you")                   # too common to mean anything
    st.add_fact("workauth", json.dumps({"status": "f1_opt", "authorized": "Yes"}), source="you", subject="US")
    vals = redact.sensitive_values(st)
    assert set(vals) == {"Female", "Asian", "Ghana", "f1_opt"}
    payload = {"fields": [{"label": "Gender", "options": ["Male", "Female"]}],
               "note": "She is from Ghana and on f1_opt"}
    allowed = ["Gender", "Male", "Female"]
    assert sorted(redact.leaks(payload, vals, allowed)) == ["Ghana", "f1_opt"]
    clean = redact.for_model(payload, vals, allowed)
    assert clean["fields"] == payload["fields"]
    assert clean["note"] == "She is from [withheld] and on [withheld]"
    assert redact.leaks(clean, vals, allowed) == []
