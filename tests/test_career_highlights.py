"""Career-diary highlights: the grounded retrieval engine behind the autonomous 'wow CV' (2026-08-03).

When tailoring for a job, the app silently recalls the person's most relevant REAL past
accomplishments from their career diary and keeps only the strong, on-point ones, so it can weave
them in without asking the (lazy-by-design) user anything. These tests pin the filtering behaviour
with injected recall hits, so they're deterministic and never touch the embedder/network.
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


class _FakePalace:
    def __init__(self, hits):
        self._hits = hits

    def recall(self, query, n=5):
        return list(self._hits)


def test_only_strong_on_point_memories_are_kept(app_mod, monkeypatch):
    A = app_mod
    # The real outage memory matches strongly; the hackathon is off-topic and weak.
    hits = [{"text": "Resolved a production outage in 20 minutes on-call.", "similarity": 0.46},
            {"text": "Led the payments migration and cut latency 40 percent.", "similarity": 0.31},
            {"text": "Organized the campus hackathon for 120 students.", "similarity": 0.12}]
    monkeypatch.setattr(A, "_palace", lambda person="default": _FakePalace(hits))
    hi = A._career_highlights_for_job("Site Reliability Engineer", "Acme", "incident response on-call")
    assert [h["text"] for h in hi] == [
        "Resolved a production outage in 20 minutes on-call.",
        "Led the payments migration and cut latency 40 percent.",
    ]                                                       # weak/off-topic dropped, strong kept


def test_no_relevant_memories_returns_empty(app_mod, monkeypatch):
    A = app_mod
    monkeypatch.setattr(A, "_palace",
                        lambda person="default": _FakePalace([{"text": "x", "similarity": 0.05}]))
    assert A._career_highlights_for_job("PM", "Acme", "roadmaps") == []
    assert A._career_highlights_for_job("", "", "") == []   # no query -> nothing


def test_highlights_endpoint_returns_grounded_hits(app_mod, monkeypatch):
    A = app_mod
    monkeypatch.setattr(A, "_palace", lambda person="default": _FakePalace(
        [{"text": "Built a Tableau dashboard that halved reporting time.", "similarity": 0.57}]))
    r = A.app.test_client().post("/api/memory/highlights",
                                 json={"role": "Data Analyst", "jd": "dashboards and reporting"})
    assert r.status_code == 200
    hi = r.get_json()["highlights"]
    assert len(hi) == 1 and "Tableau" in hi[0]["text"] and hi[0]["similarity"] == 0.57
