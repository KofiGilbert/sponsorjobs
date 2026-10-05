"""A CV in progress must survive leaving the page (2026-07).

The person's report: "when I click away to other pages and I come back to the CV that we
just created or was creating, it disappears like we were not working on anything."

It wasn't lost. Every /api/session/* route was a POST that MUTATES, and nothing could
READ the session, so the client held the whole CV in browser memory while the server sat
on it in _SESSION. Navigating away threw away work that still existed.
"""

from __future__ import annotations

import ui.app as app
from conftest import requires_latex

JD = ("Data Analyst at Northwind. Required: SQL, Python, dashboards, statistics, "
      "ETL, Tableau, forecasting, data modeling.")


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    app._SESSION.pop("s", None)


def test_no_session_is_a_state_not_an_error(tmp_path, monkeypatch):
    """An empty builder is normal. This must never 500 or the dashboard can't ask."""
    _isolate(tmp_path, monkeypatch)
    r = app.app.test_client().get("/api/session/state")
    assert r.status_code == 200
    assert r.get_json() == {"active": False}


@requires_latex
def test_a_started_cv_can_be_read_back(tmp_path, monkeypatch):
    """The core of the bug: after starting a CV, the server can hand it back."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    assert client.post("/api/session/start", json={"jd": JD}).status_code == 200

    st = client.get("/api/session/state").get_json()
    assert st["active"] is True
    assert st["phase"]                                   # a real session state
    assert isinstance(st.get("history"), list)


@requires_latex
def test_the_conversation_comes_back_too(tmp_path, monkeypatch):
    """Restoring only the PDF would still land the person in an empty chat, which reads
    as 'we weren't working on anything'. The transcript lives in session.history and is
    returned so the conversation is there when they come back."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    client.post("/api/session/start", json={"jd": JD})
    client.post("/api/session/answer", json={"text": "I'm Ada Lovelace, ada@example.com."})

    hist = client.get("/api/session/state").get_json()["history"]
    assert len(hist) >= 2, "the conversation was not preserved"
    assert any(t["role"] == "user" and "Ada Lovelace" in (t["content"] or "") for t in hist)
    assert any(t["role"] == "agent" for t in hist)


@requires_latex
def test_reading_the_session_never_mutates_it(tmp_path, monkeypatch):
    """The dashboard polls this to decide whether to offer Continue, so a read must be a
    read: no extra turns, no advancing the interview, no new LLM calls."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    client.post("/api/session/start", json={"jd": JD})
    first = client.get("/api/session/state").get_json()
    for _ in range(3):
        again = client.get("/api/session/state").get_json()
    assert again["phase"] == first["phase"]
    assert again["history"] == first["history"]
