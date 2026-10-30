"""The migration into the applicant brain (docs/applicant-brain.md, section 11; Phase 1).

A dry run writes nothing and reports counts only; apply then rerun gives the same counts and no
duplicates; every legacy file is byte-identical afterwards apart from mirrored writes; the Lam
Research raw replies become needs_check and are not deleted; a verify mismatch rolls back and
the app stays on the legacy stores (three tries); --rollback and SPONSORJOBS_BRAIN=0 put the
readers back on the legacy stores with every write made after migration present; Export and
Erase cover the brain, answers.db, app_prefs.json, the interview files and the backups."""
from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

from applicant import migrate as M
from applicant.library import BrainLibrary
from applicant.store import open_store
from autoapply.answers import AnswerLibrary
from brain_fixtures import LAM_REPLY, OLD_KEY, SECRET_VALUES, file_bytes, write_legacy

LEGACY = ("answers.db", "app_prefs.json", "resume_agent.db", "palace/transcripts/default.jsonl",
          "palace/transcripts/autoapply_123.jsonl")
EXPECTED = {
    "answers.db": {"active": 9, "needs_check": 2},
    "app_prefs.json": {"active": 7},
    "conversation_memory": {"contact": 5, "role": 2, "school": 1, "project": 1, "skill": 3},
    "memory_facts": {"active": 2, "needs_check": 1, "refused_eeo": 1, "skipped_retracted": 1},
    "transcripts": {"indexed": 1, "scratch": 1},
}


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = write_legacy(tmp_path / "data")
    yield d
    M.forget_process_state(d)


def _fact_count(d):
    con = sqlite3.connect(d / "applicant.db")
    try:
        return con.execute("SELECT COUNT(*) FROM fact").fetchone()[0], con.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    finally:
        con.close()


def test_a_dry_run_writes_nothing_and_reports_counts_only(data):
    before = file_bytes(data)
    out = M.dry_run(data)
    assert out["counts"] == EXPECTED
    assert file_bytes(data) == before and not (data / "applicant.db").exists()
    text = json.dumps(out)
    assert not any(v in text for v in SECRET_VALUES)


def test_the_command_line_dry_run_prints_counts_only(data, capsys):
    assert M.main(["--data", str(data), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["counts"] == EXPECTED and not any(v in out for v in SECRET_VALUES)
    assert not (data / "applicant.db").exists()


def test_apply_then_rerun_gives_the_same_counts_and_no_duplicates(data):
    first = M.apply(data)
    assert first["ok"] and first["active"] and first["mismatches"] == 0 and first["counts"] == EXPECTED
    n_facts, _ = _fact_count(data)
    st = open_store(data / "applicant.db")
    assert st.meta("brain_active") == "1" and st.meta("mirror_legacy") == "1"
    second = M.apply(data)
    assert second["counts"] == first["counts"] and second["ok"]
    assert _fact_count(data)[0] == n_facts
    # one backup, made the first time only
    backups = sorted(p.name for p in data.glob("brain-backup-*"))
    assert len(backups) == 1
    assert sorted(f.name for f in (data / backups[0]).iterdir()) == ["answers.db", "app_prefs.json", "resume_agent.db"]


def test_every_legacy_file_is_byte_identical_after_the_migration(data):
    before = {k: v for k, v in file_bytes(data).items() if k in LEGACY}
    assert M.apply(data)["ok"]
    after = file_bytes(data)
    assert {k: after[k] for k in LEGACY} == before


def test_the_lam_raw_replies_need_a_look_and_are_not_deleted(data):
    assert M.apply(data)["ok"]
    st = open_store(data / "applicant.db")
    rows = {r["key"]: r for r in st.facts(store="answers.db", status=("active", "needs_check"))}
    assert rows["@gender"]["status"] == "needs_check" and rows["@gender"]["check_reason"] == "raw_reply"
    assert rows["@gender"]["value"] == LAM_REPLY and rows["@gender"]["sensitivity"] == "eeo"
    assert rows["@ethnicity"]["status"] == "needs_check"
    assert rows["q:" + OLD_KEY]["status"] == "active"                 # the newer salary: usable
    with AnswerLibrary(data / "answers.db") as legacy:
        assert legacy.get_key("@gender")["answer"] == LAM_REPLY       # still in the legacy store
    lib = BrainLibrary(st)
    assert lib.lookup("Gender") is None and lib.get_key("@gender")["answer"] == LAM_REPLY


def test_the_resume_profile_and_chat_facts_become_open_facts(data):
    assert M.apply(data)["ok"]
    st = open_store(data / "applicant.db")
    subjects = {(r["key"], r["subject"]) for r in st.facts(store="", status=("active", "needs_check"))}
    assert ("role", "role:stanbic bank|analyst") in subjects and ("role", "role:viva|data analyst") in subjects
    assert ("school", "school:university of ghana") in subjects and ("skill", "skill:tableau") in subjects
    assert ("constraint", "viva") in subjects and ("preference", "tone") in subjects
    sal = [r for r in st.facts(store="") if r["key"] == "@salary"]
    assert len(sal) == 1 and sal[0]["status"] == "needs_check" and sal[0]["source"] == "chat"
    assert not [r for r in st.facts() if r["key"] == "@gender" and r["source"] == "chat"]   # refused
    assert st.facts(key="email")[0]["confidence"] == 0.9
    assert st.q("SELECT COUNT(*) FROM event WHERE kind='diary_indexed'")[0][0] == 2
    assert st.q("SELECT COUNT(*) FROM label_map WHERE key LIKE 'q:%'")[0][0] == 2


def test_a_verify_mismatch_rolls_back_and_the_app_stays_on_the_legacy_stores(data, monkeypatch):
    monkeypatch.setattr(M, "verify", lambda st, snap: ["best:@salary"])
    out = M.apply(data)
    assert not out["ok"] and out["mismatches"] == 1 and out["tries"] == 1
    st = open_store(data / "applicant.db")
    assert st.meta("brain_active") != "1" and st.facts() == []          # nothing written
    assert st.q("SELECT COUNT(*) FROM event WHERE kind='migration_mismatch'")[0][0] == 1
    assert M.apply(data)["tries"] == 2 and M.apply(data)["tries"] == 3
    M.forget_process_state(data)
    assert M.ensure(data) is None                                       # three tries, then no more
    monkeypatch.undo()


@pytest.fixture()
def app(data, monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_the_app_reads_the_brain_and_rollback_keeps_every_later_write(app, monkeypatch):
    A, c = app
    lib = A._answer_lib()
    try:
        assert isinstance(lib, BrainLibrary)
        assert lib.lookup("What is your annual salary requirement?")["answer"] == "130000"
        lib.save("What is your notice period?", "Four weeks")
    finally:
        lib.close()
    A._merge_prefs({"needs_sponsorship": "Yes"})
    assert A._work_auth()["countries"]["US"]["sponsor_now"] == "Yes"
    assert M.main(["--data", str(A._DATA), "--rollback"]) == 0
    lib = A._answer_lib()
    try:
        assert isinstance(lib, AnswerLibrary)
        assert lib.lookup("Notice period")["answer"] == "Four weeks"
    finally:
        lib.close()
    assert A._work_auth()["countries"]["US"]["sponsor_now"] == "Yes"
    assert json.loads((A._DATA / "app_prefs.json").read_text())["needs_sponsorship"] == "Yes"
    # written while rolled back, then the brain is turned on again: it catches up
    lib = A._answer_lib()
    try:
        lib.save("How did you hear about us?", "A friend")
    finally:
        lib.close()
    assert M.main(["--data", str(A._DATA), "--apply"]) == 0
    lib = A._answer_lib()
    try:
        assert isinstance(lib, BrainLibrary) and lib.lookup("How did you hear about this job?")["answer"] == "A friend"
    finally:
        lib.close()


def test_the_environment_switch_reads_the_legacy_stores_and_the_brain_catches_up(app, monkeypatch):
    A, c = app
    lib = A._answer_lib()
    assert isinstance(lib, BrainLibrary)
    lib.close()
    monkeypatch.setenv(M.ENV, "0")
    lib = A._answer_lib()
    try:
        assert isinstance(lib, AnswerLibrary)
        lib.save("What is your notice period?", "Two weeks")
        lib.delete("experience have sql")
    finally:
        lib.close()
    A._merge_prefs({"desired_salary": "99000"})
    monkeypatch.delenv(M.ENV)
    lib = A._answer_lib()
    try:
        assert isinstance(lib, BrainLibrary)
        assert lib.lookup("Notice period")["answer"] == "Two weeks"
        assert lib.get_key("experience have sql") is None
    finally:
        lib.close()
    st = open_store(A._DATA / "applicant.db")
    assert st.facts(key="q:experience have sql") == []                 # deleted there: forgotten here
    assert A._brain_prefs()["desired_salary"] == "99000"


def test_the_brain_routes_report_counts_only(app):
    A, c = app
    s = c.get("/api/brain/status").get_json()
    assert s["active"] and s["report"] == EXPECTED and s["facts"]["active"] > 0
    d = c.get("/api/brain/migrate?dry_run=1").get_json()
    assert d["dry_run"] and d["counts"] == EXPECTED
    for body in (s, d):
        assert not any(v in json.dumps(body) for v in SECRET_VALUES)


def test_export_and_erase_cover_the_brain_the_answers_the_prefs_and_the_interview_files(app):
    A, c = app
    c.get("/api/brain/status")                                         # migrated (and backed up)
    for name in ("interviews.json", "preps.json", "screens.json", "interview_goals.json", "aa_learn.json"):
        (A._DATA / name).write_text(json.dumps({"seen": name}), encoding="utf-8")
    ex = c.get("/api/memory/export").get_json()
    assert ex["brain"]["fact"] and {r["key"] for r in ex["answers"]} >= {"@salary", OLD_KEY}
    assert ex["prefs"]["desired_salary"] == "95000" and set(ex["interviews"]) == {
        "interviews.json", "preps.json", "screens.json", "interview_goals.json"}
    assert ex["aa_learn"] == {"seen": "aa_learn.json"} and ex["backups"] and ex["backups"][0]["answers"]
    r = c.post("/api/memory/erase").get_json()
    assert r["ok"] and r["erased"] >= 9
    left = {p.name for p in A._DATA.rglob("*") if p.is_file()}
    for gone in ("answers.db", "app_prefs.json", "interviews.json", "preps.json", "screens.json",
                 "interview_goals.json", "aa_learn.json"):
        assert gone not in left, gone
    fresh = open_store(A._DATA / "applicant.db")                         # a new, empty brain
    assert fresh.facts() == [] and fresh.meta("backup_dir") == "erased"
    assert not list(A._DATA.glob("brain-backup-*"))
    # no seeded answer survives anywhere in the folder. (A deleted resume_agent.db row can still
    # sit in SQLite's free pages: the byte search over chat facts and the profile is Phase 6's
    # tests/test_brain_erase.py, with secure delete.)
    blob = b"".join(p.read_bytes() for p in A._DATA.rglob("*") if p.is_file())
    for v in ("130000", "$120,000", "f1_opt", "95000"):
        assert v.encode() not in blob, v
    # and the app carries on with an empty brain
    lib = A._answer_lib()
    try:
        assert lib.count() == 0 and lib.lookup("Salary") is None
    finally:
        lib.close()
    assert A._work_auth() == {"countries": {}, "relocate": ""}
    assert not list(A._DATA.glob("brain-backup-*"))                    # nothing backed up after an erase


def test_concurrent_writes_and_reads_lose_no_answer(app):
    """A save and its mirror run under the store's lock, so a catch-up in another request never
    copies an older answers.db row back over a new answer."""
    import threading
    A, c = app
    errors = []

    def writer(i):
        try:
            for j in range(5):
                lib = A._answer_lib()
                try:
                    lib.save(f"Which tool number {i * 5 + j} do you use?", f"answer {i}.{j}")
                finally:
                    lib.close()
        except Exception as exc:          # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(10):
                lib = A._answer_lib()
                lib.lookup("Desired salary")
                lib.close()
                A._work_auth()
        except Exception as exc:          # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    lib = A._answer_lib()
    try:
        for i in range(4):
            for j in range(5):
                assert lib.lookup(f"Which tool number {i * 5 + j} do you use?")["answer"] == f"answer {i}.{j}"
    finally:
        lib.close()
    with AnswerLibrary(A._ANSWERS_DB) as legacy:
        assert sum(1 for r in legacy.list() if r["answer"].startswith("answer ")) == 20
