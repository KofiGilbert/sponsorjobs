"""Guided apply: the application package exposes the original posting URL so assisted
apply can open it for the person to click Apply on the site."""
from __future__ import annotations

import ui.app as app


def _add(tmp_path, monkeypatch, data):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    recs = app._records()
    try:
        return recs.add("Senior Engineer", "Acme", 72, "", data=data)
    finally:
        recs.close()


def test_package_exposes_apply_url(tmp_path, monkeypatch):
    rid = _add(tmp_path, monkeypatch,
               {"status": "ready", "source_job": {"url": "https://jobs.lever.co/acme/1"}})
    p = app.app.test_client().get(f"/api/record/{rid}").get_json()
    assert p["apply_url"] == "https://jobs.lever.co/acme/1"


def test_package_apply_url_empty_without_a_source(tmp_path, monkeypatch):
    rid = _add(tmp_path, monkeypatch, {"status": "ready"})
    p = app.app.test_client().get(f"/api/record/{rid}").get_json()
    assert p["apply_url"] == ""
