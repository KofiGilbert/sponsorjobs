"""Central "kitchen" job feed, bolted onto the hosted broker service (the same $7 server).

RETIRED AS THE SUPPORTED PATH (2026-09-30): the desktop app now reads a STATIC feed -- the same
crawl, run by a scheduled GitHub Action (scripts/build_feed.py) and uploaded to Cloudflare R2
(scripts/upload_feed.py) -- so there is no server to pay for or keep alive. See docs/feed.md.
This module is kept working (feed_jobs / feed_detail are reused by the builder, and the /feed
routes still serve if someone deploys the broker) but is no longer what JOBS_FEED_URL points at.

Job LISTINGS are public and costly to keep fresh, so ONE hosted service fetches them once
(with our aggregator keys) and every user's local app pulls the finished, sponsor-tagged,
quality-filtered feed. This holds NO personal data -- only public jobs + the public
government sponsor DB. It rides inside the broker process so there's no second bill, and the
crawler "robot" runs in ONE worker only (an exclusive file lock), so multiple gunicorn
workers can't multiply the paid API calls. See sourcing/scheduler.py and CLAUDE.md §5/§6.

Turned on by env: JOBS_AUTOUPDATE=1 starts the robot; JOBS_DATA_DIR (default: the broker
disk) holds resume_agent.db (jobs) + sponsors.db (the visa-sponsor overlay). The desktop
app points at this service with JOBS_FEED_URL and pulls GET /feed.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from pathlib import Path

from flask import jsonify, request

# Freshness + size bounds. The board is worthless if it surfaces filled/dead postings, and the
# feed must not recompute over tens of thousands of rows per request (that 502'd the endpoint).
# So the public feed is RECENT-sorted, dated rows older than the window are dropped, and the
# whole thing is capped and cached. All env-overridable.
FEED_FRESH_DAYS = int(os.environ.get("JOBS_FRESH_DAYS", "45"))    # drop dated rows older than this
# How many rows the cached full list HOLDS (for server-side filtering + pagination). The response
# only ever sends ONE page, so this can be large without bloating any single payload -- it just
# sets how deep the browsable board goes and the max "N jobs" total we can report.
FEED_LIMIT = int(os.environ.get("JOBS_FEED_LIMIT", "50000"))
FEED_TTL = int(os.environ.get("JOBS_FEED_TTL", "120"))            # seconds a built feed is reused
FEED_PER_PAGE = int(os.environ.get("JOBS_PER_PAGE", "30"))        # default page size
# On-demand search: when a keyword search finds fewer than SEARCH_MIN in the pre-built board,
# live-query freehire's full 1M+ index for that keyword and fold the results in (cached per term).
SEARCH_MIN = int(os.environ.get("JOBS_SEARCH_MIN", "25"))
SEARCH_TTL = int(os.environ.get("JOBS_SEARCH_TTL", "600"))       # cache a live search this long
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}

# level -> (built_at_epoch, jobs). Building the feed is the expensive part (DB read + sponsor
# tagging + filters); with a short TTL, one build serves every puller for the next two minutes.
_FEED_CACHE: dict[str, tuple[float, list[dict]]] = {}
_ENRICH_MISS: set = set()        # source_ids whose full JD freehire doesn't carry -> don't re-search


def _iso(epoch: float) -> str:
    """A cache build time (epoch seconds) as UTC ISO 8601, the same shape the local feed uses for
    `refreshed_at`, so the client's ago() parser reads either source identically."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _paths() -> tuple[str, str]:
    """(jobs_db, sponsors_db). Defaults to the broker's persistent-disk dir so the feed and
    the metering DB share the one Render disk."""
    d = os.environ.get("JOBS_DATA_DIR")
    if not d:
        bdb = os.environ.get("TAILOR_BROKER_DB")
        d = str(Path(bdb).parent) if bdb else "data"
    return f"{d}/resume_agent.db", f"{d}/sponsors.db"


_SPONSORS: dict[str, object] = {}


def _sponsors(path: str):
    """One cached SponsorDB per path (it holds an in-memory index; don't rebuild per request).
    On a fresh server the DB is empty, so rebuild the visa-sponsor overlay from the committed
    seed (seed/sponsors_seed.csv.gz) — no manual 67MB upload needed."""
    db = _SPONSORS.get(path)
    if db is None:
        from sourcing.sponsors import SponsorDB
        db = SponsorDB(path)
        if db.is_empty():
            seed = Path(__file__).resolve().parents[1] / "seed" / "sponsors_seed.csv.gz"
            if seed.exists():
                try:
                    n = db.rebuild_from_seed(seed)
                    print(f"[feed] rebuilt sponsor overlay from seed: {n} employers")
                except Exception:                    # noqa: BLE001 - serve without badges rather than crash
                    traceback.print_exc()
        _SPONSORS[path] = db
    return db


def feed_jobs(level: str | None = None) -> list[dict]:
    """The public feed: sponsor-tagged, US-only, accessible-to-international-students roles,
    each flagged entry_level. Mirrors the local app's /api/jobs shaping so the desktop app
    can drop the central list straight into its board."""
    from sourcing.quality import (employer_is_clearance_heavy, is_entry_level, is_jd_checked,
                                  is_sponsor_relevant)
    from sourcing.watchlist import Watchlist
    jobs_db, spon_db = _paths()
    w = Watchlist(jobs_db)
    try:
        # RECENT-sorted, fresh-only, and bounded -- so page 1 is never a 5-month-old posting
        # and the endpoint isn't tagging tens of thousands of rows on every hit.
        jobs = _sponsors(spon_db).tag_jobs(
            w.list_jobs(order="recent", since_days=FEED_FRESH_DAYS, limit=FEED_LIMIT))
    finally:
        w.close()
    # A list-only row (Workday) whose description was never fetched has not had the
    # citizenship / clearance / no-sponsorship check run on it: never published as accessible.
    jobs = [j for j in jobs
            if j.get("us") and not employer_is_clearance_heavy(j.get("company", ""))
            and is_jd_checked(j) and is_sponsor_relevant(j)]
    for j in jobs:
        j["entry_level"] = is_entry_level(j.get("title", ""))
    if (level or "").lower() == "entry":
        jobs = [j for j in jobs if j["entry_level"]]
    return jobs


_REBUILD_LOCK = threading.Lock()
_REBUILDING: set[str] = set()


def _rebuild_async(level: str | None, key: str) -> None:
    """Rebuild one feed level in the background (once at a time per level), then swap the cache.
    Keeps the expensive build OFF the request path so a stale-serving request never waits on it."""
    with _REBUILD_LOCK:
        if key in _REBUILDING:
            return
        _REBUILDING.add(key)

    def _run() -> None:
        try:
            jobs = feed_jobs(level)
            _FEED_CACHE[key] = (time.time(), jobs)
        except Exception:                                # noqa: BLE001 - keep the stale copy
            traceback.print_exc()
        finally:
            with _REBUILD_LOCK:
                _REBUILDING.discard(key)

    threading.Thread(target=_run, name=f"feed-rebuild-{key or 'all'}", daemon=True).start()


def _cached_feed(level: str | None) -> list[dict]:
    """Serve the feed with STALE-WHILE-REVALIDATE: a warm cache returns instantly; a STALE cache
    still returns instantly while a background thread rebuilds it. Only the very first request
    (cold, no cache yet) pays the build. So the board stays fast no matter how large it grows --
    the build cost never lands on a user request except once at startup."""
    key = (level or "").lower()
    hit = _FEED_CACHE.get(key)
    if hit is not None:
        if (time.time() - hit[0]) >= FEED_TTL:
            _rebuild_async(level, key)                   # stale: refresh in the background
        return hit[1]                                    # ...but serve immediately either way
    jobs = feed_jobs(level)                              # cold start: build once, synchronously
    _FEED_CACHE[key] = (time.time(), jobs)
    return jobs


def _freehire_live(q: str) -> list[dict]:
    """Live-query freehire's full index for a keyword and return sponsor-tagged, accessible US
    roles. Also UPSERTS them into the DB so they persist and their detail/JD opens normally --
    a user's search literally grows the board. Cached per term (best-effort; a lock/network blip
    just returns fewer results, never an error)."""
    key = (q or "").strip().lower()
    if not key:
        return []
    hit = _SEARCH_CACHE.get(key)
    if hit and (time.time() - hit[0]) < SEARCH_TTL:
        return hit[1]
    from sourcing.ats import freehire_jobs
    from sourcing.quality import employer_is_clearance_heavy, is_entry_level
    from sourcing.watchlist import Watchlist
    jobs_db, spon_db = _paths()
    try:
        raw = freehire_jobs(key)                         # US-only, recent, up to ~100 for this term
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        return []
    try:                                                 # persist so detail/JD works + it sticks
        w = Watchlist(jobs_db)
        try:
            w.upsert_jobs(raw)
        finally:
            w.close()
    except Exception:                                    # noqa: BLE001 - DB busy: show without persisting
        pass
    tagged = _sponsors(spon_db).tag_jobs(raw)
    out = [j for j in tagged
           if j.get("us") and not employer_is_clearance_heavy(j.get("company", ""))]
    for j in out:
        j["entry_level"] = is_entry_level(j.get("title", ""))
    _SEARCH_CACHE[key] = (time.time(), out)
    if len(_SEARCH_CACHE) > 30:                           # bound memory: drop the oldest entries
        for k in sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])[:-30]:
            _SEARCH_CACHE.pop(k, None)
    return out


def feed_detail(source_id: str) -> dict | None:
    """The full JD (+ tagged job) for one role, from the central DB — so a user who pulled
    the LIST from the kitchen can also open a job and read its description. Returns
    {job, jd} or None. The pre-tailor MATCH is computed by the LOCAL app (it needs the
    user's private profile), never here."""
    from sourcing.ats import html_to_text, salary_from_text
    from sourcing.quality import is_list_only
    from sourcing.watchlist import Watchlist
    jobs_db, spon_db = _paths()
    w = Watchlist(jobs_db)
    try:
        job = w.get_job(source_id)
    finally:
        w.close()
    if not job:
        return None
    job = _sponsors(spon_db).tag_jobs([job])[0]
    # A list-only row (Workday, SmartRecruiters) stored before the refresh started checking every
    # kept row's JD: fetch this one's description now and run the check it has not had yet.
    if not (job.get("jd_text") or "").strip() and is_list_only(job):
        from sourcing.feedclient import fill_list_only_detail
        from sourcing.quality import role_excludes_international
        det = fill_list_only_detail(job)
        jd_body = det.get("jd_text") or ""
        if jd_body and role_excludes_international(job.get("company", ""), jd_body):
            w = Watchlist(jobs_db)                       # checked now, for the first time: it is
            try:                                         # not open to international candidates
                w.dismiss(source_id)
            finally:
                w.close()
            return None
        job.update(det)
    jd = html_to_text((job.get("jd_text") or "")).strip()
    # Adzuna returns only a ~500-char PREVIEW (their terms forbid the full text). Pull the full JD
    # from freehire when it has the same role (company + title), persist it, else flag a preview so
    # the app points to the posting rather than showing an abrupt mid-word cutoff. Same as ui/app.py.
    from sourcing.ats import freehire_fulltext, looks_truncated
    preview = False
    if looks_truncated(job, jd):
        if source_id in _ENRICH_MISS:                # freehire already checked, doesn't carry it
            preview = True
        else:
            full = freehire_fulltext(job.get("title", ""), job.get("company", ""))
            if full and len(full) > len(jd) + 200:
                jd = html_to_text(full).strip()
                w2 = Watchlist(jobs_db)
                try:
                    w2.upsert_jobs([{**job, "jd_text": jd}])
                finally:
                    w2.close()
            else:
                preview = True
                _ENRICH_MISS.add(source_id)
    job["jd_text"] = jd
    # Surface pay stated in the posting body when the feed carried no figure (read-time net for
    # rows stored before ingest-time salary extraction). Same helper the local app uses.
    if not (job.get("salary") or "").strip():
        job["salary"] = salary_from_text(jd)
    # How this role's pay compares to typical pay for its family on the board (LinkedIn-Premium-
    # style, but from our own data). None unless the role states pay and its family has a benchmark.
    from sourcing.filters import salary_insight
    insight = salary_insight(job, _benchmarks())
    return {"job": job, "jd": jd, "pay_insight": insight, "jd_preview": preview}


_BENCH_CACHE: tuple[float, dict] | None = None
_BENCH_TTL = 600          # recompute the board-wide pay medians at most every 10 min


def _benchmarks() -> dict:
    """Board-wide {family: (median_pay, count)}, cached. Computed off the already-cached full feed
    so it costs nothing beyond one pass over rows we hold in memory; refreshed on a slow TTL since
    medians barely move between clicks."""
    global _BENCH_CACHE
    now = time.time()
    if _BENCH_CACHE is None or (now - _BENCH_CACHE[0]) >= _BENCH_TTL:
        from sourcing.filters import salary_benchmarks
        try:
            _BENCH_CACHE = (now, salary_benchmarks(_cached_feed(None)))
        except Exception:                                # noqa: BLE001 - no insight beats a 500
            traceback.print_exc()
            _BENCH_CACHE = (now, {})
    return _BENCH_CACHE[1]


_LOCK_FD = None


def _acquire_singleton() -> bool:
    """Grab an exclusive lock so only ONE gunicorn worker runs the crawler (else N workers =
    N× the paid API calls). The fd is kept open for the worker's lifetime to hold the lock.
    Where flock isn't available (e.g. a Windows dev box, single process) we just run it."""
    global _LOCK_FD
    try:
        import fcntl
    except Exception:
        return True                                  # no flock -> assume single process
    jobs_db, _ = _paths()
    lockpath = Path(jobs_db).parent / "autoupdater.lock"
    try:
        lockpath.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lockpath, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FD = fd                                # keep open -> lock held
        return True
    except Exception:
        return False                                 # another worker already holds it


def _start_robot() -> bool:
    """Start the auto-updater in this (single) worker. Returns True if started."""
    if os.environ.get("JOBS_AUTOUPDATE") != "1" or not _acquire_singleton():
        return False
    from sourcing.scheduler import AutoUpdater
    from sourcing.service import (feed_detail_budgets, refresh_freehire_only, refresh_watchlist,
                                  seed_if_empty)
    from sourcing.watchlist import Watchlist
    jobs_db, _ = _paths()

    def make_watchlist():
        w = Watchlist(jobs_db)
        seed_if_empty(w)
        return w

    # Weekly discovery: the same ranked-sponsor crawl that grew the committed seed
    # (sourcing/growth.py), a bounded slice per pass, checkpointed beside the jobs DB so each
    # week probes the NEXT best sponsors rather than the same top of the list.
    discover_fn = None
    try:
        from sourcing.growth import discover_sponsors

        def discover_fn():                           # noqa: E306
            w = Watchlist(jobs_db)
            try:
                return discover_sponsors(
                    w, Path(jobs_db).parent / "discover_checkpoint.jsonl",
                    budget=int(os.environ.get("JOBS_DISCOVER_BUDGET", "300")))
            finally:
                w.close()
    except Exception:                                # noqa: BLE001 - discovery optional
        discover_fn = None

    # Fast loop: freehire-only, so first_seen stays current between the 6h full crawls and the board
    # reads "New Xm ago". freehire is keyless/free with no published limit; each tick is a small pull.
    AutoUpdater(make_watchlist,
                lambda w: refresh_watchlist(w, detail_budgets=feed_detail_budgets()),
                discover_fn=discover_fn, fast_fn=lambda w: refresh_freehire_only(w)).start()
    return True


def register_feed(app):
    """Add the public feed routes to an existing Flask app (the broker) and start the robot.
    Read-only + public on purpose: it serves only public job listings, no personal data."""

    @app.get("/feed")
    def _feed():                                     # noqa: ANN202
        # Server-side faceted + paginated: the cached full list is filtered by the query facets,
        # then ONE page is returned. So the board can browse tens of thousands (with a true total
        # count) while every response stays small. `count` = filtered total; `total` = unfiltered.
        from sourcing.filters import apply_facets, paginate
        a = request.args
        try:
            facets = dict(q=a.get("q", ""), loc=a.get("loc", ""), days=a.get("days", 0),
                          remote=a.get("remote", ""),
                          visa=[v for v in (a.get("visa", "").split(",")) if v],
                          level=a.get("level", ""), pay=a.get("pay", 0), sort=a.get("sort", ""),
                          sponsored=a.get("sponsored", ""))
            full = _cached_feed(a.get("level"))
            filtered = apply_facets(full, **facets)
            # On-demand search: a keyword that's thin in the pre-built board triggers a live
            # query into freehire's full 1M+ index, so search reaches everything, not just the
            # pre-loaded slice. The extra roles are folded in (same facets, deduped).
            qterm = facets["q"].strip()
            if qterm and len(filtered) < SEARCH_MIN:
                extra = apply_facets(_freehire_live(qterm), **facets)
                seen = {j.get("source_id") for j in filtered}
                filtered = filtered + [j for j in extra if j.get("source_id") not in seen]
            page_jobs, count, page, per_page = paginate(
                filtered, a.get("page", 1), a.get("per_page", FEED_PER_PAGE))
            # Report WHEN this board was last built so the client's "updated X ago" is truthful.
            # The cache tuple is (build_epoch, jobs); without this the client froze on a stale value
            # and showed a made-up age (e.g. "updated 33h ago") that never advanced.
            hit = _FEED_CACHE.get((a.get("level") or "").lower())
            built = _iso(hit[0]) if hit else None
            return jsonify({"jobs": page_jobs, "count": count, "total": len(full),
                            "page": page, "per_page": per_page, "refreshed_at": built})
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
            return jsonify({"jobs": [], "count": 0, "total": 0, "page": 1,
                            "per_page": FEED_PER_PAGE, "error": "feed unavailable"}), 503

    @app.get("/feed/detail")
    def _feed_detail():                              # noqa: ANN202
        sid = (request.args.get("source_id") or "").strip()
        if not sid:
            return jsonify({"error": "missing source_id"}), 400
        try:
            d = feed_detail(sid)
        except Exception:                            # noqa: BLE001
            traceback.print_exc()
            return jsonify({"error": "detail unavailable"}), 503
        if not d:
            return jsonify({"error": "not found"}), 404
        return jsonify(d)

    @app.get("/feed/health")
    def _feed_health():                              # noqa: ANN202
        # Cheap by design: report the warm cache's size, never trigger a full (slow) rebuild.
        # Render's health check hits this; recomputing the feed here is what made it time out.
        hit = _FEED_CACHE.get("")
        return jsonify({"ok": True, "jobs": len(hit[1]) if hit else 0, "warm": bool(hit)})

    # Warm the cache on boot (background) so the FIRST request after a deploy never pays the cold
    # build synchronously -- that cold hit, on a growing DB, is what times out and forces the
    # desktop app back to its local feed. After this one warm build, stale-while-revalidate keeps
    # it fast forever. IMPORTANT: warm BEFORE starting the robot, so the build reads the existing
    # DB before the robot's heavy refresh (freehire bulk = a long write) saturates the single
    # worker. Production-only (JOBS_AUTOUPDATE) so tests keep their cold cache.
    if os.environ.get("JOBS_AUTOUPDATE") == "1":
        def _warm() -> None:
            try:
                _cached_feed(None)
            except Exception:                            # noqa: BLE001 - a failed warm just means
                traceback.print_exc()                    # the first real request builds it instead
        threading.Thread(target=_warm, name="feed-warm-on-boot", daemon=True).start()
    _start_robot()
    return app
