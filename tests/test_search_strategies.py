"""Saved search strategies (2026-10-12): a named Jobs filter set, kept locally, with the
"open on other boards" URLs and the "new since you last looked" digest.

Pins: the store (CRUD, one default, the last_seen migration), the URL builder (pure: spaces,
quotes, commas, remote / entry flags; no network), the digest math with PINNED timestamps, and
the /api/strategies routes through the Flask test client. Nothing here touches LinkedIn,
Handshake or any other site: the URLs are strings for the browser to open.
"""

from __future__ import annotations

import importlib
import sqlite3
import tempfile
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from sourcing.strategies import (StrategyStore, board_search_urls, digest, new_since,
                                 normalize_filters, parse_ts, strategy_open_urls)

T0 = datetime(2026, 10, 11, 9, 0, 0)        # the person last looked
T_OLD = "2026-10-10 18:00:00"               # arrived before T0
T_NEW = "2026-10-11 12:30:00"               # arrived after T0
T_NEWER = "2026-10-11T15:45:00Z"            # ISO with Z, also after T0


def _job(sid, title, first_seen, **kw):
    j = {"source_id": sid, "source": "greenhouse", "company": "Acme", "title": title,
         "location": "New York, NY", "remote": "", "url": "http://x", "posted_at": "2026-10-11",
         "salary": "", "first_seen": first_seen, "visa": [], "entry_level": False}
    j.update(kw)
    return j


# ---------------------------------------------------------------- store
def test_store_crud_and_single_default():
    s = StrategyStore(":memory:")
    a = s.create("Data NYC", {"q": "data analyst", "loc": "New York", "visa": "H-1B,TN", "pay": "80000"},
                 now=T0)
    assert a["id"] == 1 and a["name"] == "Data NYC"
    assert a["filters"]["visa"] == ["H-1B", "TN"] and a["filters"]["pay"] == 80000
    assert a["last_seen"] == "2026-10-11 09:00:00"          # seen from creation, never a flood
    b = s.create("Remote SWE", {"q": "software", "remote": "remote"}, is_default=True, now=T0)
    assert s.default()["id"] == b["id"]
    s.set_default(a["id"])
    assert s.default()["id"] == a["id"] and not s.get(b["id"])["is_default"]
    assert [x["name"] for x in s.list()] == ["Data NYC", "Remote SWE"]   # default first
    s.update(a["id"], name="Data NY", filters={"q": "data", "level": "entry"})
    got = s.get(a["id"])
    assert got["name"] == "Data NY" and got["filters"]["level"] == "entry" and got["filters"]["loc"] == ""
    assert s.delete(b["id"]) is True and s.delete(b["id"]) is False
    assert s.get(b["id"]) is None


def test_store_rejects_blank_name_and_drops_unknown_filter_keys():
    s = StrategyStore(":memory:")
    with pytest.raises(ValueError):
        s.create("   ", {})
    st = s.create("x", {"q": "a", "bogus": 1, "sort": "pay"})
    assert "bogus" not in st["filters"] and "sort" not in st["filters"]


def test_mark_seen_moves_last_seen_forward():
    s = StrategyStore(":memory:")
    st = s.create("x", {}, now=T0)
    later = datetime(2026, 10, 12, 8, 0, 0)
    assert s.mark_seen(st["id"], now=later)["last_seen"] == "2026-10-12 08:00:00"
    assert s.mark_seen(999) is None


def test_migration_adds_last_seen_to_an_older_table():
    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE search_strategy (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                 filters TEXT NOT NULL DEFAULT '{}', is_default INTEGER NOT NULL DEFAULT 0,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    c.execute("INSERT INTO search_strategy (name, filters, created_at, updated_at) VALUES (?,?,?,?)",
              ("old", '{"q":"ml"}', "2026-09-01 00:00:00", "2026-09-02 00:00:00"))
    c.commit(); c.close()
    s = StrategyStore(path)
    got = s.get(1)
    assert got["last_seen"] == "2026-09-02 00:00:00"      # back-filled from updated_at
    assert got["filters"]["q"] == "ml"


# ---------------------------------------------------------------- URL builder (pure)
def _q(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_board_urls_encode_spaces_quotes_and_commas():
    urls = {u["board"]: u["url"] for u in board_search_urls('"data analyst" python', "New York, NY")}
    assert set(urls) == {"linkedin", "indeed", "glassdoor", "handshake", "google"}
    li = urls["linkedin"]
    assert li.startswith("https://www.linkedin.com/jobs/search/?")
    assert "keywords=%22data+analyst%22+python" in li and "location=New+York%2C+NY" in li
    assert _q(li)["keywords"] == ['"data analyst" python'] and _q(li)["location"] == ["New York, NY"]
    assert _q(urls["indeed"])["q"] == ['"data analyst" python'] and _q(urls["indeed"])["l"] == ["New York, NY"]
    assert _q(urls["glassdoor"])["sc.keyword"] == ['"data analyst" python']
    assert _q(urls["handshake"])["query"] == ['"data analyst" python']
    g = urls["google"]
    assert g.endswith("&ibp=htl;jobs") and _q(g)["q"] == ['"data analyst" python jobs New York, NY']
    for u in urls.values():
        assert " " not in u and '"' not in u


def test_board_urls_map_remote_and_entry_level_where_the_board_has_a_flag():
    urls = {u["board"]: u["url"] for u in board_search_urls("nurse", "", "remote", "entry")}
    assert _q(urls["linkedin"])["f_WT"] == ["2"] and _q(urls["linkedin"])["f_E"] == ["2"]
    assert _q(urls["indeed"])["l"] == ["Remote"] and _q(urls["indeed"])["explvl"] == ["entry_level"]
    assert _q(urls["glassdoor"])["remoteWorkType"] == ["1"]
    assert "remote" in _q(urls["google"])["q"][0]
    plain = {u["board"]: u["url"] for u in board_search_urls("nurse")}
    assert "f_WT" not in _q(plain["linkedin"]) and "explvl" not in _q(plain["indeed"])


def test_board_urls_never_point_at_an_api_or_a_logged_in_action():
    for u in board_search_urls("x", "y"):
        assert u["url"].startswith("https://")
        assert "/api/" not in u["url"] and "apply" not in u["url"].lower()
    assert [u["label"] for u in board_search_urls("x")] == ["LinkedIn", "Indeed", "Glassdoor", "Handshake", "Google Jobs"]


def test_strategy_open_urls_reads_the_stored_filters():
    s = StrategyStore(":memory:")
    st = s.create("x", {"q": "ml engineer", "loc": "Austin", "remote": "hybrid"})
    li = next(u for u in strategy_open_urls(st) if u["board"] == "linkedin")["url"]
    assert _q(li)["keywords"] == ["ml engineer"] and _q(li)["f_WT"] == ["3"]


# ---------------------------------------------------------------- digest math (pinned clocks)
def test_parse_ts_accepts_every_stamp_shape_we_write():
    assert parse_ts("2026-10-11 09:00:00") == T0
    assert parse_ts("2026-10-11T09:00:00Z") == T0
    assert parse_ts("2026-10-11T09:00:00+00:00") == T0
    assert parse_ts("2026-10-11T11:00:00+02:00") == T0
    assert parse_ts("2026-10-11T09:00:00.250") == datetime(2026, 10, 11, 9, 0, 0, 250000)
    assert parse_ts("2026-10-11") == datetime(2026, 10, 11)
    assert parse_ts("") is None and parse_ts(None) is None and parse_ts("garbage") is None


def test_new_since_counts_only_arrivals_after_last_seen_that_match_the_filters():
    strategy = {"id": 1, "last_seen": "2026-10-11 09:00:00",
                "filters": {"q": "data", "loc": "New York", "visa": ["H-1B"], "days": 7}}
    h1b = [{"code": "H-1B", "label": "H-1B"}]
    jobs = [
        _job("a", "Data Analyst", T_NEW, visa=h1b),                  # new + matches
        _job("b", "Data Engineer", T_NEWER, visa=h1b),               # newer + matches (ISO Z)
        _job("c", "Data Analyst", T_OLD, visa=h1b),                  # arrived before last_seen
        _job("d", "Data Analyst", T_NEW),                            # no H-1B badge
        _job("e", "Nurse", T_NEW, visa=h1b),                         # keyword miss
        _job("f", "Data Scientist", T_NEW, visa=h1b, location="Austin, TX"),   # location miss
        _job("g", "Data Analyst", "", visa=h1b),                     # unreadable arrival: not new
        _job("h", "Data Analyst", "2026-10-11 09:00:00", visa=h1b),  # exactly last_seen: not after
    ]
    got = new_since(jobs, strategy)
    assert [j["source_id"] for j in got] == ["b", "a"]               # newest first
    d = digest(jobs, strategy, limit=1)
    assert d["count"] == 2 and d["newest_at"] == T_NEWER and [j["source_id"] for j in d["jobs"]] == ["b"]
    assert d["since"] == "2026-10-11 09:00:00"


def test_new_since_is_empty_when_the_strategy_was_never_seen():
    assert new_since([_job("a", "Data", T_NEW)], {"last_seen": None, "filters": {}}) == []


def test_new_since_applies_pay_entry_and_remote_facets():
    strategy = {"last_seen": "2026-10-11 09:00:00",
                "filters": {"pay": 100000, "level": "entry", "remote": "remote"}}
    jobs = [
        _job("ok", "Junior Dev", T_NEW, salary="$120k/yr", entry_level=True, remote="remote"),
        _job("cheap", "Junior Dev", T_NEW, salary="$60k/yr", entry_level=True, remote="remote"),
        _job("senior", "Staff Dev", T_NEW, salary="$200k/yr", entry_level=False, remote="remote"),
        _job("onsite", "Junior Dev", T_NEW, salary="$120k/yr", entry_level=True, remote="onsite"),
    ]
    assert [j["source_id"] for j in new_since(jobs, strategy)] == ["ok"]


def test_normalize_filters_coerces_types():
    f = normalize_filters({"q": "  ml ", "days": "7", "pay": "abc", "visa": "H-1B, ,TN", "sponsored": "1",
                           "remote": "Remote", "level": "ENTRY"})
    assert f == {"q": "ml", "loc": "", "days": 7, "remote": "remote", "visa": ["H-1B", "TN"],
                 "level": "entry", "pay": 0, "sponsored": True}


# ---------------------------------------------------------------- routes
@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as A
    importlib.reload(A)
    return A


_BOARD = [
    _job("new1", "Data Analyst", "2099-01-01 00:00:00", visa=[{"code": "H-1B", "label": "H-1B"}]),
    _job("new2", "Nurse", "2099-01-01 00:00:00"),
    _job("old1", "Data Analyst", "2000-01-01 00:00:00", visa=[{"code": "H-1B", "label": "H-1B"}]),
]


def test_strategy_routes_crud_default_and_digest(app, monkeypatch):
    monkeypatch.setattr(app, "_board_rows", lambda: [dict(j) for j in _BOARD])
    c = app.app.test_client()
    assert c.post("/api/strategies", json={"name": "  "}).status_code == 400
    r = c.post("/api/strategies", json={"name": "Data NYC", "filters": {"q": "data", "visa": ["H-1B"]}})
    assert r.status_code == 200
    st = r.get_json()["strategy"]
    sid = st["id"]
    assert st["filters"]["q"] == "data" and st["is_default"] is False
    r2 = c.post("/api/strategies", json={"name": "Nurses", "filters": {"q": "nurse"}, "is_default": True})
    sid2 = r2.get_json()["strategy"]["id"]

    lst = c.get("/api/strategies").get_json()["strategies"]
    assert [s["name"] for s in lst] == ["Nurses", "Data NYC"]          # default first
    by = {s["id"]: s for s in lst}
    assert by[sid]["new_count"] == 1 and by[sid]["newest_at"] == "2099-01-01 00:00:00"   # new1 only
    assert by[sid2]["new_count"] == 1                                   # new2
    assert all("new_count" not in s for s in c.get("/api/strategies?counts=0").get_json()["strategies"])

    d = c.get(f"/api/strategies/{sid}/new").get_json()
    assert d["count"] == 1 and d["jobs"][0]["source_id"] == "new1" and d["jobs"][0]["saved"] is False

    assert c.post(f"/api/strategies/{sid}/default", json={}).get_json()["strategy"]["is_default"] is True
    assert c.get(f"/api/strategies/{sid2}").get_json()["strategy"]["is_default"] is False

    up = c.post(f"/api/strategies/{sid}", json={"name": "Data NY", "filters": {"q": "nurse"}}).get_json()
    assert up["strategy"]["name"] == "Data NY" and up["strategy"]["filters"]["q"] == "nurse"
    assert c.put(f"/api/strategies/{sid}", json={"name": "Data NY2"}).get_json()["strategy"]["name"] == "Data NY2"

    assert c.delete(f"/api/strategies/{sid2}").status_code == 200
    assert c.get(f"/api/strategies/{sid2}").status_code == 404
    assert c.get(f"/api/strategies/{sid2}/new").status_code == 404
    assert c.delete(f"/api/strategies/{sid2}").status_code == 404


def test_seen_route_moves_the_cutoff_so_the_digest_empties(app, monkeypatch):
    # A board whose only arrival is dated later than the strategy's creation, pinned by patching
    # the row list rather than the clock; after /seen the cutoff is "now" and nothing is newer.
    monkeypatch.setattr(app, "_board_rows", lambda: [_job("x", "Data", "2099-01-01 00:00:00")])
    c = app.app.test_client()
    sid = c.post("/api/strategies", json={"name": "d", "filters": {"q": "data"}}).get_json()["strategy"]["id"]
    assert c.get(f"/api/strategies/{sid}/new").get_json()["count"] == 1
    r = c.post(f"/api/strategies/{sid}/seen", json={})
    assert r.status_code == 200 and r.get_json()["strategy"]["last_seen"]
    # The 2099 arrival is still "after" a real now, so pin the digest's view by moving the row back.
    monkeypatch.setattr(app, "_board_rows", lambda: [_job("x", "Data", "2000-01-01 00:00:00")])
    assert c.get(f"/api/strategies/{sid}/new").get_json()["count"] == 0


def test_open_urls_route_builds_from_the_stored_strategy(app):
    c = app.app.test_client()
    sid = c.post("/api/strategies", json={"name": "d", "filters": {"q": "data analyst", "loc": "Boston"}}).get_json()["strategy"]["id"]
    d = c.get(f"/api/strategies/{sid}/open-urls").get_json()
    assert d["strategy_id"] == sid and [u["board"] for u in d["urls"]] == ["linkedin", "indeed", "glassdoor", "handshake", "google"]
    assert "keywords=data+analyst" in d["urls"][0]["url"] and "location=Boston" in d["urls"][0]["url"]
    assert c.get("/api/strategies/999/open-urls").status_code == 404
