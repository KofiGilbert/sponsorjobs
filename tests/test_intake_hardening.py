"""Hardening of the intake/session core (CLAUDE.md §4). Pins the correctness bugs an
adversarial audit found:

  * a RETURNING session must not clobber the durable saved profile with an empty skeleton;
  * an autonomous build must not stall re-asking a personal/GitHub project for a location/
    dates it legitimately lacks (the gate exemption must survive the flatten);
  * the _wants_* intent detectors must not fire on ordinary intake prose;
  * ConversationMemory must tolerate concurrent access from the threaded server;
  * CV import / uploads are bounded (OCR page cap + request size limit).
"""
from __future__ import annotations

import threading

import ui.app as app
from inbox import detect  # noqa: F401  (keeps import graph warm; unrelated)
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from ui.records import CVRecords
from ui.session import (WebIntake, _flatten_profile, _missing_skeleton,
                        _wants_done, _wants_override, _wants_placeholder,
                        _wants_project_ideas)

JD = "Data Engineer - Acme - Remote. Python, SQL, pipelines."


def _rich_profile():
    return {
        "identity": {"name": "Sam Rivera", "email": "sam@x.com"},
        "experience": [{"org": "Acme", "location": "NY", "roles": [
            {"title": "Engineer", "dates": "2020 - 2022", "bullets": ["Built data pipelines."]}]}],
        "projects": [{"org": "MyTool", "title": "Creator", "personal": True,
                      "link": "https://github.com/sam/mytool", "location": "", "dates": "",
                      "bullets": ["Built an open-source tool."]}],
        "education": [{"school": "State U", "degree": "B.S. Computer Science", "date": "2019",
                       "courses": "Algorithms"}],
        "extracurricular": [{"title": "Mentor", "date": "2021", "bullets": ["Mentored students."]}],
        "interests": "Chess, cycling",
        "skills": {"Computing": "Python, SQL"},
    }


def _essentials(p):
    return {"identity": dict(p["identity"]),
            "experience": [{"org": "Acme", "title": "Engineer", "dates": "2020 - 2022",
                            "location": "NY", "bullets": ["Built data pipelines."]}],
            "education": list(p["education"])}


# ============================ data loss: returning session preserves the saved profile ============================
def test_returning_session_does_not_clobber_the_saved_profile(tmp_path, template_source, fake_llm):
    db = str(tmp_path / "m.db")
    mem = ConversationMemory(db)
    rich = _rich_profile()
    mem.save("default", rich, _essentials(rich), [])

    s = WebIntake(JD, template_source, fake_llm, mem, CVRecords(":memory:"),
                  tmp_path / "cv", jobname="cv", palace_dir=tmp_path / "palace")
    assert s.returning is True
    s.start()
    s.submit("My phone number is (312) 555-0100")     # an ordinary, non-building turn

    saved = mem.load("default")
    exp = saved["profile"].get("experience") or []
    assert exp and exp[0].get("roles"), "returning session wiped the saved experience"
    assert saved["profile"].get("projects"), "returning session wiped the saved projects"
    assert saved["profile"].get("extracurricular"), "returning session wiped extracurricular"
    mem.close()


# ============================ autonomous build: personal project isn't gated for location/dates ============================
def test_flatten_preserves_personal_project_flags_so_gate_exempts_them():
    profile = {"projects": [{"org": "MyTool", "personal": True,
                             "link": "https://github.com/sam/mytool", "location": "", "dates": ""}]}
    identity, edu, exp, projs = _flatten_profile(profile)
    assert projs and (projs[0].get("personal") or projs[0].get("link")), "flags stripped"
    missing = _missing_skeleton(identity, edu, exp, projs, critical_only=True)
    assert not any("location for" in m or "dates for" in m for m in missing), missing


def test_a_non_personal_project_still_requires_location_and_dates():
    profile = {"projects": [{"org": "Client Work", "location": "", "dates": ""}]}
    missing = _missing_skeleton(*_flatten_profile(profile), critical_only=True)
    assert any("location for" in m for m in missing) and any("dates for" in m for m in missing)


# ============================ intent detectors: no false positives on prose ============================
def test_intent_detectors_ignore_ordinary_intake_prose():
    assert not _wants_placeholder("the team was made up of five engineers")
    assert not _wants_override("I continue building data pipelines for analytics")
    assert not _wants_override("our users don't ask for extra hand-holding")
    assert not _wants_done("we had no more budget that quarter")
    assert not _wants_done("the migration was all done by Q3")
    assert not _wants_project_ideas("skills I could build on include Python and SQL")
    assert not _wants_project_ideas("which project should I list first?")


def test_intent_detectors_still_fire_on_genuine_intent():
    assert _wants_placeholder("just make it up")
    assert _wants_override("build it now")
    assert _wants_done("that's everything, i'm done")
    assert _wants_project_ideas("suggest a project I could build")


# ============================ ConversationMemory: concurrent access is safe ============================
def test_conversation_memory_survives_concurrent_access(tmp_path):
    mem = ConversationMemory(str(tmp_path / "c.db"))
    errors: list = []

    def worker(n):
        try:
            for i in range(150):
                mem.save(f"p{n}", {"i": i}, {"identity": {"name": f"n{n}"}}, [])
                mem.load(f"p{n}")
                mem.exists(f"p{n}")
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    mem.close()


# ============================ upload / OCR bounds ============================
def test_upload_size_limit_and_ocr_page_cap_are_set():
    from intake import cv_import
    assert app.app.config.get("MAX_CONTENT_LENGTH")          # oversized uploads rejected (413)
    assert 0 < cv_import._MAX_OCR_PAGES <= 25                 # OCR work is bounded
