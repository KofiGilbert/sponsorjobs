"""SmartRecruiters boards are LIST-ONLY, like Workday: the postings list omits the description,
so a row is kept only once its detail has been read and the accessibility check has seen it
(sourcing/service.py select_checked_rows). Everything here runs against a fake of the two public
endpoints (the postings list, a posting's detail); nothing touches the network. The promises
pinned: the per-board budget is respected and spent newest-first; a citizenship-barred posting is
dropped; rows the budget did not reach (or whose detail failed) are NOT stored; a legacy row stored
without a JD is hidden from the board and the feed until it is opened and checked; the env knobs
default like Workday's; and the Workday path behaves exactly as before.
"""

from __future__ import annotations

import urllib.error

import pytest

from sourcing import ats
from sourcing.quality import LIST_ONLY_SOURCES, is_jd_checked, is_list_only
from sourcing.service import (DEFAULT_MAX_DETAIL, FEED_MAX_DETAIL, feed_detail_budgets,
                              feed_max_detail_for, feed_workday_max_detail, max_detail_for,
                              refresh_watchlist, select_checked_rows, select_workday_rows)
from sourcing.watchlist import Watchlist

BOARD = "acme"
LIST_URL = f"https://api.smartrecruiters.com/v1/companies/{BOARD}/postings"


def _http(code, url="https://x", headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {}, None)


def _posting(i, title=None, released="2026-09-29", desc=None, city="Chicago", region="IL"):
    return {"id": f"p{i}", "name": title or f"Software Engineer {i}",
            "location": {"city": city, "region": region, "country": "United States", "remote": False},
            "releasedDate": f"{released}T00:00:00.000Z" if released else "",
            "ref": f"{LIST_URL}/p{i}",
            "desc": desc if desc is not None else f"<p>Build <b>pipelines</b> #{i}</p><ul><li>SQL</li></ul>"}


class FakeSmartRecruiters:
    """The postings list and a posting's detail for one board; anything else is a 404 (the
    aggregator feeds a refresh also tries). `detail_fail` makes every detail read a 500."""

    def __init__(self, postings, detail_fail=False):
        self.postings = postings
        self.detail_fail = detail_fail
        self.urls: list[str] = []

    def __call__(self, url, timeout=20, data=None, raw=False):
        self.urls.append(url)
        if url.startswith(LIST_URL + "?"):
            return {"content": [{k: p[k] for k in ("id", "name", "location", "releasedDate", "ref")}
                                for p in self.postings]}
        if url.startswith(LIST_URL + "/"):
            if self.detail_fail:
                raise _http(500, url)
            pid = url.rsplit("/", 1)[1]
            for p in self.postings:
                if p["id"] == pid:
                    return {"postingUrl": f"https://jobs.smartrecruiters.com/Acme/{pid}-{p['name'].replace(' ', '-')}",
                            "jobAd": {"sections": {"jobDescription": {"text": p["desc"]}}}}
            raise _http(404, url)
        raise _http(404, url)

    @property
    def detail_urls(self):
        return [u for u in self.urls if u.startswith(LIST_URL + "/")]


def _board_only_watchlist(path=":memory:"):
    w = Watchlist(path)
    w.add_company("Acme", "smartrecruiters", BOARD)
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any",
                    "aggregators": ["none"]})                  # boards only: no aggregator pulls
    return w


CRIT = {"titles": ["engineer"], "locations": [], "remote": "any"}


# -- the check-before-keep refresh ----------------------------------------------- #

def test_smartrecruiters_is_a_list_only_source_like_workday():
    assert LIST_ONLY_SOURCES == {"workday", "smartrecruiters"}
    assert is_list_only({"source": "smartrecruiters"}) and not is_list_only({"source": "lever"})
    assert not is_jd_checked({"source": "smartrecruiters", "jd_text": ""})
    assert not is_jd_checked({"source": "smartrecruiters", "has_jd": False})
    assert is_jd_checked({"source": "smartrecruiters", "jd_text": "Build pipelines."})
    assert is_jd_checked({"source": "greenhouse", "jd_text": ""})   # inline-JD feeds are always checked


def test_refresh_keeps_a_smartrecruiters_row_only_once_its_jd_is_checked_and_never_stores_an_unchecked_one():
    # Rows past the budget used to be stored (and published) with an empty jd_text, which
    # is_intl_student_accessible() waves through -- for a 25,000-posting franchise board. Now a row is
    # kept only after its description has been read and checked; what the budget did not reach is
    # left for the next refresh, and a posting that bars international candidates is dropped.
    posts = [_posting(i) for i in range(6)]
    posts[1]["desc"] = "<p>Must be a US citizen with an active TS/SCI clearance.</p>"
    fake = FakeSmartRecruiters(posts)
    w = _board_only_watchlist()
    s = refresh_watchlist(w, fetch=fake, detail_budgets={"smartrecruiters": 3}, sleep=lambda s: None)
    assert [e for e in s["errors"] if e["company"] == "Acme"] == []
    assert len(fake.detail_urls) == 3                           # the budget, in board order (same date)
    stored = w.get_jd_texts([j["source_id"] for j in w.list_jobs()])
    assert s["new"] == 2 and len(stored) == 2                   # #0 and #2: #1 excluded, #3-5 unread
    assert all("Build pipelines" in jd for jd in stored.values())   # every stored row carries its JD
    assert f"smartrecruiters:{BOARD}:p1" not in stored          # the citizenship-barred posting
    assert s["unchecked"] == {"workday": 0, "smartrecruiters": 3} and s["workday_unchecked"] == 0
    assert all(j["has_jd"] for j in w.list_jobs())
    # The detail also gave each stored row its real public page (not the constructed one).
    assert all(j["url"].startswith("https://jobs.smartrecruiters.com/Acme/p") and "Software-Engineer" in j["url"]
               for j in w.list_jobs())
    # The next refresh skips what is stored (no detail request for those) and reads the next rows.
    before = len(fake.urls)
    s2 = refresh_watchlist(w, fetch=fake, detail_budgets={"smartrecruiters": 3}, sleep=lambda s: None)
    new_details = [u for u in fake.urls[before:] if u.startswith(LIST_URL + "/")]
    assert len(new_details) == 3 and not any(u.endswith("/p0") or u.endswith("/p2") for u in new_details)
    assert s2["new"] == 2 and s2["unchecked"]["smartrecruiters"] == 1   # #3, #4 kept; #1 dropped; #5 left
    assert w.source_ids_with_jd("smartrecruiters") == {f"smartrecruiters:{BOARD}:p{i}" for i in (0, 2, 3, 4)}
    # A detail endpoint that cannot be read leaves the row unchecked -> not stored.
    broken = FakeSmartRecruiters([_posting(i) for i in range(2)], detail_fail=True)
    w2 = _board_only_watchlist()
    s3 = refresh_watchlist(w2, fetch=broken, sleep=lambda s: None)      # no real 5xx back-off waits
    assert s3["new"] == 0 and s3["unchecked"]["smartrecruiters"] == 2 and w2.list_jobs() == []


def test_select_checked_rows_reads_newest_postings_first_stops_at_the_cap_and_paces_the_reads():
    posts = [_posting(0, released="2026-09-01"), _posting(1, released="2026-09-30"),
             _posting(2, released="2026-09-15"), _posting(3, released="2026-09-30"), _posting(4, released="")]
    fake = FakeSmartRecruiters(posts)
    rows = ats.smartrecruiters_jobs(BOARD, "Acme", fetch=fake)
    assert all(r["jd_text"] == "" for r in rows)              # list-only, one request
    naps: list[float] = []
    kept, fetched, left = select_checked_rows("smartrecruiters", rows, CRIT, fetch=fake, max_detail=2,
                                              sleep=naps.append)
    assert [len(kept), fetched, left] == [2, 2, 3]
    assert [u.rsplit("/", 1)[1] for u in fake.detail_urls] == ["p1", "p3"]   # newest first, board order within a date
    assert [k["source_id"] for k in kept] == [f"smartrecruiters:{BOARD}:p1", f"smartrecruiters:{BOARD}:p3"]
    assert naps == [ats.SMARTRECRUITERS_REQUEST_GAP] * 2       # one polite gap after each detail read
    # Enough budget but a small cap: stop as soon as `cap` rows are confirmed, no wasted reads.
    fake.urls.clear()
    rows = ats.smartrecruiters_jobs(BOARD, "Acme", fetch=fake)
    kept, fetched, left = select_checked_rows("smartrecruiters", rows, CRIT, fetch=fake, max_detail=50,
                                              cap=3, sleep=lambda s: None)
    assert [len(kept), fetched, left] == [3, 3, 0] and len(fake.detail_urls) == 3
    # The undated posting is read last.
    fake.urls.clear()
    rows = ats.smartrecruiters_jobs(BOARD, "Acme", fetch=fake)
    select_checked_rows("smartrecruiters", rows, CRIT, fetch=fake, max_detail=50, sleep=lambda s: None)
    assert fake.detail_urls[-1].endswith("/p4")
    with pytest.raises(ValueError):
        select_checked_rows("lever", rows, CRIT, fetch=fake)


def test_detail_budgets_default_like_workdays_and_come_from_the_environment(monkeypatch):
    for name in ("SMARTRECRUITERS_MAX_DETAIL", "SMARTRECRUITERS_MAX_DETAIL_FEED",
                 "WORKDAY_MAX_DETAIL", "WORKDAY_MAX_DETAIL_FEED"):
        monkeypatch.delenv(name, raising=False)
    assert max_detail_for("smartrecruiters") == max_detail_for("workday") == DEFAULT_MAX_DETAIL == 200
    assert feed_max_detail_for("smartrecruiters") == feed_workday_max_detail() == FEED_MAX_DETAIL == 2000
    assert feed_detail_budgets() == {"workday": 2000, "smartrecruiters": 2000}
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL", "7")
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL_FEED", "70")
    monkeypatch.setenv("WORKDAY_MAX_DETAIL_FEED", "77")
    assert max_detail_for("smartrecruiters") == 7 and max_detail_for("workday") == 200
    assert feed_detail_budgets() == {"workday": 77, "smartrecruiters": 70}
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL", "nope")
    assert max_detail_for("smartrecruiters") == 200
    # The desktop default is what select_checked_rows spends when no budget is passed.
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL", "2")
    fake = FakeSmartRecruiters([_posting(i) for i in range(5)])
    rows = ats.smartrecruiters_jobs(BOARD, "Acme", fetch=fake)
    kept, fetched, left = select_checked_rows("smartrecruiters", rows, CRIT, fetch=fake, sleep=lambda s: None)
    assert [len(kept), fetched, left] == [2, 2, 3]


def test_a_429_on_a_detail_backs_off_once_then_the_row_is_left_unchecked():
    posts = [_posting(0)]
    calls: list[str] = []
    naps: list[float] = []

    def fetch(url, **kw):
        calls.append(url)
        if url.startswith(LIST_URL + "?"):
            return {"content": [{k: p[k] for k in ("id", "name", "location", "releasedDate", "ref")} for p in posts]}
        raise _http(429, url, {"Retry-After": "3"})

    rows = ats.smartrecruiters_jobs(BOARD, "Acme", fetch=fetch)
    kept, fetched, left = select_checked_rows("smartrecruiters", rows, CRIT, fetch=fetch, sleep=naps.append)
    assert (kept, fetched, left) == ([], 1, 1)
    assert len([u for u in calls if u.startswith(LIST_URL + "/")]) == 2        # one retry, not a hammering
    assert naps[0] == 3.0                                                       # Retry-After honoured


# -- legacy unchecked rows: hidden until opened and checked --------------------------- #

def test_the_board_and_the_feed_hide_a_legacy_unchecked_smartrecruiters_row_until_it_is_opened_and_checked(
        tmp_path, monkeypatch):
    import backend.feed as feed
    import ui.app as app
    from sourcing.sponsors import SponsorDB
    db = str(tmp_path / "t.db")
    row = {"source": "smartrecruiters", "source_id": f"smartrecruiters:{BOARD}:p1", "company": "Acme",
           "title": "Software Engineer", "location": "Chicago, IL", "remote": "",
           "url": f"https://jobs.smartrecruiters.com/{BOARD}/p1", "jd_text": "",
           "posted_at": "2026-09-29", "salary": ""}
    checked = {**row, "source_id": f"smartrecruiters:{BOARD}:p2", "title": "ML Engineer",
               "url": f"https://jobs.smartrecruiters.com/{BOARD}/p2", "jd_text": "Build pipelines"}
    w = Watchlist(db)
    w.upsert_jobs([row, checked])                               # a row stored list-only, pre-fix
    slim = {j["source_id"]: j for j in w.list_jobs()}
    assert slim[row["source_id"]]["has_jd"] is False and slim[checked["source_id"]]["has_jd"] is True
    assert not is_jd_checked(slim[row["source_id"]]) and is_jd_checked(slim[checked["source_id"]])
    w.close()
    # The central feed never publishes the unchecked row.
    monkeypatch.setenv("JOBS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(feed, "_paths", lambda: (db, str(tmp_path / "s.db")))
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    assert [j["source_id"] for j in feed.feed_jobs()] == [checked["source_id"]]
    # Nor does the local board.
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    monkeypatch.setattr(app, "DB_PATH", db)
    client = app.app.test_client()
    ids = [j["source_id"] for j in client.get("/api/jobs?per_page=50").get_json()["jobs"]]
    assert checked["source_id"] in ids and row["source_id"] not in ids
    # Opening it fetches the detail and runs the check THEN: a barred role is dismissed, not shown.
    seen: list[tuple] = []

    def detail(board, pid, fetch=None, sleep=None):
        seen.append((board, pid))
        return (f"https://jobs.smartrecruiters.com/Acme/{pid}-role",
                "US citizenship required." if pid == "p1" else "Build pipelines with SQL.")

    monkeypatch.setattr(ats, "smartrecruiters_detail", detail)
    r = client.get(f"/api/jobs/detail?source_id={row['source_id']}")
    assert r.status_code == 404 and "not open to international" in r.get_json()["error"]
    assert seen == [(BOARD, "p1")]
    w = Watchlist(db)
    assert w.get_job(row["source_id"])["dismissed"] == 1
    assert feed.feed_detail(row["source_id"]) is None           # the hosted detail route agrees
    # An accessible JD is shown and persisted -- with the posting's real public page -- so the row
    # is checked from now on.
    w.upsert_jobs([{**row, "source_id": f"smartrecruiters:{BOARD}:p3",
                    "url": f"https://jobs.smartrecruiters.com/{BOARD}/p3"}])
    w.close()
    d = client.get(f"/api/jobs/detail?source_id=smartrecruiters:{BOARD}:p3").get_json()
    assert "SQL" in d["jd"]
    w = Watchlist(db)
    assert {j["source_id"]: j["has_jd"] for j in w.list_jobs()}[f"smartrecruiters:{BOARD}:p3"] is True
    assert w.get_job(f"smartrecruiters:{BOARD}:p3")["url"] == "https://jobs.smartrecruiters.com/Acme/p3-role"
    w.close()
    # The hosted detail route reads an accessible legacy row the same way.
    hosted = feed.feed_detail(f"smartrecruiters:{BOARD}:p2")
    assert hosted and "Build pipelines" in hosted["jd"]


def test_feed_build_reads_every_kept_smartrecruiters_jd_with_the_feed_budget(tmp_path, monkeypatch):
    import backend.feed as feed
    import sourcing.service as service
    from scripts import build_feed
    from sourcing.feedfile import loads_maybe_gz, shard_for
    from sourcing.sponsors import SponsorDB
    seed = tmp_path / "seed.csv"
    seed.write_text(f"company,ats,board_id\nAcme,smartrecruiters,{BOARD}\n")
    monkeypatch.setattr(service, "SEED_WATCHLIST", seed)
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL", "1")       # the desktop cap would read one
    monkeypatch.setenv("SMARTRECRUITERS_MAX_DETAIL_FEED", "50") # the build reads them all
    monkeypatch.setattr(ats, "SMARTRECRUITERS_REQUEST_GAP", 0)  # no real pacing waits in a test
    posts = [_posting(i) for i in range(4)]
    posts[3]["desc"] = "<p>We are unable to sponsor visas for this role.</p>"
    fake = FakeSmartRecruiters(posts)
    m = build_feed.build(tmp_path / "out", tmp_path / "data", fetch=fake,
                         jobs_url="https://f.example.com/jobs.json.gz", log=lambda *a: None)
    head = loads_maybe_gz((tmp_path / "out" / "jobs.json.gz").read_bytes())
    rows = [j for j in head["jobs"] if j["source"] == "smartrecruiters"]
    assert len(rows) == 3 and m["count"] == 3                  # #0-2 published, #3 (no sponsorship) not
    for r in rows:
        sh = loads_maybe_gz((tmp_path / "out" / "jd" / f"{shard_for(r['source_id'])}.json.gz").read_bytes())
        assert "Build pipelines" in sh[r["source_id"]]          # every published row has its JD


# -- Workday is unchanged ------------------------------------------------------------- #

def test_workday_behaviour_is_unchanged_and_each_ats_spends_its_own_budget():
    from tests.test_workday import BOARD as WD_BOARD, FakeWorkday, _distinct, _nvidia
    wd = FakeWorkday(_nvidia(5))
    _distinct(wd, 5)
    sr = FakeSmartRecruiters([_posting(i) for i in range(5)])

    def fetch(url, **kw):
        return sr(url, **kw) if "smartrecruiters.com" in url else wd(url, **kw)

    # select_workday_rows is the generic selector on the Workday fetcher, result for result.
    from sourcing import workday
    rows = workday.workday_jobs("nvidia", "NVIDIAExternalCareerSite", "nvidia.wd5.myworkdayjobs.com",
                                fetch=wd, sleep=lambda s: None)
    a = select_workday_rows([dict(r) for r in rows], CRIT, fetch=wd, max_detail=2, sleep=lambda s: None)
    b = select_checked_rows("workday", [dict(r) for r in rows], CRIT, fetch=wd, max_detail=2, sleep=lambda s: None)
    assert a == b and [len(a[0]), a[1], a[2]] == [2, 2, 3]
    # One refresh over both boards: each ATS has its own budget and its own unchecked count, and
    # the older `workday_max_detail` knob still sets Workday's.
    w = Watchlist(":memory:")
    w.add_company("NVIDIA", "workday", WD_BOARD)
    w.add_company("Acme", "smartrecruiters", BOARD)
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any", "aggregators": ["none"]})
    before = len(wd.urls)
    s = refresh_watchlist(w, fetch=fetch, workday_max_detail=1, detail_budgets={"smartrecruiters": 3},
                          sleep=lambda s: None)
    assert [e for e in s["errors"] if e["company"] in ("NVIDIA", "Acme")] == []
    wd_details = [u for u in wd.urls[before:] if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert len(wd_details) == 1 and len(sr.detail_urls) == 3
    assert s["new"] == 4 and s["unchecked"] == {"workday": 4, "smartrecruiters": 2}
    assert s["workday_unchecked"] == 4
    by_src = {}
    for j in w.list_jobs():
        by_src.setdefault(j["source"], []).append(j)
    assert len(by_src["workday"]) == 1 and len(by_src["smartrecruiters"]) == 3
    assert all(j["has_jd"] for j in w.list_jobs())
