"""Tailoring reads MEMORY-FIRST (the August plan's step 3, the main path since Phase 6 of
docs/applicant-brain.md).

The resume's input is the profile assembled from memory (applicant/profile.py) plus the person's
own diary lines that fit THIS job (``drew_on``, surfaced as "from your career memory"). It is
what the builder, the drafting routes and the apply agent read (``_saved_full_profile`` and the
session's saved profile). Code only: no model call, nothing invented.
"""

from __future__ import annotations

import importlib

import pytest

from applicant import migrate as M


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._SESSION.pop("s", None)
    yield A
    M.forget_process_state(tmp_path / "data")


PROFILE = {"identity": {"name": "Priya Nair", "email": "priya@example.com"},
           "experience": [{"org": "Acme", "roles": [{"title": "SRE", "dates": "2021 - 2024", "bullets": ["Kept it up."]}]}]}


def test_tailoring_input_is_the_assembled_profile_plus_recalled_wins(app_mod):
    A = app_mod
    A._memory().save("default", PROFILE, {"identity": dict(PROFILE["identity"])}, [])
    A._palace().remember_turn("user", "Resolved a production outage in 20 minutes on-call.")
    out = A._memory_first_profile("Site Reliability Engineer", "Acme", "production on-call and outage response")
    assert out["essentials"]["identity"]["name"] == "Priya Nair"
    assert out["profile"]["experience"][0]["roles"][0]["bullets"] == ["Kept it up."]     # the material's bullets
    assert out["drew_on"] and "outage" in out["drew_on"][0]["text"]


def test_tailoring_input_endpoint(app_mod, monkeypatch):
    A = app_mod
    A._memory().save("default", PROFILE, {"identity": dict(PROFILE["identity"])}, [])
    monkeypatch.setattr(A, "_career_highlights_for_job", lambda role, company, jd: [])
    r = A.app.test_client().post("/api/memory/tailoring-input", json={"role": "PM", "company": "Acme", "jd": "roadmaps"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["essentials"]["identity"]["name"] == "Priya Nair" and d["drew_on"] == []


def test_no_memory_yields_a_valid_empty_input(app_mod, monkeypatch):
    A = app_mod
    monkeypatch.setattr(A, "_career_highlights_for_job", lambda role, company, jd: [])
    out = A._memory_first_profile("Analyst", "", "data")
    assert out["essentials"] == {"identity": {}, "education": [], "experience": []}
    assert out["drew_on"] == []


def test_the_drafting_profile_and_the_builder_read_the_assembled_profile(app_mod, tmp_path):
    A = app_mod
    A._memory().save("default", PROFILE, {"identity": dict(PROFILE["identity"])}, [])
    from applicant import facts as F
    F.write_contact(A._brain(), {"phone": "312-555-0177"}, source="you", source_ref="extension", confirm=True)
    assert A._saved_full_profile()["identity"]["phone"] == "312-555-0177"
    from ui.session import WebIntake
    jd, tex = "Site Reliability Engineer at Acme. On-call, production.", A._load_template(None)[0]
    s = WebIntake(jd, tex, A._make_llm(), A._memory(), A._records(), tmp_path / "work", palace_dir=A.PALACE_DIR)
    assert s.saved_profile["identity"]["phone"] == "312-555-0177"
