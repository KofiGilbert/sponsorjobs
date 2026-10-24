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
import hashlib
import json
import os
import sqlite3
import sys
import threading
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
DEFAULT_CRAWL_MINUTES = 140
CURSOR_FILE = "crawl_cursor.json"     # where the next run resumes in the board list, under --data
CAREERSITE_CURSOR_FILE = "careersite_cursor.json"   # the same for the career-site table
# The freehire search-by-sponsor pass (sourcing/freehire_sponsors.py) keeps its place in the
# employer list and its company-slug answers here; shard 0 owns both, like the career-site cursor.
FREEHIRE_SPONSOR_FILES = ("freehire_sponsor_cursor.json", "freehire_sponsor_slugs.json")
# Crawl shards (2026-10-13): the workflow splits the board list across parallel jobs, each crawling
# its own slice into a copy of the shared DB, then ONE merge job folds every shard's changes back
# into the shared DB and publishes. FEED_SHARD_INDEX / FEED_SHARD_COUNT pick the slice; the default
# 0 of 1 is the old single-job build (local runs and tests unchanged).
DELTA_SINCE_SLACK = "-5 seconds"     # export rows touched from a hair before the crawl started


def shard_config(env=None) -> tuple[int, int]:
    """(index, count) of this build's crawl shard from FEED_SHARD_INDEX / FEED_SHARD_COUNT.
    Unset means (0, 1): one shard holding every board. Raises on a nonsense pair, so a typo in
    the workflow fails the job instead of silently crawling the wrong slice."""
    env = os.environ if env is None else env
    count = int(env.get("FEED_SHARD_COUNT") or 1)
    index = int(env.get("FEED_SHARD_INDEX") or 0)
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"bad crawl shard {index} of {count}")
    return index, count


def board_shard(board: dict, count: int) -> int:
    """Which of `count` shards crawls this board: a stable hash of (ats, board_id), the board's
    unique key in the watchlist. Not Python's hash() (salted per process) and not the company
    name (a seed sync can rename it), so a board stays in the same shard run after run, and a
    board added to the seed lands in one shard without moving any other."""
    if count <= 1:
        return 0
    key = f"{str(board.get('ats') or '').lower()}:{str(board.get('board_id') or '')}"
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % count


def shard_boards(boards: list[dict], index: int, count: int) -> list[dict]:
    """This shard's boards, in the watchlist's own (company-sorted) order."""
    if count <= 1:
        return list(boards)
    return [b for b in boards if board_shard(b, count) == index]


def cursor_file_for(index: int, count: int) -> str:
    """The resume-cursor file of one shard. A single-shard build keeps the original name, so the
    cursor a pre-shard run saved is still read; each real shard has its own file, so two shards
    never overwrite each other's place in the list."""
    return CURSOR_FILE if count <= 1 else f"crawl_cursor.s{index}of{count}.json"


def owns_lanes(index: int) -> bool:
    """Only the first shard runs the lanes that are not split by board (the career-site lane and
    the aggregator APIs), so a sharded run never crawls a site or calls an API twice."""
    return index == 0


def _board_key(b: dict) -> list[str]:
    """A board's place in the watchlist order (Watchlist.companies sorts by company, case-blind)."""
    return [str(b.get("company") or "").lower(), str(b.get("ats") or ""), str(b.get("board_id") or "")]


def rotate_boards(boards: list[dict], data_dir, cursor_file: str = CURSOR_FILE,
                  fallback_file: str | None = None) -> tuple[list[dict], int]:
    """The boards in crawl order for this run, starting where the last run stopped, and that start
    index. A capped run that always began at "A" would never reach the end of the list.

    The cursor remembers the next board ITSELF, not just its index: the watchlist is sorted by
    company, so a seed sync that adds boards (the 2026-10 growth roughly tripled it) shifts every
    index after the new names, and an index-only cursor would silently skip a stretch of boards
    for a whole lap. When the remembered board has gone, the run starts at the first board after
    where it sorted. An old index-only cursor still works.

    `fallback_file` is read when `cursor_file` does not exist yet: a shard's first run starts
    where the single-job crawl stopped (its remembered board, or the first after it in this
    shard's slice) rather than back at "A"."""
    if not boards:
        return [], 0
    path = Path(data_dir) / cursor_file
    if fallback_file and not path.exists():
        path = Path(data_dir) / fallback_file
    try:
        cur = json.loads(path.read_text())
        start = int(cur["next"]) % len(boards)
    except (OSError, ValueError, KeyError, TypeError):
        return list(boards), 0
    key = cur.get("key") if isinstance(cur, dict) else None
    if isinstance(key, list) and len(key) == 3:
        keys = [_board_key(b) for b in boards]
        if key in keys:
            start = keys.index(key)
        else:
            start = next((i for i, k in enumerate(keys) if k > key), 0)
    return boards[start:] + boards[:start], start


def save_cursor(data_dir, start: int, crawled: int, total: int, order: list[dict] | None = None,
                cursor_file: str = CURSOR_FILE) -> None:
    """Remember where the next run starts: `crawled` boards into this run's `order` (the rotated
    list rotate_boards returned), by index and, when the order is given, by the board's key."""
    nxt = (start + crawled) % total if total else 0
    cur: dict = {"next": nxt}
    if order and total:
        cur["key"] = _board_key(order[crawled % len(order)])
    (Path(data_dir) / cursor_file).write_text(json.dumps(cur) + "\n")


# -- shard output ("delta") and the merge --------------------------------------------------- #

def _has_table(conn, schema: str, name: str) -> bool:
    return conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def export_delta(jobs_db, data_dir, since: str, delta_path, *, index: int = 0, count: int = 1) -> dict:
    """Write what this shard's crawl changed into `delta_path` (a small SQLite file the workflow
    hands to the merge job): every sourced_job row stamped at or after `since` (inserted, updated
    or seen again), the career-site skip memory touched today, and this shard's cursor files.
    Rows the crawl did not touch are already in the shared DB, so they are not copied."""
    delta_path = Path(delta_path)
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    if delta_path.exists():
        delta_path.unlink()
    conn = sqlite3.connect(str(jobs_db))
    try:
        conn.execute("ATTACH DATABASE ? AS d", (str(delta_path),))
        conn.execute("CREATE TABLE d.sourced_job AS SELECT * FROM main.sourced_job "
                     "WHERE last_seen >= datetime(?, ?) OR first_seen >= datetime(?, ?)",
                     (since, DELTA_SINCE_SLACK, since, DELTA_SINCE_SLACK))
        if _has_table(conn, "main", "careersite_skip"):
            conn.execute("CREATE TABLE d.careersite_skip AS SELECT * FROM main.careersite_skip "
                         "WHERE at >= substr(?, 1, 10)", (since,))
        if _has_table(conn, "main", "board_crawl"):
            # The outcome of every board this shard read (Watchlist.record_crawl), so the merge's
            # fast prune knows which boards answered in full this run.
            conn.execute("CREATE TABLE d.board_crawl AS SELECT * FROM main.board_crawl "
                         "WHERE updated_at >= datetime(?, ?)", (since, DELTA_SINCE_SLACK))
        conn.execute("CREATE TABLE d.delta_meta (k TEXT PRIMARY KEY, v TEXT)")
        files = [cursor_file_for(index, count)] + (
            [CAREERSITE_CURSOR_FILE, *FREEHIRE_SPONSOR_FILES] if owns_lanes(index) else [])
        for name in files:
            f = Path(data_dir) / name
            if f.exists():
                conn.execute("INSERT INTO d.delta_meta VALUES (?, ?)", (f"file:{name}", f.read_text()))
        conn.execute("INSERT INTO d.delta_meta VALUES ('shard', ?)", (f"{index}/{count}",))
        conn.execute("INSERT INTO d.delta_meta VALUES ('since', ?)", (since,))
        conn.commit()
        rows = conn.execute("SELECT COUNT(*) FROM d.sourced_job").fetchone()[0]
        conn.execute("DETACH DATABASE d")
    finally:
        conn.close()
    return {"rows": rows, "path": str(delta_path)}


def merge_delta(jobs_db, data_dir, delta_path, log=print) -> dict:
    """Fold one shard's delta into the shared DB: each row is inserted, or replaces the stored
    copy when it was seen at least as recently (so merging the same delta twice, or two deltas
    in either order, ends the same). first_seen keeps the earlier of the two, so a role never
    looks newer than it is. The shard's cursor files are written into `data_dir`, which moves
    that shard's place in the board list forward; a shard that produced no delta keeps its old
    cursor and simply resumes from there next run."""
    from sourcing.watchlist import Watchlist
    Watchlist(str(jobs_db)).close()                       # the shared DB exists, fully migrated
    conn = sqlite3.connect(str(jobs_db))
    try:
        conn.execute("ATTACH DATABASE ? AS d", (str(delta_path),))
        if not _has_table(conn, "d", "sourced_job"):
            raise ValueError(f"{delta_path} is not a crawl-shard delta")
        main_cols = [r[1] for r in conn.execute("PRAGMA main.table_info(sourced_job)")]
        cols = [c for c in (r[1] for r in conn.execute("PRAGMA d.table_info(sourced_job)")) if c in main_cols]
        upd = [c for c in cols if c not in ("source_id", "first_seen")]
        sets = ", ".join(f"{c}=excluded.{c}" for c in upd)
        if "first_seen" in cols:
            sets += (", first_seen=CASE WHEN sourced_job.first_seen IS NULL OR excluded.first_seen "
                     "< sourced_job.first_seen THEN excluded.first_seen ELSE sourced_job.first_seen END")
        cl = ", ".join(cols)
        cur = conn.execute(
            f"INSERT INTO main.sourced_job ({cl}) SELECT {cl} FROM d.sourced_job WHERE true "
            f"ON CONFLICT(source_id) DO UPDATE SET {sets} "
            "WHERE sourced_job.last_seen IS NULL OR excluded.last_seen >= sourced_job.last_seen")
        rows = cur.rowcount
        if _has_table(conn, "d", "careersite_skip"):
            conn.execute("CREATE TABLE IF NOT EXISTS main.careersite_skip ("
                         "url TEXT PRIMARY KEY, host TEXT, reason TEXT, at TEXT)")
            conn.execute("INSERT OR REPLACE INTO main.careersite_skip (url, host, reason, at) "
                         "SELECT url, host, reason, at FROM d.careersite_skip")
        if _has_table(conn, "d", "board_crawl"):
            # Newer wins, per column: each timestamp keeps the later of the two, and the error
            # text and streak come from the copy stamped last. Idempotent and order-free, like
            # the rows above. A delta from a build before the table existed simply has none.
            later = "excluded.updated_at >= coalesce(board_crawl.updated_at, '')"
            conn.execute(
                "INSERT INTO main.board_crawl (ats, board_id, last_ok, last_partial, last_error, error, "
                "consecutive_errors, updated_at) SELECT ats, board_id, last_ok, last_partial, "
                "last_error, error, consecutive_errors, updated_at FROM d.board_crawl WHERE true "
                "ON CONFLICT(ats, board_id) DO UPDATE SET "
                + ", ".join(f"{c}=nullif(max(coalesce(board_crawl.{c}, ''), coalesce(excluded.{c}, '')), '')"
                            for c in ("last_ok", "last_partial", "last_error", "updated_at"))
                + f", error=CASE WHEN {later} THEN excluded.error ELSE board_crawl.error END"
                f", consecutive_errors=CASE WHEN {later} THEN excluded.consecutive_errors "
                "ELSE board_crawl.consecutive_errors END")
        meta = dict(conn.execute("SELECT k, v FROM d.delta_meta").fetchall()) \
            if _has_table(conn, "d", "delta_meta") else {}
        conn.commit()
        conn.execute("DETACH DATABASE d")
    finally:
        conn.close()
    for k, v in meta.items():
        if k.startswith("file:"):
            name = Path(k[5:]).name                        # a bare file name, never a path
            if name.endswith(".json"):
                (Path(data_dir) / name).write_text(v)
    log(f"[build_feed] merged shard {meta.get('shard', '?')}: {rows} rows from {delta_path}")
    return {"rows": rows, "shard": meta.get("shard")}


def prune_rows(w, log=print, where: str = "crawl") -> tuple[int, int]:
    """Both prune rules, the fast one first, with one log line so each run shows their effect:
    a row unseen FAST_PRUNE_DAYS whose board was read in full since goes (Watchlist.prune_unlisted:
    the board answered and did not list it), then every row unseen PRUNE_DAYS goes
    (prune_stale, the safety net for boards that failed to load and for sources with no board).
    Returns (unlisted, stale)."""
    from sourcing.scheduler import FAST_PRUNE_DAYS, PRUNE_DAYS
    unlisted = w.prune_unlisted(FAST_PRUNE_DAYS)
    stale = w.prune_stale(PRUNE_DAYS)
    log(f"[build_feed] {where}: pruned unlisted={unlisted} (unseen {FAST_PRUNE_DAYS} days on a board "
        f"read in full since) stale={stale} (unseen {PRUNE_DAYS} days)")
    return unlisted, stale


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


def freehire_sponsor_pass(w, data_dir, log=print, **kw) -> dict:
    """The freehire search-by-sponsor pass (sourcing/freehire_sponsors.py): at most
    FREEHIRE_SPONSOR_PASS_REQUESTS requests (default 200, 0 = off) asking freehire for the fresh
    US roles of H-1B employers our own crawl does not cover. Shard 0 only (owns_lanes), so the
    API is never asked twice in one run. Best-effort: a failure is logged, never fatal."""
    import backend.feed as feed
    from sourcing.freehire_sponsors import run_pass
    try:
        s = run_pass(w, data_dir, feed._sponsors(feed._paths()[1]).tag_jobs, log=log, **kw)
    except Exception as exc:                             # noqa: BLE001 - never kill the build
        log(f"[build_feed] freehire sponsor pass failed: {exc}")
        return {}
    log(f"[build_feed] freehire sponsor pass: requests={s['requests']} employers={s['employers']} "
        f"(with freehire slugs {s['employers_with_slugs']}, list {s['list_size']} from #{s['start']}) "
        f"fetched={s['fetched']} new={s['new']} already_held={s['already_held']} "
        f"cross_feed_duplicates={s['duplicates']} not_sponsor={s['not_sponsor']} no_employer_url={s['no_employer_url']} "
        f"rate_limited={s['rate_limited']}" + (f" stopped: {s['stopped']}" if s["stopped"] else ""))
    return s


class _CareerSiteLane:
    """Runs sourcing.careersite.crawl_sites in a background thread for the feed build."""

    def __init__(self, db_path, log, cursor_path=None):
        from sourcing.careersite import crawl_sites
        self.summaries: list[dict] = []
        self._thread = threading.Thread(
            target=lambda: self.summaries.extend(crawl_sites(db_path, cursor_path=cursor_path, log=log)),
            name="careersite-lane", daemon=True)

    def start(self):
        self._thread.start()

    def join(self):
        self._thread.join()


def build(out_dir, data_dir, *, crawl: bool = True, fetch=None, jobs_url: str = "",
          limit: int | None = None, log=print, crawler=None,
          crawl_minutes: float | None = None, shard: tuple[int, int] = (0, 1),
          delta_out=None, merge=()) -> dict:
    """Crawl (unless crawl=False) into the DB under `data_dir`, then write the feed files into
    `out_dir`. Returns the manifest. `fetch` is injectable so tests run against canned JSON.
    `crawler(watchlist) -> summary` replaces the full refresh_watchlist crawl (e.g. the bounded,
    polite first_open_refresh). `crawl_minutes` caps the board
    pass; the next build resumes where this one stopped (rotate_boards).

    Sharded runs: `shard=(i, n)` crawls only board slice i of n (shard 0 also runs the career-site
    lane and the aggregators); with `delta_out` the build stops after the crawl and writes what it
    changed there instead of a feed (export_delta). `merge` (paths of shard deltas) folds them into
    the DB first (merge_delta), then prunes and writes the feed from the whole DB, as one build."""
    index, count = shard
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JOBS_DATA_DIR"] = str(data_dir)         # backend.feed derives its DB paths from this
    import backend.feed as feed
    from sourcing.feedfile import dedupe_rows, write_feed
    from sourcing.service import feed_detail_budgets, refresh_watchlist, seed_if_empty, sync_seed
    from sourcing.watchlist import Watchlist

    if limit:
        feed.FEED_LIMIT = int(limit)
    jobs_db, _ = feed._paths()
    t0 = time.time()
    since = None
    if crawl and delta_out is not None:
        c = sqlite3.connect(str(jobs_db))
        since = c.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]   # the clock the rows are stamped by
        c.close()
    w = Watchlist(jobs_db)
    try:
        seed_if_empty(w)
        sync_seed(w)                                     # boards added to the seed since the DB was made
        criteria = w.get_criteria()
        criteria["aggregators"] = aggregators_for_feed(log=log)
        # The seeded title list (engineer, developer, AI...) is a ONE-PERSON search filter. The
        # public board serves accountants, nurses, consultants, analysts and researchers too (the
        # template library has a resume for each), so the board pass keeps every title and the
        # sponsor-relevance / accessibility rules do the filtering. The keyword aggregators still
        # need terms. FEED_TITLE_FILTER=1 restores the old narrowing.
        if os.environ.get("FEED_TITLE_FILTER", "0") != "1":
            if criteria.get("titles"):
                criteria["aggregator_terms"] = list(criteria["titles"])
            criteria["titles"] = []
        w.set_criteria(criteria)
        if crawl:
            # The build has time to spare, so every list-only (Workday, SmartRecruiters) row it
            # keeps gets its description fetched and checked (WORKDAY_MAX_DETAIL_FEED /
            # SMARTRECRUITERS_MAX_DETAIL_FEED, default 2000 per board); rows past that budget are
            # left for the next build, never published unchecked.
            # The career-site lane (sourcing/careersite.py) runs beside the board pass in its own
            # threads and connections: different hosts, so neither slows the other's politeness.
            lane = None
            if (crawler is None and fetch is None and owns_lanes(index)
                    and os.environ.get("CAREERSITE_LANE", "1") != "0"):
                lane = _CareerSiteLane(jobs_db, log, cursor_path=data_dir / CAREERSITE_CURSOR_FILE)
                lane.start()
            if crawler is not None:
                summary = crawler(w)
            else:
                cfile = cursor_file_for(index, count)
                boards, start = rotate_boards(shard_boards(w.companies(active_only=True), index, count),
                                              data_dir, cfile,
                                              fallback_file=CURSOR_FILE if count > 1 else None)
                summary = refresh_watchlist(w, **({"fetch": fetch} if fetch else {}),
                                            detail_budgets=feed_detail_budgets(), boards=boards,
                                            time_budget=crawl_minutes * 60 if crawl_minutes else None,
                                            include_aggregators=owns_lanes(index))
                save_cursor(data_dir, start, summary.get("boards_crawled", 0), len(boards), boards,
                            cursor_file=cfile)
                log(f"[build_feed] {'shard %d/%d ' % (index, count) if count > 1 else ''}"
                    f"boards {summary.get('boards_crawled')}/{len(boards)} from #{start}"
                    + (" (stopped at the time cap; the next run continues)"
                       if summary.get("stopped_early") else ""))
            if crawler is None and fetch is None and owns_lanes(index):
                freehire_sponsor_pass(w, data_dir, log=log)
            if lane is not None:
                lane.join()
                log(f"[build_feed] careersite: sites={len(lane.summaries)} "
                    f"kept={sum(x['kept'] for x in lane.summaries)} "
                    f"known={sum(x['known'] for x in lane.summaries)} "
                    f"fetched={sum(x['fetched'] for x in lane.summaries)} "
                    f"blocked={[x['host'] for x in lane.summaries if x['blocked']]}")
            pruned = sum(prune_rows(w, log, "crawl"))
            log(f"[build_feed] crawl: fetched={summary.get('fetched')} matched={summary.get('matched')} "
                f"new={summary.get('new')} duplicates={summary.get('duplicates')} pruned={pruned} "
                f"unchecked={summary.get('unchecked')} "
                f"errors={len(summary.get('errors') or [])} in {time.time() - t0:.0f}s")
    finally:
        w.close()

    if delta_out is not None and since is not None:
        d = export_delta(jobs_db, data_dir, since, delta_out, index=index, count=count)
        log(f"[build_feed] shard {index}/{count}: wrote {d['rows']} changed rows to {delta_out} "
            f"({time.time() - t0:.0f}s total)")
        return {"delta": d["path"], "rows": d["rows"], "shard": f"{index}/{count}"}

    if merge:
        for p in merge:
            merge_delta(jobs_db, data_dir, p, log=log)
        w = Watchlist(jobs_db)
        try:
            prune_rows(w, log, "merge")
        finally:
            w.close()

    fj = feed.feed_jobs()
    rows = dedupe_rows(fj)[:feed.FEED_LIMIT]    # tagged, US-only, fresh, recent-sorted
    w = Watchlist(jobs_db)
    try:
        total = w._conn.execute("SELECT COUNT(*) FROM sourced_job").fetchone()[0]
    except Exception:
        total = -1
    finally:
        w.close()
    log(f"[build_feed] funnel: in_db={total} {feed.FUNNEL} duplicates_removed={len(fj) - len(dedupe_rows(fj))} written={len(rows)}")
    # What the dropped rows look like, so a filter is loosened on evidence, not a hunch.
    from collections import Counter
    for name, sample in (feed.FUNNEL_SAMPLES or {}).items():
        locs = Counter((r.get("location") or "<empty>")[:60] for r in sample)
        log(f"[build_feed] dropped:{name} n={len(sample)} top_locations={locs.most_common(40)}")
        log(f"[build_feed] dropped:{name} sample={[(r.get('company'), r.get('title'), r.get('location'), r.get('posted_at')) for r in sample[:25]]}")
    w = Watchlist(jobs_db)
    try:
        jd_by_id = w.get_jd_texts([r["source_id"] for r in rows])
    finally:
        w.close()
    from sourcing.feedfile import read_workers
    log(f"[build_feed] writing: reading {len(rows)} ads in {read_workers(len(rows))} process(es)")
    tw = time.time()
    # The last build's stable parts live in the data dir (cached between runs), so this build
    # re-sends only the parts whose rows changed (sourcing/feedfile.write_stable).
    from sourcing.feedfile import STABLE_STATE_DIR, save_stable_state
    prev_dir = Path(data_dir) / STABLE_STATE_DIR
    manifest = write_feed(out_dir, rows, jd_by_id, jobs_url=jobs_url, prev_dir=prev_dir)
    save_stable_state(out_dir, prev_dir)
    st = manifest.get("stable") or {}
    log(f"[build_feed] stable layout: {len(st.get('files') or [])} parts, "
        f"{(st.get('removed') or {}).get('count', 0)} removed rows, version {st.get('version')}")
    parts = manifest.get("parts") or {}
    log(f"[build_feed] wrote {manifest['count']} jobs ({len(parts.get('files') or [])} list parts, "
        f"version {parts.get('version')}) + {manifest['jd_shard_count']} JD shards to "
        f"{out_dir} in {time.time() - tw:.0f}s ({time.time() - t0:.0f}s total)")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("FEED_DATA_DIR", ".feed-data"),
                    help="persistent crawl state dir (jobs DB + sponsor overlay)")
    ap.add_argument("--out", default=os.environ.get("FEED_OUT_DIR", ".feed-out"),
                    help="where to write jobs.json.gz, parts/, jd/, manifest.json")
    ap.add_argument("--jobs-url", default=os.environ.get("FEED_JOBS_URL", ""),
                    help="absolute URL of jobs.json.gz, recorded in manifest.json")
    ap.add_argument("--limit", type=int, default=None, help="cap rows (default JOBS_FEED_LIMIT, 1000000)")
    ap.add_argument("--no-crawl", action="store_true", help="skip the crawl; publish what the DB holds")
    ap.add_argument("--crawl-minutes", type=float,
                    default=float(os.environ.get("FEED_CRAWL_MINUTES", DEFAULT_CRAWL_MINUTES)),
                    help=f"stop the board pass after this long (default {DEFAULT_CRAWL_MINUTES}; "
                         "0 = no cap); the next run resumes where it stopped")
    ap.add_argument("--export-delta", metavar="PATH", default=None,
                    help="crawl shard mode: crawl this shard's boards (FEED_SHARD_INDEX of "
                         "FEED_SHARD_COUNT), write the changed rows to PATH, write no feed")
    ap.add_argument("--merge-dir", metavar="DIR", default=None,
                    help="merge mode: fold every shard delta (*.db) under DIR into the DB, then "
                         "write the feed (no crawl). Fails when DIR holds no delta")
    a = ap.parse_args(argv)
    shard = shard_config()
    if a.export_delta and a.merge_dir:
        ap.error("--export-delta and --merge-dir are separate steps")
    merge: list[Path] = []
    if a.merge_dir:
        merge = sorted(Path(a.merge_dir).rglob("*.db"))
        if not merge:
            # Every shard failed: publishing now would only re-publish the old DB, and saving the
            # state would pretend a run happened. Fail loudly instead; the last feed stays up.
            print(f"[build_feed] no shard delta under {a.merge_dir}: refusing to publish", file=sys.stderr)
            return 2
    build(a.out, a.data, crawl=not (a.no_crawl or a.merge_dir), jobs_url=a.jobs_url, limit=a.limit,
          crawl_minutes=a.crawl_minutes, shard=shard, delta_out=a.export_delta, merge=merge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
