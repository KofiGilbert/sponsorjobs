"""POST /api/autoapply/fill-report: the desktop shell's per-page record of what the fill did
({filled, left:[labels]}), kept on the application record for the Auto-apply drawer."""
from __future__ import annotations

import importlib

import pytest

H = {"X-Tailor-Extension": "1"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={"jd_text": "Python."})
    recs.close()
    return A, A.app.test_client(), rid


def _data(A, rid):
    recs = A._records()
    try:
        return A._record_data(recs.get(rid))
    finally:
        recs.close()


def test_pages_are_kept_and_a_refill_replaces_its_page(app):
    A, c, rid = app
    r = c.post("/api/autoapply/fill-report", headers=H, json={
        "record_id": rid, "page": 1, "url": "https://x.wd5.myworkdayjobs.com/a", "filled": 7,
        "left": ["Are you willing to relocate?", "  Are you willing   to relocate? ", ""], "system": "workday"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 2, "filled": 9, "left": []})
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 8, "left": []})
    rep = _data(A, rid)["fill_report"]
    assert [(p["page"], p["filled"], p["left"]) for p in rep["pages"]] == [(1, 8, []), (2, 9, [])]
    assert rep["updated"]


def test_left_labels_are_deduped_and_trimmed(app):
    A, c, rid = app
    c.post("/api/autoapply/fill-report", headers=H, json={
        "record_id": rid, "page": 1, "filled": 1, "left": ["Gender", " Gender ", "x" * 500]})
    left = _data(A, rid)["fill_report"]["pages"][0]["left"]
    assert left == ["Gender", "x" * 200]


def test_guards(app):
    _, c, rid = app
    assert c.post("/api/autoapply/fill-report", json={"record_id": rid}).status_code == 403   # web pages can't
    assert c.post("/api/autoapply/fill-report", headers=H, json={}).status_code == 400
    assert c.post("/api/autoapply/fill-report", headers=H, json={"record_id": 99999}).status_code == 404
