"""The static-feed client under a THREADED Flask server and a flaky network (code review, 2026-10):

- two requests finding the list due at the same moment must produce ONE download, not two
  interleaved writes of the same temp file (which published a half-written jobs.json.gz and then
  failed every request for an hour);
- a failed download is remembered and backed off, so an unreachable host costs one short timeout
  per window instead of stalling every /api/jobs and every job open;
- a corrupt cached list is deleted and re-downloaded instead of raising until the next hourly slot;
- /api/jobs/detail falls through to the local store when the feed lists a role but has no JD text.

Offline: fake origin, fake clock.
"""

from __future__ import annotations

import datetime
import gzip
import importlib
import json
import os
import threading
import time
import urllib.request

import pytest

from sourcing import feedclient
from sourcing.feedclient import (CHECK_EVERY, DEFAULT_TIMEOUT, RETRY_AFTER, FeedUnavailable,
                                 StaticFeed, _atomic_write)
from sourcing.feedfile import dumps_gz, shard_for, slim_row

_TODAY = datetime.date.today().isoformat()
T0 = 1_700_000_000.0


def _row(sid, title="Software Engineer", company="Acme", source="greenhouse", **extra):
    r = {"source_id": sid, "source": source, "company": company, "title": title,
         "location": "New York, NY", "remote": "hybrid", "url": "https://acme.example/jobs/1",
         "posted_at": _TODAY, "first_seen": "2026-09-30 10:00:00", "salary": "$150k/yr",
         "sponsorship_stated": True, "us": True, "entry_level": False, "visa": [], "nationality_visas": [],
         "sponsor": {}, "jd_text": "Build " * 50}
    r.update(extra)
    return r


def _feed_bytes(rows):
    return dumps_gz({"feed_version": 1, "generated_at": "2026-09-30T12:00:00+00:00",
                     "count": len(rows), "jobs": [slim_row(r) for r in rows]})


class _Server:
    """A fake origin: records calls, honours If-None-Match, can be switched off, can block."""

    def __init__(self, rows, jd=None):
        self.body = _feed_bytes(rows)
        self.etag = '"v1"'
        self.jd = jd or {}
        self.calls = []
        self.down = False
        self.gate = None             # a threading.Event the list download waits on, if set

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.down:
            raise OSError("network down")
        if url.endswith("/jobs.json.gz"):
            if self.gate is not None:
                self.gate.wait(5)
            if headers.get("If-None-Match") == self.etag:
                return 304, {}, b""
            return 200, {"ETag": self.etag}, self.body
        shard = url.rsplit("/", 1)[1].split(".")[0]
        return 200, {}, dumps_gz({k: v for k, v in self.jd.items() if shard_for(k) == shard})


def _feed(tmp_path, srv, now):
    return StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: now[0])


# -- finding 1: concurrent refresh ---------------------------------------------------------- #

def test_two_threads_finding_the_list_due_download_it_once(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    srv.gate = threading.Event()
    sf = _feed(tmp_path, srv, [T0])
    results = {}

    def go(name):
        results[name] = sf.refresh()
    a = threading.Thread(target=go, args=("a",))
    b = threading.Thread(target=go, args=("b",))
    a.start()
    for _ in range(200):                       # until the first thread is inside the download
        if srv.calls:
            break
        time.sleep(0.01)
    b.start()
    time.sleep(0.1)                            # b is now either waiting on the lock or (bug) fetching
    srv.gate.set()
    a.join(5)
    b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert len(srv.calls) == 1                 # ONE download: the second caller waited, saw not-due
    assert sorted(results.values()) == [False, True]
    assert sf.jobs()[0][0]["source_id"] == "greenhouse:acme:1"
    assert not list(tmp_path.glob("*.tmp"))    # and nothing half-written was left behind


def test_atomic_write_uses_a_unique_temp_name_per_attempt(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "jobs.json.gz"
    _atomic_write(target, b"one")
    _atomic_write(target, b"two")
    assert target.read_bytes() == b"two"
    assert len(seen) == 2 and seen[0] != seen[1]           # never the same fixed `.tmp` path twice
    assert str(tmp_path / "jobs.json.gz.tmp") not in seen
    assert all(s.startswith(str(tmp_path / "jobs.json.gz.")) for s in seen)   # same dir -> rename
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_cleans_its_temp_file_when_the_write_fails(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_write(tmp_path / "jobs.json.gz", b"x")
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cached_list_is_deleted_and_redownloaded_at_once(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    now = [T0]
    (tmp_path / "jobs.json.gz").write_bytes(gzip.compress(b'{"jobs": "not a list'))   # half-written
    (tmp_path / "feed_state.json").write_text(json.dumps(
        {"offset_minute": 0, "last_check": T0, "etag": '"stale"'}))        # looks checked this hour
    sf = _feed(tmp_path, srv, now)
    assert not sf.due()                                   # the trap: not due for an hour...
    jobs, _ = sf.jobs()                                   # ...yet the corrupt file is NOT served
    assert [j["source_id"] for j in jobs] == ["greenhouse:acme:1"]
    assert len(srv.calls) == 1 and "If-None-Match" not in srv.calls[0][1]   # no 304 against garbage
    assert json.loads((tmp_path / "jobs.json.gz").read_bytes() and
                      gzip.decompress((tmp_path / "jobs.json.gz").read_bytes()))["count"] == 1


def test_corrupt_cache_with_the_origin_down_raises_once_and_removes_the_file(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    srv.down = True
    (tmp_path / "jobs.json.gz").write_bytes(b"garbage")
    (tmp_path / "feed_state.json").write_text(json.dumps({"offset_minute": 0, "last_check": T0}))
    sf = _feed(tmp_path, srv, [T0])
    with pytest.raises(FeedUnavailable):
        sf.jobs()
    assert not (tmp_path / "jobs.json.gz").exists()       # treated as no cache: next attempt downloads
    assert len(srv.calls) == 1


def test_a_good_copy_in_memory_keeps_serving_while_a_corrupt_file_is_replaced(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    now = [T0]
    sf = _feed(tmp_path, srv, now)
    assert len(sf.jobs()[0]) == 1
    (tmp_path / "jobs.json.gz").write_bytes(b"garbage")   # another writer clobbered it
    os.utime(tmp_path / "jobs.json.gz", (T0 + 5, T0 + 5))
    srv.down = True
    assert len(sf.jobs()[0]) == 1                         # memory copy serves; file is gone
    assert not (tmp_path / "jobs.json.gz").exists()


# -- finding 2: failure back-off ------------------------------------------------------------ #

def test_failed_refresh_is_recorded_and_backed_off_while_the_cache_serves(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    now = [T0]
    sf = _feed(tmp_path, srv, now)
    sf.jobs()                                             # first run downloads
    off = sf.offset_minute()
    now[0] = (T0 // CHECK_EVERY + 1) * CHECK_EVERY + off * 60 + 1    # next slot: due again
    srv.down = True
    for _ in range(5):                                    # five requests in the same moment...
        assert len(sf.jobs()[0]) == 1                     # ...all served from cache
    assert len(srv.calls) == 2                            # ...and ONE failed attempt, not five
    assert sf.retry_after() == RETRY_AFTER and not sf.due()
    now[0] += RETRY_AFTER - 1
    sf.jobs()
    assert len(srv.calls) == 2                            # still inside the window
    now[0] += 1
    sf.jobs()
    assert len(srv.calls) == 3                            # window over: one more try
    assert sf.retry_after() == 2 * RETRY_AFTER            # doubling
    for i in range(10):                                   # repeated failures cap at the hourly slot
        now[0] += sf.retry_after()
        sf.jobs()
    assert sf.retry_after() == CHECK_EVERY
    n = len(srv.calls)
    # The back-off is persisted: a fresh instance (new process) honours it too.
    sf2 = _feed(tmp_path, srv, now)
    assert not sf2.due() and len(sf2.jobs()[0]) == 1 and len(srv.calls) == n
    # Recovery: the origin is back; the next window's attempt succeeds and clears the counter.
    srv.down = False
    now[0] += CHECK_EVERY
    sf.jobs()
    assert len(srv.calls) == n + 1 and sf.retry_after() == 0 and not sf.due()
    st = json.loads((tmp_path / "feed_state.json").read_text())
    assert "fail_count" not in st and "last_fail" not in st


def test_forced_refresh_ignores_the_back_off(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    srv.down = True
    sf = _feed(tmp_path, srv, [T0])
    with pytest.raises(FeedUnavailable):
        sf.jobs()
    srv.down = False
    assert sf.refresh(force=True) is True and sf.retry_after() == 0


def test_default_network_timeout_is_short(monkeypatch, tmp_path):
    seen = {}

    class _Resp:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _feed_bytes([_row("greenhouse:acme:1")])

    def urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    sf = StaticFeed("https://f.example.com/feed", tmp_path)          # production wiring, no fake fetch
    assert len(sf.jobs()[0]) == 1
    assert seen["timeout"] == DEFAULT_TIMEOUT == 10
    StaticFeed("https://f.example.com/feed", tmp_path, timeout=3)._fetch("https://f.example.com/x", {})
    assert seen["timeout"] == 3                                        # still overridable


# -- the app: instance cache lock + detail fall-through -------------------------------------- #

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("JOBS_FEED_URL", "https://feed.tailor.test/feed")
    import ui.app as A
    importlib.reload(A)
    return A


class _Resp:
    def __init__(self, body, status=200, headers=None):
        self._b, self.status, self.headers = body, status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _install_origin(monkeypatch, rows, jd=None):
    srv = _Server(rows, jd)

    def urlopen(req, timeout=30):
        status, hdrs, body = srv(req.full_url, dict(req.header_items()))
        return _Resp(body, status, hdrs)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return srv


def test_static_feed_instance_cache_builds_one_reader_under_concurrency(client, monkeypatch):
    built = []
    real = feedclient.StaticFeed

    class Slow(real):
        def __init__(self, *a, **k):
            built.append(1)
            time.sleep(0.1)                   # widen the race window
            super().__init__(*a, **k)
    monkeypatch.setattr(feedclient, "StaticFeed", Slow)
    client._STATIC_FEEDS.clear()
    got = []
    start = threading.Barrier(4)

    def go():
        start.wait(5)
        got.append(client._static_feed())
    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert len(built) == 1 and len(got) == 4 and all(g is got[0] for g in got)


def test_detail_falls_through_to_the_local_store_when_the_feed_has_no_jd(client, monkeypatch):
    # The feed LISTS the role but neither its shard nor the board endpoint yields a description;
    # the person saved it earlier with the full JD, so the local store answers.
    _install_origin(monkeypatch, [_row("greenhouse:acme:1")], jd={})
    monkeypatch.setattr("sourcing.feedclient.fetch_board_jd", lambda job, fetch=None: "")
    w = client._watchlist()
    try:
        w.upsert_jobs([_row("greenhouse:acme:1", jd_text="The saved full description. " * 10)])
    finally:
        w.close()
    d = client.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()
    assert d["jd"].startswith("The saved full description.") and d["job"]["company"] == "Acme"
    assert d["job"]["jd_text"] == d["jd"] and "match" in d and "pay_insight" in d


def test_detail_keeps_the_empty_jd_shape_when_nobody_has_the_text(client, monkeypatch):
    _install_origin(monkeypatch, [_row("greenhouse:acme:1")], jd={})
    monkeypatch.setattr("sourcing.feedclient.fetch_board_jd", lambda job, fetch=None: "")
    r = client.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1")
    assert r.status_code == 200
    d = r.get_json()
    assert set(d) == {"job", "jd", "match", "terms", "pay_insight", "jd_preview"}
    assert d["terms"] == []                     # no description, so no skills to underline
    assert d["jd"] == "" and d["jd_preview"] is False and d["job"]["source_id"] == "greenhouse:acme:1"


def test_detail_still_404s_for_a_role_nobody_lists(client, monkeypatch):
    _install_origin(monkeypatch, [_row("greenhouse:acme:1")], jd={})
    r = client.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:404")
    assert r.status_code == 404


def test_an_empty_download_never_replaces_a_cached_list(tmp_path):
    """A crawl that errored on every board still publishes a valid, empty jobs.json.gz. The
    client keeps the rows it has instead of blanking the board."""
    import gzip, json
    from sourcing.feedclient import StaticFeed
    def feed(rows):
        return gzip.compress(json.dumps({"feed_version": 1, "generated_at": "2027-01-14T12:00:00+00:00",
                                         "count": len(rows), "jobs": rows}).encode())
    bodies = [feed([{"source_id": "greenhouse:a:1", "title": "SWE", "company": "Acme"}]), feed([])]
    calls = []
    def fetch(url, headers):
        calls.append(url); return (200, {}, bodies[min(len(calls) - 1, 1)])
    now = [1_800_000_000.0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path / "c", fetch=fetch, clock=lambda: now[0])
    rows, _ = sf.jobs(); assert len(rows) == 1
    now[0] += 7200                                            # a later slot: the empty list arrives
    sf.refresh(force=True)
    rows, _ = sf.jobs()
    assert [r["source_id"] for r in rows] == ["greenhouse:a:1"]  # kept
    assert len(calls) == 2
