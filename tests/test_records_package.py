"""Application-package review screen backend (Phase 3 review queue / Phase 4 package).

Each finished application persists its CV + cover letter + screening answers + coverage
onto the record's data bag; these endpoints surface it as a reviewable, exportable
package. Offline (no model) — the cover-letter SAVE path takes text directly.
"""

from __future__ import annotations

import io
import types
import zipfile

from ui.records import CVRecords


def test_merge_data_persists_and_merges(tmp_path):
    db = str(tmp_path / "r.db")
    recs = CVRecords(db)
    rid = recs.add("Engineer", "Acme", 72, "", data={"status": "ready"})
    recs.merge_data(rid, {"cover_letter": "Dear team", "status": "applied"})
    recs.merge_data(rid, {"applied_at": "2026-07-13T10:00"})
    import json
    data = json.loads(recs.get(rid)["data"])
    assert data["status"] == "applied" and data["cover_letter"] == "Dear team"
    assert data["applied_at"] == "2026-07-13T10:00"
    recs.close()


def _app_with_record(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    # No sponsor lookups needed offline -> stub it out.
    monkeypatch.setattr(app, "_sponsors", lambda: types.SimpleNamespace(lookup=lambda c: None))
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Machine Learning Engineer", "Synechron", 68, "", jd_label="ML Engineer",
                   data={"jd_text": "ML role. Python, SQL.", "status": "ready",
                         "coverage": {"ratio": 68, "present": ["Python", "SQL"],
                                      "missing": ["AWS"], "missing_supported": []},
                         "cover_letter": "Dear Hiring Team, ...",
                         "screening": [{"question": "Why us?", "answer": "Because ML."}]})
    recs.close()
    (tmp_path / "cv_build" / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 fake")
    return app, rid


def test_get_record_helper_fetches_and_handles_missing(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Eng", "Acme", 50, "", data={"x": 1})
    recs.close()
    assert app._get_record(rid)["id"] == rid       # fetches (and closes its connection)
    assert app._get_record(999999) is None         # missing id -> None, no crash


def test_record_status_on_a_missing_id_is_a_clean_404(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    r = app.app.test_client().post("/api/record/999/status", json={"status": "applied"})
    assert r.status_code == 404 and "999" in r.get_json()["error"]


def test_records_ready_is_the_apply_todo_with_url_and_lane(tmp_path, monkeypatch):
    """The apply TO-DO lists only built-but-unsubmitted applications, with each one's
    destination URL and submission lane (auto vs assisted)."""
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    recs = CVRecords(app.DB_PATH)
    ready = recs.add("Backend", "Chan", 80, "", data={
        "status": "ready", "source_job": {"url": "https://chan.recruitee.com/o/backend"}})
    assisted = recs.add("Data", "Acme", 70, "", data={
        "status": "ready", "source_job": {"url": "https://boards.greenhouse.io/acme/jobs/1"}})
    recs.add("FE", "Beta", 60, "", data={   # already applied -> not on the to-do
        "status": "applied", "source_job": {"url": "https://x"}})
    recs.add("SRE", "Gamma", 55, "", data={"status": "skipped"})   # skipped -> not on the to-do
    recs.close()

    out = app.app.test_client().get("/api/records/ready").get_json()
    by_id = {x["id"]: x for x in out["ready"]}
    assert set(by_id) == {ready, assisted} and out["count"] == 2   # applied/skipped excluded
    assert by_id[ready]["lane"] == "auto"                          # recruitee.com is AUTO
    assert by_id[ready]["apply_url"] == "https://chan.recruitee.com/o/backend"
    assert by_id[assisted]["lane"] == "assisted"                   # greenhouse needs employer key


def test_records_list_and_detail(tmp_path, monkeypatch):
    app, rid = _app_with_record(tmp_path, monkeypatch)
    client = app.app.test_client()
    lst = client.get("/api/records").get_json()["records"]
    assert lst and lst[0]["id"] == rid
    assert lst[0]["status"] == "ready" and lst[0]["has_cover_letter"] is True
    assert lst[0]["screening_count"] == 1

    d = client.get(f"/api/record/{rid}").get_json()
    assert d["role"] == "Machine Learning Engineer" and d["company"] == "Synechron"
    assert d["coverage"]["present"] == ["Python", "SQL"]
    assert d["cover_letter"].startswith("Dear Hiring Team")
    assert d["screening"][0]["question"] == "Why us?"
    assert d["visa"] == [] and d["pdf"] == f"/api/cv.pdf?job=cv-{rid}"


def test_status_toggle(tmp_path, monkeypatch):
    app, rid = _app_with_record(tmp_path, monkeypatch)
    client = app.app.test_client()
    r = client.post(f"/api/record/{rid}/status", json={"status": "applied"}).get_json()
    assert r["status"] == "applied" and r["applied_at"]
    assert client.get(f"/api/record/{rid}").get_json()["status"] == "applied"
    r2 = client.post(f"/api/record/{rid}/status", json={"status": "ready"}).get_json()
    assert r2["status"] == "ready" and r2["applied_at"] is None


def test_save_edited_cover_letter(tmp_path, monkeypatch):
    app, rid = _app_with_record(tmp_path, monkeypatch)
    client = app.app.test_client()
    r = client.post(f"/api/record/{rid}/cover_letter", json={"text": "My edited letter."})
    assert r.get_json()["cover_letter"] == "My edited letter."
    # Persisted to the package and to a servable file.
    assert client.get(f"/api/record/{rid}").get_json()["cover_letter"] == "My edited letter."
    assert (app.WORKDIR / f"cover-{rid}.txt").read_text(encoding="utf-8") == "My edited letter."


def test_export_bundle_is_a_zip_with_the_pieces(tmp_path, monkeypatch):
    app, rid = _app_with_record(tmp_path, monkeypatch)
    r = app.app.test_client().get(f"/api/record/{rid}/export")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert any(n.endswith("_CV.pdf") for n in names)
    assert any("Cover_Letter" in n for n in names)
    summary = next(n for n in names if "Summary" in n)
    text = z.read(summary).decode("utf-8")
    assert "68%" in text and "Why us?" in text
