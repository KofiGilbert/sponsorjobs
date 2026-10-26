"""Auto-apply pre-flight: a saved profile that still lacks a HARD detail (a role's dates or
location, a contact) cannot be built unattended, so the run and the preview say so BEFORE any
role is ranked, counted or tailored, with the engine's own questions and an action the page
turns into "Add the details". Measured on a real install (2026-10-26): two undated projects made
every role come back "needs a detail we don't have" and the page blamed the fit floor. Since
the owner's HARD/SOFT split the same day, a project's dates are SOFT and never stop a run.
Offline, no model."""
from __future__ import annotations

from pathlib import Path

import pytest

import ui.app as app

ROOT = Path(__file__).resolve().parents[1]

COMPLETE = {"identity": {"name": "Sam Rivera", "email": "sam@example.com"},
            "experience": [{"org": "Acme", "location": "Austin, TX",
                            "roles": [{"title": "Backend Engineer", "dates": "2021 - 2024",
                                       "bullets": ["Built Python APIs on AWS"]}]}],
            "education": [{"school": "UT Austin", "degree": "BS Computer Science",
                           "date": "May 2021"}],
            "projects": [{"org": "Ledger", "location": "Python", "dates": "2023",
                          "bullets": ["Double-entry bookkeeping CLI"]}]}

# Two HARD gaps: the role's dates and location.
GAPPY = {**COMPLETE,
         "experience": [{"org": "Acme", "roles": [{"title": "Backend Engineer",
                                                   "bullets": ["Built Python APIs on AWS"]}]}]}
HARD_QS = ["the employment dates for your role at Acme",
           "the location (city, state) for your role at Acme"]

UNDATED = {**COMPLETE,
           "projects": [{"org": "Ledger", "location": "Python", "bullets": ["Bookkeeping CLI"]},
                        {"org": "Mapper", "location": "Go", "bullets": ["Tile renderer"]}]}


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_LEARN_FILE", tmp_path / "aa_learn.json")
    monkeypatch.setattr(app, "_PREFS_PATH", tmp_path / "answers.json")

    def never(*a, **k):
        raise AssertionError("the run must stop before ranking, counting or tailoring")
    # Nothing past the pre-flight may run: no board, no model, no package counted, no tailoring.
    for name in ("_board_rows", "_make_llm", "_count_package", "_tailor_job_unattended",
                 "_load_template"):
        monkeypatch.setattr(app, name, never)
    return app.app.test_client()


def test_preflight_lists_the_hard_questions(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    app._memory().save("default", GAPPY, {}, [])
    assert app._aa_missing_details() == HARD_QS


def test_undated_projects_no_longer_block_a_run(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    app._memory().save("default", UNDATED, {}, [])
    assert app._aa_missing_details() == []               # SOFT: they build as undated
    monkeypatch.setattr(app, "_board_rows", lambda: [])  # past the pre-flight: the board check
    d = client.post("/api/autoapply/run", json={"count": 1}).get_json()
    assert d.get("action") != "complete_profile" and "missing" not in d


def test_preflight_is_empty_for_a_complete_profile(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    app._memory().save("default", COMPLETE, {}, [])
    assert app._aa_missing_details() == []


def test_preflight_respects_what_the_person_declined(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    one = {**GAPPY, "identity": {"name": "Sam Rivera"}}
    app._memory().save("default", one, {"declined": ["projects"]}, [])
    assert app._aa_missing_details() == ["an email or phone number"] + HARD_QS


def test_preflight_is_empty_without_a_profile(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert app._aa_missing_details() == []       # "add your resume" is a different message


def test_run_stops_with_the_missing_details_before_counting_a_package(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    app._memory().save("default", GAPPY, {}, [])
    r = client.post("/api/autoapply/run", json={"count": 3, "min_fit": 0})
    assert r.status_code == 400
    d = r.get_json()
    assert d["error"] == "Auto-apply needs 2 details first."
    assert d["action"] == "complete_profile"
    assert d["missing"] == HARD_QS
    assert "queued" not in d                     # never "Queued 0" with the cause buried


def test_one_missing_detail_is_singular(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    one = {**COMPLETE, "identity": {"name": "Sam Rivera"}}
    app._memory().save("default", one, {}, [])
    d = client.post("/api/autoapply/run", json={"count": 1}).get_json()
    assert d["error"] == "Auto-apply needs 1 detail first." and len(d["missing"]) == 1


def test_preview_carries_the_missing_details(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    app._memory().save("default", GAPPY, {}, [])
    r = client.get("/api/autoapply/preview")
    assert r.status_code == 400
    d = r.get_json()
    assert d["action"] == "complete_profile" and len(d["missing"]) == 2


def test_run_proceeds_once_the_details_exist(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    app._memory().save("default", COMPLETE, {}, [])
    monkeypatch.setattr(app, "_board_rows", lambda: [])    # past the pre-flight: the board check
    d = client.post("/api/autoapply/run", json={"count": 1}).get_json()
    assert d.get("action") != "complete_profile" and "missing" not in d


def test_telegram_tailor_button_says_what_is_missing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    app._memory().save("default", GAPPY, {}, [])
    out = app._JobAlertActions()._tailor({"sid": "gh:1", "title": "Analyst", "company": "Acme"})
    assert out["ok"] is False
    assert out["reason"] == ("your profile is missing 2 details (the employment dates for your role "
                             "at Acme, the location (city, state) for your role at Acme). "
                             "Add them in SponsorJobs")


@pytest.mark.parametrize("needle", [
    'complete_profile: [() => T("aa.addDetails", "Add the details")',   # the action button
    'T("aa.missing", "Auto-apply needs {n} details first.")',           # the one-line head
    'T("aa.nothingQueued", "Nothing queued.")',                          # zero told as zero
    'T("aa.notTailored", "Not tailored: {s}.")',                         # tailoring skips shown
])
def test_the_page_shows_the_cause(needle):
    src = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert needle in src


def test_the_page_never_says_to_review_for_zero():
    src = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'r.queued ? T("aa.queued", "Queued {n}. They are in To review.")' in src
