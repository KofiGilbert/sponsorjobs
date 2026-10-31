"""The resume's structured facts are assembled FROM MEMORY, not filled into a form (the August
plan's step 2, finished in Phase 6 of docs/applicant-brain.md).

The August version replayed up to 80 chat turns through the model on every call and was never
wired in. The brain now holds the facts, so /api/memory/derived-profile assembles them by code:
the saved resume material as the base (only it holds the bullets), the person's own contact
details over it (a phone number corrected anywhere is corrected everywhere), and the dates the
brain holds for what the material leaves empty. No model call; nothing invented.
"""

from __future__ import annotations

import importlib

import pytest

from applicant import migrate as M


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._SESSION.pop("s", None)
    yield A, A.app.test_client()
    M.forget_process_state(tmp_path / "data")


def _save(A, profile, essentials=None):
    A._memory().save("default", profile, essentials or {"identity": dict(profile.get("identity") or {})}, [])


def test_identity_comes_from_what_the_person_said(client):
    A, c = client
    _save(A, {"identity": {"name": "Maya Rodriguez", "phone": "312-555-0100"},
              "experience": [{"org": "Acme", "roles": [{"title": "Analyst", "dates": "2021 - 2023"}]}]})
    from applicant import facts as F
    # the person corrected the phone and gave an email somewhere else (the extension, the builder)
    F.write_contact(A._brain(), {"phone": "312-555-0199", "email": "maya@example.com"}, source="you",
                    source_ref="extension", confirm=True)
    d = c.get("/api/memory/derived-profile").get_json()["essentials"]
    assert d["identity"]["name"] == "Maya Rodriguez"
    assert d["identity"]["phone"] == "312-555-0199"            # corrected everywhere
    assert d["identity"]["email"] == "maya@example.com"         # filled from memory
    assert d["experience"] == [{"org": "Acme", "title": "Analyst", "dates": "2021 - 2023", "location": ""}]


def test_empty_memory_assembles_an_empty_but_valid_shape(client):
    _, c = client
    d = c.get("/api/memory/derived-profile").get_json()["essentials"]
    assert d == {"identity": {}, "education": [], "experience": []}


def test_dates_the_brain_holds_fill_what_the_material_lacks(client):
    A, c = client
    _save(A, {"identity": {"name": "Alex Chen"},
              "experience": [{"org": "Stanbic Bank", "roles": [{"title": "Analyst"}]}],
              "education": [{"school": "DePaul University", "degree": "MBA"}]})
    from applicant import facts as F
    st = A._brain()
    F.put(st, "role", {"org": "Stanbic Bank", "title": "Analyst", "dates": "2020 - 2021", "location": "Accra, GH"},
          source="you", subject="role:stanbic bank|analyst", source_ref="details")
    F.put(st, "school", {"school": "DePaul University", "date": "2025"}, source="you",
          subject="school:depaul university", source_ref="details")
    p = c.get("/api/memory/derived-profile").get_json()["profile"]
    assert p["experience"][0]["roles"][0]["dates"] == "2020 - 2021" and p["experience"][0]["location"] == "Accra, GH"
    assert p["education"][0]["date"] == "2025"
    # the saved material itself is untouched: the assembly is a view
    assert "dates" not in A._memory().load("default")["profile"]["experience"][0]["roles"][0]


def test_a_value_less_sure_than_the_material_never_replaces_it(client):
    A, c = client
    _save(A, {"identity": {"name": "Alex Chen", "email": "alex@chen.dev"},
              "experience": [{"org": "Acme", "roles": [{"title": "Analyst"}]}]})
    from applicant import facts as F
    F.put(A._brain(), "email", "alex@old-resume.dev", source="resume")   # 0.9, never confirmed
    d = c.get("/api/memory/derived-profile").get_json()["essentials"]
    assert d["identity"]["email"] == "alex@chen.dev"
