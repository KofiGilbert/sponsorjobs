"""Step 2 of 'no profile page': the resume's structured facts are COMPUTED from memory, not filled
into a form (2026-08-03).

Everything the person tells the app goes to memory; the identity / roles / dates / education a resume
needs are then DERIVED from that memory by replaying it through the same grounded extractor the live
intake uses (invents nothing). This proves the derivation before the profile page is retired.
Offline: FakeLLM + verbatim-only memory (no embedder/network).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_identity_is_derived_from_what_the_person_told_memory(client):
    A, c = client
    # The person just talks; nothing is typed into a profile form.
    A._palace().remember_summary("I'm Maya Rodriguez, you can reach me at maya@example.com")
    A._palace().remember_summary("My LinkedIn is linkedin.com/in/mayarodriguez")
    d = c.get("/api/memory/derived-profile").get_json()["essentials"]
    assert d["identity"]["name"] == "Maya Rodriguez"           # derived, not filled
    assert d["identity"]["email"] == "maya@example.com"
    assert "linkedin" in d["identity"].get("linkedin", "").lower()
    # The essentials always come back in the shape the resume assembler expects.
    assert isinstance(d["education"], list) and isinstance(d["experience"], list)


def test_empty_memory_derives_an_empty_but_valid_shape(client):
    _, c = client
    d = c.get("/api/memory/derived-profile").get_json()["essentials"]
    assert d == {"identity": {}, "education": [], "experience": []}


def test_derivation_accumulates_across_separate_statements(client):
    A, c = client
    # Facts arrive over MULTIPLE separate messages (like real life), and accumulate into one profile.
    A._palace().remember_summary("I'm Alex Chen.")
    A._palace().remember_summary("Email me at alex@chen.dev")
    d = A._derive_essentials_from_memory()
    assert d["identity"]["name"] == "Alex Chen" and d["identity"]["email"] == "alex@chen.dev"
