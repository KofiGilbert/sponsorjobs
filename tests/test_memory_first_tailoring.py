"""Step 3 of 'no profile page': tailoring reads MEMORY-FIRST (2026-08-03).

The CV's input is built from memory instead of the stored profile form: the structured facts are
derived from what the person told us, and the strongest recalled accomplishments for THIS job are
folded in (grounded, never invented). Built ALONGSIDE the form path so we can prove quality before
switching. Offline: FakeLLM + verbatim memory; recall is injected so it's deterministic.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
    import ui.app as A
    importlib.reload(A)
    return A


def test_tailoring_input_is_built_from_memory_plus_recalled_wins(app_mod, monkeypatch):
    A = app_mod
    # The person only ever TALKED to the app; no form was filled.
    A._palace().remember_summary("I'm Priya Nair, priya@example.com")
    # The strongest recalled accomplishment for this job (recall injected for determinism).
    monkeypatch.setattr(A, "_career_highlights_for_job", lambda role, company, jd: [
        {"text": "Resolved a production outage in 20 minutes on-call.", "similarity": 0.46}])
    out = A._memory_first_profile("Site Reliability Engineer", "Acme", "incident response")
    # Facts came from memory...
    assert out["essentials"]["identity"]["name"] == "Priya Nair"
    assert out["essentials"]["identity"]["email"] == "priya@example.com"
    # ...and the recalled win is what it drew on (surfaceable as 'from your career memory').
    assert out["drew_on"] and "outage" in out["drew_on"][0]["text"]


def test_tailoring_input_endpoint(app_mod, monkeypatch):
    A = app_mod
    A._palace().remember_summary("I'm Sam Okoro, sam@dev.io")
    monkeypatch.setattr(A, "_career_highlights_for_job", lambda role, company, jd: [])
    r = A.app.test_client().post("/api/memory/tailoring-input",
                                 json={"role": "PM", "company": "Acme", "jd": "roadmaps"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["essentials"]["identity"]["name"] == "Sam Okoro" and d["drew_on"] == []


def test_no_memory_yields_a_valid_empty_input(app_mod, monkeypatch):
    A = app_mod
    monkeypatch.setattr(A, "_career_highlights_for_job", lambda role, company, jd: [])
    out = A._memory_first_profile("Analyst", "", "data")
    assert out["essentials"] == {"identity": {}, "education": [], "experience": []}
    assert out["drew_on"] == []
