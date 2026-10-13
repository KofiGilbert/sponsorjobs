"""AI-built projects: build it, learn it, defend it, then (and only then) publish it.

Acceptance (approved 2026-10-13): "Start this project" counts ONE package and builds a small
working project; files are validated (no secrets, no absolute paths, sane sizes, real syntax);
with GitHub the files land in a PRIVATE repo as one commit through the Git Data API (blobs, tree,
commit, ref); without GitHub they stay here and download as a zip; Learn it is generated from the
real files and its progress is saved; Defend it is a 20-question quiz on the project (see
tests/test_projects_quiz.py); publish and Add to resume refuse anything that is not Verified; the README carries the
"Built with an AI pair-programmer" line by default and loses it at publish when toggled off.
Offline: FakeLLM returns a tiny deterministic project, GitHub is a recorded fixture behind FETCH.
"""

import base64
import importlib
import io
import zipfile

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._BUILD_INLINE = True            # run the build job in the request, deterministically
    counted = []
    real = A._count_package

    def count(llm):
        counted.append(1)
        return real(llm)
    monkeypatch.setattr(A, "_count_package", count)
    A._test_counted = counted
    return A, A.app.test_client()


class GitData:
    """api.github.com as far as an AI-built project needs it, recorded call by call."""

    def __init__(self):
        self.calls, self.private, self.blobs, self.n = [], None, {}, 0

    def __call__(self, method, url, token="", body=None, params=None):
        self.calls.append((method, url, body))
        self.n += 1
        if method == "POST" and url.endswith("/user/repos"):
            self.private = body["private"]
            return 201, {"full_name": f"octocat/{body['name']}", "private": body["private"],
                         "html_url": f"https://github.com/octocat/{body['name']}"}
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return 200, {"object": {"sha": "init0"}}
        if method == "POST" and url.endswith("/git/blobs"):
            sha = f"blob{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(body["content"]).decode()
            return 201, {"sha": sha}
        if method == "POST" and url.endswith("/git/trees"):
            return 201, {"sha": "tree1"}
        if method == "POST" and url.endswith("/git/commits"):
            return 201, {"sha": "commit1"}
        if method == "PATCH" and "/git/refs/heads/" in url:
            return 200, {"object": {"sha": body["sha"]}}
        if method == "GET" and url.endswith("/contents/README.md"):
            return 200, {"sha": "readme-sha"}
        if method == "PUT" and url.endswith("/contents/README.md"):
            return 200, {}
        if method == "PATCH" and url.count("/") == 5:   # /repos/{owner}/{repo}: visibility
            self.private = body["private"]
            full = url.split("/repos/")[1]
            return 200, {"full_name": full, "private": body["private"], "html_url": f"https://github.com/{full}"}
        if method == "GET" and "/repos/" in url and url.count("/") == 5:
            return 200, {"default_branch": "main", "private": self.private}
        return 404, {}

    def of(self, method, frag):
        return [c for c in self.calls if c[0] == method and frag in c[1]]


IDEA = {"title": "Spend stats CLI", "pitch": "Summarises spending numbers.", "tech": "Python, pytest",
        "skills": ["Python", "Testing"], "repo": "spend-stats-cli"}


def _connect(A, monkeypatch, gh):
    import ui.github_publish as G
    monkeypatch.setattr(G, "validate_token", lambda t: {"ok": True, "login": "octocat"})
    A.app.test_client().post("/api/github", json={"token": "ghp_fixture"})
    monkeypatch.setattr(G, "FETCH", gh)


def _project(c, pid):
    return next(p for p in c.get("/api/projects").get_json()["projects"] if p["id"] == pid)


def _learn_all(c, pid):
    d = c.post(f"/api/projects/{pid}/learn").get_json()
    A_steps = d["steps"]
    import ui.app as A
    full = next(p for p in A._load_projects() if p["id"] == pid)["learn"]["steps"]
    for i, s in enumerate(full):
        ans = s["check"]["answer"] if s["check"]["kind"] == "choice" else s["check"]["accept"][0]
        assert c.post(f"/api/projects/{pid}/learn/{i}", json={"answer": ans}).get_json()["correct"] is True
    return A_steps


def _quiz_answers(pid, right=20):
    """The shown option index for each question of the open attempt: ``right`` correct, the
    rest wrong. Read from the server's own store (the client never gets the key)."""
    import ui.app as A
    qz = next(p for p in A._load_projects() if p["id"] == pid)["quiz"]
    att, qs = qz["attempt"], qz["questions"]
    out = []
    for k, qi in enumerate(att["order"]):
        good = att["opts"][k].index(qs[qi]["answer"])
        out.append(good if k < right else (good + 1) % 4)
    return att["id"], out


def _defend(c, pid, right=20):
    opened = c.post(f"/api/projects/{pid}/quiz").get_json()
    aid, answers = _quiz_answers(pid, right)
    assert aid == opened["attempt"]
    return opened, c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": aid, "answers": answers}).get_json()


# ---- Build -----------------------------------------------------------------------------------
def test_build_with_github_counts_one_package_and_pushes_one_private_commit(client, monkeypatch):
    A, c = client
    gh = GitData()
    _connect(A, monkeypatch, gh)
    d = c.post("/api/projects/start", json={"idea": IDEA}).get_json()
    pid = d["project"]["id"]
    assert A._test_counted == [1]                       # one package for the build
    p = _project(c, pid)
    assert p["origin"] == "ai" and p["target"] == "github" and p["build"]["state"] == "ready"
    assert p["stage"] == "learn" and p["learn"]["total"] >= 5 and p["repo_private"] is True

    # the repo is PRIVATE and auto-initialised, then exactly one commit through the Git Data API
    create = gh.of("POST", "/user/repos")
    assert len(create) == 1 and create[0][2]["private"] is True and create[0][2]["auto_init"] is True
    blobs, trees = gh.of("POST", "/git/blobs"), gh.of("POST", "/git/trees")
    commits, refs = gh.of("POST", "/git/commits"), gh.of("PATCH", "/git/refs/heads/main")
    assert len(trees) == 1 and len(commits) == 1 and len(refs) == 1
    assert len(blobs) == len(trees[0][2]["tree"]) == 7
    assert commits[0][2]["parents"] == ["init0"] and commits[0][2]["tree"] == "tree1"
    assert refs[0][2]["sha"] == "commit1"
    order = [i for i, cl in enumerate(gh.calls) if "/git/" in cl[1] and cl[0] != "GET"]
    kinds = [gh.calls[i][1].rsplit("/git/", 1)[1].split("/")[0] for i in order]
    assert kinds == ["blobs"] * 7 + ["trees", "commits", "refs"]
    assert gh.private is True
    assert not [cl for cl in gh.calls if cl[0] == "PATCH" and cl[1].endswith("/spend-stats-cli")]   # stays private

    # the README has every section and the AI line near the top, by default
    readme = next(v for v in gh.blobs.values() if v.startswith("# Spend stats CLI"))
    assert readme.splitlines()[2] == "> Built with an AI pair-programmer."
    for head in ("## Problem", "## Approach", "## Tech stack", "## How to run", "## Screenshots", "## What I learned"):
        assert head in readme
    paths = {t["path"] for t in trees[0][2]["tree"]}
    assert {"README.md", ".gitignore", "requirements.txt", "tests/test_core.py"} <= paths

    # token counts are logged on the build
    tok = next(x for x in A._load_projects() if x["id"] == pid)["build"]["tokens"]
    assert tok["total"] > 0 and tok["calls"] >= 3 and tok["budget"] == 150_000


def test_upgrade_prompt_when_no_packages_left(client, monkeypatch):
    A, c = client
    from llm.broker_client import BrokerUnavailable

    def exhausted(llm):
        raise BrokerUnavailable("You've used the 3 free tailored packages this month.", "upgrade_required")
    monkeypatch.setattr(A, "_count_package", exhausted)
    r = c.post("/api/projects/start", json={"idea": IDEA, "local": True})
    assert r.status_code == 503 and r.get_json()["reason"] == "upgrade_required"
    assert c.get("/api/projects").get_json()["projects"] == []     # nothing created, nothing built


def test_failed_build_retries_without_a_second_charge(client, monkeypatch):
    A, c = client
    from llm.base import FakeLLM
    monkeypatch.setattr(FakeLLM, "plan_project_files", lambda self, *a, **k: {"files": []})
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    p = _project(c, pid)
    assert p["stage"] == "failed" and p["build"]["error"] and A._test_counted == [1]
    # two more tries ride on the package already paid for
    for n in (2, 3):
        c.post(f"/api/projects/{pid}/build")
        assert A._test_counted == [1]
        assert next(x for x in A._load_projects() if x["id"] == pid)["package"]["generations"] == n
    # past _BUILD_ATTEMPTS, a rebuild is a new package
    c.post(f"/api/projects/{pid}/build")
    assert A._test_counted == [1, 1]


def test_push_failure_keeps_files_and_retry_only_pushes(client, monkeypatch):
    A, c = client
    gh = GitData()
    _connect(A, monkeypatch, gh)
    real = gh.__call__
    state = {"fail": True}

    def flaky(method, url, token="", body=None, params=None):
        if state["fail"] and url.endswith("/git/commits"):
            gh.calls.append((method, url, body))
            return 500, {}
        return real(method, url, token, body, params)
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", flaky)
    pid = c.post("/api/projects/start", json={"idea": IDEA}).get_json()["project"]["id"]
    p = _project(c, pid)
    assert p["stage"] == "failed" and p["has_files"] is True
    from llm.base import FakeLLM
    monkeypatch.setattr(FakeLLM, "plan_project_files", lambda *a, **k: pytest.fail("no regeneration"))
    state["fail"] = False
    c.post(f"/api/projects/{pid}/build")
    assert _project(c, pid)["build"]["state"] == "ready" and A._test_counted == [1]
    assert len(gh.of("POST", "/user/repos")) == 1                 # the same repo, not a second


def test_build_without_github_keeps_files_here_and_zips(client, monkeypatch):
    A, c = client
    gh = GitData()
    import ui.github_publish as G
    monkeypatch.setattr(G, "FETCH", gh)
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    p = _project(c, pid)
    assert p["target"] == "local" and p["build"]["state"] == "ready" and p["repo_url"] == ""
    assert gh.calls == []                                            # GitHub never touched
    files = A._build_dir(pid) / "files"
    assert (files / "tracker" / "core.py").exists() and (files / "README.md").exists()
    r = c.get(f"/api/projects/{pid}/download.zip")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert "spend-stats-cli/tracker/core.py" in names and "spend-stats-cli/.gitignore" in names
    # the old hand-built start is still there on request
    d = c.post("/api/projects/start", json={"idea": {**IDEA, "title": "By hand"}, "build": False}).get_json()
    assert d["project"]["origin"] == "manual"


# ---- Validation ------------------------------------------------------------------------------
def test_validation_rejects_secrets_paths_sizes_and_bad_code():
    from ui import project_builder as PB
    assert PB.check_file("app.py", 'KEY = "sk-ant-api03-abcdefghijklmnopqrstuv"\n')
    assert PB.check_file("app.py", 'AWS = "AKIAABCDEFGHIJKLMNOP"\n')
    assert PB.check_file("cfg.py", 'password = "Zr4LmP0vXk2Jw9qT"\n')
    assert PB.check_file("k.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    assert not PB.check_file("cfg.py", 'import os\npassword = os.environ.get("DB_PASSWORD", "change-me-please")\n')
    assert PB.check_path("/etc/passwd") and PB.check_path("C:/x.py") and PB.check_path("../up.py")
    assert PB.check_path(".env") and PB.check_path("certs/server.pem") and PB.check_path("logo.png")
    assert PB.check_file("load.py", 'DATA = "/Users/kofi/Desktop/data.csv"\n')
    assert PB.check_file("big.py", "x = 1\n" * 4000)                      # over 16 KB
    many = [{"path": f"m{i}.py", "content": "x = 1\n" * 1600} for i in range(7)]
    assert any("KB in all" in e for e in PB.check_project(many))           # over 60 KB total
    assert PB.check_file("bad.py", "def broken(:\n")
    assert PB.check_file("bad.json", "{not json")
    assert PB.check_file("net.py", "import requests\nDATA = requests.get('https://x.example').json()\n")
    assert not PB.check_file("net_ok.py", "import requests\n\ndef fetch(u):\n    return requests.get(u)\n")
    assert PB.check_file("me.py", "AUTHOR = 'Ada Lovelace'\n", personal=["Ada Lovelace"])
    errs = PB.check_project([{"path": "a.py", "content": "x = 1\n"}])
    assert {"no README.md", "no .gitignore", "no unit tests"} <= set(errs)


def test_build_stops_on_a_file_that_stays_invalid(client, monkeypatch):
    A, c = client
    from llm.base import FakeLLM
    real = FakeLLM.write_project_files

    def leaky(self, project, plan, paths, written, fix=None):
        out = real(self, project, plan, paths, written, fix)
        for f in out["files"]:
            if f["path"] == "tracker/core.py":
                f["content"] += '\nTOKEN = "ghp_' + "a" * 36 + '"\n'
        return out
    monkeypatch.setattr(FakeLLM, "write_project_files", leaky)
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    p = _project(c, pid)
    assert p["stage"] == "failed" and "GitHub token" in p["build"]["error"]
    assert not (A._build_dir(pid) / "files").exists()                     # nothing written


# ---- Learn it --------------------------------------------------------------------------------
def test_learn_it_steps_come_from_the_real_files_and_progress_is_saved(client, monkeypatch):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    d = c.post(f"/api/projects/{pid}/learn").get_json()
    steps = d["steps"]
    assert 5 <= len(steps) <= 8
    for s in steps:
        real = (A._build_dir(pid) / "files" / s["path"]).read_text().splitlines()
        assert s["code"] == "\n".join(real[s["start"] - 1:s["end"]])        # excerpt cut from disk
        assert "answer" not in s["check"] and s["check"]["q"]               # key stays server-side
    # a wrong answer is not learned; a right one is, and it is saved
    r = c.post(f"/api/projects/{pid}/learn/0", json={"answer": 2}).get_json()
    assert r["correct"] is False and r["explain"]
    assert c.post(f"/api/projects/{pid}/learn/0", json={"answer": 0}).get_json()["correct"] is True
    assert c.post(f"/api/projects/{pid}/learn/2", json={"answer": "It exports summarize"}).get_json()["correct"]
    p = _project(c, pid)
    assert p["learn"] == {"total": len(steps), "learned": 2} and p["stage"] == "learn"
    # generated once and cached: a second open makes no model call
    from llm.base import FakeLLM
    monkeypatch.setattr(FakeLLM, "generate_learn_steps", lambda *a, **k: pytest.fail("regenerated"))
    assert len(c.post(f"/api/projects/{pid}/learn").get_json()["steps"]) == len(steps)
    # Defend it waits for Learn it
    assert c.post(f"/api/projects/{pid}/quiz").status_code == 409


# ---- Defend it and the gate ------------------------------------------------------------------
def test_defend_it_quiz_verifies_and_the_gate_holds_until_then(client, monkeypatch):
    A, c = client
    gh = GitData()
    _connect(A, monkeypatch, gh)
    pid = c.post("/api/projects/start", json={"idea": IDEA}).get_json()["project"]["id"]

    # the gate: nothing unverified goes public or onto the resume
    assert c.post(f"/api/projects/{pid}/publish").status_code == 400
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 400
    assert gh.private is True

    _learn_all(c, pid)
    assert _project(c, pid)["stage"] == "defend"
    # a miss keeps the gate shut
    _, miss = _defend(c, pid, right=13)
    assert miss["passed"] is False and _project(c, pid)["status"] == "draft"
    assert c.post(f"/api/projects/{pid}/publish").status_code == 400
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 400
    # a pass verifies
    _, good = _defend(c, pid, right=14)
    p = _project(c, pid)
    assert good["passed"] is True and p["status"] == "verified" and p["stage"] == "verified"
    assert p["defense"] == {"score": 14, "total": 20, "pass_mark": 14, "passed": True, "tried": True}

    # Verified: publish flips the same private repo (README untouched: the line stays on)
    r = c.post(f"/api/projects/{pid}/publish").get_json()
    assert r["url"] == "https://github.com/octocat/spend-stats-cli" and gh.private is False
    assert gh.of("PUT", "/contents/README.md") == []
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 200
    entry = next(e for e in A._memory().load("default")["profile"]["projects"] if e["org"] == IDEA["title"])
    assert entry["link"] == "https://github.com/octocat/spend-stats-cli"


def test_ai_line_toggle_updates_the_readme_at_publish(client, monkeypatch):
    A, c = client
    gh = GitData()
    _connect(A, monkeypatch, gh)
    pid = c.post("/api/projects/start", json={"idea": IDEA}).get_json()["project"]["id"]
    c.post(f"/api/projects/{pid}/settings", json={"ai_credit": False})
    local = (A._build_dir(pid) / "files" / "README.md").read_text()
    assert "AI pair-programmer" not in local and local.startswith("# Spend stats CLI")
    assert gh.of("PUT", "/contents/README.md") == []                 # GitHub untouched until publish
    _learn_all(c, pid)
    _defend(c, pid)
    c.post(f"/api/projects/{pid}/publish")
    put = gh.of("PUT", "/contents/README.md")
    assert len(put) == 1 and put[0][2]["sha"] == "readme-sha"
    assert "AI pair-programmer" not in base64.b64decode(put[0][2]["content"]).decode()
    i_put = gh.calls.index(put[0])
    i_pub = next(i for i, cl in enumerate(gh.calls) if cl[0] == "PATCH" and cl[1].endswith("/spend-stats-cli"))
    assert i_put < i_pub                                             # README fixed before it goes public


def test_local_build_published_later_creates_the_repo_with_one_commit(client, monkeypatch):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    _learn_all(c, pid)
    _defend(c, pid)
    gh = GitData()
    _connect(A, monkeypatch, gh)
    r = c.post(f"/api/projects/{pid}/publish").get_json()
    assert r["ok"] and gh.private is False
    assert len(gh.of("POST", "/user/repos")) == 1 and len(gh.of("POST", "/git/commits")) == 1
    readme = next(v for v in gh.blobs.values() if v.startswith("# Spend stats CLI"))
    assert "> Built with an AI pair-programmer." in readme


def test_readme_line_helpers():
    from ui import project_builder as PB
    on = PB.with_ai_line("# T\n\nPitch.\n\n## Problem\n\nx\n")
    assert on.splitlines()[:3] == ["# T", "", PB.AI_LINE]
    assert PB.with_ai_line(on, True) == on                           # idempotent
    off = PB.with_ai_line(on, False)
    assert PB.AI_LINE not in off and off.startswith("# T\n\nPitch.")
