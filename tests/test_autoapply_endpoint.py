"""The IN-APP autonomous-applier endpoint + its honest site gate.

/api/autoapply/plan is the brain the Electron pane calls: given a form's fields + the application
record, it returns concrete fill ops (real values joined LOCALLY) for the pane to fill in. It refuses
bot-prohibited sites (LinkedIn etc.) and never guesses. Uses the FakeLLM (deterministic), so no model.
"""
from __future__ import annotations

import importlib

import pytest

from submit.policy import fill_permitted


def test_fill_permitted_gate():
    assert fill_permitted("https://boards.greenhouse.io/acme/jobs/1") is True
    assert fill_permitted("https://jobs.lever.co/acme/1") is True
    assert fill_permitted("https://www.linkedin.com/jobs/view/123") is False   # bot-prohibited
    assert fill_permitted("https://www.indeed.com/viewjob?jk=1") is False
    assert fill_permitted("") is False                                         # unparseable -> no


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Gilbert", "email": "kofi@example.com",
                                              "phone": "555-100-2000"}, "experience": [{"org": "X"}]},
                     {}, [])
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={"jd_text": "We need Python."})
    recs.close()
    A.WORKDIR.mkdir(parents=True, exist_ok=True)
    (A.WORKDIR / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 test resume")   # the tailored CV to upload
    return A, A.app.test_client(), rid


_FIELDS = [
    {"ref": "f1", "label": "First name", "type": "text", "required": True},
    {"ref": "f2", "label": "Email", "type": "email", "required": True},
    {"ref": "f3", "label": "Phone", "type": "tel"},
    {"ref": "f4", "label": "Resume", "type": "file"},
]


def test_plan_returns_resolved_ops_with_real_values(app):
    _, c, rid = app
    d = c.post("/api/autoapply/plan", json={
        "url": "https://boards.greenhouse.io/acme/jobs/1", "record_id": rid, "fields": _FIELDS,
    }).get_json()
    assert d["ok"] is True and d["filled"] >= 3
    by = {o["ref"]: o for o in d["ops"]}
    # Real values are joined LOCALLY (fine -- they go to the in-app form, never to the model).
    assert by["f1"]["text"] == "Kofi" and by["f1"]["sensitive"] is True
    assert by["f2"]["text"] == "kofi@example.com"
    assert by["f4"]["op"] == "upload"                    # the résumé field becomes an upload op


def test_plan_refuses_bot_prohibited_sites(app):
    _, c, rid = app
    d = c.post("/api/autoapply/plan", json={
        "url": "https://www.linkedin.com/jobs/view/1", "record_id": rid, "fields": _FIELDS,
    }).get_json()
    assert d["ok"] is False and d["needs_assist"] is True


def test_plan_needs_assist_without_fields(app):
    _, c, rid = app
    d = c.post("/api/autoapply/plan", json={
        "url": "https://boards.greenhouse.io/acme/jobs/1", "record_id": rid, "fields": [],
    }).get_json()
    assert d["ok"] is False and d["needs_assist"] is True
