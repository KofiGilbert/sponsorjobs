"""My Profile screen — view the saved profile and edit contact identity (CLAUDE.md §4b).

The saved identity powers autofill, every CV letterhead, and cover letters, so an edit
must land in BOTH the essentials.identity (autofill reads it) and profile.identity
(letterhead/drafting read it) while preserving history and the CV sections.
"""

from __future__ import annotations

import ui.app as app
from intake.memory import ConversationMemory


def _seed(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    app._SESSION.pop("s", None)   # no active builder session
    ident = {"name": "Old Name", "email": "old@example.com"}
    m = ConversationMemory(db)
    m.save("default",
           {"identity": dict(ident), "skills": {"Computing": "Python, SQL"},
            "experience": [{"org": "Acme", "location": "Chicago", "dates": "2021",
                            "roles": [{"title": "Engineer", "dates": "2021",
                                       "bullets": ["Built things."]}]}],
            "education": [{"school": "Northwestern", "degree": "M.S.", "date": "2018",
                           "location": "", "courses": ""}],
            "interests": "hiking, chess"},
           {"identity": dict(ident), "education": [], "experience": []},
           [{"role": "user", "content": "hi"}])
    m.close()
    return db


def test_profile_view_returns_identity_and_sections(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/profile").get_json()
    assert d["has_profile"] is True
    assert d["identity"]["name"] == "Old Name" and d["identity"]["email"] == "old@example.com"
    assert d["identity"]["blog"] == ""                       # missing keys default to ""
    assert d["skills"] == {"Computing": "Python, SQL"}
    assert d["experience"][0]["org"] == "Acme"
    assert d["education"][0]["school"] == "Northwestern"
    assert d["interests"] == "hiking, chess"


def test_identity_save_updates_both_blobs_and_autofill(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    client = app.app.test_client()
    r = client.post("/api/profile/identity", json={
        "name": "New Name", "email": "new@example.com", "phone": "(312) 555-0001",
        "address": "1 Main St, Chicago, IL 60601",
        "linkedin": "https://linkedin.com/in/new", "github": "https://github.com/new",
        "blog": ""})
    assert r.get_json()["ok"] is True

    saved = ConversationMemory(db).load("default")
    # Written to BOTH blobs so autofill AND the letterhead see it.
    assert saved["profile"]["identity"]["name"] == "New Name"
    assert saved["essentials"]["identity"]["name"] == "New Name"
    # Read-modify-write preserved history and the CV sections.
    assert saved["history"] == [{"role": "user", "content": "hi"}]
    assert saved["profile"]["experience"][0]["org"] == "Acme"
    assert saved["profile"]["skills"] == {"Computing": "Python, SQL"}

    # Autofill (reads essentials.identity) reflects the edit and derives its parts.
    # (autofill is extension-only, so send the extension header.)
    f = client.get("/api/profile/autofill",
                   headers={"X-Tailor-Extension": "1"}).get_json()["fields"]
    assert f["first_name"] == "New" and f["last_name"] == "Name"
    assert f["email"] == "new@example.com"
    assert f["city"] == "Chicago" and f["state"] == "IL" and f["zip"] == "60601"


def test_profile_view_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "empty.db"))
    app._SESSION.pop("s", None)
    d = app.app.test_client().get("/api/profile").get_json()
    assert d["has_profile"] is False
    assert d["identity"]["name"] == "" and d["experience"] == []


def test_identity_save_creates_profile_when_none(tmp_path, monkeypatch):
    db = str(tmp_path / "fresh.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    app._SESSION.pop("s", None)
    app.app.test_client().post("/api/profile/identity",
                               json={"name": "First Timer", "email": "ft@example.com"})
    saved = ConversationMemory(db).load("default")
    assert saved["profile"]["identity"]["name"] == "First Timer"
    assert saved["essentials"]["identity"]["name"] == "First Timer"
