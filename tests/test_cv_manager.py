"""Saved CVs manager backend: rename, refile, reorder, folders registry."""

from __future__ import annotations

import json

import ui.app as app
from ui.records import CVRecords


def _recs(monkeypatch):
    recs = CVRecords(":memory:")
    monkeypatch.setattr(app, "_records", lambda: recs)
    monkeypatch.setattr(recs, "close", lambda: None)   # shared across requests in tests
    return recs


def test_meta_renames_refiles_and_reorders(monkeypatch):
    recs = _recs(monkeypatch)
    rid = recs.add(role="Area Manager 2026 · Experienced hire", company="Amazon",
                   coverage=22, pdf_path="", jd_label="Area Manager 2026",
                   data={"status": "ready"})
    client = app.app.test_client()

    r = client.post(f"/api/record/{rid}/meta",
                    json={"name": "Amazon AM (final)", "folder": "Amazon", "sort": 2.5})
    assert r.status_code == 200
    body = r.get_json()
    assert body["role"] == "Amazon AM (final)"
    assert body["folder"] == "Amazon" and body["sort"] == 2.5

    row = recs.get(rid)
    assert row["role"] == "Amazon AM (final)"
    data = json.loads(row["data"])
    assert data["folder"] == "Amazon" and data["sort"] == 2.5
    assert data["status"] == "ready"          # merge, never clobber the bag

    # The list view carries folder/sort for grouping.
    monkeypatch.setattr(app, "_record_data", lambda r: json.loads(r.get("data") or "{}"))
    lst = client.get("/api/records").get_json()["records"]
    assert lst[0]["folder"] == "Amazon" and lst[0]["sort"] == 2.5

    # Guards: empty name, unknown record.
    assert client.post(f"/api/record/{rid}/meta", json={"name": "  "}).status_code == 400
    assert client.post("/api/record/999/meta", json={"name": "x"}).status_code == 404


def test_folders_registry_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    client = app.app.test_client()
    assert client.get("/api/folders").get_json()["folders"] == []
    assert client.post("/api/folders", json={"add": "Amazon"}).get_json()["folders"] == ["Amazon"]
    # duplicate (case-insensitive) is ignored
    assert client.post("/api/folders", json={"add": "amazon"}).get_json()["folders"] == ["Amazon"]
    out = client.post("/api/folders", json={"add": "Tech"}).get_json()["folders"]
    assert out == ["Amazon", "Tech"]
    assert client.post("/api/folders", json={"remove": "Amazon"}).get_json()["folders"] == ["Tech"]


def test_folder_rename_moves_registry_and_records(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    recs = _recs(monkeypatch)
    rid = recs.add(role="AM", company="Amazon", coverage=20, pdf_path="",
                   jd_label="", data={"folder": "Amazon", "status": "ready"})
    client = app.app.test_client()
    client.post("/api/folders", json={"add": "Amazon"})

    out = client.post("/api/folders",
                      json={"rename_from": "Amazon", "rename_to": "Ops"}).get_json()
    assert out["folders"] == ["Ops"]                       # registry renamed
    assert json.loads(recs.get(rid)["data"])["folder"] == "Ops"   # record refiled too
