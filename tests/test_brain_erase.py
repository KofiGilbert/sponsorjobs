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
