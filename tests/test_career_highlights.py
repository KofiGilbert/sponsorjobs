"""Career-diary highlights: the grounded retrieval engine behind the autonomous resume (2026-08-03).

When tailoring for a job, the app silently recalls the person's most relevant REAL past
accomplishments from their chat diary and keeps only the strong, on-point ones, so it can weave
them in without asking the (lazy-by-design) user anything. Recall is the diary's plain keyword
search (intake/diary.py, docs/applicant-brain.md section 12): model-free and deterministic, so
these tests write real diary lines.
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
    import ui.app as A
    importlib.reload(A)
    return A


def _seed(A, *lines, role="user"):
    d = A._palace()
    for ln in lines:
        d.remember_turn(role, ln)


def test_only_strong_on_point_memories_are_kept(app_mod):
    A = app_mod
    _seed(A, "Resolved a production outage in 20 minutes on-call.",      # 3 job words: strong
          "Led the incident review and the response runbook rewrite.",     # 2 job words: kept
          "Mentored a junior engineer.",                                   # 1 job word: too weak
          "Organized the campus hackathon for 120 students.")             # off-topic
    A._palace().remember_turn("agent", "Tell me about a production outage you handled on-call.")
    hi = A._career_highlights_for_job("Site Reliability Engineer", "Acme",
                                      "incident response, on-call, production outages")
    assert [h["text"] for h in hi] == [
        "Resolved a production outage in 20 minutes on-call.",
        "Led the incident review and the response runbook rewrite.",
    ]                                       # weak/off-topic and the app's own question dropped
    assert hi[0]["similarity"] > hi[1]["similarity"] >= 0.5


def test_no_relevant_memories_returns_empty(app_mod):
    A = app_mod
    assert A._career_highlights_for_job("PM", "Acme", "roadmaps") == []   # empty diary
    _seed(A, "Organized the campus hackathon for 120 students.")
    assert A._career_highlights_for_job("PM", "Acme", "roadmaps") == []
    assert A._career_highlights_for_job("", "", "") == []   # no query -> nothing


def test_highlights_endpoint_returns_grounded_hits(app_mod):
    A = app_mod
    _seed(A, "Built a Tableau dashboard that halved reporting time.", role="note")
    r = A.app.test_client().post("/api/memory/highlights",
                                 json={"role": "Data Analyst", "jd": "dashboards and reporting"})
    assert r.status_code == 200
    hi = r.get_json()["highlights"]
    assert len(hi) == 1 and "Tableau" in hi[0]["text"] and hi[0]["similarity"] == 0.5
