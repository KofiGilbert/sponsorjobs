"""Run one bounded batch of new-board discovery in CI and hand the result over as a pull request.

The GitHub Actions workflow .github/workflows/grow-boards.yml calls this; it can also be run by
hand (with --dry-run it changes nothing outside the working tree). One run:

  1. restores the discovery checkpoint (data/grow_watchlist.jsonl's CI copy) from the
     `grow-boards-state` branch, so the batch continues down the ranked sponsor list instead of
     re-probing its top: employers already probed, skipped, or found on a blocked host are not
     asked again (sourcing/growth.py Checkpoint);
  2. runs scripts/grow_watchlist.py for the next --limit employers under a time cap (robots.txt
     obeyed, blocked hosts never re-asked, per-host spacing and 429 back-off as always);
  3. saves the grown checkpoint back to that branch, even when the batch failed part-way,
     so the next run resumes where this one stopped;
  4. if seed/watchlist_seed.csv or seed/watchlist_evidence.csv changed, force-pushes them to the
     `bot/grow-boards` branch and opens (or updates) one pull request with a table of the new
     boards. A PR opened with the workflow's GITHUB_TOKEN starts no workflows by itself, so this
     then dispatches the required checks (tests.yml, cla.yml) on that branch.

Why a branch and not the Actions cache for the checkpoint: the feed workflow saves a ~250 MB
cache every 3 hours, so the repository's 10 GB cache fills in about five days and a weekly
checkpoint would be evicted between runs. A branch never is.

    python scripts/grow_boards_ci.py --limit 300 --max-minutes 100
    python scripts/grow_boards_ci.py --limit 5 --max-minutes 5 --dry-run   # local proof run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATE_BRANCH = "grow-boards-state"
PR_BRANCH = "bot/grow-boards"
STATE_FILE = "grow_watchlist.jsonl"
SUMMARY_FILE = "grow_watchlist.summary.json"
SEED_FILES = ("seed/watchlist_seed.csv", "seed/watchlist_evidence.csv")
PR_TITLE = "Watchlist: new sponsor job boards from the weekly discovery batch"
CHECK_WORKFLOWS = ("tests.yml", "cla.yml")


def sh(cmd: list[str], check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command in the repo, echoing it. Tests replace this module-level function."""
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True, capture_output=True, input=input)


def restore_state(checkpoint: Path, remote: str = "origin", branch: str = STATE_BRANCH) -> int:
    """Copy the saved checkpoint from `branch` to `checkpoint`. Returns the records restored
    (0 on the first run, when the branch does not exist yet)."""
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if sh(["git", "fetch", "--depth=1", remote, f"refs/heads/{branch}"], check=False).returncode:
        print(f"no {branch} branch yet: starting from the top of the ranked list")
        return 0
    got = sh(["git", "show", f"FETCH_HEAD:{STATE_FILE}"], check=False)
    if got.returncode:
        return 0
    checkpoint.write_text(got.stdout, encoding="utf-8")
    return sum(1 for line in got.stdout.splitlines() if line.strip())


def save_state(checkpoint: Path, summary: Path, remote: str = "origin",
               branch: str = STATE_BRANCH, message: str = "Discovery checkpoint") -> str | None:
    """Commit the checkpoint (and the run summary) as the new tip of `branch` with git
    plumbing, never touching the working tree or the current branch, and push it."""
    if not checkpoint.exists():
        return None
    entries = []
    for path, name in ((checkpoint, STATE_FILE), (summary, SUMMARY_FILE)):
        if path.exists():
            blob = sh(["git", "hash-object", "-w", str(path)]).stdout.strip()
            entries.append(f"100644 blob {blob}\t{name}\n")
    tree = sh(["git", "mktree"], input="".join(entries)).stdout.strip()
    parent = sh(["git", "fetch", "--depth=1", remote, f"refs/heads/{branch}"], check=False)
    cmd = ["git", "commit-tree", tree, "-m", message]
    if parent.returncode == 0:
        cmd[3:3] = ["-p", sh(["git", "rev-parse", "FETCH_HEAD"]).stdout.strip()]
    commit = sh(cmd).stdout.strip()
    sh(["git", "push", remote, f"{commit}:refs/heads/{branch}"])
    return commit


def checkpoint_totals(checkpoint: Path) -> dict[str, int]:
    from sourcing import growth
    if not checkpoint.exists():
        return {}
    out: dict[str, int] = {}
    for rec in growth.Checkpoint(checkpoint).records.values():
        out[rec.get("status") or "?"] = out.get(rec.get("status") or "?", 0) + 1
    return out


def report(summary: dict, totals: dict[str, int], limit: int) -> str:
    """The PR body / job summary: what this batch did and every board it adds."""
    ev = summary.get("evidence") or []
    us = [int(e["us_jobs"]) for e in ev if e.get("us_jobs") not in (None, "")]
    blocked = summary.get("blocked") or []
    lines = [
        "Adds sponsor job boards found by the bounded discovery batch "
        "(`scripts/grow_boards_ci.py` -> `scripts/grow_watchlist.py`). Each board's identity "
        "was confirmed and its live public feed returned at least one job; its evidence row "
        "(URL, verified date, job count) is in `seed/watchlist_evidence.csv`. After merge the "
        "feed build's `sync_seed` picks the boards up.",
        "",
        "## This batch",
        f"- Employers probed: {summary.get('probed', 0)} (batch size {limit}; "
        f"{summary.get('skipped', 0)} skipped by the name filters without a request, "
        f"{summary.get('throttled', 0)} throttled and left for the next run)",
        f"- Requests: {summary.get('requests', 0)}; 429s: {summary.get('rate_limited', 0)}; "
        f"stopped: {summary.get('stopped')}; {summary.get('minutes', 0)} min",
        f"- New boards in this PR: {len(ev)} with {summary.get('new_jobs', 0)} listed jobs"
        + (f", {sum(us)} of them in the US" if us else ""),
        f"- Watchlist: {summary.get('before', 0)} -> {summary.get('after', 0)} boards",
        f"- Blocked hosts (401 / 403 / challenge, never asked again): {len(blocked)}",
        f"- robots.txt left out: {summary.get('robots_denied') or 'nothing'}",
    ]
    if totals:
        lines.append("- Checkpoint so far (all runs): " +
                     ", ".join(f"{k} {v}" for k, v in sorted(totals.items())))
    if ev:
        lines += ["", "## New boards", "",
                  "| Company | ATS | Board | Jobs | US jobs | Cap-exempt |",
                  "|---|---|---|---:|---:|---|"]
        for e in sorted(ev, key=lambda e: -int(e.get("jobs") or 0)):
            lines.append(f"| {e['company']} | {e['ats']} | `{e['board_id']}` | {e.get('jobs', '')} "
                         f"| {'' if e.get('us_jobs') is None else e['us_jobs']} "
                         f"| {'yes' if str(e.get('cap_exempt')) in ('1', 'True', 'true') else ''} |")
    return "\n".join(lines) + "\n"


def seed_changed() -> bool:
    return bool(sh(["git", "status", "--porcelain", "--", *SEED_FILES]).stdout.strip())


def open_or_update_pr(body: str, remote: str = "origin", branch: str = PR_BRANCH,
                      base: str = "main") -> str:
    sh(["git", "checkout", "-B", branch])
    sh(["git", "add", "--", *SEED_FILES])
    sh(["git", "commit", "-m", PR_TITLE])
    sh(["git", "push", "--force", remote, f"HEAD:refs/heads/{branch}"])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    found = sh(["gh", "pr", "list", "--head", branch, "--base", base, "--state", "open",
                "--json", "number", "--jq", ".[0].number"]).stdout.strip()
    if found:
        sh(["gh", "pr", "edit", found, "--title", PR_TITLE, "--body-file", f.name])
        url = sh(["gh", "pr", "view", found, "--json", "url", "--jq", ".url"]).stdout.strip()
    else:
        url = sh(["gh", "pr", "create", "--head", branch, "--base", base, "--title", PR_TITLE,
                  "--body-file", f.name]).stdout.strip()
    # A PR opened with GITHUB_TOKEN triggers no workflows; dispatch the required checks on the
    # branch so its head commit gets them (workflow_dispatch is the documented exception).
    for wf in CHECK_WORKFLOWS:
        sh(["gh", "workflow", "run", wf, "--ref", branch], check=False)
    return url


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--max-minutes", type=float, default=100)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cap-exempt-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and report, but write no seed rows, push nothing, open no PR "
                         "(the checkpoint is still restored, into --state-dir)")
    ap.add_argument("--state-dir", default=str(ROOT / ".grow-state"))
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--no-restore", action="store_true",
                    help="start from an empty checkpoint (or whatever is in --state-dir)")
    ap.add_argument("--ipv4", action="store_true")
    args = ap.parse_args(argv)

    checkpoint = Path(args.state_dir) / STATE_FILE
    summary_path = checkpoint.with_suffix(".summary.json")
    if not args.no_restore:
        n = restore_state(checkpoint, args.remote)
        print(f"restored {n} checkpoint records from {STATE_BRANCH}")

    import grow_watchlist  # scripts/ is on sys.path when this file runs as a script
    grow_argv = ["--limit", str(args.limit), "--max-minutes", str(args.max_minutes),
                 "--workers", str(args.workers), "--checkpoint", str(checkpoint), "--count-us"]
    if args.cap_exempt_only:
        grow_argv.append("--cap-exempt-only")
    if args.ipv4:
        grow_argv.append("--ipv4")
    if args.dry_run:
        grow_argv.append("--dry-run")
    rc = 1
    try:
        rc = grow_watchlist.main(grow_argv)
    finally:
        # Progress is saved even when the batch dies part-way: the next run resumes from it.
        if not args.dry_run:
            save_state(checkpoint, summary_path, args.remote,
                       message=f"Discovery checkpoint after a batch of {args.limit}")
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    body = report(summary, checkpoint_totals(checkpoint), args.limit)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write(body)
    print("\n" + body)
    if rc != 0:
        return rc
    if args.dry_run:
        print("[dry run] would save the checkpoint to the "
              f"{STATE_BRANCH} branch and, if the seed changed, open a PR from {PR_BRANCH}")
        return 0
    if not seed_changed():
        print("no new boards this batch: no pull request")
        return 0
    print(f"pull request: {open_or_update_pr(body, args.remote)}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
