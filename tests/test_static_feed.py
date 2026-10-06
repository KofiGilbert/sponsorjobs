"""The STATIC job feed (2026-09-30): a scheduled crawl writes jobs.json.gz + sharded JDs + a
manifest to object storage; every desktop app downloads the slim list hourly and filters it
locally. These pin the file format, the builder (from a fake crawl), the uploader (fake S3 client),
the hourly ETag-conditional client, and the app's filter / detail / fallback behaviour. Offline.
"""

from __future__ import annotations

import datetime
import gzip
import importlib
import json
import urllib.request

import pytest

from sourcing import feedfile
from sourcing.feedclient import (RETRY_AFTER, FeedUnavailable, StaticFeed, fetch_board_jd,
                                 is_placeholder)
from sourcing.feedfile import (JD_SHARDS, dedupe_rows, dumps_gz, loads_maybe_gz, shard_for,
                               slim_row, write_feed)

_TODAY = datetime.date.today().isoformat()


def _row(sid, title="Software Engineer", company="Acme", url="https://acme.example/jobs/1",
         source="greenhouse", **extra):
    r = {"source_id": sid, "source": source, "company": company, "title": title,
         "location": "New York, NY", "remote": "hybrid", "url": url, "posted_at": _TODAY,
         "first_seen": "2026-09-30 10:00:00", "salary": "$150k/yr", "sponsorship_stated": True,
         "us": True, "entry_level": False, "visa": [{"code": "H-1B"}], "nationality_visas": [],
         "sponsor": {"h1b_approvals": 10}, "jd_text": "Build " * 50, "is_new": 1, "dismissed": 0,
         "saved": 0, "last_seen": "x"}
    r.update(extra)
    return r


# -- format ------------------------------------------------------------------------------ #

def test_slim_row_drops_jd_and_personal_flags_but_keeps_every_facet_field():
    s = slim_row(_row("greenhouse:acme:1"))
    assert "jd_text" not in s and "is_new" not in s and "saved" not in s and "dismissed" not in s
    for k in ("source_id", "source", "company", "title", "location", "remote", "url", "posted_at",
              "first_seen", "salary", "sponsorship_stated", "us", "entry_level", "visa",
              "nationality_visas", "sponsor"):
        assert k in s


def test_aggregator_rows_keep_what_the_via_credit_needs():
    """Remotive / Remote OK let us share their jobs on condition that each one names them and
    links back (Remote OK: a follow link). The card's "via Remotive" line reads `source` and `url`
    off the slim row, and the file header carries the credit for every aggregator present."""
    rows = [_row("remotive:1", source="remotive", url="https://remotive.com/remote-jobs/1"),
            _row("remoteok:2", source="remoteok", url="https://remoteok.com/remote-jobs/2"),
            _row("greenhouse:acme:3", url="https://acme.example/jobs/3")]
    for r in rows:
        s = slim_row(r)
        assert s["source"] == r["source"] and s["url"] == r["url"]
    for key, name in (("remotive", "Remotive"), ("remoteok", "Remote OK")):
        assert feedfile.ATTRIBUTION[key]["name"] == name
        assert "link back" in feedfile.ATTRIBUTION[key]["note"].lower()
    assert "follow" in feedfile.ATTRIBUTION["remoteok"]["note"].lower()


def test_shard_is_two_hex_chars_and_stable():
    assert shard_for("greenhouse:acme:1") == shard_for("greenhouse:acme:1")
    assert len(shard_for("x")) == 2 and int(shard_for("x"), 16) < 256


def test_dedupe_prefers_the_board_row_over_the_aggregator_copy():
    agg = _row("remotive:1", source="remotive", url="https://acme.example/jobs/1?utm_source=remotive")
    board = _row("greenhouse:acme:1")
    other = _row("greenhouse:acme:2", title="Data Engineer", url="https://acme.example/jobs/2")
    out = dedupe_rows([agg, board, other])             # aggregator listed FIRST, still loses
    assert [r["source_id"] for r in out] == ["greenhouse:acme:1", "greenhouse:acme:2"]


def test_write_feed_builds_list_shards_and_manifest(tmp_path):
    rows = [_row("greenhouse:acme:1"), _row("lever:beta:9", company="Beta", source="lever",
                                            url="https://beta.example/9")]
    jd = {"greenhouse:acme:1": "Build the thing.", "lever:beta:9": ""}   # second has no JD
    m = write_feed(tmp_path, rows, jd, jobs_url="https://feed.example.com/feed/jobs.json.gz",
                   generated_at="2026-09-30T12:00:00+00:00")
    head = loads_maybe_gz((tmp_path / "jobs.json.gz").read_bytes())
    assert head["feed_version"] == 1 and head["count"] == 2 and head["generated_at"] == "2026-09-30T12:00:00+00:00"
    assert all("jd_text" not in j for j in head["jobs"])
    assert len(list((tmp_path / "jd").glob("*.json.gz"))) == JD_SHARDS     # every shard exists
    sh = loads_maybe_gz((tmp_path / "jd" / f"{shard_for('greenhouse:acme:1')}.json.gz").read_bytes())
    assert sh == {"greenhouse:acme:1": "Build the thing."} or sh["greenhouse:acme:1"] == "Build the thing."
    assert "lever:beta:9" not in loads_maybe_gz((tmp_path / "jd" / f"{shard_for('lever:beta:9')}.json.gz").read_bytes())
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man == m and man["count"] == 2 and man["jd_shard_count"] == 256
    assert man["jobs_url"].endswith("/jobs.json.gz")


def test_loads_maybe_gz_accepts_plain_json_too():
    assert loads_maybe_gz(b'{"a": 1}') == {"a": 1} and loads_maybe_gz(dumps_gz({"a": 1})) == {"a": 1}


# -- builder from a fake crawl ------------------------------------------------------------ #

def test_build_feed_from_a_fake_crawl(tmp_path, monkeypatch):
    import backend.feed as feed
    import sourcing.service as service
    from scripts import build_feed
    from sourcing.sponsors import SponsorDB
    monkeypatch.setattr(service, "SEED_WATCHLIST", tmp_path / "nope.csv")  # tiny built-in seed
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    gh_job = {"id": 11, "title": "Software Engineer", "location": {"name": "New York, NY"},
              "absolute_url": "https://boards.greenhouse.io/dropbox/jobs/11",
              "content": "<p>Build sync.</p>", "first_published": _TODAY}

    def fake_fetch(url, timeout=20):
        if "greenhouse.io" in url:
            return {"jobs": [gh_job]}
        if "remotive.com" in url:            # the SAME opening via an aggregator: must dedupe away
            return {"jobs": [{"id": 5, "company_name": "Dropbox", "title": "Software Engineer",
                              "candidate_required_location": "New York, NY",
                              "url": "https://boards.greenhouse.io/dropbox/jobs/11?utm_source=remotive",
                              "description": "dup", "publication_date": _TODAY}]}
        if "lever.co" in url:
            return []
        return {}

    m = build_feed.build(tmp_path / "out", tmp_path / "data", fetch=fake_fetch,
                         jobs_url="https://f.example.com/jobs.json.gz", log=lambda *a: None)
    head = loads_maybe_gz((tmp_path / "out" / "jobs.json.gz").read_bytes())
    ids = [j["source_id"] for j in head["jobs"]]
    assert "greenhouse:dropbox:11" in ids and not any(i.startswith("remotive:") for i in ids)
    assert m["count"] == len(ids) and "jd_text" not in head["jobs"][0]
    sh = loads_maybe_gz((tmp_path / "out" / "jd" / f"{shard_for('greenhouse:dropbox:11')}.json.gz").read_bytes())
    assert "Build sync." in sh["greenhouse:dropbox:11"]


def test_a_capped_crawl_resumes_where_the_last_run_stopped(tmp_path):
    # The first real run (2026-10-06) went past the workflow's 2-hour limit and was killed, which
    # saves nothing. Capped runs must instead walk the whole board list across runs.
    from scripts import build_feed
    boards = [{"company": c} for c in "ABCDE"]
    order, start = build_feed.rotate_boards(boards, tmp_path)
    assert start == 0 and [b["company"] for b in order] == list("ABCDE")   # no cursor yet
    build_feed.save_cursor(tmp_path, start, crawled=3, total=5)            # stopped after A, B, C
    order, start = build_feed.rotate_boards(boards, tmp_path)
    assert start == 3 and [b["company"] for b in order] == list("DEABC")
    build_feed.save_cursor(tmp_path, start, crawled=5, total=5)            # a full lap: same spot
    assert build_feed.rotate_boards(boards, tmp_path)[1] == 3
    assert build_feed.rotate_boards(boards[:2], tmp_path)[1] == 1          # the list shrank
    (tmp_path / build_feed.CURSOR_FILE).write_text("garbage")
    assert build_feed.rotate_boards(boards, tmp_path)[1] == 0              # unreadable: start over


def test_aggregator_policy_refuses_adzuna_and_defaults_to_jsearch():
    from scripts import build_feed
    assert build_feed.aggregators_for_feed({}, log=lambda *a: None) == ["remotive", "remoteok", "freehire", "jsearch"]
    assert build_feed.aggregators_for_feed({"FEED_INCLUDE_AGGREGATORS": "off"}) == ["remotive", "remoteok", "freehire"]
    msgs = []
    assert "adzuna" not in build_feed.aggregators_for_feed({"FEED_INCLUDE_AGGREGATORS": "adzuna,jsearch"}, log=msgs.append)
    assert any("adzuna" in m for m in msgs)


# -- uploader ------------------------------------------------------------------------------ #

class _FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


def test_uploader_sets_headers_and_orders_manifest_last(tmp_path):
    from scripts import upload_feed
    write_feed(tmp_path, [_row("greenhouse:acme:1")], {"greenhouse:acme:1": "jd"})
    c = _FakeS3()
    keys = upload_feed.upload(tmp_path, c, "bucket", prefix="/feed/", log=lambda *a: None)
    assert len(keys) == 258 and keys[-1] == "feed/manifest.json" and keys[-2] == "feed/jobs.json.gz"
    assert keys[0].startswith("feed/jd/")
    by_key = {p["Key"]: p for p in c.puts}
    lst = by_key["feed/jobs.json.gz"]
    assert lst["ContentEncoding"] == "gzip" and lst["ContentType"] == "application/json"
    assert lst["CacheControl"] == "public, max-age=600" and lst["Bucket"] == "bucket"
    assert by_key["feed/jd/00.json.gz"]["CacheControl"] == "public, max-age=86400"
    man = by_key["feed/manifest.json"]
    assert man["CacheControl"] == "no-cache" and "ContentEncoding" not in man
    assert json.loads(man["Body"])["count"] == 1


def test_uploader_refuses_an_unbuilt_dir_and_needs_creds(tmp_path):
    from scripts import upload_feed
    with pytest.raises(FileNotFoundError):
        upload_feed.plan(tmp_path)
    with pytest.raises(SystemExit):
        upload_feed.make_client({})


# -- the desktop client -------------------------------------------------------------------- #

def _feed_bytes(rows, generated_at="2026-09-30T12:00:00+00:00"):
    return dumps_gz({"feed_version": 1, "generated_at": generated_at, "count": len(rows),
                     "jobs": [slim_row(r) for r in rows]})


class _Server:
    """A fake static-feed origin: records requests, honours If-None-Match, can be switched off."""

    def __init__(self, rows, jd=None):
        self.body = _feed_bytes(rows)
        self.etag = '"v1"'
        self.jd = jd or {}
        self.calls = []
        self.down = False

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.down:
            raise OSError("network down")
        if url.endswith("/jobs.json.gz"):
            if headers.get("If-None-Match") == self.etag:
                return 304, {}, b""
            return 200, {"ETag": self.etag}, self.body
        shard = url.rsplit("/", 1)[1].split(".")[0]
        return 200, {}, dumps_gz({k: v for k, v in self.jd.items() if shard_for(k) == shard})


def test_client_downloads_once_an_hour_at_its_offset_with_etag(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    now = [1_700_000_000.0]
    sf = StaticFeed("https://f.example.com/feed/", tmp_path, fetch=srv, clock=lambda: now[0])
    jobs, header = sf.jobs()                                    # first run: downloads immediately
    assert len(jobs) == 1 and header["generated_at"] == "2026-09-30T12:00:00+00:00"
    assert len(srv.calls) == 1 and srv.calls[0][0] == "https://f.example.com/feed/jobs.json.gz"
    sf.jobs()
    assert len(srv.calls) == 1                                  # within the hour: no request
    off = sf.offset_minute()
    sf2 = StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: now[0])
    assert sf2.offset_minute() == off                           # persisted per install
    assert json.loads((tmp_path / "feed_state.json").read_text())["offset_minute"] == off
    # Advance to just past the NEXT slot (next hour boundary + offset): a conditional check fires.
    now[0] = (now[0] // 3600 + 1) * 3600 + off * 60 + 1
    sf.jobs()
    assert len(srv.calls) == 2 and srv.calls[1][1].get("If-None-Match") == '"v1"'
    assert sf.jobs()[0][0]["source_id"] == "greenhouse:acme:1"  # 304 kept the cached copy
    assert len(srv.calls) == 2                                  # ...and the slot is consumed


def test_client_serves_cache_when_origin_is_down_and_raises_when_nothing_cached(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")])
    srv.down = True
    now = [1_700_000_000.0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: now[0])
    with pytest.raises(FeedUnavailable):
        sf.jobs()
    srv.down = False
    now[0] += RETRY_AFTER                                       # a failure is held off, not hammered
    assert len(sf.jobs()[0]) == 1
    srv.down = True
    sf2 = StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: 1_700_000_000.0 + 7200)
    assert len(sf2.jobs()[0]) == 1                              # due, download fails, cache serves
    assert sf2.get("greenhouse:acme:1")["company"] == "Acme"


def test_get_indexes_the_list_that_is_loaded_now_not_the_one_a_reload_superseded(tmp_path):
    """Thread A: jobs() hands back list L1. Thread B: refreshes to L2 (which nulls the index).
    Thread A then built the index from its L1 copy -- stale for the whole hour, so the detail
    view said "no longer in the feed" for roles the board was showing. The index is now built
    under the lock from the loaded list and keyed to its mtime, so a reload is always visible."""
    srv = _Server([_row("greenhouse:acme:1")])
    now = [1_700_000_000.0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: now[0])
    assert sf.get("greenhouse:acme:1")["company"] == "Acme"
    real_jobs = sf.jobs

    def jobs_then_reload():                         # a concurrent refresh lands between jobs() and the index
        out = real_jobs()
        sf._set({"generated_at": "later", "jobs": [_row("greenhouse:acme:2", company="Beta")]},
                (sf._loaded_mtime or 0) + 1)
        return out
    sf.jobs = jobs_then_reload
    assert sf.get("greenhouse:acme:2")["company"] == "Beta"
    assert sf.get("greenhouse:acme:1") is None
    sf.jobs = real_jobs
    # And a plain reload (new file on disk) is picked up by get() at once, from any thread.
    srv.body = _feed_bytes([_row("greenhouse:acme:3", company="Gamma")], "2026-09-30T13:00:00+00:00")
    srv.etag = '"v2"'
    now[0] = (now[0] // 3600 + 1) * 3600 + sf.offset_minute() * 60 + 1
    import threading
    seen = {}

    def look(tag):
        seen[tag] = (sf.get("greenhouse:acme:3") or {}).get("company"), sf.get("greenhouse:acme:2")
    ts = [threading.Thread(target=look, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(v == ("Gamma", None) for v in seen.values()), seen


def test_client_caches_jd_shards_for_a_day(tmp_path):
    srv = _Server([_row("greenhouse:acme:1")], jd={"greenhouse:acme:1": "Full JD here."})
    now = [1_700_000_000.0]
    sf = StaticFeed("https://f.example.com/feed", tmp_path, fetch=srv, clock=lambda: now[0])
    assert sf.jd("greenhouse:acme:1") == "Full JD here."
    n = len(srv.calls)
    assert sf.jd("greenhouse:acme:1") == "Full JD here." and len(srv.calls) == n   # from disk
    assert sf.jd("greenhouse:acme:999") is None                 # shard fetched, id absent -> None
    assert (tmp_path / "jd" / f"{shard_for('greenhouse:acme:1')}.json.gz").exists()


def test_placeholder_domain_is_treated_as_unconfigured():
    assert is_placeholder("https://tailor.example/feed") and is_placeholder("")
    assert not is_placeholder("https://feed.tailor.app/feed") and not is_placeholder("https://kitchen.example.com")


def test_fetch_board_jd_asks_the_boards_own_public_endpoint():
    seen = []

    def fetch(url, timeout=20):
        seen.append(url)
        return {"content": "&lt;p&gt;Ship it.&lt;/p&gt;"}
    jd = fetch_board_jd({"source_id": "greenhouse:acme:77"}, fetch=fetch)
    assert jd == "Ship it." and seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/77"]
    assert fetch_board_jd({"source_id": "greenhouse:evil.com/:77"}, fetch=fetch) == ""  # slug guard
    assert fetch_board_jd({"source_id": "garbage"}, fetch=fetch) == ""


# -- the app -------------------------------------------------------------------------------- #

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


def test_app_filters_the_static_feed_locally_and_overlays_bookmarks(client, monkeypatch):
    rows = [_row("greenhouse:acme:1", first_seen="2026-09-30 11:00:00"),
            _row("lever:beta:2", title="New Grad Nurse", company="Beta Health", source="lever",
                 url="https://beta.example/2", entry_level=True, salary="")]
    srv = _install_origin(monkeypatch, rows)
    c = client.app.test_client()
    d = c.get("/api/jobs").get_json()
    assert d["source"] == "central" and d["total"] == 2 and d["count"] == 2
    assert d["refreshed_at"] == "2026-09-30T12:00:00+00:00" and d.get("degraded") is None
    d = c.get("/api/jobs?q=nurse").get_json()
    assert d["count"] == 1 and d["jobs"][0]["source_id"] == "lever:beta:2" and d["total"] == 2
    # The freshness label reads when the newest job ARRIVED, not when the list was built, and
    # over the whole board, not just the filtered page (the nurse row arrived at 10:00).
    assert d["newest_at"] == "2026-09-30 11:00:00"
    assert c.get("/api/jobs?level=entry").get_json()["count"] == 1
    assert c.get("/api/jobs?pay=100000").get_json()["count"] == 1
    assert c.get("/api/jobs?visa=H-1B").get_json()["count"] == 2
    assert c.get("/api/jobs?per_page=1&page=2").get_json()["jobs"][0]["source_id"] == "lever:beta:2"
    assert len(srv.calls) == 1                                  # one download served every facet
    c.post("/api/jobs/save", json={"source_id": "greenhouse:acme:1", "saved": True,
                                   "job": slim_row(rows[0])})
    d = c.get("/api/jobs").get_json()
    assert {j["source_id"]: j.get("saved") for j in d["jobs"]}["greenhouse:acme:1"] is True
    assert (client._DATA / "feed_cache" / "jobs.json.gz").exists()


def test_app_detail_reads_the_jd_shard_and_computes_match_locally(client, monkeypatch):
    _install_origin(monkeypatch, [_row("greenhouse:acme:1")],
                    jd={"greenhouse:acme:1": "Build distributed systems in Go."})
    d = client.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()
    assert d["jd"] == "Build distributed systems in Go." and d["job"]["company"] == "Acme"
    assert "match" in d and d["jd_preview"] is False
    job = client._job_from_feed("greenhouse:acme:1")
    assert job["jd_text"] == "Build distributed systems in Go."
    assert client._job_from_feed("greenhouse:acme:404") is None


def test_app_detail_falls_back_to_the_board_endpoint_when_the_shard_lacks_the_jd(client, monkeypatch):
    srv = _install_origin(monkeypatch, [_row("greenhouse:acme:1")], jd={})
    monkeypatch.setattr("sourcing.feedclient.fetch_board_jd", lambda job, fetch=None: "From the board.")
    d = client.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()
    assert d["jd"] == "From the board."
    assert any("/jd/" in u for u, _ in srv.calls)             # the shard WAS tried first


def test_app_falls_back_to_local_and_flags_degraded_when_feed_is_unreachable(client, monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    r = client.app.test_client().get("/api/jobs")
    assert r.status_code == 200
    d = r.get_json()
    assert d["source"] == "local" and d["degraded"] is True


def test_app_treats_the_placeholder_domain_as_local_only(client, monkeypatch):
    monkeypatch.setenv("JOBS_FEED_URL", "https://tailor.example/feed")
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError()))
    d = client.app.test_client().get("/api/jobs").get_json()
    assert d["source"] == "local" and not d.get("degraded") and calls == []
