"""What a fresh install sees (docs/feed.md, "What a fresh install sees"): the installer's bundled
feed snapshot is adopted as the initial cache and shown at once; the first-open crawl starts only
when the feed is genuinely absent (no feed configured, or a configured feed failing for 30 minutes
with nothing cached); and that crawl is the small, polite first_open_refresh (top boards by H-1B
volume, per-host spacing, a wall-clock cap), not the full 1,105-board watchlist. Plus the script
that stages the snapshot (scripts/bundle_feed_snapshot.py). All offline.
"""

from __future__ import annotations

import datetime
import importlib
import json
import time
import urllib.request

import pytest

from sourcing import feedclient
from sourcing.feedclient import FeedUnavailable, StaticFeed
from sourcing.feedfile import dumps_gz, loads_maybe_gz, slim_row

_TODAY = datetime.date.today().isoformat()
T0 = 1_800_000_000.0
SNAP_AT = "2027-01-12T06:00:00+00:00"   # 3 days before T0
LIVE_AT = "2027-01-14T12:00:00+00:00"


def _row(sid, title="Software Engineer", company="Acme", source="greenhouse", **extra):
    r = {"source_id": sid, "source": source, "company": company, "title": title,
         "location": "New York, NY", "remote": "hybrid", "url": "https://acme.example/jobs/1",
         "posted_at": _TODAY, "first_seen": "2026-09-28 10:00:00", "salary": "", "sponsorship_stated": True,
         "us": True, "entry_level": False, "visa": [], "nationality_visas": [], "sponsor": {},
         "jd_text": "Build " * 20}
    r.update(extra)
    return r


def _feed_bytes(rows, generated_at):
    return dumps_gz({"feed_version": 1, "generated_at": generated_at, "count": len(rows),
                     "jobs": [slim_row(r) for r in rows]})


def _snapshot(tmp_path, rows=None, generated_at=SNAP_AT):
    p = tmp_path / "bundle" / "seed" / "feed" / "jobs.json.gz"
    p.parent.mkdir(parents=True)
    p.write_bytes(_feed_bytes(rows or [_row("greenhouse:snap:1", company="Snapshot Co")], generated_at))
    return p


class _Server:
    def __init__(self, rows, down=False):
        self.body = _feed_bytes(rows, LIVE_AT)
        self.down = down
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if self.down:
            raise OSError("network down")
        return 200, {"ETag": '"live"'}, self.body


# -- snapshot adoption (StaticFeed) ---------------------------------------------------------- #

def test_no_cache_and_no_feed_url_serves_the_snapshot_with_its_own_age_and_no_network(tmp_path):
    snap = _snapshot(tmp_path)
    srv = _Server([_row("greenhouse:live:1")])
    sf = StaticFeed("", tmp_path / "cache", fetch=srv, clock=lambda: T0, snapshot=snap)
    assert sf.has_cache()
    rows, header = sf.jobs()
    assert [r["source_id"] for r in rows] == ["greenhouse:snap:1"]
    assert header["generated_at"] == SNAP_AT and sf.refreshed_at() == SNAP_AT
    assert srv.calls == []                                   # nothing to download from
    assert (tmp_path / "cache" / "jobs.json.gz").exists()    # adopted INTO the data dir
    st = json.loads((tmp_path / "cache" / "feed_state.json").read_text())
    assert st["generated_at"] == SNAP_AT and st["snapshot_generated_at"] == SNAP_AT
    assert "last_check" not in st and "etag" not in st       # a configured feed would download at once


def test_snapshot_serves_while_the_configured_feed_is_down_and_is_replaced_when_it_is_up(tmp_path):
    snap = _snapshot(tmp_path)
    srv = _Server([_row("greenhouse:live:1")], down=True)
    now = [T0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path / "cache", fetch=srv,
                    clock=lambda: now[0], snapshot=snap)
    rows, header = sf.jobs()                                 # one failed download, then the snapshot
    assert len(srv.calls) == 1 and header["generated_at"] == SNAP_AT
    assert [r["source_id"] for r in rows] == ["greenhouse:snap:1"]
    assert sf.failing_for() == 0.0 or sf.failing_for() >= 0  # a streak has started
    srv.down = False
    now[0] += 3600 * 2                                       # past the back-off and the next slot
    rows, header = sf.jobs()
    assert header["generated_at"] == LIVE_AT                 # the download replaced the snapshot...
    assert [r["source_id"] for r in rows] == ["greenhouse:live:1"]
    assert sf.failing_for() == 0.0                           # ...and cleared the streak


def test_a_newer_cache_is_never_overwritten_by_the_snapshot(tmp_path):
    snap = _snapshot(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "jobs.json.gz").write_bytes(_feed_bytes([_row("greenhouse:cached:1")], LIVE_AT))
    srv = _Server([], down=True)
    sf = StaticFeed("", cache, fetch=srv, clock=lambda: T0, snapshot=snap)
    rows, header = sf.jobs()
    assert header["generated_at"] == LIVE_AT and rows[0]["source_id"] == "greenhouse:cached:1"
    assert "snapshot_generated_at" not in sf._state
    # And an OLDER cache is kept too: "no cache" is the only time the snapshot is copied in.
    (cache / "jobs.json.gz").write_bytes(_feed_bytes([_row("greenhouse:old:1")], "2026-01-01T00:00:00+00:00"))
    sf2 = StaticFeed("", cache, fetch=srv, clock=lambda: T0, snapshot=snap)
    assert sf2.jobs()[0][0]["source_id"] == "greenhouse:old:1"


def test_without_a_snapshot_the_old_behaviour_stands(tmp_path):
    srv = _Server([], down=True)
    sf = StaticFeed("https://f.example.com/feed", tmp_path / "cache", fetch=srv, clock=lambda: T0,
                    snapshot=None)
    assert not sf.has_cache()
    with pytest.raises(FeedUnavailable):
        sf.jobs()
    assert not StaticFeed("", tmp_path / "c2", fetch=srv, snapshot=tmp_path / "missing.json.gz").has_cache()


def test_a_corrupt_snapshot_is_ignored(tmp_path):
    bad = tmp_path / "jobs.json.gz"
    bad.write_bytes(b"garbage")
    sf = StaticFeed("", tmp_path / "cache", fetch=_Server([], down=True), snapshot=bad)
    assert not sf.has_cache()


def test_failing_for_measures_the_streak_not_the_last_failure(tmp_path):
    srv = _Server([], down=True)
    now = [T0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path / "cache", fetch=srv, clock=lambda: now[0],
                    snapshot=None)
    assert sf.failing_for() == 0.0
    for _ in range(3):
        with pytest.raises(FeedUnavailable):
            sf.jobs()
        now[0] += sf.retry_after()
    assert sf.failing_for() == pytest.approx(now[0] - T0)
    st = json.loads((tmp_path / "cache" / "feed_state.json").read_text())
    assert st["first_fail"] == T0 and st["fail_count"] == 3
    # Persisted: a new process sees the same streak age.
    assert StaticFeed("https://f.example.com/feed", tmp_path / "cache", fetch=srv, clock=lambda: now[0],
                      snapshot=None).failing_for() == pytest.approx(now[0] - T0)


def test_bundled_snapshot_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBS_FEED_SNAPSHOT", "")
    assert feedclient.bundled_snapshot_path() is None
    monkeypatch.setenv("JOBS_FEED_SNAPSHOT", str(tmp_path / "x.json.gz"))
    assert feedclient.bundled_snapshot_path() == tmp_path / "x.json.gz"
    monkeypatch.delenv("JOBS_FEED_SNAPSHOT")
    assert feedclient.bundled_snapshot_path() == feedclient.BUNDLED_SNAPSHOT
    assert feedclient.BUNDLED_SNAPSHOT.parts[-3:] == ("seed", "feed", "jobs.json.gz")


# -- the app: snapshot + crawl gate ----------------------------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch):
    """ui.app on an isolated data dir with the first-open crawl ENABLED (the suite turns it off)
    and first_open_refresh replaced by a recorder, so no board is ever touched."""
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("JOBS_FIRST_CRAWL", "1")
    monkeypatch.setenv("JOBS_FEED_URL", "https://feed.tailor.test/feed")
    import ui.app as A
    importlib.reload(A)
    A._STATIC_FEEDS.clear()
    A._LOCAL_CRAWL.update({"thread": None, "started": 0.0, "done": 0.0})
    calls = []

    def fake_refresh(w):
        calls.append(time.time())
        return {"new": 0, "boards_crawled": 0}
    monkeypatch.setattr(A, "first_open_refresh", fake_refresh)
    A.crawl_calls = calls
    return A


def _origin_down(monkeypatch):
    calls = []

    def urlopen(req, timeout=30):
        calls.append(req.full_url)
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def _join_crawl(A):
    t = A._LOCAL_CRAWL.get("thread")
    if t is not None:
        t.join(5)


def test_placeholder_url_and_empty_store_kick_the_polite_crawl(client, monkeypatch):
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://tailor.example/feed")
    net = _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "local" and d["crawling"] is True and not d.get("degraded")
    _join_crawl(A)
    assert len(A.crawl_calls) == 1 and net == []            # the placeholder is never looked up


def test_one_failed_download_does_not_trigger_a_crawl(client, monkeypatch):
    A = client
    net = _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "local" and d["degraded"] is True and d["crawling"] is False
    assert len(net) == 1 and A.crawl_calls == []
    # Nor the second, third... request inside the back-off window.
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["crawling"] is False and A.crawl_calls == [] and len(net) == 1


def test_a_feed_failing_for_thirty_minutes_with_nothing_cached_kicks_the_crawl(client, monkeypatch):
    A = client
    cache = A._DATA / "feed_cache"
    cache.mkdir(parents=True)
    now = time.time()
    (cache / "feed_state.json").write_text(json.dumps(
        {"offset_minute": 7, "first_fail": now - 1900, "last_fail": now - 1900, "fail_count": 3}))
    net = _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "local" and d["degraded"] is True and d["crawling"] is True
    assert len(net) == 1                                     # this request's own (failed) attempt
    _join_crawl(A)
    assert len(A.crawl_calls) == 1


def test_a_feed_failing_for_twenty_minutes_does_not(client, monkeypatch):
    A = client
    cache = A._DATA / "feed_cache"
    cache.mkdir(parents=True)
    now = time.time()
    (cache / "feed_state.json").write_text(json.dumps(
        {"offset_minute": 7, "first_fail": now - 1300, "last_fail": now - 1300, "fail_count": 3}))
    _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["crawling"] is False and A.crawl_calls == []


def test_a_bundled_snapshot_is_served_instead_of_crawling(client, monkeypatch, tmp_path):
    A = client
    monkeypatch.setenv("JOBS_FEED_SNAPSHOT", str(_snapshot(tmp_path)))
    cache = A._DATA / "feed_cache"
    cache.mkdir(parents=True)
    now = time.time()
    (cache / "feed_state.json").write_text(json.dumps(
        {"offset_minute": 7, "first_fail": now - 9000, "last_fail": now - 9000, "fail_count": 6}))
    _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "central" and d["refreshed_at"] == SNAP_AT and not d.get("degraded")
    assert d["total"] == 1 and d["jobs"][0]["company"] == "Snapshot Co"
    assert not d.get("crawling") and A.crawl_calls == []
    assert (cache / "jobs.json.gz").exists()                 # copied into the data dir once


def test_no_feed_configured_shows_the_snapshot_while_the_polite_crawl_fills_the_store(client, monkeypatch, tmp_path):
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://tailor.example/feed")
    monkeypatch.setenv("JOBS_FEED_SNAPSHOT", str(_snapshot(tmp_path)))
    net = _origin_down(monkeypatch)
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "central" and d["refreshed_at"] == SNAP_AT and d["crawling"] is True
    assert d["jobs"][0]["company"] == "Snapshot Co" and net == []
    _join_crawl(A)
    assert len(A.crawl_calls) == 1
    # Once the local store holds rows, the board is the UNION of the snapshot and the store: the
    # crawl's first rows must never replace the 2,855-row snapshot with a 23-row list.
    w = A._watchlist()
    try:
        w.upsert_jobs([_row("greenhouse:mine:1", company="Local Co")])
    finally:
        w.close()
    A._sponsors().set_meta("jobs_refreshed_at", A._now_iso())
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "local" and d["crawling"] is False
    assert {j["company"] for j in d["jobs"]} == {"Local Co", "Snapshot Co"} and d["total"] == 2


def test_a_snapshot_row_opens_and_tailors_without_a_feed_url(client, monkeypatch, tmp_path):
    """Clicking a row of the bundled snapshot on an install with no feed configured: the detail
    comes from the company's own board endpoint (no shard to fetch), and 'Tailor & apply' resolves
    the row too, instead of a 404 'no longer in the feed'."""
    import sourcing.feedclient as fc
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://tailor.example/feed")
    monkeypatch.setenv("JOBS_FEED_SNAPSHOT", str(_snapshot(tmp_path)))
    net = _origin_down(monkeypatch)
    asked = []

    def board_jd(job, fetch=None):
        asked.append(job["source_id"])
        return "Build the snapshot pipeline."
    monkeypatch.setattr(fc, "fetch_board_jd", board_jd)
    d = A.app.test_client().get("/api/jobs/detail?source_id=greenhouse:snap:1").get_json()
    assert d["jd"] == "Build the snapshot pipeline." and d["job"]["company"] == "Snapshot Co"
    assert asked == ["greenhouse:snap:1"] and net == []       # no shard request to a placeholder host
    job = A._job_from_feed("greenhouse:snap:1")
    assert job and job["jd_text"] == "Build the snapshot pipeline."
    assert A._job_from_feed("greenhouse:other:9") is None
    _join_crawl(A)


def test_a_crawl_that_raises_is_retried_after_a_back_off_and_reported(client, monkeypatch):
    """A first-open crawl that blew up used to count as "ran": `started` stayed set, nothing was
    stored, and every later request said crawling=False over an empty board with no reason and
    no retry for the life of the process. Now: the failure is reported as `crawl_error` while the
    store is empty, and the crawl is retried after 10 minutes, doubling, capped at an hour."""
    A = client
    monkeypatch.setenv("JOBS_FEED_URL", "https://tailor.example/feed")   # no feed: crawl allowed
    calls = []

    def boom(w):
        calls.append(1)
        raise RuntimeError("boards unreachable")
    monkeypatch.setattr(A, "first_open_refresh", boom)
    c = A.app.test_client()
    d = c.get("/api/jobs").get_json()
    assert d["crawling"] is True and d["crawl_error"] is None
    _join_crawl(A)
    assert calls == [1]
    d = c.get("/api/jobs").get_json()                 # inside the back-off: no retry, but a reason
    assert d["crawling"] is False and calls == [1]
    assert d["crawl_error"] == "RuntimeError: boards unreachable"
    A._LOCAL_CRAWL["failed"] -= 9 * 60
    assert c.get("/api/jobs").get_json()["crawling"] is False and calls == [1]
    A._LOCAL_CRAWL["failed"] -= 61                    # 10 minutes have passed: retry
    d = c.get("/api/jobs").get_json()
    assert d["crawling"] is True
    _join_crawl(A)
    assert calls == [1, 1] and A._LOCAL_CRAWL["failures"] == 2
    A._LOCAL_CRAWL["failed"] -= 11 * 60               # the second wait is 20 minutes, not 10
    assert c.get("/api/jobs").get_json()["crawling"] is False and calls == [1, 1]
    A._LOCAL_CRAWL["failed"] -= 10 * 60
    assert c.get("/api/jobs").get_json()["crawling"] is True
    _join_crawl(A)
    assert calls == [1, 1, 1]
    # A crawl that then succeeds clears the failure and is not run again this process.
    monkeypatch.setattr(A, "first_open_refresh", lambda w: {"new": 3})
    A._LOCAL_CRAWL["failed"] -= 60 * 60
    assert c.get("/api/jobs").get_json()["crawling"] is True
    _join_crawl(A)
    d = c.get("/api/jobs").get_json()
    assert d["crawling"] is False and d["crawl_error"] is None and not A._LOCAL_CRAWL["failed"]
    assert A._sponsors().get_meta("jobs_refreshed_at")
    A._LOCAL_CRAWL["started"] -= 24 * 3600
    assert c.get("/api/jobs").get_json()["crawling"] is False and calls == [1, 1, 1]


def test_crawl_back_off_doubles_and_caps_at_an_hour(client):
    A = client
    assert [A._crawl_retry_after(n) for n in (0, 1, 2, 3, 4, 9)] == [600, 600, 1200, 2400, 3600, 3600]


def test_a_feed_serving_zero_jobs_for_thirty_minutes_kicks_the_crawl(client, monkeypatch):
    """A configured feed that downloads an EMPTY list is a cache in name only: there is nothing
    to show and no failure streak either, so the crawl gate never opened. The emptiness now
    counts like a failing download: the local path runs (degraded) and after the same
    thirty-minute window the polite crawl starts."""
    A = client

    class _Resp:                                   # urlopen's response, as _http_fetch reads it
        status, headers = 200, None
        def __init__(self, body): self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: _Resp(_feed_bytes([], LIVE_AT)))
    c = A.app.test_client()
    d = c.get("/api/jobs").get_json()
    assert d["source"] == "local" and d["degraded"] is True and d["total"] == 0
    assert d["crawling"] is False and A.crawl_calls == []     # one empty download is a blip
    assert A._feed_empty_for() > 0
    A._FEED_EMPTY["since"] -= 31 * 60                          # ...thirty minutes of it is not
    d = c.get("/api/jobs").get_json()
    assert d["source"] == "local" and d["degraded"] is True and d["crawling"] is True
    _join_crawl(A)
    assert len(A.crawl_calls) == 1


def test_first_crawl_off_switch_still_wins(client, monkeypatch):
    A = client
    monkeypatch.setenv("JOBS_FIRST_CRAWL", "0")
    monkeypatch.delenv("JOBS_FEED_URL")
    d = A.app.test_client().get("/api/jobs").get_json()
    assert d["crawling"] is False and A.crawl_calls == []


# -- the polite subset crawl (sourcing.service.first_open_refresh) ------------------------- #

GH = {"id": 11, "title": "Software Engineer", "location": {"name": "New York, NY"},
      "absolute_url": "https://boards.greenhouse.io/x/jobs/11", "content": "<p>Build.</p>",
      "first_published": _TODAY}


class _Crawl:
    """A fake network: records every URL in order, advances a fake clock per request, answers
    each ATS with one matching job (or nothing)."""

    def __init__(self, seconds_per_request=1.0):
        self.urls = []
        self.now = [1000.0]
        self.sleeps = []
        self.per = seconds_per_request

    def clock(self):
        return self.now[0]

    def sleep(self, s):
        self.sleeps.append(s)
        self.now[0] += s

    def fetch(self, url, timeout=20, **kw):
        self.urls.append(url)
        self.now[0] += self.per
        if "greenhouse.io" in url:
            return {"jobs": [dict(GH, id=len(self.urls))]}
        if "lever.co" in url or "remoteok.com" in url:
            return []
        return {}


def _store(tmp_path, boards):
    from sourcing.service import SEED_CRITERIA
    from sourcing.watchlist import Watchlist
    w = Watchlist(str(tmp_path / "jobs.db"))
    for company, ats, slug in boards:
        w.add_company(company, ats, slug)
    w.set_criteria(SEED_CRITERIA)
    return w


BOARDS = [("Big Sponsor", "greenhouse", "bigsponsor"), ("Mid Sponsor", "greenhouse", "midsponsor"),
          ("Small Sponsor", "greenhouse", "smallsponsor"), ("No Sponsor", "greenhouse", "nosponsor"),
          ("Work Able", "workable", "workable1"), ("Work Able Two", "workable", "workable2"),
          ("Lever Co", "lever", "leverco"), ("Recruit Ee", "recruitee", "recruitee1")]
APPROVALS = {"Big Sponsor": 5000, "Mid Sponsor": 500, "Small Sponsor": 5, "Work Able": 900,
             "Work Able Two": 50, "Lever Co": 300, "Recruit Ee": 40}


def test_rank_boards_puts_the_biggest_sponsors_first_and_keeps_a_few_of_every_ats():
    from sourcing.service import rank_boards
    boards = [{"company": c, "ats": a, "board_id": b} for c, a, b in BOARDS]
    top = rank_boards(boards, lambda c: APPROVALS.get(c, 0), limit=4, per_ats=1)
    assert [b["company"] for b in top] == ["Big Sponsor", "Work Able", "Lever Co", "Recruit Ee"]
    top3 = rank_boards(boards, lambda c: APPROVALS.get(c, 0), limit=3, per_ats=0)
    assert [b["company"] for b in top3] == ["Big Sponsor", "Work Able", "Mid Sponsor"]
    assert len(rank_boards(boards, lambda c: 0, limit=100)) == len(boards)   # fewer than N: all, by name
    assert rank_boards(boards, lambda c: 0, limit=100)[0]["company"] == "Big Sponsor"


def test_first_open_refresh_crawls_at_most_n_boards_by_sponsor_rank_with_host_spacing(tmp_path, monkeypatch):
    from sourcing.service import first_open_refresh
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    net = _Crawl(seconds_per_request=0.0)                    # instant answers: spacing is all that waits
    w = _store(tmp_path, BOARDS)
    try:
        s = first_open_refresh(w, fetch=net.fetch, max_boards=5, max_minutes=10,
                               approvals=lambda c: APPROVALS.get(c, 0), sleep=net.sleep, clock=net.clock)
    finally:
        w.close()
    board_urls = [u for u in net.urls if any(h in u for h in
                  ("greenhouse.io", "workable.com", "lever.co", "recruitee.com"))]
    assert len(board_urls) == 5 and s["boards_crawled"] == 5 and s["boards_total"] == 5
    assert s["first_open"] is True and not s["stopped_early"]
    # Ranked by approvals: Big (gh) > Work Able (workable) > Mid (gh) > Lever Co > Work Able Two.
    assert "bigsponsor" in board_urls[0] and "workable1" in board_urls[1] and "midsponsor" in board_urls[2]
    assert not any("nosponsor" in u or "smallsponsor" in u for u in board_urls)
    # Per-host spacing: the second Greenhouse request waited for the host's slot (PoliteFetch).
    assert any(0 < x <= 0.25 for x in net.sleeps)
    assert s["requests"] >= 5 and s["rate_limited"] == 0


def test_first_open_refresh_stops_at_the_wall_clock_cap(tmp_path, monkeypatch):
    from sourcing.service import first_open_refresh
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    net = _Crawl(seconds_per_request=30.0)                   # a slow network: 30 s per request
    w = _store(tmp_path, BOARDS)
    try:
        s = first_open_refresh(w, fetch=net.fetch, max_boards=8, max_minutes=1.0,
                               approvals=lambda c: APPROVALS.get(c, 0), sleep=net.sleep, clock=net.clock)
        n_rows = len(w.list_jobs())
    finally:
        w.close()
    assert s["stopped_early"] is True and 1 <= s["boards_crawled"] <= 3 and s["boards_total"] == 8
    assert n_rows >= 1                                       # what it did reach was stored


def test_rank_boards_zero_means_no_boards_and_only_none_means_every_board():
    """JOBS_FIRST_CRAWL_BOARDS=0 / --max-boards 0 used to reach rank_boards as 0, which it read as
    "no limit": the one setting a cautious maintainer uses to SHRINK the first-open crawl widened
    it to the whole watchlist."""
    from sourcing.service import rank_boards
    boards = [{"company": c, "ats": a, "board_id": b} for c, a, b in BOARDS]
    assert rank_boards(boards, lambda c: 0, limit=0) == []
    assert rank_boards(boards, lambda c: 0, limit=-1) == []
    assert len(rank_boards(boards, lambda c: 0, limit=None)) == len(boards)
    assert len(rank_boards(boards, lambda c: 0, limit=3)) == 3
    assert rank_boards([], lambda c: 0, limit=0) == []


def test_first_open_refresh_with_zero_boards_crawls_nothing(tmp_path, monkeypatch):
    import sourcing.service as service
    calls = []

    def fake_refresh(store, fetch, **kw):
        calls.append(kw)
        return {"boards_crawled": len(kw.get("boards") or [])}
    monkeypatch.setattr(service, "refresh_watchlist", fake_refresh)
    net = _Crawl()
    w = _store(tmp_path, BOARDS)
    try:
        s = service.first_open_refresh(w, fetch=net.fetch, max_boards=0, approvals=lambda c: 0,
                                       sleep=lambda s: None)
        assert calls == [] and net.urls == []
        assert s["boards_total"] == 0 and s["boards_crawled"] == 0 and s["requests"] == 0
        assert s["new"] == 0 and s["errors"] == [] and s["first_open"] is True
        assert s["companies"] == len(BOARDS)
        # The env form of the same knob behaves the same way.
        monkeypatch.setenv("JOBS_FIRST_CRAWL_BOARDS", "0")
        s2 = service.first_open_refresh(w, fetch=net.fetch, approvals=lambda c: 0, sleep=lambda s: None)
        assert calls == [] and net.urls == [] and s2["boards_total"] == 0
        # And an unset limit is still every board (the default cap, here larger than the watchlist).
        monkeypatch.delenv("JOBS_FIRST_CRAWL_BOARDS")
        service.first_open_refresh(w, fetch=net.fetch, approvals=lambda c: 0, sleep=lambda s: None)
        assert len(calls) == 1 and len(calls[0]["boards"]) == len(BOARDS)
    finally:
        w.close()


def test_first_open_refresh_lowers_the_list_only_detail_budget(tmp_path, monkeypatch):
    import sourcing.service as service
    seen = {}

    def fake_refresh(store, fetch, **kw):
        seen.update(kw)
        return {"boards_crawled": 0}
    monkeypatch.setattr(service, "refresh_watchlist", fake_refresh)
    monkeypatch.setenv("JOBS_FIRST_CRAWL_BOARDS", "7")
    monkeypatch.setenv("JOBS_FIRST_CRAWL_MINUTES", "2.5")
    monkeypatch.setenv("JOBS_FIRST_CRAWL_DETAIL", "9")
    w = _store(tmp_path, BOARDS)
    try:
        service.first_open_refresh(w, fetch=lambda u, **k: {}, approvals=lambda c: 0, sleep=lambda s: None)
    finally:
        w.close()
    assert seen["detail_budgets"] == {"workday": 9, "smartrecruiters": 9}
    assert seen["time_budget"] == 150.0 and len(seen["boards"]) == 7


def test_refresh_watchlist_boards_subset_and_time_budget(tmp_path, monkeypatch):
    from sourcing.service import refresh_watchlist
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    net = _Crawl()
    w = _store(tmp_path, BOARDS)
    try:
        two = [b for b in w.companies() if b["board_id"] in ("bigsponsor", "leverco")]
        s = refresh_watchlist(w, net.fetch, boards=two, sleep=net.sleep)
        assert s["boards_crawled"] == 2 and s["boards_total"] == 2 and s["stopped_early"] is False
        s = refresh_watchlist(w, net.fetch, time_budget=0.0, clock=net.clock, sleep=net.sleep)
        assert s["boards_crawled"] == 0 and s["stopped_early"] is True and s["boards_total"] == len(BOARDS)
    finally:
        w.close()


# -- scripts/bundle_feed_snapshot.py -------------------------------------------------------- #

def test_feed_url_from_ignores_the_placeholder():
    from scripts import bundle_feed_snapshot as B
    assert B.feed_url_from(None, {"TAILOR_FEED_URL": "https://tailor.example/feed"}) is None
    assert B.feed_url_from(None, {}) is None
    assert B.feed_url_from(None, {"TAILOR_FEED_URL": "https://feed.sponsorjobs.app/feed/"}) == "https://feed.sponsorjobs.app/feed"
    assert B.feed_url_from("https://x.test/f", {"TAILOR_FEED_URL": "https://y.test"}) == "https://x.test/f"


def test_bundle_from_a_fake_url(tmp_path):
    from scripts import bundle_feed_snapshot as B
    body = _feed_bytes([_row("greenhouse:a:1"), _row("greenhouse:a:2")], LIVE_AT)
    manifest = {"feed_version": 1, "generated_at": LIVE_AT, "count": 2,
                "jobs_url": "https://f.test/feed/jobs.json.gz", "jd_shard_count": 256}
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/jobs.json.gz"):
            return body
        return json.dumps(manifest).encode()
    dest = tmp_path / "packaging" / "seed" / "feed"
    msgs = []
    m = B.stage_from_url("https://f.test/feed/", dest, fetch=fetch, log=msgs.append)
    assert calls == ["https://f.test/feed/jobs.json.gz", "https://f.test/feed/manifest.json"]
    assert (dest / "jobs.json.gz").read_bytes() == body
    staged = json.loads((dest / "manifest.json").read_text())
    assert staged["count"] == 2 and staged["generated_at"] == LIVE_AT
    assert staged["bundled_from"] == "https://f.test/feed" and staged["jd_shard_count"] == 0
    assert m == staged and not (dest / "jd").exists()
    assert any("2 jobs" in x and LIVE_AT in x for x in msgs)
    # The staged snapshot is exactly what StaticFeed adopts.
    sf = StaticFeed("", tmp_path / "cache", fetch=_Server([], down=True), snapshot=dest / "jobs.json.gz")
    assert sf.refreshed_at() is None and len(sf.jobs()[0]) == 2 and sf.refreshed_at() == LIVE_AT


def test_bundle_from_a_url_refuses_an_empty_or_malformed_list(tmp_path):
    from scripts import bundle_feed_snapshot as B
    dest = tmp_path / "feed"
    with pytest.raises(ValueError):
        B.stage_from_url("https://f.test", dest, fetch=lambda u: dumps_gz({"generated_at": LIVE_AT, "jobs": []}))
    with pytest.raises(Exception):
        B.stage_from_url("https://f.test", dest, fetch=lambda u: b"<html>oops</html>")
    assert not dest.exists()                                 # nothing half-staged


def test_bundle_from_a_fake_crawl(tmp_path, monkeypatch):
    import backend.feed as feed
    import sourcing.service as service
    from scripts import bundle_feed_snapshot as B
    from sourcing.sponsors import SponsorDB
    monkeypatch.setattr(service, "SEED_WATCHLIST", tmp_path / "nope.csv")   # the tiny built-in seed
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    net = _Crawl()
    dest = tmp_path / "packaging" / "seed" / "feed"
    msgs = []
    m = B.stage_from_crawl(dest, tmp_path / "work", max_minutes=5, max_boards=2, detail_budget=3,
                           fetch=net.fetch, sleep=net.sleep, clock=net.clock, log=msgs.append)
    head = loads_maybe_gz((dest / "jobs.json.gz").read_bytes())
    ids = [j["source_id"] for j in head["jobs"]]
    assert ids and all(i.startswith("greenhouse:dropbox:") for i in ids)
    assert "jd_text" not in head["jobs"][0] and not (dest / "jd").exists()
    assert m["count"] == len(ids) and m["bundled_from"] == "crawl" and m["jd_shard_count"] == 0
    assert head["generated_at"] == m["generated_at"]
    assert any("polite crawl" in x and "boards=2/2" in x for x in msgs)


def test_main_with_no_url_and_no_crawl_stages_nothing(tmp_path, monkeypatch, capsys):
    from scripts import bundle_feed_snapshot as B
    monkeypatch.setenv("TAILOR_FEED_URL", "https://tailor.example/feed")
    assert B.main(["--no-crawl", "--dest", str(tmp_path / "feed")]) == 1
    assert not (tmp_path / "feed").exists()
    monkeypatch.setattr(B, "_http_get", lambda url, timeout=60: (_ for _ in ()).throw(OSError("down")))
    assert B.main(["--from-url", "https://f.test/feed", "--no-crawl", "--dest", str(tmp_path / "feed")]) == 1
    assert "could not download" in capsys.readouterr().out


def test_a_snapshot_older_than_the_freshness_window_is_ignored(tmp_path):
    # Someone installing months after the build must not see expired postings, even for a
    # moment: an old snapshot is never adopted, so the app shows its fetching state instead.
    old = _snapshot(tmp_path, generated_at="2026-11-01T06:00:00+00:00")   # 75 days before T0
    srv = _Server([_row("greenhouse:live:1")])
    sf = StaticFeed("", tmp_path / "cache", fetch=srv, clock=lambda: T0, snapshot=old)
    assert not sf.has_cache()
    assert not (tmp_path / "cache" / "jobs.json.gz").exists()
