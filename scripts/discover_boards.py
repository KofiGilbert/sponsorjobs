"""Discover public ATS boards for the top visa-sponsoring employers and add them to the
watchlist, so the app's job feed grows from a handful of seeded companies to thousands of
pre-vetted sponsors. GREEN lane only — see sourcing/discover.py for the how and why.

This hits the live internet (one small public request per probe), so run it occasionally
as a background pass, not on a web request. The web app then refreshes jobs from the
discovered boards exactly as it does for the seed companies.

    python scripts/discover_boards.py --limit 500 [--min-approvals 50] [--dry-run]
                                      [--delay 0.15] [--data-dir data]

--limit          how many top sponsors to probe (default 500)
--min-approvals  skip sponsors below this many H-1B approvals (default 1)
--dry-run        discover and report, but don't add anything to the watchlist
--delay          seconds to pause between probes, to stay polite (default 0.1)
--data-dir       where sponsors.db and resume_agent.db live (default: <repo>/data)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sourcing.discover import DISCOVER_ATS, discover_and_watch  # noqa: E402
from sourcing.sponsors import SponsorDB  # noqa: E402
from sourcing.watchlist import Watchlist  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--min-approvals", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    sponsor_db = SponsorDB(data / "sponsors.db")
    watchlist = Watchlist(data / "resume_agent.db")
    if sponsor_db.is_empty():
        print("No sponsor data found. Run the visa-data update first.", file=sys.stderr)
        return 1

    print(f"Probing top {args.limit} sponsors (>= {args.min_approvals} approvals) "
          f"across {', '.join(DISCOVER_ATS)}{' [dry run]' if args.dry_run else ''}...\n")

    def on_progress(i: int, name: str, hit) -> None:
        if hit:
            print(f"  [{i}] HIT  {name}  ->  {hit['ats']}:{hit['board_id']} "
                  f"({hit['jobs']} jobs)")
        elif i % 25 == 0:
            print(f"  [{i}] ...probed {i} so far")

    delay = max(0.0, args.delay)
    summary = discover_and_watch(
        sponsor_db, watchlist, limit=args.limit, min_approvals=args.min_approvals,
        sleep=(lambda: time.sleep(delay)) if delay else None,
        on_progress=on_progress, dry_run=args.dry_run, max_workers=args.workers)

    print(f"\nProbed {summary['probed']} sponsors -> {summary['hits']} live boards "
          f"-> {summary['added']} newly added to the watchlist.")
    if summary["hits"]:
        print("Run the app's job refresh to pull live roles from the new boards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
