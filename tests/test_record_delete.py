"""Deleting an application removes its record AND every artifact file it produced
(compiled CV, cover-letter PDF + text) — completing the review-queue CRUD."""

from __future__ import annotations

import types

from ui.records import CVRecords


def test_records_delete_removes_row(tmp_path):
    recs = CVRecords(str(tmp_path / "r.db"))
    rid = recs.add("Engineer", "Acme", 70, "")
    assert recs.get(rid) is not None
    assert recs.delete(rid) is True
    assert recs.get(rid) is None
    assert recs.delete(rid) is False          # already gone
    recs.close()


def _app(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    monkeypatch.setattr(app, "_sponsors", lambda: types.SimpleNamespace(lookup=lambda c: None))
    return app


def test_delete_endpoint_removes_record_and_artifacts(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("ML Engineer", "Synechron", 70, "", data={"cover_letter": "Dear team"})
    recs.close()
    # Artifacts the app would have produced for this record.
    for name in (f"cv-{rid}.pdf", f"cover-{rid}.pdf", f"cover-{rid}.txt"):
        (app.WORKDIR / name).write_bytes(b"x")

    client = app.app.test_client()
    r = client.post(f"/api/record/{rid}/delete", json={})
    assert r.status_code == 200 and r.get_json()["deleted"] == rid

    assert CVRecords(app.DB_PATH).get(rid) is None
    for name in (f"cv-{rid}.pdf", f"cover-{rid}.pdf", f"cover-{rid}.txt"):
        assert not (app.WORKDIR / name).exists()
    # It's gone from the queue too.
    assert client.get("/api/records").get_json()["records"] == []


def test_delete_missing_record_is_404(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    assert app.app.test_client().post("/api/record/999/delete", json={}).status_code == 404
