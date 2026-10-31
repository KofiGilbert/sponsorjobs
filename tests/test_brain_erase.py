"""Erase everything really erases (docs/applicant-brain.md, section 10 and Phase 6).

The person's own Erase must leave nothing behind: after it, NO file in the data folder holds any
value that was seeded into any store, found by a plain byte search over every file (the brain and
its WAL, the answer library, the prefs, the profile and fact rows in resume_agent.db, the chat
diary and the old MemPalace folder, the interview files, the skip list, the LinkedIn review, the
profile photo and the migration backups). SQLite can keep a deleted row in a free page of its
file, so Erase vacuums resume_agent.db and every store deletes with ``secure_delete`` on.

With "and my resumes and applications" ticked, the resume records and their files go too;
without it, they stay. Offline: FakeLLM and a throwaway data folder."""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from applicant import migrate as M
from autoapply.answers import AnswerLibrary
from intake.memory import ConversationMemory

# Every value is unique, so a hit can only be a survivor of the erase.
NAME = "Zephyrine Quillfeather"
EMAIL = "zq.erase@seed-example.test"
PHONE = "773-555-0147"
LEGACY_SALARY = "176543"
CARD_SALARY = "187654"
ANSWER = "Seedanswer Velvetmoth"
CONSTRAINT = "Never mention Vortexcorp"
DIARY = "Diaryline Marigoldsplash"
INTERVIEW = "Interviewnote Copperfinch"
LEARN = "Learnskip Tallowbrook"
LINKEDIN = "Linkedinheadline Ravenquartz"
PHOTO = b"Photobytes Saffronwhisk"
COMPANY = "Resumeco Ambergrove"
MEMORY_ONLY = [NAME, EMAIL, PHONE, LEGACY_SALARY, CARD_SALARY, ANSWER, CONSTRAINT, DIARY, INTERVIEW, LEARN, LINKEDIN]


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    # The legacy stores first, so the brain's migration copies them and makes its backup.
    lib = AnswerLibrary(data / "answers.db")
    lib.save("What is your desired salary?", LEGACY_SALARY, "you")
    lib.close()
    (data / "app_prefs.json").write_text(json.dumps({"desired_salary": LEGACY_SALARY}), encoding="utf-8")
    mem = ConversationMemory(str(data / "resume_agent.db"))
    ident = {"name": NAME, "email": EMAIL, "phone": PHONE}
    mem.save("default", {"identity": ident, "experience": [{"org": "Stanbic Bank", "roles": [{"title": "Analyst"}]}]},
             {"identity": ident}, [{"role": "user", "content": DIARY}])
    mem.save("aa_batch_scratch", {"identity": ident}, {"identity": ident}, [])     # a batch's scratch copy
    mem.upsert_fact("default", "constraint", "vortexcorp", CONSTRAINT, provenance="chat")
    mem.close()
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._SESSION.pop("s", None)
    assert A._brain() is not None
    c = A.app.test_client()
    assert c.post("/api/answers/basics", json={"fields": {"salary": CARD_SALARY, "gender": "female"}}).status_code == 200
    assert c.post("/api/answers", json={"question": "Describe your favourite tool", "answer": ANSWER}).status_code == 200
    c.post("/api/profile/identity", json={"name": NAME, "email": EMAIL, "phone": PHONE})
    A._palace().remember_turn("user", DIARY)
    store = A.PALACE_DIR / "store"
    store.mkdir(parents=True, exist_ok=True)
    (store / "chroma.sqlite3").write_text(DIARY, encoding="utf-8")
    (data / "interviews.json").write_text(json.dumps([{"note": INTERVIEW}]), encoding="utf-8")
    (data / "aa_learn.json").write_text(json.dumps({"skip": [LEARN]}), encoding="utf-8")
    (data / "linkedin_review.json").write_text(json.dumps({"headline": LINKEDIN}), encoding="utf-8")
    (data / "profile_photo.png").write_bytes(PHOTO)
    rid = A._records().add("Analyst", COMPANY, 0.8, str(A.WORKDIR / "cv-1.pdf"), data={"company": COMPANY})
    A.WORKDIR.mkdir(parents=True, exist_ok=True)
    (A.WORKDIR / f"cv-{rid}.tex").write_text(f"{NAME} {COMPANY}", encoding="utf-8")
    A.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (A.UPLOADS_DIR / "resume.txt").write_text(f"{NAME} {EMAIL}", encoding="utf-8")
    from applicant import facts as F
    F.sync_chat(A._brain(), A._memory().list_facts("default", status=None))
    # every seed is somewhere on disk before the erase (the test would prove nothing otherwise)
    before = _hits(data, MEMORY_ONLY + [COMPANY])
    assert set(before) == set(MEMORY_ONLY + [COMPANY]), sorted(set(MEMORY_ONLY + [COMPANY]) - set(before))
    assert list(data.glob("brain-backup-*")), "the migration backup exists"
    yield A, c, data
    A._SESSION.pop("s", None)
    M.forget_process_state(data)


def _hits(data: Path, values) -> dict:
    """{value: [files]} for every seeded value still found in any file under ``data``."""
    out: dict = {}
    needles = [(v, v.encode("utf-8") if isinstance(v, str) else v) for v in values]
    for root, _dirs, files in os.walk(data):
        for name in files:
            p = Path(root) / name
            try:
                blob = p.read_bytes()
            except OSError:
                continue
            for v, b in needles:
                if b in blob:
                    out.setdefault(v, []).append(str(p.relative_to(data)))
    return out


def test_erase_everything_leaves_no_seeded_value_in_any_file(seeded):
    A, c, data = seeded
    r = c.post("/api/memory/erase", json={"resumes": True})
    assert r.status_code == 200 and r.get_json()["resumes"] is True
    left = _hits(data, MEMORY_ONLY + [COMPANY, PHOTO])
    assert not left, left
    assert not list(data.glob("brain-backup-*"))
    # the app still works afterwards: an empty memory, not a broken one
    d = c.get("/api/memory/page").get_json()
    assert d["about"] == [] and d["answers"] == [] and not d["has_profile"]


def test_erase_keeps_the_resumes_unless_asked(seeded):
    A, c, data = seeded
    assert c.post("/api/memory/erase").status_code == 200        # Settings' older call, no body
    left = _hits(data, MEMORY_ONLY + [PHOTO])
    # the resume record and its files still carry the name; nothing else does
    assert not {k: v for k, v in left.items() if any(not f.startswith(("cv_build", "uploads")) and f != "resume_agent.db"
                                                     for f in v)}, left
    recs = A._records()
    try:
        assert [r["company"] for r in recs.list()] == [COMPANY]
    finally:
        recs.close()
    con = sqlite3.connect(data / "resume_agent.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM conversation_memory").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0] == 0
    finally:
        con.close()
    blob = (data / "resume_agent.db").read_bytes()
    for v in (CONSTRAINT, DIARY, CARD_SALARY, ANSWER, LEGACY_SALARY):
        assert v.encode() not in blob, v                         # vacuumed: no free page keeps it


def test_forget_overwrites_the_value_on_disk(seeded):
    A, c, data = seeded
    c.post("/api/memory/forget", json={"id": "key:@salary"})
    time.sleep(0.05)
    for f in ("applicant.db", "applicant.db-wal", "answers.db"):
        p = data / f
        if p.exists():
            assert CARD_SALARY.encode() not in p.read_bytes(), f


# The Tracker: the mail the inbox tied to an application, the inbox's words in the stage history,
# the suggestion file, and (with the tick) the applications themselves.
MAIL_SUBJECT = "Mailsubject Glimmerfenwick"
MAIL_THREAD = "thread-Quorlapine"
JOB_NOTE = "Cardnote Brindlemoss"
CONTACT = "Contactname Pewterhollow"
TRACKER_JOB = {"source_id": "careers::erase-co", "source": "careers", "company": "Erasetest Larkspur",
               "title": "Data Analyst", "location": "Boston", "remote": "", "posted_at": "2026-10-20",
               "url": "https://careers.erasetest.example/jobs/1", "salary": "", "jd_text": ""}


def _seed_tracker(A, data):
    sid = TRACKER_JOB["source_id"]
    w = A._watchlist()
    try:
        w.upsert_jobs([TRACKER_JOB])
        w.track_job(sid, "applied")
        w.record_mail(sid, "msg-erase-1", thread_id=MAIL_THREAD, rfc_id="<rfc-erase@x>",
                      matched_by="thread", label="interview", subject=MAIL_SUBJECT)
        w.move_stage(sid, "interviewing", note=f'From your inbox: "{MAIL_SUBJECT}"', ref="sg-erase")
        w.set_notes(sid, JOB_NOTE)
        w.add_contact(sid, CONTACT, email="pewter@erasetest.example")
    finally:
        w.close()
    (data / "inbox_suggestions.json").write_text(json.dumps(
        [{"id": "sg-erase", "subject": MAIL_SUBJECT, "status": "pending"}]), encoding="utf-8")
    return sid


def test_export_carries_the_tracker_mail_and_history(seeded):
    A, c, data = seeded
    sid = _seed_tracker(A, data)
    d = c.get("/api/memory/export").get_json()
    t = d["tracker"]
    assert [m["subject"] for m in t["mail"]] == [MAIL_SUBJECT] and t["mail"][0]["thread_id"] == MAIL_THREAD
    assert [h["to_stage"] for h in t["history"] if h["source_id"] == sid][-1] == "interviewing"
    assert [x["name"] for x in t["contacts"]] == [CONTACT]
    assert d["inbox_suggestions"][0]["subject"] == MAIL_SUBJECT
    import io
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/memory/export.zip").data))
    assert MAIL_SUBJECT in z.read("memory.json").decode() and "stores/inbox_suggestions.json" in z.namelist()


def test_erase_leaves_no_inbox_mail_but_keeps_the_cards(seeded):
    A, c, data = seeded
    sid = _seed_tracker(A, data)
    assert set(_hits(data, [MAIL_SUBJECT, MAIL_THREAD])) == {MAIL_SUBJECT, MAIL_THREAD}
    assert c.post("/api/memory/erase").status_code == 200
    assert not _hits(data, [MAIL_SUBJECT, MAIL_THREAD])          # the mail is gone from every file
    w = A._watchlist()
    try:
        assert w.stage_of(sid) == "interviewing"                   # the application itself stays
        assert w.tracker_export()["mail"] == []
    finally:
        w.close()


def test_erase_with_applications_clears_the_tracker(seeded):
    A, c, data = seeded
    sid = _seed_tracker(A, data)
    assert c.post("/api/memory/erase", json={"resumes": True}).status_code == 200
    assert not _hits(data, [MAIL_SUBJECT, MAIL_THREAD, JOB_NOTE, CONTACT])
    w = A._watchlist()
    try:
        assert not w.stage_of(sid)
        t = w.tracker_export()
        assert t["mail"] == [] and t["history"] == [] and t["contacts"] == [] and t["cards"] == []
    finally:
        w.close()
