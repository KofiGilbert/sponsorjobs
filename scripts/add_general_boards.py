#!/usr/bin/env python3
"""Add job boards of employers WITHOUT a sponsorship record to the watchlist (GREEN lane,
CLAUDE.md section 6).

Until 2026-11-25 a board joined the watchlist only when its employer had an H-1B or PERM record:
discovery walked the sponsor list, and a live board of any other employer was left out (57 of the
99 live boards found from the remoteintech list on 2026-10-30, for one). The board now serves
every job seeker, so those boards are wanted. This script is the one way in for them:

    # employer names, one per line: each is looked for on the public ATS APIs
    python scripts/add_general_boards.py --names names.txt --source my-list

    # boards already known: "ats,board_id,company" lines, or board URLs, one per line
    python scripts/add_general_boards.py --boards boards.csv --source my-list

    # nothing is written with --dry-run; --max-minutes caps the run (it can be re-run)
    python scripts/add_general_boards.py --names names.txt --max-minutes 5 --dry-run

What it does, per employer: finds the board with the same identity-checked discovery the sponsor
lane uses (sourcing.discover.discover_for_company), or reads the given board once; counts its
jobs and its US jobs with one list read; leaves out a board already watched, an empty board and
one with fewer than --min-us-jobs US jobs. Every request goes through sourcing.growth.PoliteFetch:
robots.txt obeyed, one host one request at a time, 429 back-off, a host that refuses is not asked
again, the crawl's own identifying User-Agent. Only the ATS vendors' public job-board APIs are
asked (Greenhouse, Lever, Ashby, Workable, Recruitee, Workday); SmartRecruiters stays off.

What it writes: the board into seed/watchlist_seed.csv and its evidence into
seed/watchlist_evidence.csv (as the sponsor lane does), and, when the employer has no sponsorship
record in seed/sponsors_seed.csv.gz, a line in seed/watchlist_general.csv. That last file is what
keeps the board honest: sourcing.quality.is_sponsor_relevant marks rows from a listed board as
general rows (shown to people who need no sponsorship) unless the employer carries a sponsor badge
or the ad itself offers sponsorship.

The names and board ids you feed it must come from a source that permits the use (a public list
under an open licence, an employer's own careers page): never a job board's listings.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing import growth  # noqa: E402
from sourcing.ats import ats_disabled, valid_board_id_for  # noqa: E402
from sourcing.discover import DISCOVER_ATS, discover_for_company  # noqa: E402
from sourcing.quality import GENERAL_BOARDS_SEED  # noqa: E402
from sourcing.sponsors import normalize_employer  # noqa: E402

# A board URL -> (ats, board_id). Only the employer-board hosts of the ATSs we already read.
_BOARD_URL = (
    ("greenhouse", re.compile(r"^https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/"
                              r"(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"^https?://jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"^https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"^https?://apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"^https?://([a-z0-9-]+)\.recruitee\.com", re.I)),
)
_WORKDAY_URL = re.compile(r"^https?://([a-z0-9-]+)\.(wd\d{1,3})\.myworkdayjobs\.com/"
                          r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)
_NOT_A_BOARD = {"embed", "jobs", "j", "api", "careers", "www"}


def parse_board_url(url: str) -> tuple[str, str] | None:
    """(ats, board_id) for a public board URL of a supported ATS, else None."""
    url = str(url or "").strip()
    m = _WORKDAY_URL.match(url)
    if m:
        board = f"{m.group(1).lower()}.{m.group(2).lower()}/{m.group(3)}"
        return ("workday", board) if valid_board_id_for("workday", board) else None
    for ats, rx in _BOARD_URL:
        m = rx.match(url)
        if m and m.group(1).lower() not in _NOT_A_BOARD and valid_board_id_for(ats, m.group(1)):
            return ats, m.group(1)
    return None


def read_names(path) -> list[str]:
    """Employer names, one per line; blank lines and # comments skipped; order kept, no repeats."""
    seen, out = set(), []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#") and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def read_boards(path) -> list[dict]:
    """Known boards: each line is "ats,board_id,company" or "url[,company]". Bad lines skipped."""
    out = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            row = [c.strip() for c in row]
            if not row or not row[0] or row[0].startswith("#") or row[0].lower() == "ats":
                continue
            if re.match(r"^https?://", row[0], re.I):
                hit = parse_board_url(row[0])
                if hit:
                    out.append({"ats": hit[0], "board_id": hit[1],
                                "company": row[1] if len(row) > 1 and row[1] else hit[1]})
            elif len(row) >= 2 and valid_board_id_for(row[0].lower(), row[1]):
                out.append({"ats": row[0].lower(), "board_id": row[1],
                            "company": row[2] if len(row) > 2 and row[2] else row[1]})
    return out


def run(names=(), boards=(), *, seed=growth.WATCHLIST_SEED, evidence=growth.WATCHLIST_EVIDENCE,
        general=GENERAL_BOARDS_SEED, sponsors=growth.SPONSOR_SEED, fetch=None,
        discover=discover_for_company, count=None, source: str = "", min_us_jobs: int = 1,
        max_seconds: float | None = None, dry_run: bool = False, atss=DISCOVER_ATS,
        clock=time.monotonic, log=print, today: str | None = None, workers: int = 1) -> dict:
    """Find / verify the boards, then (unless dry_run) write the seed, the evidence and the
    general list in one go. `fetch` is the polite fetch every request goes through; `discover`
    and `count(ats, board_id, company) -> (jobs, us_jobs)` are injectable for tests. Returns a
    summary; `added` lists the new boards with `general` True when the employer has no record."""
    today = today or date.today().isoformat()
    before = growth.read_watchlist_seed(seed)
    known = {(r["ats"], r["board_id"].lower()) for r in before}
    watched = growth.watched_norms(before)
    record = {r["norm_name"]: r for r in growth.load_sponsor_seed(sponsors)}
    if count is None:
        def count(ats, board_id, company):
            from sourcing.ats import normalize_jobs
            from sourcing.sponsors import looks_us
            from sourcing.workday import LIST_CEILING
            kw = {"max_jobs": LIST_CEILING} if ats == "workday" else {}
            rows = normalize_jobs(ats, board_id, company, fetch=fetch, **kw)
            return len(rows), sum(1 for r in rows if looks_us(r.get("location") or ""))
    t0 = clock()
    summary = {"names": 0, "boards": 0, "found": 0, "already_watched": 0, "no_board": 0,
               "unreadable": 0, "too_few_us": 0, "added": [], "stopped": ""}

    def out_of_time() -> bool:
        if max_seconds is not None and clock() - t0 >= max_seconds:
            summary["stopped"] = "time cap"
            return True
        return False

    candidates: list[dict] = []
    for b in boards:
        if ats_disabled(b["ats"]):
            continue                                   # robots.txt says no: never asked
        summary["boards"] += 1
        candidates.append(dict(b))

    def probe(name: str):
        if out_of_time():
            return name, "late"
        try:
            return name, (discover(name, atss=atss, fetch=fetch) if fetch else discover(name, atss=atss))
        except Exception as exc:                       # noqa: BLE001 - one employer never stops the run
            log(f"  {name}: probe failed ({str(exc)[:80]})")
            return name, None

    todo = []
    for name in names:
        if normalize_employer(name) in watched:
            summary["already_watched"] += 1
        else:
            todo.append(name)
    # Employers are asked side by side (the polite fetch still gives each HOST one request at a
    # time); results are taken in the list's own order, so a run is repeatable.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for name, hit in pool.map(probe, todo):
            if hit == "late":
                continue                               # the time cap: left for a re-run
            summary["names"] += 1
            if not hit:
                summary["no_board"] += 1
                continue
            summary["found"] += 1
            log(f"  FOUND {name} -> {hit['ats']}:{hit['board_id']}")
            candidates.append({"ats": hit["ats"], "board_id": hit["board_id"], "company": name,
                               "found": True})

    rows, ev_rows, gen_rows = [], [], []
    for c in candidates:
        # The time cap stops NEW work (name probes, boards from a long list). A board a probe
        # already found is always read: dropping it here would waste the requests that found it.
        found = c.pop("found", False)
        if not found and out_of_time():
            continue
        key = (c["ats"], c["board_id"].lower())
        if key in known:
            summary["already_watched"] += 1
            continue
        known.add(key)
        try:
            jobs, us = count(c["ats"], c["board_id"], c["company"])
        except Exception as exc:                       # noqa: BLE001
            summary["unreadable"] += 1
            log(f"  {c['company']} [{c['ats']}:{c['board_id']}]: unreadable ({str(exc)[:80]})")
            continue
        if jobs < 1 or us < max(0, min_us_jobs):
            summary["too_few_us"] += 1
            continue
        rec = record.get(normalize_employer(c["company"]))
        ev = growth.evidence_row({**c, "jobs": jobs, "verified": today}, rec or {})
        ev["source"] = source
        ev["us_jobs"] = us
        if rec is None:
            ev["h1b_approvals"], ev["perm_certs"] = 0, 0
            gen_rows.append(growth.general_board_row(ev, source=source, today=today))
        rows.append({"company": c["company"], "ats": c["ats"], "board_id": c["board_id"]})
        ev_rows.append(ev)
        summary["added"].append({**c, "jobs": jobs, "us_jobs": us, "general": rec is None})
        log(f"  ADD {c['company']} [{c['ats']}:{c['board_id']}] jobs={jobs} us={us}"
            f"{'' if rec else ' (no sponsorship record: general board)'}")
    if rows and not dry_run:
        growth.write_watchlist_seed(seed, before + rows)
        growth.write_evidence(evidence, ev_rows)
        if gen_rows:
            growth.write_general_boards(general, gen_rows)
    summary["general"] = len(gen_rows)
    summary["jobs"] = sum(a["jobs"] for a in summary["added"])
    summary["us_jobs"] = sum(a["us_jobs"] for a in summary["added"])
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--names", help="file of employer names, one per line")
    ap.add_argument("--boards", help='file of known boards: "ats,board_id,company" or a board URL per line')
    ap.add_argument("--source", default="", help="where the list came from (recorded as evidence)")
    ap.add_argument("--min-us-jobs", type=int, default=1, help="leave out boards with fewer US jobs")
    ap.add_argument("--max-minutes", type=float, default=20, help="stop after this long; re-run to continue")
    ap.add_argument("--dry-run", action="store_true", help="probe and report only; write nothing")
    ap.add_argument("--workers", type=int, default=8, help="employers probed side by side")
    ap.add_argument("--seed", default=str(growth.WATCHLIST_SEED))
    ap.add_argument("--evidence", default=None)
    ap.add_argument("--general", default=None)
    ap.add_argument("--sponsors", default=str(growth.SPONSOR_SEED))
    ap.add_argument("--ats", default=",".join(DISCOVER_ATS), help="ATSs a name is looked for on")
    a = ap.parse_args(argv)
    if not (a.names or a.boards):
        ap.error("give --names and / or --boards")
    seed = Path(a.seed)
    fetch = growth.PoliteFetch(robots=True)
    atss = tuple(x.strip().lower() for x in a.ats.split(",") if x.strip() and not ats_disabled(x.strip()))
    atss, denied = fetch.allowed_atss(atss)
    for ats, why in denied.items():
        print(f"robots.txt: leaving {ats} out of this run ({why})")
    s = run(read_names(a.names) if a.names else (), read_boards(a.boards) if a.boards else (),
            seed=seed, evidence=a.evidence or seed.with_name(growth.WATCHLIST_EVIDENCE.name),
            general=a.general or seed.with_name(GENERAL_BOARDS_SEED.name), sponsors=a.sponsors,
            fetch=fetch, source=a.source, min_us_jobs=a.min_us_jobs,
            max_seconds=a.max_minutes * 60, dry_run=a.dry_run, atss=tuple(atss), workers=a.workers)
    print(f"\n== add_general_boards{' [dry run]' if a.dry_run else ''} =="
          f"\nnames asked {s['names']}, boards given {s['boards']}, boards found by name {s['found']}"
          f"\nalready watched {s['already_watched']}, no board {s['no_board']}, unreadable "
          f"{s['unreadable']}, empty or under {a.min_us_jobs} US jobs {s['too_few_us']}"
          f"\nadded {len(s['added'])} boards ({s['general']} with no sponsorship record): "
          f"{s['jobs']} jobs listed, {s['us_jobs']} in the US"
          f"\nrequests {fetch.requests}, 429s {fetch.rate_limited}, cooling {fetch.cooling()}"
          + (f"\nstopped: {s['stopped']}" if s["stopped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
