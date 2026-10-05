"""GitHub publish for a verified project (Feature 2, phase 4).

Acceptance: connect the person's own account with a token (validated, stored locally); a project
can only be published AFTER it passes the defend gate AND GitHub is connected; publishing stores
the repo URL and carries it onto the CV entry. The GitHub API is mocked, no real repo is created,
because creating a repo is an outward action that only ever happens on the person's real click.
"""

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_connect_status_disconnect(client, monkeypatch):
    A, c = client
    import ui.github_publish as G

    assert c.get("/api/github/status").get_json()["configured"] is False
    # a token GitHub rejects is refused
    monkeypatch.setattr(G, "validate_token", lambda t: {"ok": False, "error": "nope"})
    assert c.post("/api/github", json={"token": "bad"}).status_code == 400
    # a good token is stored with its login
    monkeypatch.setattr(G, "validate_token", lambda t: {"ok": True, "login": "octocat"})
    assert c.post("/api/github", json={"token": "ghp_x"}).get_json()["login"] == "octocat"
    st = c.get("/api/github/status").get_json()
    assert st["configured"] and st["login"] == "octocat"
    # disconnect clears it
    c.post("/api/github/disconnect")
    assert c.get("/api/github/status").get_json()["configured"] is False


def test_publish_is_gated_then_carries_link(client, monkeypatch):
    A, c = client
    import ui.github_publish as G
    pid = c.post("/api/projects", json={"title": "My Cool Project", "summary": "does things", "tech": "Python"}).get_json()["project"]["id"]

    # a draft cannot be published (same gate as the CV)
    assert c.post(f"/api/projects/{pid}/publish").status_code == 400

    # pass the defend gate (FakeLLM passes on substantive answers)
    t = c.post(f"/api/projects/{pid}/test").get_json()
    good = {"answers": [{"q": q, "a": "because " + "x" * 50} for q in t["questions"]]}
    assert c.post(f"/api/projects/{pid}/grade", json=good).get_json()["pass"] is True

    # verified but GitHub not connected -> still refused, with a helpful message
    r = c.post(f"/api/projects/{pid}/publish")
    assert r.status_code == 400 and "Connect GitHub" in r.get_json()["error"]

    # connect GitHub and publish (API mocked, no real repo created)
    monkeypatch.setattr(G, "validate_token", lambda tk: {"ok": True, "login": "octocat"})
    c.post("/api/github", json={"token": "ghp_x"})
    monkeypatch.setattr(G, "publish_project",
                        lambda tk, p, private=False: {"ok": True, "url": "https://github.com/octocat/my-cool-project", "full_name": "octocat/my-cool-project"})
    assert c.post(f"/api/projects/{pid}/publish").get_json()["url"].endswith("my-cool-project")

    # the repo URL is stored and carried onto the CV entry
    assert c.get("/api/projects").get_json()["projects"][0]["repo_url"].endswith("my-cool-project")
    c.post(f"/api/projects/{pid}/tocv")
    prof = A._memory().load("default")["profile"]
    assert any(pr.get("link", "").endswith("my-cool-project") for pr in prof.get("projects", []))


def test_helpers_are_pure():
    from ui.github_publish import slugify, readme_for
    assert slugify("My Cool Project!") == "my-cool-project"
    assert slugify("") == "project"
    md = readme_for({"title": "T", "summary": "S", "tech": "Python", "plan": {"milestones": [{"title": "Step one"}]}})
    assert "# T" in md and "Step one" in md and "git push" in md
