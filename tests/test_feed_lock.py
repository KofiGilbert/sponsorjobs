"""The locked job list (2026-11-25, docs/feed.md "The locked route"). The official app reads the
feed from the broker's /v1/feed/<file>, which serves only installs holding a valid broker account
token. These pin the client side (sourcing/feedclient.py): the bearer token on every file (the
manifest, list parts, the single list, description shards), one repair-and-retry on a 401, no
network at all when no token can be had, the token never following a redirect to another host,
ETag and gzip unchanged; and the app's wiring (ui/app.py): the token goes only to the broker's own
origin. A real local HTTP server plays the broker. Offline."""

from __future__ import annotations

import datetime
import importlib
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sourcing import feedclient
from sourcing.feedclient import RETRY_AFTER, FeedUnavailable, StaticFeed, _http_fetch
from sourcing.feedfile import write_feed

_TODAY = datetime.date.today().isoformat()


def _rows(n):
    return [{"source_id": f"greenhouse:acme:{i}", "source": "greenhouse", "company": "Acme",
             "title": f"Software Engineer {i}", "location": "New York, NY", "remote": "hybrid",
             "url": f"https://acme.example/jobs/{i}/{os.urandom(8).hex()}", "posted_at": _TODAY,
             "first_seen": "2026-11-20 10:00:00", "salary": "", "sponsorship_stated": True,
             "us": True, "entry_level": False, "visa": [], "nationality_visas": [], "sponsor": {},
             "jd_text": f"Build things {i}. " * 20}
            for i in range(n)]


class Broker:
    """A stand-in for the broker's /v1/feed/<file>: 401 unless `Authorization: Bearer <good>`,
    then the file from a built feed dir, with an ETag (304 on If-None-Match). Logs every request's
    path and Authorization header."""

    def __init__(self, root: Path, good: set[str]):
        self.root, self.good = Path(root), good
        self.log: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        b = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                auth = self.headers.get("Authorization") or ""
                rel = self.path.split("/v1/feed/", 1)[-1]
                with b.lock:
                    b.log.append((rel, auth))
                if not (auth.startswith("Bearer ") and auth[7:] in b.good):
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"no account","reason":"no_account"}')
                    return
                path = b.root / rel
                if not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = path.read_bytes()
                etag = f'"{len(body)}-{int(path.stat().st_mtime)}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if rel.endswith(".gz"):
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/feed"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_PAUSE", 0)
    site = tmp_path / "site"
    rows = _rows(300)
    write_feed(site, rows, {r["source_id"]: r["jd_text"] for r in rows}, jobs_url="", generated_at="2026-11-25T12:00:00+00:00",
               part_bytes=3000)
    b = Broker(site, {"good"})
    yield b
    b.close()


def _feed(b, cache, **kw):
    return StaticFeed(b.url, cache, retry_pause=0, wait=5, **kw)


def test_every_file_carries_the_bearer_token(broker, tmp_path):
    sf = _feed(broker, tmp_path / "c", token=lambda: "good")
    jobs, _ = sf.jobs()
    assert len(jobs) == 300
    paths = [p for p, _ in broker.log]
    assert "manifest.json" in paths and any(p.startswith("parts/") for p in paths)
    assert all(a == "Bearer good" for _, a in broker.log)
    assert sf.jd("greenhouse:acme:7").startswith("Build things 7.")
    assert broker.log[-1][0].startswith("jd/") and broker.log[-1][1] == "Bearer good"


def test_the_single_list_and_its_etag_still_work_behind_the_lock(broker, tmp_path):
    sf = StaticFeed(broker.url, tmp_path / "c", retry_pause=0, wait=5, parts=False,
                    token=lambda: "good")
    assert len(sf.jobs()[0]) == 300
    assert sf.refresh(force=True) is False                         # unchanged: one 304
    assert broker.log[-1] == ("jobs.json.gz", "Bearer good")


def test_a_401_repairs_the_token_once_and_retries(broker, tmp_path):
    tok = {"v": "stale"}
    repaired = []

    def repair():
        repaired.append(1)
        tok["v"] = "good"                                         # a fresh account registered
        return True
    sf = _feed(broker, tmp_path / "c", token=lambda: tok["v"], on_unauthorized=repair)
    assert len(sf.jobs()[0]) == 300
    assert repaired == [1]
    assert broker.log[0] == ("manifest.json", "Bearer stale")
    assert broker.log[1] == ("manifest.json", "Bearer good")


def test_a_401_that_stays_is_a_failed_download_with_the_usual_back_off(broker, tmp_path):
    calls = []
    sf = _feed(broker, tmp_path / "c", token=lambda: "revoked",
               on_unauthorized=lambda: calls.append(1) or False)
    with pytest.raises(FeedUnavailable):
        sf.refresh(force=True)
    assert calls == [1]
    assert [p for p, _ in broker.log] == ["manifest.json"]       # not retried, no single-file try
    st = sf.status()
    assert st["failing"] and "401" in st["error"] and st["retry_in"] > 0


def test_no_token_to_be_had_sends_nothing_and_is_not_asked_again_at_once(broker, tmp_path):
    asked = []
    now = [1_800_000_000.0]
    sf = StaticFeed(broker.url, tmp_path / "c", retry_pause=0, token=lambda: asked.append(1) or None,
                    clock=lambda: now[0])
    with pytest.raises(FeedUnavailable):
        sf.refresh(force=True)
    with pytest.raises(FeedUnavailable):
        sf.refresh(force=True)
    assert broker.log == [] and asked == [1]
    now[0] += RETRY_AFTER + 1
    with pytest.raises(FeedUnavailable):
        sf.refresh(force=True)
    assert asked == [1, 1]


def test_a_cached_list_keeps_serving_when_the_token_is_refused(broker, tmp_path):
    good = _feed(broker, tmp_path / "c", token=lambda: "good")
    assert len(good.jobs()[0]) == 300
    later = _feed(broker, tmp_path / "c", token=lambda: "revoked")
    jobs, _ = later.jobs()
    assert len(jobs) == 300


def test_an_open_feed_sends_no_authorization(broker, tmp_path):
    seen = []

    def fetch(url, headers):
        seen.append(dict(headers))
        return 404, {}, b""
    StaticFeed("https://feed.example.com/feed", tmp_path / "c", fetch=fetch).jd("x")
    assert seen and not any(k.lower() == "authorization" for h in seen for k in h)


def test_the_token_never_follows_a_redirect_to_another_host():
    """urllib copies a request's headers onto a redirect; the token is added unredirected, so a
    3xx to another host (a CDN, a bucket) never receives it. If-None-Match and gzip still go."""
    seen: list[tuple[str, dict]] = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.headers.get("Host"), dict(self.headers)))
            if self.path == "/start":
                port = self.server.server_address[1]
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/object")   # another host name
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        status, _, body = _http_fetch(f"http://127.0.0.1:{port}/start",
                                      {"Authorization": "Bearer secret", "If-None-Match": '"e1"'})
    finally:
        srv.shutdown()
        srv.server_close()
    assert status == 200 and body == b"{}"
    first, second = seen[0][1], seen[1][1]
    assert first.get("Authorization") == "Bearer secret"
    assert "Authorization" not in second and "authorization" not in {k.lower() for k in second}
    assert second.get("If-None-Match") == '"e1"' and second.get("Accept-Encoding") == "gzip"


# -- the app's wiring ------------------------------------------------------------------------ #

@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("TAILOR_BROKER_URL", "https://api.broker.test")
    monkeypatch.setenv("JOBS_FEED_URL", "https://api.broker.test/v1/feed")
    import ui.app as A
    importlib.reload(A)
    yield A
    A._STATIC_FEEDS.clear()


def test_the_app_sends_its_account_token_to_the_brokers_own_feed(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_account_token", lambda: "tok-123")
    sf = app_mod._static_feed()
    assert sf._auth_headers() == {"Authorization": "Bearer tok-123"}
    assert sf._on_unauthorized is app_mod._recover_from_401


def test_the_token_is_never_sent_to_a_feed_on_another_host(app_mod, monkeypatch):
    monkeypatch.setenv("JOBS_FEED_URL", "https://feed.elsewhere.test/feed")
    monkeypatch.setattr(app_mod, "_account_token", lambda: pytest.fail("token asked for another host"))
    sf = app_mod._static_feed()
    assert sf._auth_headers() == {}


def test_same_origin_rule():
    import ui.app as A
    same = A._same_origin
    assert same("https://api.sponsorjobs.ai/v1/feed", "https://api.sponsorjobs.ai")
    assert same("https://API.sponsorjobs.ai:443/v1/feed", "https://api.sponsorjobs.ai/")
    assert not same("https://feed.sponsorjobs.ai/feed", "https://api.sponsorjobs.ai")
    assert not same("http://api.sponsorjobs.ai/v1/feed", "https://api.sponsorjobs.ai")
    assert not same("https://api.sponsorjobs.ai.evil.test/v1/feed", "https://api.sponsorjobs.ai")
    assert not same("", "https://api.sponsorjobs.ai")
