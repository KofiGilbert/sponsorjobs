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


# ---- GitHub project ideas: one click, no typing; GitHub-shaped ideas with REAL example repos;
# Start creates a private repo (README + milestone issues) only when GitHub is connected. ----
import base64


class FakeGitHub:
    """A recorded stand-in for api.github.com behind github_publish.FETCH."""

    def __init__(self, search=None, search_status=200):
        self.calls = []
        self.search = search if search is not None else {"items": []}
        self.search_status = search_status
        self.private = None

    def __call__(self, method, url, token="", body=None, params=None):
        self.calls.append((method, url, token, body, params))
        if url.endswith("/search/repositories"):
            return self.search_status, self.search
        if method == "POST" and url.endswith("/user/repos"):
            self.private = body["private"]
            return 201, {"full_name": f"octocat/{body['name']}", "private": body["private"],
                         "html_url": f"https://github.com/octocat/{body['name']}"}
        if method == "PUT" and url.endswith("/contents/README.md"):
            return 201, {}
        if method == "POST" and url.endswith("/issues"):
            return 201, {"number": len(self.calls)}
        if method == "PATCH":
            self.private = body["private"]
            full = url.split("/repos/")[1]
            return 200, {"full_name": full, "private": body["private"], "html_url": f"https://github.com/{full}"}
        return 404, {}

    def of(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


SEARCH_FIXTURE = {"items": [
    {"full_name": "real/one", "html_url": "https://github.com/real/one", "stargazers_count": 1200, "description": "x"},
    {"full_name": "real/forked", "html_url": "https://github.com/real/forked", "stargazers_count": 900, "fork": True},
    {"full_name": "real/old", "html_url": "https://github.com/real/old", "stargazers_count": 800, "archived": True},
    {"full_name": "real/spoof", "html_url": "https://evil.example/real/spoof", "stargazers_count": 700},
    {"full_name": "real/two", "html_url": "https://github.com/real/two", "stargazers_count": 40},
    {"full_name": "real/three", "html_url": "https://github.com/real/three", "stargazers_count": 12},
    {"full_name": "real/four", "html_url": "https://github.com/real/four", "stargazers_count": 11},
]}


def _connect(A, monkeypatch):
    import ui.github_publish as G
    monkeypatch.setattr(G, "validate_token", lambda t: {"ok": True, "login": "octocat"})
    A.app.test_client().post("/api/github", json={"token": "ghp_x"})


def test_ideas_one_click_from_last_tailored_job(client, monkeypatch):
    """No typing: the target comes from the last tailored job, its JD terms drive the ideas, and
    the skills the JD asks for that the profile lacks are flagged as gaps."""
    A, c = client
    import ui.github_publish as G
    gh = FakeGitHub(search=SEARCH_FIXTURE)
    monkeypatch.setattr(G, "FETCH", gh)
    A._memory().save("default", {"skills": {"Data": "Python, Excel"},
                                 "experience": [{"org": "Acme", "roles": [{"title": "Intern"}]}]}, {}, [])
    recs = A._records()
    recs.add("Data Engineer", "Bank", 0.8, "", data={"jd": "We need SQL, Airflow, Docker and Python."})
    recs.close()

    d = c.post("/api/projects/ideas", json={}).get_json()
    assert d["role"] == "Data Engineer" and d["source"] == "job" and d["cached"] is False
    assert 4 <= len(d["ideas"]) <= 6
    for i in d["ideas"]:
        assert i["title"] and i["pitch"] and 3 <= len(i["skills"]) <= 5
        assert i["size"] in ("Weekend", "1 week", "2 weeks")
        assert i["repo"] == i["repo"].lower() and " " not in i["repo"]
        assert set(i["gaps"]) <= set(i["skills"]) and "Python" not in i["gaps"]
    assert any("Airflow" in i["gaps"] or "SQL" in i["gaps"] for i in d["ideas"])

    # examples: only what search returned, forks/archived/spoofed URLs dropped, max 3, with stars
    ex = d["ideas"][0]["examples"]
    assert [e["full_name"] for e in ex] == ["real/one", "real/two", "real/three"]
    assert ex[0]["stars"] == 1200 and ex[0]["url"] == "https://github.com/real/one"

    # cached per role: the second click costs no model and no search
    n = len(gh.calls)
    again = c.post("/api/projects/ideas", json={}).get_json()
    assert again["cached"] is True and again["ideas"] == d["ideas"] and len(gh.calls) == n


def test_ideas_fall_back_to_profile_then_typed_role(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search_status=0))   # GitHub unreachable
    A._memory().save("default", {"experience": [{"org": "Acme", "roles": [{"title": "Data Analyst"}]}]}, {}, [])

    d = c.post("/api/projects/ideas", json={}).get_json()
    assert d["role"] == "Data Analyst" and d["source"] == "profile" and d["ideas"]
    assert all(i["examples"] == [] for i in d["ideas"])   # no search, no examples, no invented URLs

    t = c.post("/api/projects/ideas", json={"role": "Backend Engineer"}).get_json()
    assert t["role"] == "Backend Engineer" and t["source"] == "typed" and t["cached"] is False

    # nothing known at all still returns ideas, with an empty role for the chip to ask about
    A._memory().save("default", {}, {}, [])
    assert c.post("/api/projects/ideas", json={"refresh": True}).get_json()["ideas"]


def test_search_examples_never_invents(monkeypatch):
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search={"items": []}))
    assert G.search_examples("anything") == []
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search_status=403, search={"message": "rate limited"}))
    assert G.search_examples("anything") == []
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search=SEARCH_FIXTURE))
    assert len(G.search_examples("x", limit=2)) == 2
    assert G.search_examples("") == []


def test_start_without_github_creates_the_draft_only(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    gh = FakeGitHub()
    monkeypatch.setattr(G, "FETCH", gh)
    idea = {"title": "Job market tracker", "pitch": "Charts rising skills.", "tech": "Python, SQLite",
            "skills": ["Python", "SQL", "Airflow"], "size": "1 week", "repo": "job-market-tracker"}
    d = c.post("/api/projects/start", json={"idea": idea}).get_json()
    assert d["github"]["connected"] is False and d["github"]["ok"] is False
    p = d["project"]
    assert p["status"] == "draft" and p["repo_url"] == "" and p["repo"] == "job-market-tracker"
    assert p["skills"] == ["Python", "SQL", "Airflow"] and p["size"] == "1 week"
    assert gh.calls == []   # nothing touched GitHub
    assert c.post("/api/projects/start", json={"idea": {}}).status_code == 400


def test_start_with_github_private_repo_readme_issues_then_publish_flips(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    _connect(A, monkeypatch)
    gh = FakeGitHub()
    monkeypatch.setattr(G, "FETCH", gh)
    idea = {"title": "Expense insights API", "pitch": "Flags unusual spend.", "tech": "Python, FastAPI",
            "skills": ["Python", "REST APIs", "PostgreSQL"], "size": "Weekend", "repo": "expense-insights-api"}
    d = c.post("/api/projects/start", json={"idea": idea}).get_json()
    assert d["github"]["ok"] is True and d["github"]["url"] == "https://github.com/octocat/expense-insights-api"
    p = d["project"]
    assert p["repo_private"] is True and p["status"] == "draft"

    # the repo is created PRIVATE, with no code: a README and one issue per milestone
    create = gh.of("POST", "/user/repos")[0]
    assert create[3]["private"] is True and create[3]["name"] == "expense-insights-api"
    readme = base64.b64decode(gh.of("PUT", "/contents/README.md")[0][3]["content"]).decode()
    for head in ("## Problem", "## Approach", "## Tech stack", "## How to run", "## Screenshots", "## What I learned"):
        assert head in readme
    issues = gh.of("POST", "/issues")
    plan = next(x for x in A._load_projects() if x["id"] == p["id"])["plan"]["milestones"]
    assert [i[3]["title"] for i in issues] == [m["title"] for m in plan] and d["github"]["issues"] == len(plan)
    assert not [cl for cl in gh.calls if "/git/" in cl[1]]   # never pushes code

    # the gate: an unverified project cannot be published or reach the resume
    assert c.post(f"/api/projects/{p['id']}/publish").status_code == 400
    assert c.post(f"/api/projects/{p['id']}/tocv").status_code == 400
    assert gh.of("PATCH", "/expense-insights-api") == [] and gh.private is True

    # verified + added to the resume while still private: no link (it would 404 for a recruiter)
    t = c.post(f"/api/projects/{p['id']}/test").get_json()
    good = {"answers": [{"q": q, "a": "because " + "x" * 50} for q in t["questions"]]}
    assert c.post(f"/api/projects/{p['id']}/grade", json=good).get_json()["pass"] is True
    c.post(f"/api/projects/{p['id']}/tocv")
    entry = next(e for e in A._memory().load("default")["profile"]["projects"] if e["org"] == "Expense insights API")
    assert entry["link"] == ""

    # publish flips the SAME repo to public (no second repo) and the resume entry gets the link
    n_create = len(gh.of("POST", "/user/repos"))
    r = c.post(f"/api/projects/{p['id']}/publish").get_json()
    assert r["url"] == "https://github.com/octocat/expense-insights-api"
    assert gh.of("PATCH", "/repos/octocat/expense-insights-api")[0][3] == {"private": False}
    assert len(gh.of("POST", "/user/repos")) == n_create and gh.private is False
    assert c.get("/api/projects").get_json()["projects"][0]["repo_private"] is False
    entry = next(e for e in A._memory().load("default")["profile"]["projects"] if e["org"] == "Expense insights API")
    assert entry["link"] == "https://github.com/octocat/expense-insights-api"


def test_publish_without_a_started_repo_creates_a_public_one(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    gh = FakeGitHub()
    monkeypatch.setattr(G, "FETCH", gh)
    res = G.publish_project("tok", {"title": "Churn model", "summary": "s"})
    assert res["ok"] and gh.of("POST", "/user/repos")[0][3]["private"] is False
    assert gh.of("PATCH", "") == []
