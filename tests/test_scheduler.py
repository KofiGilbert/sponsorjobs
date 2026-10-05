"""Auto-updater: keep the job feed fresh on its own (2026-08-08).

Covers the two guarantees that make the feed trustworthy without anyone pressing a button:
stale roles (vanished from their board = filled/closed) get pruned, and one maintenance pass
runs refresh + prune + a paced discovery WITHOUT ever crashing the loop. All offline: fake
refresh/discover callables, a fake clock, no threads, no network.
"""

from __future__ import annotations

from sourcing.scheduler import AutoUpdater
from sourcing.watchlist import Watchlist


def _job(sid, title="Engineer"):
    return {"source_id": sid, "source": "greenhouse", "company": "Acme", "title": title,
            "location": "Austin, TX", "remote": "", "url": "https://x", "jd_text": "",
            "posted_at": "2026-08-01"}


def test_reader_not_blocked_by_open_writer(tmp_path):
    """WAL: a feed-style reader must serve the committed snapshot even while the crawler holds an
    OPEN write transaction. SQLite's default rollback-journal mode blocked the reader and returned
    'database is locked' 503s on the hosted feed once the fast refresh loop made writes frequent."""
    p = tmp_path / "wl.db"
    w = Watchlist(p)
    assert w._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    w.upsert_jobs([_job("greenhouse:acme:1")])
    w._conn.execute("BEGIN IMMEDIATE")                       # crawler grabs the write lock
    w._conn.execute("INSERT INTO sourced_job(source_id, company, title) VALUES('x','C','T')")
    reader = Watchlist(p)                                    # a separate connection, like a request
    try:
        rows = reader.list_jobs()                            # must NOT raise "database is locked"
        assert any(r["source_id"] == "greenhouse:acme:1" for r in rows)   # sees the committed row
    finally:
        reader.close()
        w._conn.rollback()
        w.close()


# -- freshness: prune roles that have disappeared ------------------------- #

def test_prune_removes_only_long_unseen_jobs(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1"), _job("greenhouse:acme:2")])
    # Age job 1 far past the window; job 2 stays fresh (seen just now).
    w._conn.execute("UPDATE sourced_job SET last_seen=datetime('now','-40 days') "
                    "WHERE source_id='greenhouse:acme:1'")
    w._conn.commit()
    assert w.prune_stale(days=21) == 1
    left = {j["source_id"] for j in w.list_jobs()}
    assert left == {"greenhouse:acme:2"}


def test_reappearing_in_a_refresh_keeps_a_job_alive(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1")])
    w._conn.execute("UPDATE sourced_job SET last_seen=datetime('now','-40 days')")
    w._conn.commit()
    w.upsert_jobs([_job("greenhouse:acme:1", title="Engineer (updated)")])   # seen again -> refreshed
    assert w.prune_stale(days=21) == 0                                       # no longer stale
    assert w.list_jobs()[0]["title"] == "Engineer (updated)"


# -- one maintenance pass ------------------------------------------------- #

class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def test_run_once_refreshes_prunes_and_paces_discovery(tmp_path):
    calls = {"refresh": 0, "discover": 0}

    def refresh_fn(w):
        calls["refresh"] += 1
        w.upsert_jobs([_job("greenhouse:acme:1")])
        return {"new": 1}

    def discover_fn():
        calls["discover"] += 1
        return {"added": 3}

    clock = _Clock()
    up = AutoUpdater(make_watchlist=lambda: Watchlist(tmp_path / "wl.db"),
                     refresh_fn=refresh_fn, discover_fn=discover_fn,
                     discover_every=1000, clock=clock)
    s1 = up.run_once()                       # first pass: refresh + discover
    assert calls == {"refresh": 1, "discover": 1} and s1["discover"] == {"added": 3}
    clock.t += 500                           # not yet a week -> no discovery
    up.run_once()
    assert calls == {"refresh": 2, "discover": 1}
    clock.t += 600                           # now past the discover window -> discovers again
    up.run_once()
    assert calls == {"refresh": 3, "discover": 2}


def test_run_once_survives_a_failing_refresh(tmp_path):
    def boom(w):
        raise RuntimeError("board exploded")

    up = AutoUpdater(make_watchlist=lambda: Watchlist(tmp_path / "wl.db"), refresh_fn=boom)
    summary = up.run_once()                   # must NOT raise
    assert summary.get("error") == "refresh"


# -- fast (freehire-only) light loop -------------------------------------- #

def test_run_fast_once_runs_only_the_fast_fn(tmp_path):
    calls = {"full": 0, "fast": 0}

    def full(w):
        calls["full"] += 1
        return {"new": 0}

    def fast(w):
        calls["fast"] += 1
        w.upsert_jobs([_job("freehire:acme:1")])
        return {"new": 1}

    up = AutoUpdater(make_watchlist=lambda: Watchlist(tmp_path / "wl.db"),
                     refresh_fn=full, fast_fn=fast)
    s = up.run_fast_once()
    assert calls == {"full": 0, "fast": 1}     # the fast pass touches ONLY the fast source
    assert s["fast"] == {"new": 1}


def test_run_fast_once_survives_a_failing_fast_fn(tmp_path):
    def boom(w):
        raise RuntimeError("freehire down")

    up = AutoUpdater(make_watchlist=lambda: Watchlist(tmp_path / "wl.db"),
                     refresh_fn=lambda w: None, fast_fn=boom)
    assert up.run_fast_once().get("error") == "fast"   # must NOT raise
