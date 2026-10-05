"""My Profile section editor (case-study obs #15: when saved data went bad, the only
fix was SQL surgery on conversation_memory — three times in one application). The
editor endpoint writes BOTH saved bags coherently and refuses to race a live session."""

from __future__ import annotations

import ui.app as app
from intake.memory import ConversationMemory


def _mem(monkeypatch):
    mem = ConversationMemory(":memory:")
    monkeypatch.setattr(app, "_memory", lambda: mem)
    return mem


EDIT = {
    "summary": "Operations leader with real scale numbers.",
    "experience": [
        {"org": "Ampsel Commodities Limited", "title": "Supply Chain Operations Manager",
         "dates": "June 2020 - Sept 2024", "location": "Accra, GH",
         "bullets": ["Exported over $60 million in six months."]},
        {"org": "Viva Technologies", "title": "Operations Lead",
         "dates": "Sept 2016 - Jan 2018", "location": "Accra, GH",
         "bullets": ["Led a fleet of 250 drivers."]},
    ],
    "education": [{"school": "DePaul University", "degree": "MBA - Business Analytics (STEM)",
                   "date": "Sept 2024 - Dec 2025", "location": "Chicago, IL",
                   "courses": "Business Analytics Tools, Data Visualization"}],
    "projects": [{"org": "Warehouse Safety Benchmark",
                  "link": "https://github.com/x/warehouse", "location": "Python, pandas",
                  "dates": "", "bullets": ["Benchmarked 30,000+ warehouses."]}],
    "extracurricular": [{"title": "Team Captain, Intramural Soccer", "date": "",
                         "bullets": []}],
    "interests": "Competitive soccer and basketball.",
    "skills_input": ["team leadership", "dispatch operations"],
    "declined": ["blog", "not-a-real-key"],
}


def test_sections_roundtrip_writes_both_bags(monkeypatch):
    mem = _mem(monkeypatch)
    monkeypatch.setitem(app._SESSION, "s", None)
    client = app.app.test_client()

    r = client.post("/api/profile/sections", json=EDIT)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["summary"] == EDIT["summary"]
    assert body["skills_input"] == EDIT["skills_input"]
    assert body["declined"] == ["blog"]                     # unknown keys dropped

    saved = mem.load("default")
    ess, prof = saved["essentials"], saved["profile"]
    # essentials: flat rows
    assert ess["experience"][0]["title"] == "Supply Chain Operations Manager"
    assert ess["experience"][0]["bullets"] == ["Exported over $60 million in six months."]
    # profile: grouped org -> roles (the renderer's shape)
    assert prof["experience"][0]["org"] == "Ampsel Commodities Limited"
    assert prof["experience"][0]["roles"][0]["dates"] == "June 2020 - Sept 2024"
    # projects carry the personal flag and the link verbatim (obs #8)
    assert prof["projects"][0]["personal"] is True
    assert prof["projects"][0]["link"] == "https://github.com/x/warehouse"
    # both bags agree on the shared sections
    for bag in (ess, prof):
        assert bag["summary"] == EDIT["summary"]
        assert bag["interests"] == EDIT["interests"]
        assert bag["extracurricular"][0]["title"] == "Team Captain, Intramural Soccer"

    # GET reflects the same state
    g = client.get("/api/profile").get_json()
    assert g["summary"] == EDIT["summary"]
    assert g["experience"][0]["org"] == "Ampsel Commodities Limited"


def test_sections_save_refuses_while_a_session_is_open(monkeypatch):
    _mem(monkeypatch)
    monkeypatch.setitem(app._SESSION, "s", object())        # a live CV session
    client = app.app.test_client()
    r = client.post("/api/profile/sections", json=EDIT)
    assert r.status_code == 409
    assert "autosave" in r.get_json()["error"]


def test_empty_rows_are_dropped_not_saved(monkeypatch):
    mem = _mem(monkeypatch)
    monkeypatch.setitem(app._SESSION, "s", None)
    client = app.app.test_client()
    r = client.post("/api/profile/sections", json={
        "experience": [{"org": "", "title": "", "dates": "", "location": "",
                        "bullets": []}],
        "education": [], "projects": [], "extracurricular": [],
        "summary": "", "interests": "", "skills_input": [], "declined": [],
    })
    assert r.status_code == 200
    saved = mem.load("default")
    assert saved["essentials"]["experience"] == []
    assert saved["profile"]["experience"] == []
