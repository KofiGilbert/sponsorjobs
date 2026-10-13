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
    {"full_name": "real/one", "html_url": "https://github.com/real/one", "stargazers_count": 1200, "description": "x",
     "language": "Python"},
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
        assert "size" not in i   # no build-time label: AI build time is not human build time
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


def test_ideas_cache_heals_examples_without_language(client, monkeypatch):
    """Ideas cached before ``language`` was passed through drew a hollow ring on every repo. A hit
    with such examples is stale: the examples are refetched (at most 4 ideas a pass without a
    token, passes a minute apart, inside GitHub's 10 searches a minute), the rest are reused."""
    A, c = client
    import ui.github_publish as G
    gh = FakeGitHub(search=SEARCH_FIXTURE)
    monkeypatch.setattr(G, "FETCH", gh)
    old_ex = [{"full_name": "old/repo", "url": "https://github.com/old/repo", "stars": 5, "description": ""}]
    fresh_ex = [{"full_name": "keep/me", "url": "https://github.com/keep/me", "stars": 9, "description": "",
                 "language": "Go"}]
    ideas = [{"title": f"Idea {n}", "pitch": "p", "skills": ["Python"], "gaps": [], "size": "Weekend",
              "repo": f"idea-{n}", "search": f"idea {n}", "examples": [dict(e) for e in old_ex]} for n in range(6)]
    ideas.append({"title": "Fine", "pitch": "p", "skills": ["Go"], "gaps": [], "size": "Weekend",
                  "repo": "fine", "search": "fine", "examples": fresh_ex})
    ideas.append({"title": "None found", "pitch": "p", "skills": ["Go"], "gaps": [], "size": "Weekend",
                  "repo": "none", "search": "none", "examples": []})
    A._ideas_cache_put("data engineer", {"role": "Data Engineer", "ideas": ideas, "at": 1.0})

    d = c.post("/api/projects/ideas", json={"role": "Data Engineer"}).get_json()
    assert d["cached"] is True
    searches = gh.of("GET", "/search/repositories")
    assert 1 <= len(searches) <= 8            # 4 ideas at most, each at most 2 searches
    healed = [i for i in d["ideas"] if i["examples"] and i["examples"][0]["full_name"] == "real/one"]
    assert len(healed) == 4 and all(i["examples"][0]["language"] == "Python" for i in healed)
    assert next(i for i in d["ideas"] if i["title"] == "Fine")["examples"] == fresh_ex   # reused
    assert next(i for i in d["ideas"] if i["title"] == "None found")["examples"] == []
    # the healed set is saved, so reopening does not search again for those ideas
    assert sum(1 for i in A._ideas_cache()["data engineer"]["ideas"] if A._stale_examples(i)) == 2

    # within the minute, a reopen spends no searches; after it, the last two heal
    n = len(gh.calls)
    c.post("/api/projects/ideas", json={"role": "Data Engineer"})
    assert len(gh.calls) == n
    monkeypatch.setattr(A, "_HEAL_GAP", 0)
    d = c.post("/api/projects/ideas", json={"role": "Data Engineer"}).get_json()
    assert not any(A._stale_examples(i) for i in d["ideas"])
    n = len(gh.calls)
    c.post("/api/projects/ideas", json={"role": "Data Engineer"})
    assert len(gh.calls) == n                 # nothing stale, nothing fetched


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


def test_search_examples_passes_the_language_through(monkeypatch):
    # The gallery colours each example repo's dot by its GitHub language; a repo with none stays "".
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search=SEARCH_FIXTURE))
    one, two = G.search_examples("x", limit=2)
    assert one["full_name"] == "real/one" and one["language"] == "Python"
    assert two["full_name"] == "real/two" and two["language"] == ""


def test_start_without_github_creates_the_draft_only(client, monkeypatch):
    # The hand-built start ({build: false}); the AI build is in tests/test_projects_ai_build.py.
    A, c = client
    import ui.github_publish as G
    gh = FakeGitHub()
    monkeypatch.setattr(G, "FETCH", gh)
    idea = {"title": "Job market tracker", "pitch": "Charts rising skills.", "tech": "Python, SQLite",
            "skills": ["Python", "SQL", "Airflow"], "size": "1 week", "repo": "job-market-tracker"}
    d = c.post("/api/projects/start", json={"idea": idea, "build": False}).get_json()
    assert d["github"]["connected"] is False and d["github"]["ok"] is False
    p = d["project"]
    assert p["status"] == "draft" and p["repo_url"] == "" and p["repo"] == "job-market-tracker"
    assert p["skills"] == ["Python", "SQL", "Airflow"] and "size" not in p
    assert gh.calls == []   # nothing touched GitHub
    assert c.post("/api/projects/start", json={"idea": {}, "build": False}).status_code == 400
    assert c.post("/api/projects/start", json={"idea": {}}).status_code == 400


def test_start_with_github_private_repo_readme_issues_then_publish_flips(client, monkeypatch):
    # The hand-built start ({build: false}): a starter README and issues, no code pushed.
    A, c = client
    import ui.github_publish as G
    _connect(A, monkeypatch)
    gh = FakeGitHub()
    monkeypatch.setattr(G, "FETCH", gh)
    idea = {"title": "Expense insights API", "pitch": "Flags unusual spend.", "tech": "Python, FastAPI",
            "skills": ["Python", "REST APIs", "PostgreSQL"], "size": "Weekend", "repo": "expense-insights-api"}
    d = c.post("/api/projects/start", json={"idea": idea, "build": False}).get_json()
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


# ---- Ideas gallery: search, More ideas, the daily cap, saved ideas, client-side filters. ----

def _seed_job(A):
    A._memory().save("default", {"skills": {"Data": "Python, Excel"},
                                 "experience": [{"org": "Acme", "roles": [{"title": "Intern"}]}]}, {}, [])
    recs = A._records()
    recs.add("Data Engineer", "Bank", 0.8, "", data={"jd": "We need SQL, Airflow, Docker and Python."})
    recs.close()


class _SpyLLM:
    """Wraps FakeLLM's suggest_github_ideas and records each call's kwargs."""

    def __init__(self, inner, override=None):
        self.inner, self.override, self.calls = inner, override, []

    def suggest_github_ideas(self, profile, role="", jd_terms=None, missing=None, count=6, query="", exclude=None):
        self.calls.append({"role": role, "query": query, "exclude": list(exclude or [])})
        if self.override:
            return self.override(len(self.calls))
        return self.inner.suggest_github_ideas(profile, role, jd_terms, missing, count, query=query, exclude=exclude)


def _spy(A, monkeypatch, override=None):
    from llm.base import FakeLLM
    spy = _SpyLLM(FakeLLM(), override)
    monkeypatch.setattr(A, "_make_llm", lambda: spy)
    return spy


def _pin_day(A, monkeypatch, day):
    monkeypatch.setattr(A, "_ideas_day", lambda: day)


def test_ideas_search_query_reaches_model_and_is_cached_separately(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search=SEARCH_FIXTURE))
    _seed_job(A)
    _pin_day(A, monkeypatch, "2026-10-13")
    spy = _spy(A, monkeypatch)

    base = c.post("/api/projects/ideas", json={}).get_json()
    q = c.post("/api/projects/ideas", json={"query": "  fraud   detection "}).get_json()
    assert q["query"] == "fraud detection" and q["role"] == "Data Engineer" and q["cached"] is False
    assert spy.calls[-1]["query"] == "fraud detection" and spy.calls[-1]["role"] == "Data Engineer"
    assert all("Fraud detection" in i["title"] for i in q["ideas"])
    assert {i["title"] for i in q["ideas"]}.isdisjoint({i["title"] for i in base["ideas"]})
    assert len(spy.calls) == 2

    # each (role, query) set is its own cache entry: both reopen free, and the role's set is intact
    again = c.post("/api/projects/ideas", json={"query": "Fraud Detection"}).get_json()
    assert again["cached"] is True and again["ideas"] == q["ideas"]
    plain = c.post("/api/projects/ideas", json={}).get_json()
    assert plain["cached"] is True and plain["ideas"] == base["ideas"] and plain["query"] == ""
    assert len(spy.calls) == 2
    assert set(A._ideas_cache()) == {"data engineer", "data engineer|q:fraud detection"}


def test_more_ideas_excludes_shown_titles_and_server_dedupes(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search=SEARCH_FIXTURE))
    _seed_job(A)
    _pin_day(A, monkeypatch, "2026-10-13")
    spy = _spy(A, monkeypatch)

    first = c.post("/api/projects/ideas", json={}).get_json()["ideas"]
    shown = [i["title"] for i in first]
    more = c.post("/api/projects/ideas", json={"exclude": shown}).get_json()
    assert more["append"] is True and more["cached"] is False and more["ideas"]
    assert set(shown) <= set(spy.calls[-1]["exclude"])          # the model is told what is on screen
    assert {i["title"] for i in more["ideas"]}.isdisjoint(shown)
    # the cache now holds both batches, so reopening restores the whole gallery
    hit = c.post("/api/projects/ideas", json={}).get_json()
    assert hit["cached"] and len(hit["ideas"]) == len(first) + len(more["ideas"])
    assert hit["batches"] == [len(first), len(more["ideas"])]

    # a model that ignores exclude: the server drops repeats by title (any case) and by repo name
    def stubborn(n):
        return {"ideas": [
            {"title": shown[0].upper(), "pitch": "p", "skills": ["SQL", "Python", "Git"], "size": "Weekend", "repo": "x-1"},
            {"title": "Brand new name", "pitch": "p", "skills": ["SQL", "Python", "Git"], "size": "Weekend",
             "repo": first[1]["repo"]},
            {"title": "Truly new", "pitch": "p", "skills": ["SQL", "Python", "Git"], "size": "Weekend", "repo": "truly-new"},
            {"title": "truly new", "pitch": "p", "skills": ["SQL", "Python", "Git"], "size": "Weekend", "repo": "other"},
        ]}
    spy.override = stubborn
    d = c.post("/api/projects/ideas", json={"exclude": shown[:1]}).get_json()
    assert [i["title"] for i in d["ideas"]] == ["Truly new"]


def test_daily_cap_counts_only_model_batches_and_resets_each_day(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub(search=SEARCH_FIXTURE))
    _seed_job(A)
    monkeypatch.setenv("PROJECT_IDEAS_DAILY_CAP", "3")
    _pin_day(A, monkeypatch, "2026-10-13")
    spy = _spy(A, monkeypatch)

    d = c.post("/api/projects/ideas", json={}).get_json()                      # 1: model
    assert d["cap"] == {"limit": 3, "used": 1, "left": 2}
    assert c.post("/api/projects/ideas", json={}).get_json()["cap"]["used"] == 1   # cache hit: free
    c.post("/api/projects/ideas", json={"query": "fraud"})                       # 2: search
    c.post("/api/projects/ideas", json={"query": "fraud"})                       # cache hit: free
    c.post("/api/projects/ideas", json={"refresh": True})                        # 3: refresh
    assert len(spy.calls) == 3

    r = c.post("/api/projects/ideas", json={"exclude": [i["title"] for i in d["ideas"]]})   # 4: refused
    assert r.status_code == 429
    body = r.get_json()
    assert body["code"] == "ideas_cap" and "reason" not in body             # no outage overlay
    assert body["error"] == "You've reached today's 3 idea batches. More tomorrow."
    assert body["cap"] == {"limit": 3, "used": 3, "left": 0}
    assert c.post("/api/projects/ideas", json={"query": "new topic"}).status_code == 429
    assert len(spy.calls) == 3                                             # refused calls cost nothing
    # cached sets keep working at the cap
    ok = c.post("/api/projects/ideas", json={"query": "fraud"})
    assert ok.status_code == 200 and ok.get_json()["cached"] is True

    # a new local day starts from zero
    _pin_day(A, monkeypatch, "2026-10-14")
    d2 = c.post("/api/projects/ideas", json={"query": "new topic"}).get_json()
    assert d2["cap"] == {"limit": 3, "used": 1, "left": 2} and len(spy.calls) == 4


def test_daily_cap_defaults_to_ten(client, monkeypatch):
    A, _ = client
    monkeypatch.delenv("PROJECT_IDEAS_DAILY_CAP", raising=False)
    assert A._ideas_cap() == 10
    monkeypatch.setenv("PROJECT_IDEAS_DAILY_CAP", "nonsense")
    assert A._ideas_cap() == 10


def test_saved_ideas_crud_and_persistence(client, monkeypatch, tmp_path):
    A, c = client
    assert c.get("/api/projects/ideas/saved").get_json() == {"saved": []}
    idea = {"title": "Fraud detection API", "pitch": "Flags odd payments.", "skills": ["Python", "SQL", "Kafka"],
            "gaps": ["Kafka"], "tech": "Python, FastAPI", "size": "1 week", "repo": "fraud-detection-api",
            "examples": [{"full_name": "real/one", "url": "https://github.com/real/one", "stars": 3},
                         {"full_name": "bad/one", "url": "https://evil.example/bad/one", "stars": 9}]}
    other = {"title": "Open data dashboard", "skills": ["Pandas"], "size": "Weekend"}
    c.post("/api/projects/ideas/saved", json={"idea": idea})
    s = c.post("/api/projects/ideas/saved", json={"idea": other}).get_json()["saved"]
    assert [i["title"] for i in s] == ["Open data dashboard", "Fraud detection API"]   # newest first
    assert s[1]["examples"] == [idea["examples"][0]]                                      # only github.com
    assert s[0]["repo"] == "open-data-dashboard" and "size" not in s[0] and "size" not in s[1]
    # saving again replaces, never duplicates
    s = c.post("/api/projects/ideas/saved", json={"idea": idea}).get_json()["saved"]
    assert [i["title"] for i in s] == ["Fraud detection API", "Open data dashboard"]
    assert c.post("/api/projects/ideas/saved", json={"idea": {}}).status_code == 400

    # persisted in the data dir: a fresh app (a restart) still has them
    assert (tmp_path / "data" / "saved_ideas.json").exists()
    importlib.reload(A)
    c2 = A.app.test_client()
    assert [i["title"] for i in c2.get("/api/projects/ideas/saved").get_json()["saved"]] == \
        ["Fraud detection API", "Open data dashboard"]

    # delete by repo (query string, as the UI sends it) or by title in the body
    s = c2.delete("/api/projects/ideas/saved?repo=fraud-detection-api").get_json()["saved"]
    assert [i["title"] for i in s] == ["Open data dashboard"]
    s = c2.delete("/api/projects/ideas/saved", json={"title": "Open data dashboard"}).get_json()["saved"]
    assert s == []
    assert c2.delete("/api/projects/ideas/saved").status_code == 400

    # starting a saved idea is the same flow as any idea
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", FakeGitHub())
    d = c2.post("/api/projects/start", json={"idea": idea}).get_json()
    assert d["project"]["title"] == "Fraud detection API"


def _js_function(src, name):
    """The source of a top-level-ish `function name(` or `const name = ` in app.js, to its balanced end."""
    import re
    m = re.search(r"(function " + re.escape(name) + r"\(|const " + re.escape(name) + r" = )", src)
    assert m, name
    i, depth, started = m.start(), 0, False
    for j in range(m.start(), len(src)):
        ch = src[j]
        if ch in "({[":
            depth += 1; started = True
        elif ch in ")}]":
            depth -= 1
            if started and depth == 0 and (ch == "}" or src[j + 1] in ";\n"):
                if ch == "}" or src[j + 1] == ";":
                    return src[i:j + 1]
    raise AssertionError(name)


def test_idea_filters_are_client_side_only():
    """The category select narrows what is loaded; nothing in the filter path makes a request.
    There is no size filter, pill or tie-break any more."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for name in ("renderIdeaFilters", "renderIdeaGrid", "ideaPasses", "ideaCat"):
        body = _js_function(src, name)
        for banned in ("api(", "fetch(", "loadIdeas(", "loadMoreIdeas(", "XMLHttpRequest"):
            assert banned not in body, f"{name} calls {banned}"
    filt = _js_function(src, "renderIdeaFilters")
    assert "#projIdeasCat" in filt and "<option" in filt and "projects.anyCategory" in filt
    assert "s.size" not in src and "IDEA_SIZE" not in src and "data-fsize" not in src
    assert "renderIdeaGrid({ still: true })" in src.split('$("#projIdeasCat")?.addEventListener("change"', 1)[1].split("\n", 1)[0]
    html = (Path(__file__).resolve().parents[1] / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert '<select class="jf-input jf-sel" id="projIdeasCat"' in html


def test_start_without_github_asks_on_the_card_first():
    """Start while GitHub is not connected opens an inline ask (Connect, or Start without GitHub)
    instead of silently creating a project with no repo."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    start = _js_function(src, "startIdea")
    assert "if (!local && !IDEAS.github?.connected) return askGitHubFirst(s, btn);" in start
    ask = _js_function(src, "askGitHubFirst")
    assert "data-gh" in ask and "data-nogh" in ask and "startIdea(s, btn, true)" in ask and "openGitHubSettings()" in ask


def test_ideas_gallery_is_a_page_with_a_back_link():
    """The gallery has the Templates back link above the title, no close or refresh icons, and
    Esc goes back; while it is open the title reads Project ideas and the header buttons hide."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'class="btn btn-ghost btn-back tpl-back proj-back" id="projIdeasBack"' in html
    assert 'id="projIdeasClose"' not in html and 'id="projIdeasRefresh"' not in html
    css = (root / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".view-projects .proj-back { margin-left: -8px; margin-bottom: 0; }" in css
    src = (root / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    body = _js_function(src, "renderProjList")
    assert 'projects.ideasPageTitle' in body and "acts.hidden = true" in body
    assert 'e.key !== "Escape"' in src and "closeIdeas();" in src


def test_no_repeated_titles_and_defend_it_is_a_page_of_its_own():
    """The ideas page has no "Ideas for you" eyebrow under "Project ideas". Defend it is a quiz page
    in Projects, like Learn it: "< Projects" above "Defend <title>", the header's buttons hidden,
    Esc and the back link leave it. Interview prep has no project mode left (no back link to
    Projects, no leave bar)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="projIdeasH"' not in html and "Ideas for you" not in html
    assert 'id="projQuiz"' in html and 'id="prepProjBack"' not in html and 'id="prepProjLeave"' not in html
    src = (root / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    start = _js_function(src, "startQuiz")
    page = _js_function(src, "projPage")                  # shared with Learn it
    assert "projects.defendTitle" in start and "projPage(" in start
    assert "#projIdeasBack" in page and "acts.hidden = true" in page
    assert "leaveQuiz()" in src and "prepHead" not in src and "SCR_PROJECT" not in src