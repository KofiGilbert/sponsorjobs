"""Server-side search, phase 3 (docs/server-search-plan.md): the feed's SLICE (roles first seen in the
last 7 days, listed as "slice" in the manifest), the search client (sourcing/remotesearch.py) and the
JOBS_SEARCH=server switch in /api/jobs and the Auto-apply picker. Offline: a fake search server
(a `fetch` function) and a real local HTTP origin for the feed (tests/test_feed_parts.Origin).

The PRIVACY GUARD at the end inspects every request the client sends: only allowlisted keys, only
the install token header, and nothing taken from the profile, saved or dismissed jobs."""

from __future__ import annotations

import importlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from sourcing import feedclient
from sourcing.feedclient import StaticFeed
from sourcing.feedfile import (MANIFEST_FILE, SLICE_PREFIX, SLICE_REMOVED_PREFIX, load_stable_state,
                               save_stable_state, slice_rows, write_feed)
from sourcing.filters import apply_facets
from sourcing.remotesearch import (CANDIDATE_KEYS, SEARCH_KEYS, TOKEN_HEADER, RemoteSearch,
                                   SearchUnavailable, build_query, count_label)
from test_feed_parts import Origin

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _row(i, days_ago, title=None, company="Acme"):
    return {"source_id": f"greenhouse:acme:{i}", "source": "greenhouse", "company": company,
            "title": title or f"Software Engineer {i}", "location": "New York, NY",
            "remote": "hybrid", "url": f"https://acme.example/jobs/{i}",
            "posted_at": (NOW - timedelta(days=days_ago, minutes=i)).strftime("%Y-%m-%dT%H:%M:%S"),
            "first_seen": _ts(days_ago), "salary": "", "sponsorship_stated": True, "us": True,
            "entry_level": False, "visa": [], "nationality_visas": [], "sponsor": {}}


def _rows(recent=60, old=90):
    return ([_row(i, 1 + (i % 5)) for i in range(recent)]
            + [_row(1000 + i, 15 + (i % 20)) for i in range(old)])


# -- the slice in the feed build --------------------------------------------------------------- #

def test_the_slice_holds_only_recent_roles_capped_by_newest_first_seen():
    rows = [_row(1, 1), _row(2, 3), _row(3, 6.9), _row(4, 8), _row(5, 30), dict(_row(6, 1), first_seen="")]
    got = slice_rows(rows, NOW.isoformat(), days=7, max_rows=10)
    assert [r["source_id"] for r in got] == [f"greenhouse:acme:{i}" for i in (1, 2, 3)]
    capped = slice_rows(rows, NOW.isoformat(), days=7, max_rows=2)
    assert [r["source_id"] for r in capped] == ["greenhouse:acme:1", "greenhouse:acme:2"]


def test_write_feed_lists_the_slice_next_to_the_parts_in_its_own_files(tmp_path):
    rows = _rows()
    m = write_feed(tmp_path / "site", rows, {}, generated_at=NOW.isoformat(), part_bytes=3000)
    sl = m["slice"]
    assert set(m) >= {"parts", "stable", "slice"} and sl["count"] == 60 and sl["days"] == 7
    assert all(Path(f["url"]).name.startswith(SLICE_PREFIX) for f in sl["files"])
    assert m["count"] == len(rows) and m["stable"]["count"] == len(rows)   # the full list is unchanged
    # The slice keeps its part names across a rebuild (the stable layout's saved state).
    save_stable_state(tmp_path / "site", tmp_path / "state")
    assert load_stable_state(tmp_path / "state", "slice") is not None
    m2 = write_feed(tmp_path / "site", rows, {}, generated_at=NOW.isoformat(), part_bytes=3000,
                    prev_dir=tmp_path / "state")
    assert [f["url"] for f in m2["slice"]["files"]] == [f["url"] for f in sl["files"]]
    assert [f["url"] for f in m2["stable"]["files"]] == [f["url"] for f in m["stable"]["files"]]
    for f in sl["files"] + m["stable"]["files"]:                     # neither layout pruned the other
        assert (tmp_path / "site" / f["url"]).exists()


def test_a_feed_with_no_recent_roles_publishes_no_slice(tmp_path):
    m = write_feed(tmp_path / "site", _rows(recent=0), {}, generated_at=NOW.isoformat(), part_bytes=3000)
    assert "slice" not in m
    assert not any(p.name.startswith((SLICE_PREFIX, SLICE_REMOVED_PREFIX))
                   for p in (tmp_path / "site" / "parts").iterdir())


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_PAUSE", 0)
    rows = _rows()
    m = write_feed(tmp_path / "site", rows, {}, generated_at=NOW.isoformat(), part_bytes=3000)
    o = Origin(tmp_path / "site")
    o.rows, o.manifest = rows, m
    yield o
    o.close()


def test_an_old_style_reader_ignores_the_slice_and_reads_the_whole_list(tmp_path, site):
    sf = StaticFeed(site.url, tmp_path / "cache", retry_pause=0)
    assert sf.refresh(force=True) is True
    jobs, header = sf.jobs()
    assert len(jobs) == len(site.rows) and "board_count" not in header
    assert not any(Path(u).name.startswith(SLICE_PREFIX) for u in site.parts_fetched())


def test_the_slice_reader_downloads_only_the_slice(tmp_path, site):
    sf = StaticFeed(site.url, tmp_path / "cache", retry_pause=0, slice_only=True)
    assert sf.refresh(force=True) is True
    jobs, header = sf.jobs()
    assert len(jobs) == 60 and header["board_count"] == len(site.rows)
    assert site.parts_fetched() and all(Path(u).name.startswith(SLICE_PREFIX)
                                        for u in site.parts_fetched())
    st = sf.status()
    assert st["parts_done"] == st["parts_total"] == len(site.manifest["slice"]["files"])
    # A later restart reads the same slice from the cache, nothing re-downloaded.
    site.log.clear()
    sf2 = StaticFeed(site.url, tmp_path / "cache", retry_pause=0, slice_only=True)
    assert sf2.refresh(force=True) is False and site.log == [MANIFEST_FILE]
    # Rows the search API returned resolve through get() too.
    sf2.remember([{"source_id": "lever:far:9", "title": "Old role"}])
    assert sf2.get("lever:far:9")["title"] == "Old role"
    assert sf2.get(site.rows[0]["source_id"])["source_id"] == site.rows[0]["source_id"]


def test_the_slice_reader_falls_back_to_the_whole_list_on_a_feed_without_a_slice(tmp_path, site):
    man = json.loads((Path(site.root) / MANIFEST_FILE).read_text())
    man.pop("slice")
    (Path(site.root) / MANIFEST_FILE).write_text(json.dumps(man))
    sf = StaticFeed(site.url, tmp_path / "cache", retry_pause=0, slice_only=True)
    assert sf.refresh(force=True) is True and len(sf.jobs()[0]) == len(site.rows)


# -- a fake search server ---------------------------------------------------------------------- #

class FakeServer:
    """/v1/jobs/search and /v1/jobs/candidates over `rows`, filtered with the app's own facets.
    The cursor is "<index_version>:<offset>"; a cursor from another version answers 409."""

    def __init__(self, rows, version="v1"):
        self.rows, self.version = rows, version
        self.calls: list[tuple[str, dict]] = []
        self.down = False

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.down:
            raise OSError("network is unreachable")
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query))
        rows = apply_facets(self.rows, q=q.get("q", ""), loc=q.get("loc", ""), days=q.get("days", 0),
                            remote=q.get("remote", ""), visa=[v for v in q.get("visa", "").split(",") if v],
                            level=q.get("level", ""), pay=q.get("pay", 0), sort=q.get("sort", ""),
                            sponsored=q.get("sponsored", ""))
        if parts.path.endswith("/candidates") and q.get("titles"):
            want = [t.lower() for t in q["titles"].split("|")]
            rows = [r for r in rows if any(t in r["title"].lower() for t in want)]
        off = 0
        if q.get("cursor"):
            ver, _, n = q["cursor"].partition(":")
            if ver != self.version:
                return 409, b'{"error":"stale_cursor"}'
            off = int(n)
        lim = int(q.get("limit", 30))
        page = rows[off:off + lim]
        nxt = f"{self.version}:{off + lim}" if off + lim < len(rows) else None
        body = {"jobs": page, "count": min(len(rows), 1000), "count_capped": len(rows) > 1000,
                "next_cursor": nxt, "index_version": self.version, "generated_at": NOW.isoformat()}
        return 200, json.dumps(body).encode()

    def urls(self):
        return [u for u, _ in self.calls]


ALL = [_row(i, 1 + i / 100, title=("Data Analyst" if i % 3 == 0 else f"Engineer {i}"))
       for i in range(200)]


def test_build_query_keeps_only_allowlisted_keys_in_a_fixed_order():
    pairs = build_query({"q": "  data  analyst ", "visa": ["H1B", "OPT"], "pay": "0", "days": 7,
                         "sponsored": True, "resume": "secret", "dismissed": ["x"], "sort": "evil"},
                        cursor="v1:30", limit=999)
    assert pairs == [("q", "data analyst"), ("days", "7"), ("visa", "H1B,OPT"), ("sponsored", "1"),
                     ("limit", "50"), ("cursor", "v1:30")]
    cand = build_query({}, titles=["BI | Analyst", "BI Analyst", ""], candidates=True, limit=900)
    assert cand == [("titles", "BI Analyst"), ("limit", "500")]
    assert "titles" not in dict(build_query({}, titles=["x"]))       # search never sends titles


def test_responses_are_kept_in_a_small_lru(tmp_path):
    srv = FakeServer(ALL)
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv, cache_size=3)
    a = rs.search({"q": "engineer"})
    assert rs.search({"q": "engineer"}) is a and len(srv.calls) == 1   # Back / toggle: free
    for q in ("a", "b", "c"):
        rs.search({"q": q})
    assert len(srv.calls) == 4
    rs.search({"q": "engineer"})                                     # evicted: asked again
    assert len(srv.calls) == 5
    assert rs.cached({"q": "engineer"}) is not None and rs.cached({"q": "nope"}) is None


def test_the_install_token_is_random_kept_and_the_only_header(tmp_path):
    srv = FakeServer(ALL)
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv)
    rs.search({})
    tok = srv.calls[0][1][TOKEN_HEADER]
    assert len(tok) == 32 and set(srv.calls[0][1]) == {TOKEN_HEADER}
    rs2 = RemoteSearch("https://api.test", tmp_path, fetch=srv)
    rs2.search({"q": "x"})
    assert srv.calls[1][1][TOKEN_HEADER] == tok                       # kept across restarts
    other = RemoteSearch("https://api.test", tmp_path / "other", fetch=srv).token()
    assert other != tok


def test_board_pages_follow_cursors_and_refill_short_pages(tmp_path):
    srv = FakeServer(ALL)
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv)
    hidden = {r["source_id"] for r in ALL[:40]}                      # most of the first server page
    keep = lambda r: r["source_id"] not in hidden                    # noqa: E731
    p1 = rs.board_page({}, 1, 30, keep=keep)
    assert len(p1["jobs"]) == 30 and not hidden & {r["source_id"] for r in p1["jobs"]}
    assert len(srv.calls) == 3 and p1["count"] == 200 and not p1["end"]
    p2 = rs.board_page({}, 2, 30, keep=keep)
    expect = [r["source_id"] for r in ALL[40:]]
    assert [r["source_id"] for r in p1["jobs"] + p2["jobs"]] == expect[:60]   # no gap, no repeat
    # A deep page walks forward from the last page it knows.
    p4 = rs.board_page({}, 4, 30, keep=keep)
    assert [r["source_id"] for r in p4["jobs"]] == expect[90:120]
    # The last page is short and final; past it, an empty page.
    last = rs.board_page({}, 6, 30, keep=keep)
    assert last["end"] and len(last["jobs"]) == 160 - 150
    past = rs.board_page({}, 7, 30, keep=keep)
    assert past["end"] and past["jobs"] == []


def test_a_page_stays_short_after_three_calls(tmp_path):
    srv = FakeServer(ALL)
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv)
    p = rs.board_page({}, 1, 30, keep=lambda r: r["source_id"].endswith("7"))
    assert len(srv.calls) == 3 and 0 < len(p["jobs"]) < 30


def test_a_stale_cursor_restarts_from_page_one(tmp_path):
    srv = FakeServer(ALL)
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv)
    rs.board_page({}, 1, 30)
    srv.version = "v2"                                               # the index was rebuilt
    p2 = rs.board_page({}, 2, 30)
    assert [r["source_id"] for r in p2["jobs"]] == [r["source_id"] for r in ALL[30:60]]
    assert p2["index_version"] == "v2"
    assert any("cursor=v1" in u for u in srv.urls()) and "cursor" not in srv.urls()[-2]


def test_a_down_server_fails_fast_then_recovers(tmp_path):
    srv = FakeServer(ALL)
    t = [1000.0]
    rs = RemoteSearch("https://api.test", tmp_path, fetch=srv, clock=lambda: t[0], down_for=30)
    srv.down = True
    with pytest.raises(SearchUnavailable):
        rs.search({"q": "a"})
    with pytest.raises(SearchUnavailable):
        rs.search({"q": "b"})
    assert len(srv.calls) == 1 and rs.down()                         # one timeout, not one per request
    srv.down = False
    t[0] += 31
    assert not rs.down() and rs.search({"q": "b"})["jobs"] is not None


def test_count_label():
    assert count_label(812, False) == "about 812" and count_label(1000, True) == "1,000+"


# -- the app ------------------------------------------------------------------------------------- #

@pytest.fixture
def app(tmp_path, monkeypatch, site):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("JOBS_FIRST_CRAWL", "0")
    monkeypatch.setenv("JOBS_FEED_URL", site.url)
    monkeypatch.setenv("JOBS_SEARCH_URL", "https://api.test")
    monkeypatch.delenv("JOBS_SEARCH", raising=False)
    import ui.app as A
    importlib.reload(A)
    srv = FakeServer(site.rows)

    def install(server: bool):
        if server:
            monkeypatch.setenv("JOBS_SEARCH", "server")
            sf = StaticFeed(site.url, A._DATA / "feed_slice", retry_pause=0, slice_only=True)
            A._STATIC_FEEDS[(site.url, str(A._DATA), "slice")] = sf
        else:
            sf = StaticFeed(site.url, A._DATA / "feed_cache", retry_pause=0)
            A._STATIC_FEEDS[(site.url, str(A._DATA))] = sf
        sf.refresh(force=True)
        rs = RemoteSearch("https://api.test", A._DATA, fetch=srv)
        A._REMOTE_SEARCH[("https://api.test", str(A._DATA))] = rs
        return sf, rs
    A.test_install, A.test_server = install, srv
    return A


def _wait(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end and not pred():
        time.sleep(0.02)
    return pred()


def test_switch_off_is_the_full_list_and_never_calls_the_server(app):
    app.test_install(False)
    d = app.app.test_client().get("/api/jobs?q=engineer&page=2").get_json()
    assert d["total"] == 150 and d["page"] == 2 and d["source"] == "central"
    assert set(d) == {"jobs", "count", "total", "page", "per_page", "refreshed_at", "newest_at",
                      "stale", "source"}
    assert app.test_server.calls == []


def test_page_one_without_a_keyword_comes_from_the_slice_then_gets_the_server_count(app):
    sf, rs = app.test_install(True)
    c = app.app.test_client()
    d = c.get("/api/jobs").get_json()
    assert d["search"] == "slice" and d["count"] == 60 and d["total"] == 150
    assert {j["source_id"] for j in d["jobs"]} <= {r["source_id"] for r in site_rows(sf)}
    assert d.get("count_pending") is True
    assert _wait(lambda: rs.cached(app._facets({}), limit=30) is not None)
    d = c.get("/api/jobs").get_json()
    assert d["count"] == 150 and d["count_label"] == "about 150" and not d.get("count_pending")
    assert d["search"] == "slice"                                    # rows still from the slice


def site_rows(sf):
    return sf.jobs()[0]


def test_a_keyword_goes_to_the_server_through_the_local_layers(app, monkeypatch):
    sf, rs = app.test_install(True)
    rows = app.test_server.rows
    old = [r for r in rows if r["first_seen"] < _ts(10)]
    target = dict(old[0], title="Quantum Widget Wrangler")
    gone = dict(old[1], title="Quantum Widget Shaper")
    app.test_server.rows = [target, gone] + rows
    monkeypatch.setattr(app, "_dismissed_ids", lambda: {gone["source_id"]})
    monkeypatch.setattr(app, "_saved_ids", lambda: {target["source_id"]})
    d = app.app.test_client().get("/api/jobs?q=quantum").get_json()
    assert d["search"] == "server" and [j["source_id"] for j in d["jobs"]] == [target["source_id"]]
    assert d["jobs"][0]["saved"] is True and d["count"] == 1
    assert sf.get(target["source_id"])["title"] == "Quantum Widget Wrangler"   # opens like a feed row


def test_deeper_pages_go_to_the_server(app):
    app.test_install(True)
    d = app.app.test_client().get("/api/jobs?page=3").get_json()
    assert d["search"] == "server" and len(d["jobs"]) == 30 and d["page"] == 3
    assert d["count_label"] == "about 150"


def test_offline_shows_the_slice_with_the_note(app):
    app.test_install(True)
    app.test_server.down = True
    c = app.app.test_client()
    d = c.get("/api/jobs?q=engineer").get_json()
    assert d["search"] == "slice" and d["note"] == app.OFFLINE_SLICE_NOTE
    assert d["jobs"] and all("engineer" in j["title"].lower() for j in d["jobs"])
    d = c.get("/api/jobs").get_json()                                # page 1 knows it is offline too
    assert d["note"] == app.OFFLINE_SLICE_NOTE and not d.get("count_pending")


def test_the_offline_note_is_in_every_locale():
    root = Path(__file__).resolve().parents[1] / "ui" / "static" / "locales"
    for p in root.glob("*.json"):
        msg = json.loads(p.read_text(encoding="utf-8"))["jobs.offlineSlice"]
        assert msg and "—" not in msg and "–" not in msg, p.name


def test_the_picker_merges_server_candidates_with_the_slice(app):
    sf, rs = app.test_install(True)
    rows = app.test_server.rows
    old = dict([r for r in rows if r["first_seen"] < _ts(10)][0], title="Data Analyst II")
    app.test_server.rows = [old] + rows
    with app.app.app_context():
        got, meta = app._aa_candidates({"titles": ["Data Analyst"]})
    assert old["source_id"] in {j["source_id"] for j in got}
    url = [u for u in app.test_server.urls() if "/candidates" in u][-1]
    assert dict(parse_qsl(urlsplit(url).query))["titles"] == "Data Analyst"
    # Offline: the slice alone, no error.
    app.test_server.down = True
    rs._lru.clear()
    with app.app.app_context():
        got, _ = app._aa_candidates({"titles": ["Engineer"]})
    assert got and old["source_id"] not in {j["source_id"] for j in got}


# -- the privacy guard ------------------------------------------------------------------------- #

SECRET_WORDS = ("Zorblax", "Kubernetes", "Quillfeather", "acme:7")


def test_privacy_guard_nothing_personal_leaves_the_machine(app, monkeypatch):
    """Every request the client sent during searches, deep pages and an Auto-apply run carries
    only allowlisted keys and the install token, and none of the profile's words, saved IDs or
    dismissed IDs."""
    sf, rs = app.test_install(True)
    profile = {"name": "Zorblax Quillfeather", "skills": ["Kubernetes", "Zorblax"],
               "experiences": [{"title": "Kubernetes Platform Lead", "company": "Quillfeather"}],
               "target_titles": ["Zorblax Engineer"]}
    monkeypatch.setattr(app, "_match_profile", lambda: profile)
    monkeypatch.setattr(app, "_dismissed_ids", lambda: {"greenhouse:acme:7"})
    monkeypatch.setattr(app, "_saved_ids", lambda: {"greenhouse:acme:70"})
    c = app.app.test_client()
    for qs in ("q=engineer", "q=engineer&page=2", "page=3&visa=H1B,OPT&sponsored=1",
               "loc=New%20York&remote=hybrid&level=entry&pay=50000&sort=pay&days=7&page=2"):
        c.get("/api/jobs?" + qs)
    with app.app.app_context():
        app._aa_candidates({"titles": ["Data Analyst"], "locations": ["Zorblax City"]})
    assert app.test_server.calls
    for url, headers in app.test_server.calls:
        parts = urlsplit(url)
        keys = [k for k, _ in parse_qsl(parts.query)]
        allowed = CANDIDATE_KEYS if parts.path.endswith("/candidates") else SEARCH_KEYS
        assert set(keys) <= allowed, url
        assert set(headers) == {TOKEN_HEADER}, headers
        for word in SECRET_WORDS + ("greenhouse:acme:70",):
            assert word.lower() not in url.lower(), (word, url)
