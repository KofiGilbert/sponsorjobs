"""Run one bounded batch of new-board discovery in CI and hand the result over as a pull request.

The GitHub Actions workflow .github/workflows/grow-boards.yml calls this; it can also be run by
hand (with --dry-run it changes nothing outside the working tree). One run:

  1. restores the discovery checkpoint (data/grow_watchlist.jsonl's CI copy) from the
     `grow-boards-state` branch, so the batch continues down the ranked sponsor list instead of
     re-probing its top: employers already probed, skipped, or found on a blocked host are not
     asked again (sourcing/growth.py Checkpoint);
  2. first finishes what earlier runs left owing: an employer whose probe on a slow lane
     (Workable's 4-second spacing) was deferred is asked on just that ATS, as a single-lane
     pass that waits its turn instead of deferring again (--owed-only, up to --owed-share of
     the time cap). Without this the owed employers would fill every later batch;
  3. runs scripts/grow_watchlist.py for the next --limit employers under the rest of the time
     cap (robots.txt obeyed, blocked hosts never re-asked, per-host spacing and 429 back-off as
     always), counting each new board's US jobs and leaving out boards with none;
  4. saves the grown checkpoint back to that branch, even when the batch failed part-way,
     so the next run resumes where this one stopped;
  5. if seed/watchlist_seed.csv or seed/watchlist_evidence.csv changed, force-pushes them to the
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
import time
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


def owed_atss(checkpoint: Path, atss=None) -> tuple[list[str], int]:
    """The ATSs (among `atss`) earlier runs left owing, and how many employers owe one."""
    from sourcing import growth
    from sourcing.discover import DISCOVER_ATS
    atss = tuple(atss or DISCOVER_ATS)
    if not checkpoint.exists():
        return [], 0
    pending = growth.Checkpoint(checkpoint).pending(atss)
    # Only what a run was cut short on (unprobed_ats), not an ATS a record predates.
    owed = {a for r in growth.Checkpoint(checkpoint).records.values()
            if r.get("norm") in pending for a in (r.get("unprobed_ats") or ())}
    return [a for a in atss if a in owed], sum(
        1 for n, r in growth.Checkpoint(checkpoint).records.items()
        if n in pending and set(r.get("unprobed_ats") or ()) & owed)


def checkpoint_totals(checkpoint: Path) -> dict[str, int]:
    from sourcing import growth
    if not checkpoint.exists():
        return {}
    out: dict[str, int] = {}
    for rec in growth.Checkpoint(checkpoint).records.values():
        out[rec.get("status") or "?"] = out.get(rec.get("status") or "?", 0) + 1
    return out


def report(summary: dict, totals: dict[str, int], limit: int, owed: dict | None = None) -> str:
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
        *([f"- Owed probes finished first: {owed.get('probed', 0)} employers on "
            f"{', '.join(owed.get('atss') or [])} ({owed.get('requests', 0)} requests, "
            f"stopped: {owed.get('stopped')})"] if owed else []),
        *([f"- Left out (no US jobs): " + ", ".join(
            f"{e['company']} ({e['ats']}:{e['board_id']})" for e in summary['left_out'])]
          if summary.get("left_out") else []),
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
        made = sh(["gh", "pr", "create", "--head", branch, "--base", base, "--title", PR_TITLE,
                   "--body-file", f.name], check=False)
        if made.returncode:
            # Usually the repository setting "Allow GitHub Actions to create and approve pull
            # requests" (Settings > Actions > General > Workflow permissions) is off. The branch
            # is pushed and the checkpoint saved, so nothing is lost: open the PR by hand, or
            # turn the setting on and the next run opens it.
            raise SystemExit(
                f"could not open the pull request ({made.stderr.strip()}).\n"
                f"The new rows are on the {branch} branch. Open a PR from it by hand, or turn on "
                "Settings > Actions > General > 'Allow GitHub Actions to create and approve pull "
                "requests' so the next run opens it.")
        url = made.stdout.strip()
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
    ap.add_argument("--owed-share", type=float, default=0.4,
                    help="share of --max-minutes the owed-probe pass may use (default 0.4)")
    ap.add_argument("--skip-ats", default="workable",
                    help="comma list of ATSs CI batches leave out ('none' = probe all). Default "
                         "workable: its API answered 429 after ~2 employers a minute even at 4 s "
                         "spacing (run 39528717978: 86 employers in 50 minutes), it holds 1 of the "
                         "1,385 watched boards, and its owed probes stalled the whole list")
    ap.add_argument("--min-us-jobs", type=int, default=1,
                    help="leave out new boards with fewer US jobs than this (default 1)")
    args = ap.parse_args(argv)

    checkpoint = Path(args.state_dir) / STATE_FILE
    summary_path = checkpoint.with_suffix(".summary.json")
    if not args.no_restore:
        n = restore_state(checkpoint, args.remote)
        print(f"restored {n} checkpoint records from {STATE_BRANCH}")

    import grow_watchlist  # scripts/ is on sys.path when this file runs as a script
    common = ["--workers", str(args.workers), "--checkpoint", str(checkpoint)]
    if args.ipv4:
        common.append("--ipv4")
    if args.dry_run:
        common.append("--dry-run")
    grow_argv = ["--limit", str(args.limit), "--count-us", "--min-us-jobs", str(args.min_us_jobs),
                 *common]
    if args.cap_exempt_only:
        grow_argv.append("--cap-exempt-only")
    from sourcing.discover import DISCOVER_ATS
    skip = {a.strip().lower() for a in args.skip_ats.split(",")} - {"none", ""}
    run_atss = [a for a in DISCOVER_ATS if a not in skip]
    common += ["--ats", ",".join(run_atss)]
    grow_argv += ["--ats", ",".join(run_atss)]
    rc, owed_summary = 1, None
    t0 = time.monotonic()
    try:
        atss, n_owed = owed_atss(checkpoint, run_atss)
        if atss and n_owed:
            print(f"finishing {n_owed} employers that still owe {', '.join(atss)} first", flush=True)
            rest = [c for c in common if c != "--dry-run"]
            rest = rest[:rest.index("--ats")] + rest[rest.index("--ats") + 2:]
            rc = grow_watchlist.main(["--owed-only", "--ats", ",".join(atss), "--limit", str(n_owed),
                                      "--max-minutes", f"{args.max_minutes * args.owed_share:g}",
                                      "--dry-run", *rest])
            if summary_path.exists():
                s = json.loads(summary_path.read_text())
                owed_summary = {"atss": atss, "probed": s.get("probed"),
                                "requests": s.get("requests"), "stopped": s.get("stopped")}
        left = max(args.max_minutes - (time.monotonic() - t0) / 60, 5)
        rc = grow_watchlist.main([*grow_argv, "--max-minutes", f"{left:.0f}"])
    finally:
        # Progress is saved even when the batch dies part-way: the next run resumes from it.
        if not args.dry_run:
            save_state(checkpoint, summary_path, args.remote,
                       message=f"Discovery checkpoint after a batch of {args.limit}")
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    body = report(summary, checkpoint_totals(checkpoint), args.limit, owed_summary)
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
