#!/usr/bin/env python3
"""Build the STATIC job feed: run the same crawl the hosted kitchen ran, then write the slim list,
the sharded descriptions and a manifest into an output directory for scripts/upload_feed.py.

    python scripts/build_feed.py --data .feed-data --out .feed-out \\
        --jobs-url https://tailor.example/feed/jobs.json.gz

Runs from a scheduled GitHub Action (.github/workflows/feed.yml). `--data` is the crawl's
persistent state (the jobs DB + sponsor overlay); keep it between runs (actions/cache) so
first_seen, dedup and the 21-day prune behave as they did on the server. Reuses, never copies:
sourcing.service.refresh_watchlist for the crawl and backend.feed.feed_jobs for the
sponsor-tagging / US-only / freshness shaping. GREEN-lane sources only (CLAUDE.md §6).

Aggregator policy (details + sources in docs/feed.md): keyless feeds whose API notices invite
sharing with attribution (Remotive, Remote OK) or set no restriction (freehire) are always in.
Keyed aggregators are governed by FEED_INCLUDE_AGGREGATORS: JSearch's publisher (OpenWeb Ninja)
grants redistribution inside a broader product, so it is included by default when RAPIDAPI_KEY is
set; Adzuna's developer terms forbid redistribution without written consent, so it is refused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Always in the public file (terms permit sharing further; see docs/feed.md).
PUBLIC_OK_AGGREGATORS = ("remotive", "remoteok", "freehire")
# Keyed aggregators that MAY be included when FEED_INCLUDE_AGGREGATORS names them.
REDISTRIBUTABLE_KEYED = ("jsearch",)
DEFAULT_INCLUDE = "jsearch"
# The board pass stops after this many minutes so the run always finishes inside the workflow's
# 120-minute limit. A run the limit kills saves nothing (actions/cache only saves on success), so
# every later run would start from zero and be killed too -- the first run on 2026-10-06 did that.
DEFAULT_CRAWL_MINUTES = 70
CURSOR_FILE = "crawl_cursor.json"     # where the next run resumes in the board list, under --data


def rotate_boards(boards: list[dict], data_dir) -> tuple[list[dict], int]:
    """The boards in crawl order for this run, starting where the last run stopped, and that start
    index. A capped run that always began at "A" would never reach the end of the list."""
    if not boards:
        return [], 0
    try:
        start = int(json.loads((Path(data_dir) / CURSOR_FILE).read_text())["next"]) % len(boards)
    except (OSError, ValueError, KeyError, TypeError):
        start = 0
    return boards[start:] + boards[:start], start


def save_cursor(data_dir, start: int, crawled: int, total: int) -> None:
    nxt = (start + crawled) % total if total else 0
    (Path(data_dir) / CURSOR_FILE).write_text(json.dumps({"next": nxt}) + "\n")


def aggregators_for_feed(env=None, log=print) -> list[str]:
    """The aggregator feeds this build may crawl into the PUBLIC file."""
    env = os.environ if env is None else env
    raw = (env.get("FEED_INCLUDE_AGGREGATORS", DEFAULT_INCLUDE) or "").strip()
    keyed: list[str] = []
    if raw.lower() not in ("", "0", "off", "none", "false"):
        for name in (x.strip().lower() for x in raw.split(",")):
            if not name:
                continue
            if name in REDISTRIBUTABLE_KEYED:
                keyed.append(name)
            elif name == "adzuna":
                log("[build_feed] refusing to include adzuna: its developer terms forbid "
                    "redistributing results without written consent (docs/feed.md)")
            else:
                log(f"[build_feed] ignoring unknown aggregator {name!r}")
    return [*PUBLIC_OK_AGGREGATORS, *keyed]


def build(out_dir, data_dir, *, crawl: bool = True, fetch=None, jobs_url: str = "",
          limit: int | None = None, log=print, crawler=None,
          crawl_minutes: float | None = None) -> dict:
    """Crawl (unless crawl=False) into the DB under `data_dir`, then write the feed files into
    `out_dir`. Returns the manifest. `fetch` is injectable so tests run against canned JSON.
    `crawler(watchlist) -> summary` replaces the full refresh_watchlist crawl (e.g. the bounded,
    polite first_open_refresh). `crawl_minutes` caps the board
    pass; the next build resumes where this one stopped (rotate_boards)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JOBS_DATA_DIR"] = str(data_dir)         # backend.feed derives its DB paths from this
    import backend.feed as feed
    from sourcing.feedfile import dedupe_rows, write_feed
    from sourcing.scheduler import PRUNE_DAYS
    from sourcing.service import feed_detail_budgets, refresh_watchlist, seed_if_empty
    from sourcing.watchlist import Watchlist

    if limit:
        feed.FEED_LIMIT = int(limit)
    jobs_db, _ = feed._paths()
    t0 = time.time()
    w = Watchlist(jobs_db)
    try:
        seed_if_empty(w)
        criteria = w.get_criteria()
        criteria["aggregators"] = aggregators_for_feed(log=log)
        w.set_criteria(criteria)
        if crawl:
            # The build has time to spare, so every list-only (Workday, SmartRecruiters) row it
            # keeps gets its description fetched and checked (WORKDAY_MAX_DETAIL_FEED /
            # SMARTRECRUITERS_MAX_DETAIL_FEED, default 2000 per board); rows past that budget are
            # left for the next build, never published unchecked.
            if crawler is not None:
                summary = crawler(w)
            else:
                boards, start = rotate_boards(w.companies(active_only=True), data_dir)
                summary = refresh_watchlist(w, **({"fetch": fetch} if fetch else {}),
                                            detail_budgets=feed_detail_budgets(), boards=boards,
                                            time_budget=crawl_minutes * 60 if crawl_minutes else None)
                save_cursor(data_dir, start, summary.get("boards_crawled", 0), len(boards))
                log(f"[build_feed] boards {summary.get('boards_crawled')}/{len(boards)} from #{start}"
                    + (" (stopped at the time cap; the next run continues)"
                       if summary.get("stopped_early") else ""))
            pruned = w.prune_stale(PRUNE_DAYS)
            log(f"[build_feed] crawl: fetched={summary.get('fetched')} matched={summary.get('matched')} "
                f"new={summary.get('new')} duplicates={summary.get('duplicates')} pruned={pruned} "
                f"unchecked={summary.get('unchecked')} "
                f"errors={len(summary.get('errors') or [])} in {time.time() - t0:.0f}s")
    finally:
        w.close()

    rows = dedupe_rows(feed.feed_jobs())[:feed.FEED_LIMIT]    # tagged, US-only, fresh, recent-sorted
    w = Watchlist(jobs_db)
    try:
        jd_by_id = w.get_jd_texts([r["source_id"] for r in rows])
    finally:
        w.close()
    manifest = write_feed(out_dir, rows, jd_by_id, jobs_url=jobs_url)
    log(f"[build_feed] wrote {manifest['count']} jobs + {manifest['jd_shard_count']} JD shards to "
        f"{out_dir} ({time.time() - t0:.0f}s total)")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("FEED_DATA_DIR", ".feed-data"),
                    help="persistent crawl state dir (jobs DB + sponsor overlay)")
    ap.add_argument("--out", default=os.environ.get("FEED_OUT_DIR", ".feed-out"),
                    help="where to write jobs.json.gz, jd/, manifest.json")
    ap.add_argument("--jobs-url", default=os.environ.get("FEED_JOBS_URL", ""),
                    help="absolute URL of jobs.json.gz, recorded in manifest.json")
    ap.add_argument("--limit", type=int, default=None, help="cap rows (default JOBS_FEED_LIMIT, 50000)")
    ap.add_argument("--no-crawl", action="store_true", help="skip the crawl; publish what the DB holds")
    ap.add_argument("--crawl-minutes", type=float,
                    default=float(os.environ.get("FEED_CRAWL_MINUTES", DEFAULT_CRAWL_MINUTES)),
                    help=f"stop the board pass after this long (default {DEFAULT_CRAWL_MINUTES}; "
                         "0 = no cap); the next run resumes where it stopped")
    a = ap.parse_args(argv)
    build(a.out, a.data, crawl=not a.no_crawl, jobs_url=a.jobs_url, limit=a.limit,
          crawl_minutes=a.crawl_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
