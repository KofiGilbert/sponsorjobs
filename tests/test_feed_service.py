"""Central job-feed service — the shared "kitchen" (2026-08-08).

The hosted service serves a public, read-only jobs feed and runs the freshness robot in ONE
worker. These tests pin the parts that must be right for a safe deploy: the /feed route shape,
the single-worker lock (so N gunicorn workers don't multiply paid API calls), the robot staying
OFF unless JOBS_AUTOUPDATE=1, and the desktop app pulling from the central feed (with a local
fallback). All offline — no network, no real crawl.
"""

from __future__ import annotations

import importlib

import pytest
from flask import Flask

import backend.feed as feed


def _app_with_feed(monkeypatch, jobs):
    monkeypatch.delenv("JOBS_AUTOUPDATE", raising=False)      # robot stays off in tests
    feed._FEED_CACHE.clear()                                  # module-global cache: isolate tests
    monkeypatch.setattr(feed, "feed_jobs", lambda level=None:
                        [j for j in jobs if level != "entry" or j.get("entry_level")])
    app = Flask(__name__)
    feed.register_feed(app)
    return app.test_client()


def test_feed_route_returns_jobs(monkeypatch):
    jobs = [{"company": "Stripe", "title": "Engineer", "us": True, "entry_level": False},
            {"company": "Datadog", "title": "New Grad Engineer", "us": True, "entry_level": True}]
    c = _app_with_feed(monkeypatch, jobs)
    r = c.get("/feed")
    assert r.status_code == 200
    d = r.get_json()
    assert d["count"] == 2 and len(d["jobs"]) == 2


def test_feed_route_reports_build_time(monkeypatch):
    # The client's "updated X ago" needs a real timestamp; without it the label froze on a stale,
    # made-up age. /feed must report when the served cache was built, as parseable UTC ISO 8601.
    from datetime import datetime
    c = _app_with_feed(monkeypatch, [{"company": "Stripe", "title": "Engineer", "us": True}])
    d = c.get("/feed").get_json()
    assert d.get("refreshed_at")                                  # present, not None
    parsed = datetime.fromisoformat(d["refreshed_at"])           # valid ISO 8601...
    assert parsed.tzinfo is not None                             # ...and timezone-aware (UTC)


def test_feed_route_honors_entry_level_filter(monkeypatch):
    jobs = [{"company": "Stripe", "title": "Engineer", "entry_level": False},
            {"company": "Datadog", "title": "New Grad Engineer", "entry_level": True}]
    c = _app_with_feed(monkeypatch, jobs)
    d = c.get("/feed?level=entry").get_json()
    assert d["count"] == 1 and d["jobs"][0]["company"] == "Datadog"


def test_feed_health_is_cheap_and_reports_the_warm_cache(monkeypatch):
    # Health must NOT recompute the feed (that full rebuild is what timed out on Render). Cold,
    # it reports 0/not-warm; after a /feed call warms the cache, it reports the cached count.
    c = _app_with_feed(monkeypatch, [{"company": "X", "title": "Y"}])
    assert c.get("/feed/health").get_json() == {"ok": True, "jobs": 0, "warm": False}
    c.get("/feed")                                            # warm the cache
    assert c.get("/feed/health").get_json() == {"ok": True, "jobs": 1, "warm": True}


def test_feed_detail_route(monkeypatch):
    c = _app_with_feed(monkeypatch, [])
    monkeypatch.setattr(feed, "feed_detail",
                        lambda sid: {"job": {"source_id": sid, "company": "Stripe"}, "jd": "Build."}
                        if sid == "greenhouse:stripe:1" else None)
    assert c.get("/feed/detail").status_code == 400                       # missing source_id
    assert c.get("/feed/detail?source_id=nope").status_code == 404        # unknown role
    d = c.get("/feed/detail?source_id=greenhouse:stripe:1").get_json()
    assert d["job"]["company"] == "Stripe" and d["jd"] == "Build."


def test_robot_stays_off_without_the_env_flag(monkeypatch):
    monkeypatch.delenv("JOBS_AUTOUPDATE", raising=False)
    assert feed._start_robot() is False


def test_sponsor_overlay_rebuilds_from_the_committed_seed(tmp_path):
    # The server has no 67MB sponsors.db; it rebuilds the overlay from the small seed instead.
    import csv
    import gzip
    seed = tmp_path / "seed.csv.gz"
    with gzip.open(seed, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["norm_name", "display_name", "h1b_approvals", "h1b_last_fy",
                    "h1b_first_fy", "naics", "state", "cap_exempt", "e_verify", "perm_certs"])
        w.writerow(["acme", "Acme Inc", 50, 2023, 2021, "5415", "CA", 0, 1, 3])
    from sourcing.sponsors import SponsorDB
    db = SponsorDB(str(tmp_path / "s.db"))
    assert db.is_empty()
    assert db.rebuild_from_seed(str(seed)) == 1
    rec = db.lookup("Acme")
    assert rec and rec.h1b_approvals == 50 and rec.e_verify and rec.perm_certs == 3
    assert {b["code"] for b in rec.badges()} >= {"H-1B", "GREEN-CARD", "STEM-OPT"}


def test_only_one_worker_wins_the_singleton_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_DATA_DIR", str(tmp_path))
    feed._LOCK_FD = None
    assert feed._acquire_singleton() is True          # first worker grabs it
    assert feed._acquire_singleton() is False         # second worker is locked out


# -- the desktop app pulling from the central feed ------------------------ #

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A


def _gz_feed(rows):
    import gzip
    import json
    return gzip.compress(json.dumps({"feed_version": 1, "generated_at": "2026-09-30T12:00:00+00:00",
                                     "count": len(rows), "jobs": rows}).encode())


class _Resp:
    def __init__(self, body): self._b = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def test_app_pulls_from_central_feed_when_configured(client, monkeypatch):
    # The "central" source is now the STATIC feed: a gzipped jobs.json.gz downloaded into the data
    # dir and filtered locally (docs/feed.md). The response contract the client reads is unchanged.
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://kitchen.example.com")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(_gz_feed([{"source_id": "x:1", "company": "Stripe", "title": "SWE"}])))
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "central" and d["jobs"][0]["company"] == "Stripe"
    assert d["refreshed_at"] == "2026-09-30T12:00:00+00:00" and d["total"] == 1


def test_an_empty_central_feed_is_not_an_authoritative_empty_board(client, monkeypatch):
    # A feed that downloads fine but holds ZERO jobs (an empty jobs.json.gz from a bad central
    # crawl, a fresh bucket) used to come back as source=central, total=0, crawling=False: the
    # client saw nothing wrong and the first-open crawl never fired. It is now treated like an
    # unreachable feed: the local path runs, flagged degraded, and the emptiness is remembered.
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://kitchen.example.com")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(_gz_feed([])))
    d = A.app.test_client().get("/api/jobs").get_json()
    assert not (d["source"] == "central" and d["total"] == 0)
    assert d["source"] == "local" and d["degraded"] is True
    assert A._feed_empty_for() > 0
    # Rows coming back clear the streak and the feed is central again. (Make the next download
    # due: the hourly slot is persisted in the reader's state file.)
    import json
    state = A._DATA / "feed_cache" / "feed_state.json"
    st = json.loads(state.read_text())
    st.pop("last_check", None)
    state.write_text(json.dumps(st))
    A._STATIC_FEEDS.clear()
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp(_gz_feed([{"source_id": "x:1", "company": "Stripe", "title": "SWE"}])))
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "central" and d["total"] == 1 and A._feed_empty_for() == 0


def test_app_falls_back_to_local_when_central_is_down(client, monkeypatch):
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://kitchen.example.com")
    def boom(*a, **k):
        raise OSError("central down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = A.app.test_client().get("/api/jobs")
    assert r.status_code == 200                        # did NOT crash; fell back to local
    d = r.get_json()
    assert d.get("source") != "central"
    # Flagged degraded so the client keeps its last good board (and retries) instead of flashing the
    # small stale fallback every time the kitchen restarts. See app.js loadPage/scheduleReconnect.
    assert d.get("degraded") is True


def test_local_only_install_is_not_flagged_degraded(client, monkeypatch):
    # With no JOBS_FEED_URL this is a genuine local install, not a failed kitchen fetch, so the feed
    # must NOT be marked degraded (else the client would sit in a pointless reconnect loop).
    A = client
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d.get("source") == "local" and not d.get("degraded")


def test_app_pulls_job_detail_from_central(client, monkeypatch):
    import gzip
    import json
    from sourcing.feedfile import shard_for
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://kitchen.example.com")
    row = {"source_id": "greenhouse:stripe:1", "company": "Stripe", "title": "SWE"}

    def urlopen(req, timeout=30):
        if req.full_url.endswith("/jobs.json.gz"):
            return _Resp(_gz_feed([row]))
        assert req.full_url.endswith(f"/jd/{shard_for('greenhouse:stripe:1')}.json.gz")
        return _Resp(gzip.compress(json.dumps({"greenhouse:stripe:1": "Build systems."}).encode()))
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    d = A.app.test_client().get("/api/jobs/detail?source_id=greenhouse:stripe:1").get_json()
    assert d["jd"] == "Build systems." and d["job"]["company"] == "Stripe"
    assert "match" in d                                # match is computed locally, not fetched


def test_empty_local_store_starts_one_first_open_crawl(client, monkeypatch):
    # A fresh install with no reachable feed used to sit on "0 live roles" forever: nothing ever
    # filled the empty local store. The local path now starts ONE background crawl and tells the
    # client, which re-polls. A second request while it runs must not start another.
    import threading
    A = client
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    monkeypatch.setattr(A, "_LOCAL_CRAWL", {"thread": None, "started": 0.0, "done": 0.0})
    monkeypatch.setenv("JOBS_FIRST_CRAWL", "1")          # the suite switches it off by default
    calls = []
    gate = threading.Event()

    def fake_refresh(w):
        calls.append(1)
        gate.wait(5)
        return {"new": 0}
    monkeypatch.setattr(A, "first_open_refresh", fake_refresh)   # the small polite pass, not the full crawl
    c = A.app.test_client()
    d1 = c.get("/api/jobs").get_json()
    d2 = c.get("/api/jobs").get_json()
    assert d1["source"] == "local" and d1["crawling"] is True
    assert d2["crawling"] is True
    gate.set()
    A._LOCAL_CRAWL["thread"].join(5)
    assert calls == [1]                                  # one crawl, not one per request
    d3 = c.get("/api/jobs").get_json()
    assert d3["crawling"] is False                       # finished: the client drops back to its slow poll
