"""The CI board-discovery batch (scripts/grow_boards_ci.py, .github/workflows/grow-boards.yml).

Pins, offline: successive bounded batches continue down the ranked sponsor list instead of
re-probing its top; hosts that refused us in an earlier batch are handed to the next one so they
are never asked again; the CI driver restores and saves the checkpoint on its state branch, opens
a PR only when the seed changed, and a --dry-run pushes nothing and opens nothing; and the
workflow file keeps its dispatch inputs, write permissions, time caps and the dispatched checks
a GITHUB_TOKEN pull request would otherwise never get.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from sourcing import growth

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
_JOB = [{"id": 1, "title": "Engineer", "location": {"name": "Remote - US"}}]


def _load(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GreenhouseOnly:
    """Public Greenhouse boards for the given slugs; every other request is a 404."""

    def __init__(self, boards):
        self.boards, self.urls = boards, []

    def __call__(self, url, timeout=20, **kw):
        import urllib.error
        self.urls.append(url)
        for slug, name in self.boards.items():
            if f"/boards/{slug}/jobs" in url:
                return {"jobs": _JOB}
            if url.rstrip("/").endswith(f"/boards/{slug}"):
                return {"name": name}
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)


def _sponsors(tmp_path, names):
    path = tmp_path / "sponsors.csv.gz"
    with gzip.open(path, "wt", newline="") as f:
        f.write("norm_name,display_name,h1b_approvals,h1b_last_fy,h1b_first_fy,naics,state,"
                "cap_exempt,e_verify,perm_certs\n")
        for i, name in enumerate(names):
            f.write(f"{growth.normalize_employer(name)},{name},{1000 - i * 100},2023,2020,51,NY,0,0,0\n")
    return path


# -- batching and the checkpoint ------------------------------------------------ #

def test_successive_batches_continue_down_the_ranked_list(tmp_path, monkeypatch):
    names = ["Datadog Inc", "Nobody Here Ltd", "Stripe Inc", "Plaid Inc"]
    sponsors = _sponsors(tmp_path, names)
    seed = tmp_path / "watchlist.csv"
    seed.write_text("company,ats,board_id\n")
    ats = GreenhouseOnly({"datadog": "Datadog", "stripe": "Stripe", "plaid": "Plaid"})
    real = growth.PoliteFetch

    def fetch_factory(**kw):
        kw.pop("fetch", None)
        return real(fetch=ats, sleep=lambda s: None, **kw)
    monkeypatch.setattr(growth, "PoliteFetch", fetch_factory)
    script = _load("grow_watchlist")
    cp = tmp_path / "cp.jsonl"
    base = ["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
            "--workers", "1", "--no-robots", "--ats", "greenhouse"]

    def asked():
        return {u.split("/boards/")[1].split("/")[0] for u in ats.urls if "/boards/" in u}

    assert script.main([*base, "--limit", "2"]) == 0
    first = asked()
    assert "datadog" in first and "stripe" not in first and "plaid" not in first
    assert [r["board_id"] for r in growth.read_watchlist_seed(seed)] == ["datadog"]

    ats.urls.clear()
    assert script.main([*base, "--limit", "2"]) == 0          # the NEXT two, not the top two again
    second = asked()
    assert "datadog" not in second and {"stripe", "plaid"} <= second
    assert {r["board_id"] for r in growth.read_watchlist_seed(seed)} == {"datadog", "stripe", "plaid"}
    statuses = {r["norm"]: r["status"] for r in growth.Checkpoint(cp).records.values()}
    assert sorted(statuses.values()) == ["hit", "hit", "hit", "miss"]

    ats.urls.clear()
    assert script.main([*base, "--limit", "2"]) == 0          # list exhausted: no requests
    assert ats.urls == []


def test_a_host_blocked_in_one_batch_is_never_asked_in_the_next(tmp_path, monkeypatch):
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    cp.record("acme", name="Acme", status="miss", probed_ats=["workday"],
              blocked_hosts=["acme.wd5.myworkdayjobs.com"])
    seen = {}
    real = growth.PoliteFetch

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)
    monkeypatch.setattr(growth, "PoliteFetch", Spy)
    monkeypatch.setattr(growth, "grow", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    script = _load("grow_watchlist")
    sponsors = _sponsors(tmp_path, ["Acme Inc"])
    script.main(["--sponsors", str(sponsors), "--seed", str(tmp_path / "w.csv"),
                 "--checkpoint", str(cp.path), "--no-robots", "--limit", "1"])
    assert seen["blocked_hosts"] == {"acme.wd5.myworkdayjobs.com"}
    p = real(fetch=lambda *a, **k: pytest.fail("asked a blocked host"),
                           sleep=lambda s: None, blocked_hosts=seen["blocked_hosts"])
    with pytest.raises(growth.HostBlocked):
        p("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/jobs")


# -- the CI driver -------------------------------------------------------------- #

class FakeGit:
    """Records every command; answers the few whose output the driver reads."""

    def __init__(self, state=None, seed_dirty=True, open_pr=""):
        self.cmds, self.state, self.seed_dirty, self.open_pr = [], state, seed_dirty, open_pr

    def __call__(self, cmd, check=True, input=None):
        import subprocess
        self.cmds.append(cmd)
        out, rc = "", 0
        if cmd[:2] == ["git", "fetch"]:
            rc = 0 if self.state is not None else 128
        elif cmd[:2] == ["git", "show"]:
            out = self.state or ""
        elif cmd[:2] == ["git", "hash-object"]:
            out = "b" * 40
        elif cmd[:2] == ["git", "mktree"]:
            out = "t" * 40
        elif cmd[:2] == ["git", "rev-parse"]:
            out = "p" * 40
        elif cmd[:2] == ["git", "commit-tree"]:
            out = "c" * 40
        elif cmd[:2] == ["git", "status"]:
            out = " M seed/watchlist_seed.csv\n" if self.seed_dirty else ""
        elif cmd[:3] == ["gh", "pr", "list"]:
            out = self.open_pr
        elif cmd[:3] == ["gh", "pr", "create"]:
            out = "https://github.com/o/r/pull/9\n"
        elif cmd[:3] == ["gh", "pr", "view"]:
            out = f"https://github.com/o/r/pull/{self.open_pr}\n"
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def ran(self, *prefix):
        return [c for c in self.cmds if c[:len(prefix)] == list(prefix)]


def _driver(monkeypatch, tmp_path, git, hits=1):
    ci = _load("grow_boards_ci")
    monkeypatch.setattr(ci, "sh", git)
    calls = []

    def fake_grow(argv):
        calls.append(argv)
        cp = Path(argv[argv.index("--checkpoint") + 1])
        growth.Checkpoint(cp).record("datadog", name="Datadog Inc", status="hit",
                                     ats="greenhouse", board_id="datadog", jobs=3)
        ev = [{"company": "Datadog Inc", "ats": "greenhouse", "board_id": "datadog", "jobs": 3,
               "us_jobs": 2, "cap_exempt": 0}][:hits]
        cp.with_suffix(".summary.json").write_text(json.dumps(
            {"probed": 5, "skipped": 0, "throttled": 0, "requests": 40, "rate_limited": 0,
             "stopped": None, "minutes": 1.0, "new_jobs": 3 * hits, "before": 10,
             "after": 10 + hits, "blocked": [], "evidence": ev}))
        return 0
    monkeypatch.setitem(sys.modules, "grow_watchlist", type(sys)("grow_watchlist"))
    sys.modules["grow_watchlist"].main = fake_grow
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return ci, calls


def test_dry_run_restores_the_checkpoint_but_pushes_nothing_and_opens_no_pr(tmp_path, monkeypatch):
    git = FakeGit(state='{"norm": "acme", "status": "miss"}\n')
    ci, calls = _driver(monkeypatch, tmp_path, git)
    assert ci.main(["--limit", "5", "--max-minutes", "5", "--dry-run",
                    "--state-dir", str(tmp_path / "st")]) == 0
    assert "--dry-run" in calls[0] and "--limit" in calls[0] and "5" in calls[0]
    restored = (tmp_path / "st" / "grow_watchlist.jsonl").read_text()
    assert '"acme"' in restored                             # continued from the saved state
    assert not git.ran("git", "push") and not git.ran("gh")


def test_a_real_run_saves_the_checkpoint_then_opens_one_pr_and_dispatches_its_checks(tmp_path, monkeypatch):
    git = FakeGit(state=None)                               # first run: no state branch yet
    ci, _ = _driver(monkeypatch, tmp_path, git)
    assert ci.main(["--limit", "300", "--state-dir", str(tmp_path / "st")]) == 0
    pushes = git.ran("git", "push")
    assert pushes[0][-1] == "c" * 40 + ":refs/heads/grow-boards-state"
    assert not [c for c in git.ran("git", "commit-tree") if "-p" in c]   # first state commit
    assert pushes[1][:3] == ["git", "push", "--force"] and pushes[1][-1].endswith("bot/grow-boards")
    assert git.ran("git", "add", "--") == [["git", "add", "--", *ci.SEED_FILES]]
    assert len(git.ran("gh", "pr", "create")) == 1
    assert [c[3] for c in git.ran("gh", "workflow", "run")] == ["tests.yml", "cla.yml"]


def test_an_open_pr_is_updated_and_the_state_commit_chains_on_the_last(tmp_path, monkeypatch):
    git = FakeGit(state="", open_pr="12")
    ci, _ = _driver(monkeypatch, tmp_path, git)
    assert ci.main(["--state-dir", str(tmp_path / "st")]) == 0
    assert [c for c in git.ran("git", "commit-tree") if "-p" in c]
    assert git.ran("gh", "pr", "edit") and not git.ran("gh", "pr", "create")


def test_no_seed_change_means_no_pr(tmp_path, monkeypatch):
    git = FakeGit(state="", seed_dirty=False)
    ci, _ = _driver(monkeypatch, tmp_path, git, hits=0)
    assert ci.main(["--state-dir", str(tmp_path / "st")]) == 0
    assert len(git.ran("git", "push")) == 1                 # the checkpoint only
    assert not git.ran("gh")


def test_the_report_lists_every_new_board_with_its_jobs():
    ci = _load("grow_boards_ci")
    body = ci.report({"probed": 300, "new_jobs": 50, "before": 1, "after": 2, "blocked": [{}],
                      "evidence": [{"company": "Datadog Inc", "ats": "greenhouse",
                                    "board_id": "datadog", "jobs": 50, "us_jobs": 40,
                                    "cap_exempt": "1"}]}, {"hit": 1, "miss": 299}, 300)
    assert "Employers probed: 300" in body and "40 of them in the US" in body
    assert "| Datadog Inc | greenhouse | `datadog` | 50 | 40 | yes |" in body
    assert "Blocked hosts (401 / 403 / challenge, never asked again): 1" in body
    assert "Generated with" not in body


# -- the workflow files --------------------------------------------------------- #

def _workflow(name):
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    return data, data.get("on", data.get(True))            # YAML 1.1 reads a bare `on` as True


def test_grow_boards_workflow_inputs_permissions_and_time_caps():
    wf, on = _workflow("grow-boards.yml")
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["limit"]["default"] == "300"
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
    assert set(inputs) >= {"limit", "max_minutes", "cap_exempt_only", "dry_run"}
    assert on["schedule"][0]["cron"]
    assert wf["permissions"] == {"contents": "write", "pull-requests": "write", "actions": "write"}
    assert wf["concurrency"]["group"] == "grow-boards"
    job = wf["jobs"]["grow"]
    # The probe cap plus counting and the PR must fit inside the job limit (and under 6 h).
    assert int(inputs["max_minutes"]["default"]) + 30 <= job["timeout-minutes"] <= 360
    run = "\n".join(s.get("run", "") for s in job["steps"])
    assert "scripts/grow_boards_ci.py" in run and "$DRY_RUN" in run
    assert "feed" not in run                                # never writes the feed


def test_the_required_checks_can_be_dispatched_on_the_bot_branch():
    _, tests_on = _workflow("tests.yml")
    assert "workflow_dispatch" in tests_on
    cla, cla_on = _workflow("cla.yml")
    assert "workflow_dispatch" in cla_on
    steps = cla["jobs"]["cla"]["steps"]
    assert "github-actions[bot]" in steps[0]["with"]["allowlist"]
    assert any("origin/main..HEAD" in s.get("run", "") for s in steps)


def test_owed_slow_lane_probes_are_finished_first_on_just_that_ats(tmp_path, monkeypatch):
    state = json.dumps({"norm": "acme", "name": "Acme", "status": "miss",
                        "probed_ats": ["greenhouse", "lever", "ashby", "workday"],
                        "unprobed_ats": ["workable"]}) + "\n"
    git = FakeGit(state=state, seed_dirty=False)
    ci, calls = _driver(monkeypatch, tmp_path, git)
    assert ci.main(["--limit", "300", "--max-minutes", "100",
                    "--state-dir", str(tmp_path / "st")]) == 0
    owed, batch = calls
    assert "--owed-only" in owed and owed[owed.index("--ats") + 1] == "workable"
    assert "--dry-run" in owed and "--count-us" not in owed          # probes only; batch writes
    assert float(owed[owed.index("--max-minutes") + 1]) == 40
    assert "--owed-only" not in batch and "--count-us" in batch
    assert batch[batch.index("--min-us-jobs") + 1] == "1"


def test_a_refused_pr_create_says_how_to_fix_it(tmp_path, monkeypatch):
    import subprocess

    class Refusing(FakeGit):
        def __call__(self, cmd, check=True, input=None):
            if cmd[:3] == ["gh", "pr", "create"]:
                self.cmds.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "not permitted to create PRs")
            return super().__call__(cmd, check, input)
    git = Refusing(state="")
    ci, _ = _driver(monkeypatch, tmp_path, git)
    with pytest.raises(SystemExit, match="Allow GitHub Actions to create"):
        ci.main(["--state-dir", str(tmp_path / "st")])
    assert git.ran("git", "push", "--force")                # the rows are on the branch
    assert not git.ran("gh", "workflow")


def test_a_new_board_with_no_us_jobs_is_left_out_and_not_added_back(tmp_path, monkeypatch):
    sponsors = _sponsors(tmp_path, ["Datadog Inc", "Stripe Inc"])
    seed = tmp_path / "watchlist.csv"
    seed.write_text("company,ats,board_id\n")
    ats = GreenhouseOnly({"datadog": "Datadog", "stripe": "Stripe"})
    real = growth.PoliteFetch
    monkeypatch.setattr(growth, "PoliteFetch",
                        lambda **kw: real(fetch=ats, sleep=lambda s: None,
                                          **{k: v for k, v in kw.items() if k != "fetch"}))
    monkeypatch.setattr(growth, "count_us_jobs",
                        lambda a, b, c="": 0 if b == "stripe" else 5)
    script = _load("grow_watchlist")
    cp = tmp_path / "cp.jsonl"
    argv = ["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
            "--workers", "1", "--no-robots", "--ats", "greenhouse", "--limit", "5",
            "--count-us", "--min-us-jobs", "1"]
    assert script.main(argv) == 0
    assert [r["board_id"] for r in growth.read_watchlist_seed(seed)] == ["datadog"]
    summary = json.loads(cp.with_suffix(".summary.json").read_text())
    assert [e["board_id"] for e in summary["left_out"]] == ["stripe"]
    assert growth.Checkpoint(cp).records["stripe"]["status"] == "miss"
    assert script.main(argv) == 0                          # the checkpointed hit stays out
    assert [r["board_id"] for r in growth.read_watchlist_seed(seed)] == ["datadog"]
