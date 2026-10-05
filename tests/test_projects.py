"""Project builder: the defend-your-work gate.

Acceptance: create a project (draft); generate a defense test; a thin defense FAILS and the
project stays draft and CANNOT be added to the CV; a substantive defense PASSES, marks the
project verified, and only then can it be added to the saved profile's projects.
Uses the FakeLLM (deterministic length-heuristic grade), so no network/model needed.
"""

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")   # deterministic grade, no network
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_defend_gate(client):
    A, c = client

    # create a draft project
    r = c.post("/api/projects", json={"title": "Credit-risk model", "summary": "A model", "tech": "Python"})
    pid = r.get_json()["project"]["id"]
    assert r.get_json()["project"]["status"] == "draft"

    # generate the defense test
    t = c.post(f"/api/projects/{pid}/test").get_json()
    assert len(t["questions"]) == 5

    # thin answers -> FAIL, stays draft, and the CV gate refuses it
    thin = {"answers": [{"q": q, "a": "idk"} for q in t["questions"]]}
    res = c.post(f"/api/projects/{pid}/grade", json=thin).get_json()
    assert res["pass"] is False
    assert next(p for p in A._load_projects() if p["id"] == pid)["status"] == "draft"
    gated = c.post(f"/api/projects/{pid}/tocv")
    assert gated.status_code == 400 and "Pass" in gated.get_json()["error"]

    # substantive answers -> PASS, verified, and now it can be added to the CV
    good = {"answers": [{"q": q, "a": "I chose this approach because " + ("x" * 50)} for q in t["questions"]]}
    res = c.post(f"/api/projects/{pid}/grade", json=good).get_json()
    assert res["pass"] is True
    assert next(p for p in A._load_projects() if p["id"] == pid)["status"] == "verified"

    ok = c.post(f"/api/projects/{pid}/tocv")
    assert ok.status_code == 200
    # the verified project is now in the saved profile's projects, with the tech stack
    prof = A._memory().load("default")["profile"]
    entry = next(pr for pr in prof.get("projects", []) if pr.get("org") == "Credit-risk model")
    bullets = " ".join(entry.get("bullets", []))
    assert "A model" in bullets and "Built with Python" in bullets   # summary + tech stack


def test_create_requires_title(client):
    _, c = client
    assert c.post("/api/projects", json={"title": ""}).status_code == 400


def test_suggest_returns_buildable_ideas(client):
    """Suggest-a-project returns concrete ideas grounded in the saved profile + a target, each
    with the shape the UI renders. Picking one is just a normal draft create, so the defend gate
    still applies (covered by test_defend_gate)."""
    A, c = client
    A._memory().save("default", {"skills": {"Data": "SQL, Python, dashboards"}}, {}, [])

    r = c.post("/api/projects/suggest", json={"target": "Data Analyst at a fintech"})
    assert r.status_code == 200
    sug = r.get_json()["suggestions"]
    assert len(sug) == 3
    for s in sug:
        assert s["title"] and set(s) >= {"title", "skill", "summary", "tech", "why"}

    # works with no target too (falls back to profile-driven suggestions)
    assert len(c.post("/api/projects/suggest", json={}).get_json()["suggestions"]) == 3


def test_build_guide_plan_step_and_coach(client):
    """The guided build-and-teach loop: generate a plan, teach through a step, track progress.
    It is guidance only, so it must NOT let an un-defended project reach the CV."""
    A, c = client
    pid = c.post("/api/projects", json={"title": "Churn model", "summary": "Predict churn", "tech": "Python"}).get_json()["project"]["id"]

    # a build plan with teaching milestones, persisted on the project
    plan = c.post(f"/api/projects/{pid}/plan").get_json()
    ms = plan["plan"]["milestones"]
    assert ms and all(set(m) >= {"title", "build", "learn", "check"} for m in ms)
    assert c.get("/api/projects").get_json()["projects"][0]["plan"]["milestones"]

    # progress tracking round-trips
    assert c.post(f"/api/projects/{pid}/step", json={"index": 0, "done": True}).get_json()["done"] == [0]
    assert c.post(f"/api/projects/{pid}/step", json={"index": 0, "done": False}).get_json()["done"] == []

    # the coach teaches on a step; an empty question is refused
    assert c.post(f"/api/projects/{pid}/coach", json={"index": 0, "question": "how do I start?"}).get_json()["answer"]
    assert c.post(f"/api/projects/{pid}/coach", json={"index": 0, "question": ""}).status_code == 400

    # building does NOT unlock the CV: the defend gate still applies
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 400
