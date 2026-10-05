"""Save / bookmark roles (2026-08-09).

Bookmarks are PERSONAL, so they live in the local db, never the shared kitchen. Saving a
kitchen-sourced role snapshots it locally (the client sends the job) so the Saved list still
renders after the role ages out of the live feed. These pin that contract + the /api/jobs/save
route and the saved=1 view.
"""

from __future__ import annotations

import importlib
import tempfile

import pytest

from sourcing.watchlist import Watchlist

_KJOB = {"source_id": "freehire::abc", "source": "freehire", "company": "Acme",
         "title": "Data Analyst", "location": "NYC", "remote": "", "url": "http://x",
         "posted_at": "2026-08-09", "salary": "$120k/yr"}


def test_saving_a_kitchen_job_snapshots_it_locally():
    w = Watchlist(tempfile.mktemp(suffix=".db"))
    w.set_saved("freehire::abc", True, _KJOB)              # not in DB before -> snapshot inserts it
    assert w.saved_ids() == {"freehire::abc"}
    saved = w.list_jobs(saved_only=True)
    assert [j["title"] for j in saved] == ["Data Analyst"]
    assert saved[0]["salary"] == "$120k/yr"               # snapshot carried the pay


def test_unsaving_keeps_the_row_but_clears_the_flag():
    w = Watchlist(tempfile.mktemp(suffix=".db"))
    w.set_saved("freehire::abc", True, _KJOB)
    w.set_saved("freehire::abc", False)
    assert w.saved_ids() == set()
    assert w.list_jobs(saved_only=True) == []
    assert w.get_job("freehire::abc") is not None          # row remains, just unflagged


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)     # exercise the LOCAL path
    import ui.app as A
    importlib.reload(A)
    return A


def test_save_route_then_saved_view_lists_it(app):
    c = app.app.test_client()
    r = c.post("/api/jobs/save", json={"source_id": "freehire::abc", "saved": True, "job": _KJOB})
    assert r.status_code == 200 and r.get_json()["saved"] is True
    view = c.get("/api/jobs?saved=1").get_json()
    assert view["saved_view"] is True
    assert [j["title"] for j in view["jobs"]] == ["Data Analyst"]
    assert all(j["saved"] for j in view["jobs"])

    c.post("/api/jobs/save", json={"source_id": "freehire::abc", "saved": False})
    assert c.get("/api/jobs?saved=1").get_json()["jobs"] == []


def test_save_route_needs_a_source_id(app):
    assert app.app.test_client().post("/api/jobs/save", json={"saved": True}).status_code == 400
