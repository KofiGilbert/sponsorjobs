"""The static feed on a SLOW link (2026-10-14): the ~10 MB list took minutes at 10 to 60 KB/s, the
download ran inside the request under the feed lock, a 10 s read timeout killed it, and /api/jobs
answered after 332 s with an empty local board. Now the list downloads in the background,
streamed to disk with a stall timeout and an overall cap, and a request answers at once: the
cached list, or an explicit loading state with the bytes so far.

A real local HTTP server trickles, stalls, or answers 304, all fast enough for a unit test."""

from __future__ import annotations

import datetime
import gzip
import importlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sourcing import feedclient
from sourcing.feedclient import FeedLoading, FeedUnavailable, StaticFeed
from sourcing.feedfile import dumps_gz

_TODAY = datetime.date.today().isoformat()


def _rows(n, company="Acme"):
    return [{"source_id": f"greenhouse:acme:{i}", "source": "greenhouse", "company": company,
             "title": f"Software Engineer {i}", "location": "New York, NY", "remote": "hybrid",
             "url": f"https://acme.example/jobs/{i}/{os.urandom(24).hex()}", "posted_at": _TODAY,
             "first_seen": "2026-10-14 10:00:00", "salary": "", "sponsorship_stated": True,
             "us": True, "entry_level": False, "visa": [], "nationality_visas": [], "sponsor": {}}
            for i in range(n)]


def _body(n=400, company="Acme", generated="2026-10-14T12:00:00+00:00"):
    return dumps_gz({"feed_version": 1, "generated_at": generated, "count": n,
                     "jobs": _rows(n, company)})


class Origin:
    """A local origin for jobs.json.gz. `mode`: "trickle" (chunks with a pause, no
    Content-Length), "stall" (a few bytes, then silence until released), "fast"."""

    def __init__(self, body, mode="fast", chunk=2048, pause=0.02, etag='"v1"'):
        self.body, self.mode, self.chunk, self.pause, self.etag = body, mode, chunk, pause, etag
        self.release = threading.Event()
        self.requests: list[dict] = []
        origin = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"         # no Content-Length: read to EOF, like the CDN

            def log_message(self, *a):
                pass

            def do_GET(self):
                origin.requests.append(dict(self.headers.items()))
                if self.headers.get("If-None-Match") == origin.etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("ETag", origin.etag)
                self.end_headers()
                try:
                    if origin.mode == "stall":
                        self.wfile.write(origin.body[:100])
                        self.wfile.flush()
                        origin.release.wait(10)
                        return
                    step = origin.chunk if origin.mode == "trickle" else len(origin.body)
                    for i in range(0, len(origin.body), step):
                        if origin.release.is_set() and origin.mode == "trickle":
                            return
                        self.wfile.write(origin.body[i:i + step])
                        self.wfile.flush()
                        if origin.mode == "trickle":
                            time.sleep(origin.pause)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/feed"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def origin_factory():
    made = []

    def make(*a, **k):
        o = Origin(*a, **k)
        made.append(o)
        return o
    yield make
    for o in made:
        o.close()


def test_a_trickling_download_runs_in_the_background_and_jobs_answers_at_once(tmp_path, origin_factory):
    body = _body(1500)
    o = origin_factory(body, mode="trickle", chunk=2048, pause=0.03)    # ~1.5 s for the whole list
    sf = StaticFeed(o.url, tmp_path, wait=0.2)
    t0 = time.monotonic()
    with pytest.raises(FeedLoading):
        sf.jobs()
    assert time.monotonic() - t0 < 1.0                       # never held for the download
    time.sleep(0.3)
    st = sf.status()
    assert st["downloading"] and 0 < st["bytes"] < len(body) and not st["has_cache"]
    assert o.requests[0].get("Accept-Encoding") == "gzip"     # the stored gzip, not ~18x inflated
    assert sf.wait_download(10)
    rows, header = sf.jobs()
    assert len(rows) == 1500 and header["generated_at"].startswith("2026-10-14")
    st = sf.status()
    assert not st["downloading"] and st["has_cache"] and st["bytes"] == len(body) and not st["error"]
    assert sf.jobs_path.read_bytes() == body                 # stored as served, renamed into place
    assert not list(tmp_path.glob("*.part")) and not list(tmp_path.glob("*.tmp"))


def test_a_stalled_link_is_abandoned_retried_and_reported(tmp_path, origin_factory, monkeypatch):
    monkeypatch.setattr(feedclient, "STALL_TIMEOUT", 0.4)
    o = origin_factory(_body(), mode="stall")
    sf = StaticFeed(o.url, tmp_path, wait=0.1, tries=2, retry_pause=0)
    with pytest.raises(FeedLoading):
        sf.jobs()
    assert sf.wait_download(10)
    assert len(o.requests) == 2                              # one retry, then counted as a failure
    st = sf.status()
    assert st["failing"] and "imed out" in st["error"] and not st["has_cache"]
    with pytest.raises(FeedUnavailable) as exc:
        sf.jobs()                                            # backing off: no new download
    assert not isinstance(exc.value, FeedLoading)
    assert not list(tmp_path.glob("*.part"))                 # the partial file was cleaned up
    assert sf.start_refresh(force=True)                      # Retry ignores the back-off


def test_the_overall_cap_ends_a_download_that_never_finishes(tmp_path, origin_factory, monkeypatch):
    monkeypatch.setattr(feedclient, "DOWNLOAD_CAP", 0.3)
    o = origin_factory(_body(), mode="trickle", chunk=256, pause=0.05)  # bytes keep coming, slowly
    sf = StaticFeed(o.url, tmp_path, tries=1)
    with pytest.raises(TimeoutError):
        sf.refresh(force=True)
    assert sf.status()["failing"] and not sf.jobs_path.exists()


def test_a_304_keeps_the_cached_list_and_records_the_check(tmp_path, origin_factory):
    body = _body(5)
    o = origin_factory(body, mode="fast")
    sf = StaticFeed(o.url, tmp_path)
    assert sf.refresh(force=True) is True
    before = sf.jobs_path.stat().st_mtime
    assert sf.refresh(force=True) is False                   # If-None-Match -> 304
    assert o.requests[-1].get("If-None-Match") == '"v1"'
    assert sf.jobs_path.stat().st_mtime == before and len(sf.jobs()[0]) == 5
    assert sf._state.get("last_check") and not sf.status()["failing"]


def test_the_cached_list_keeps_serving_while_a_newer_one_downloads(tmp_path, origin_factory):
    old = origin_factory(_body(3, company="Old Co"), mode="fast")
    StaticFeed(old.url, tmp_path).refresh(force=True)
    new = origin_factory(_body(1000, company="New Co", generated="2026-10-14T15:00:00+00:00"),
                         mode="trickle", chunk=2048, pause=0.03, etag='"v2"')
    sf = StaticFeed(new.url, tmp_path, wait=0.1)
    assert sf.start_refresh(force=True)
    t0 = time.monotonic()
    rows, _ = sf.jobs()
    assert time.monotonic() - t0 < 1.0 and {r["company"] for r in rows} == {"Old Co"}
    assert sf.wait_download(10)
    assert {r["company"] for r in sf.jobs()[0]} == {"New Co"}


def test_one_download_at_a_time(tmp_path, origin_factory):
    o = origin_factory(_body(1000), mode="trickle", chunk=2048, pause=0.03)
    sf = StaticFeed(o.url, tmp_path, wait=0)
    assert sf.start_refresh() and sf.start_refresh() and sf.start_refresh(force=True)
    assert sf.wait_download(10)
    assert len(o.requests) == 1


# -- the app ------------------------------------------------------------------------------------ #

@pytest.fixture
def app_on(tmp_path, monkeypatch):
    def make(url):
        monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
        monkeypatch.setenv("RESUME_AGENT_DEV", "1")
        monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
        monkeypatch.setenv("JOBS_FEED_URL", url)
        monkeypatch.setattr(feedclient, "REQUEST_WAIT", 0.2)
        import ui.app as A
        importlib.reload(A)
        A._STATIC_FEEDS.clear()
        A._sponsors()           # load the bundled H-1B seed now (synchronous under test, ~20 s)
        return A
    return make


def test_api_jobs_answers_fast_with_a_loading_state_then_serves_the_list(app_on, origin_factory):
    body = _body(1500)
    o = origin_factory(body, mode="trickle", chunk=2048, pause=0.05)    # ~2.5 s
    A = app_on(o.url)
    c = A.app.test_client()
    t0 = time.monotonic()
    d = c.get("/api/jobs").get_json()
    assert time.monotonic() - t0 < 1.0
    assert d["loading"] is True and d["jobs"] == [] and d["feed"]["downloading"] is True
    assert not d.get("degraded")                             # not the empty local stand-in
    time.sleep(0.4)
    st = c.get("/api/jobs/feed-status").get_json()
    assert st["downloading"] and st["bytes"] > 0
    # Home's other screens do not block either.
    t0 = time.monotonic()
    nxt = c.get("/api/autoapply/upnext").get_json()
    assert time.monotonic() - t0 < 3.0 and nxt.get("feed_loading") is True
    assert A._static_feed().wait_download(10)
    d = c.get("/api/jobs").get_json()
    assert d["source"] == "central" and d["total"] == 1500 and not d.get("loading")
    assert c.get("/api/jobs/feed-status").get_json()["has_cache"] is True


def test_a_failed_first_download_reports_and_retry_starts_another(app_on, origin_factory, monkeypatch):
    monkeypatch.setattr(feedclient, "STALL_TIMEOUT", 0.3)
    o = origin_factory(_body(5), mode="stall")
    A = app_on(o.url)
    c = A.app.test_client()
    assert c.get("/api/jobs").get_json()["loading"] is True
    assert A._static_feed().wait_download(10)
    d = c.get("/api/jobs").get_json()
    assert d["degraded"] is True and d["feed"]["failing"] and not d["feed"]["downloading"]
    o.mode = "fast"
    o.release.set()
    st = c.post("/api/jobs/feed-retry", json={}).get_json()
    assert st["downloading"] is True or st["has_cache"] is True
    assert A._static_feed().wait_download(10)
    d = c.get("/api/jobs").get_json()
    assert d["source"] == "central" and d["total"] == 5
