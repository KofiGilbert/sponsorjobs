"""Workday career sites (sourcing/workday.py) -- the public JSON behind *.myworkdayjobs.com.

Everything here runs against a fake of the three Workday endpoints (robots.txt, the POST job
list, the job detail); nothing touches the network. The promises pinned: rows come out in the
shared ATS shape; paging walks 20 at a time, stops at the cap, and is not fooled by Workday's
wrap-around past the end; "Posted N Days Ago" becomes a date; descriptions are fetched lazily
and capped, never for rows the store already holds, and a list-only re-ingest never blanks a
stored JD; discovery reads the site name from robots.txt (or a landing redirect), respects a
Disallow, confirms identity with the same brand-word rule as the other ATSs, and treats a 422
as "no tenant"; the dedup key is the job page URL; and Workday is ASSISTED in the allowlist.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import re
import urllib.error
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from sourcing import growth, workday
from sourcing.ats import normalize_jobs, valid_board_id_for
from sourcing.dedup import DedupIndex
from sourcing.discover import DISCOVER_ATS, discover_for_company
from sourcing.feedclient import fetch_board_jd
from sourcing.service import refresh_watchlist
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 30)


# -- a fake of the public Workday endpoints ------------------------------------- #

def _http(code, url="https://x"):
    return urllib.error.HTTPError(url, code, "err", {}, None)


def _posting(i, title="Software Engineer", loc="US, CA, Santa Clara", posted="Posted Today",
             remote_type=None, desc=None, start=None, bullets=None):
    rid = f"JR{1000 + i}"
    return {"title": title, "externalPath": f"/job/US-CA-Santa-Clara/{title.replace(' ', '-')}_{rid}",
            "locationsText": loc, "postedOn": posted, "bulletFields": bullets or [rid],
            "jobDescription": desc if desc is not None else f"<p>Build <b>GPUs</b> #{i}</p><ul><li>CUDA</li></ul>",
            "startDate": start or "2026-09-28", "location": loc, "remoteType": remote_type}


def _robots(*sites, disallow=()):
    lines = [f"Sitemap: https://host/{s}/siteMap.xml" for s in sites]
    lines += ["", "User-agent: *"] + [f"Allow: /{s}/" for s in sites]
    lines += [f"Disallow: /{s}/" for s in disallow] + ["Disallow: /refreshFacet/"]
    return "\n".join(lines)


class FakeWorkday:
    """tenants: {("nvidia", 5): {"robots": text, "sites": {"Site": {"org": name, "postings": [...],
    "total": reported_total}}}}. Hosts of unknown tenants answer 422 like the real edge."""

    def __init__(self, tenants, detail_fail=False):
        self.tenants = tenants
        self.detail_fail = detail_fail
        self.urls: list[str] = []
        self.posts: list[dict] = []

    def __call__(self, url, timeout=20, data=None, raw=False):
        self.urls.append(url)
        u = urlsplit(url)
        m = re.match(r"^([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com$", u.hostname or "")
        if not m:
            raise _http(404, url)
        t = self.tenants.get((m.group(1), int(m.group(2))))
        if t is None:
            raise _http(422, url)
        if u.path == "/robots.txt":
            assert raw, "robots.txt is text, must be fetched raw"
            return t["robots"]
        if u.path == "/":
            raise _http(406, url)                       # the landing page refuses non-browsers
        m2 = re.match(r"^/wday/cxs/([^/]+)/([^/]+)(/.*)$", u.path)
        if not m2 or m2.group(1) != m.group(1) or m2.group(2) not in t["sites"]:
            raise _http(404, url)
        site = t["sites"][m2.group(2)]
        postings = site["postings"]
        if m2.group(3) == "/jobs":
            assert data is not None, "the job list is POST-only"
            self.posts.append(data)
            offset, limit = int(data["offset"]), int(data["limit"])
            if offset >= len(postings):
                offset = 0                              # Workday wraps past the end
            page = postings[offset:offset + limit]
            return {"total": site.get("total", len(postings)),
                    "jobPostings": [{k: p[k] for k in ("title", "externalPath", "locationsText",
                                                       "postedOn", "bulletFields")} for p in page]}
        if self.detail_fail:
            raise _http(500, url)
        for p in postings:
            if p["externalPath"] == m2.group(3):
                info = {"jobDescription": p["jobDescription"], "startDate": p["startDate"],
                        "location": p["location"], "timeType": "Full time",
                        "jobReqId": p["bulletFields"][-1],
                        "externalUrl": f"https://{u.hostname}/{m2.group(2)}{p['externalPath']}"}
                if p.get("remoteType"):
                    info["remoteType"] = p["remoteType"]
                return {"jobPostingInfo": info, "hiringOrganization": {"name": site["org"]}}
        raise _http(404, url)


def _nvidia(n=3, **kw):
    return {("nvidia", 5): {"robots": _robots("NVIDIAExternalCareerSite"),
                            "sites": {"NVIDIAExternalCareerSite": {
                                "org": "2100 NVIDIA USA",
                                "postings": [_posting(i) for i in range(n)], **kw}}}}


HOST = "nvidia.wd5.myworkdayjobs.com"
SITE = "NVIDIAExternalCareerSite"
BOARD = "nvidia.wd5/NVIDIAExternalCareerSite"


# -- rows ---------------------------------------------------------------------- #

def test_rows_mirror_the_shared_ats_shape():
    fake = FakeWorkday(_nvidia(2))
    fake.tenants[("nvidia", 5)]["sites"][SITE]["postings"][1].update(
        {"locationsText": "US, Remote", "postedOn": "Posted 3 Days Ago", "title": "ML Engineer"})
    rows = workday.workday_jobs("nvidia", SITE, HOST, "NVIDIA", fetch=fake, today=TODAY)
    assert [r["source_id"] for r in rows] == [f"workday:{BOARD}:JR1000", f"workday:{BOARD}:JR1001"]
    r = rows[0]
    assert r["source"] == "workday" and r["company"] == "NVIDIA" and r["title"] == "Software Engineer"
    assert r["url"] == f"https://{HOST}/{SITE}/job/US-CA-Santa-Clara/Software-Engineer_JR1000"
    assert r["location"] == "US, CA, Santa Clara" and r["remote"] == "" and r["posted_at"] == "2026-09-30"
    assert r["jd_text"] == "" and r["salary"] == ""            # list-only: the JD comes later
    assert rows[1]["remote"] == "remote" and rows[1]["posted_at"] == "2026-09-27"
    # Every request stayed on the tenant's own Workday host.
    assert all(urlsplit(u).hostname == HOST for u in fake.urls)


def test_adapter_is_registered_and_the_board_id_names_tenant_instance_and_site():
    fake = FakeWorkday(_nvidia(1))
    rows = normalize_jobs("workday", BOARD, "NVIDIA", fetch=fake, today=TODAY)
    assert len(rows) == 1 and rows[0]["source_id"].startswith(f"workday:{BOARD}:")
    assert workday.parse_board_id(BOARD) == ("nvidia", HOST, SITE)
    assert workday.board_id_for("nvidia", HOST, SITE) == BOARD
    assert valid_board_id_for("workday", BOARD)
    for bad in ("nvidia/Site", "evil.com/x", "nvidia.wd5/../x", "nvidia.wd5/", "nvidia.wd5",
                "nvidia.wd5/Site/extra", "a.b.wd5/Site", "NVIDIA.wd5/Site"):
        assert not valid_board_id_for("workday", bad), bad
        with pytest.raises(ValueError):
            normalize_jobs("workday", bad, fetch=fake)
    assert len(fake.urls) == 1                                  # the bad ids never reached a URL
    assert not valid_board_id_for("greenhouse", BOARD)          # a slash is still wrong elsewhere


def test_watchlist_accepts_a_workday_board_and_rejects_a_malformed_one():
    w = Watchlist(":memory:")
    w.add_company("NVIDIA", "workday", BOARD)
    assert [(c["ats"], c["board_id"]) for c in w.companies()] == [("workday", BOARD)]
    with pytest.raises(ValueError):
        w.add_company("Evil", "workday", "evil.com/x")


# -- paging ------------------------------------------------------------------- #

def test_paging_walks_twenty_at_a_time_and_stops_at_the_cap():
    fake = FakeWorkday(_nvidia(50))
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, max_jobs=30, sleep=lambda s: None)
    assert len(rows) == 30 and len(fake.posts) == 2
    assert [(p["offset"], p["limit"]) for p in fake.posts] == [(0, 20), (20, 10)]
    assert fake.posts[0]["appliedFacets"] == {} and fake.posts[0]["searchText"] == ""


def test_paging_is_not_fooled_by_the_wrap_around_past_the_end():
    # Workday reports total=2000 for a big board and serves page one again for any offset past
    # the real end; a 40-posting site must yield 40 rows and stop on the first repeat.
    fake = FakeWorkday(_nvidia(40, total=2000))
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, max_jobs=1000, sleep=lambda s: None)
    assert len(rows) == 40 and len({r["source_id"] for r in rows}) == 40
    assert [p["offset"] for p in fake.posts] == [0, 20, 40]


def test_paging_stops_on_a_short_page_and_never_passes_workdays_ceiling():
    fake = FakeWorkday(_nvidia(25))
    assert len(workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)) == 25
    assert [p["offset"] for p in fake.posts] == [0, 20]
    _, postings = workday.list_postings("nvidia", SITE, HOST, fake, max_jobs=5000, sleep=lambda s: None)
    assert len(postings) == 25 and max(p["offset"] for p in fake.posts) < workday.LIST_CEILING


def test_max_jobs_defaults_from_the_environment(monkeypatch):
    monkeypatch.setenv("WORKDAY_MAX_JOBS", "20")
    fake = FakeWorkday(_nvidia(50))
    assert len(workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)) == 20
    assert len(fake.posts) == 1


def test_a_429_then_5xx_are_retried_with_backoff_and_a_404_is_not():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky(url, timeout=20, data=None, raw=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 429, "slow down", {"Retry-After": "3"}, None)
        if calls["n"] == 2:
            raise _http(503, url)
        return {"total": 1, "jobPostings": [{k: _posting(0)[k] for k in
                                             ("title", "externalPath", "locationsText", "postedOn", "bulletFields")}]}
    _, postings = workday.list_postings("nvidia", SITE, HOST, flaky, sleep=sleeps.append)
    assert len(postings) == 1 and calls["n"] == 3
    assert sleeps[0] == pytest.approx(3.0) and sleeps[1] == pytest.approx(4.0)   # Retry-After, then 2*2^1

    def gone(url, timeout=20, data=None, raw=False):
        raise _http(404, url)
    with pytest.raises(urllib.error.HTTPError):
        workday.list_postings("nvidia", SITE, HOST, gone, sleep=sleeps.append)
    assert len(sleeps) == 2                                     # no retry sleeps for a 404


# -- posted_at ----------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("Posted Today", "2026-09-30"), ("Posted Yesterday", "2026-09-29"),
    ("Posted 5 Days Ago", "2026-09-25"), ("Posted 1 Day Ago", "2026-09-29"),
    ("Posted 30+ Days Ago", "2026-08-31"), ("posted  12  days  ago", "2026-09-18"),
    ("", ""), ("Posted in a while", ""), (None, ""),
])
def test_posted_on_to_date(text, expected):
    assert workday.posted_on_to_date(text, TODAY) == expected


def test_req_id_prefers_the_path_suffix_then_an_id_like_bullet():
    assert workday.req_id(_posting(7)) == "JR1007"
    assert workday.req_id({"externalPath": "/job/x/Senior-Manager_REF088642W-1",
                           "bulletFields": ["Spotlight Job", "REF088642W-1"]}) == "REF088642W-1"
    assert workday.req_id({"externalPath": "/job/x/No-Suffix-Here",
                           "bulletFields": ["Spotlight Job", "R0000475998"]}) == "R0000475998"
    assert workday.req_id({"externalPath": "/job/x/Just-A-Slug", "bulletFields": []}) == "Just-A-Slug"


# -- lazy, capped descriptions ------------------------------------------------- #

def test_fill_details_is_capped_and_skips_rows_the_store_already_holds():
    fake = FakeWorkday(_nvidia(5))
    posts = fake.tenants[("nvidia", 5)]["sites"][SITE]["postings"]
    posts[0].update({"locationsText": "2 Locations", "location": "Austin, TX", "remoteType": "Hybrid",
                     "startDate": "2026-09-01"})
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, today=TODAY, sleep=lambda s: None)
    before = len(fake.urls)
    n = workday.fill_details(rows, fetch=fake, known_ids={rows[1]["source_id"]}, max_detail=2,
                             sleep=lambda s: None)
    assert n == 2 and len(fake.urls) - before == 2
    assert "Build GPUs #0" in rows[0]["jd_text"] and "• CUDA" in rows[0]["jd_text"]
    assert rows[0]["location"] == "Austin, TX" and rows[0]["remote"] == "hybrid"
    assert rows[0]["posted_at"] == "2026-09-01"                 # the exact date beats "Posted Today"
    assert rows[1]["jd_text"] == ""                             # known: not fetched again
    assert rows[2]["jd_text"] and rows[3]["jd_text"] == "" and rows[4]["jd_text"] == ""   # cap


def test_fill_details_default_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("WORKDAY_MAX_DETAIL", "1")
    fake = FakeWorkday(_nvidia(3))
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)
    assert workday.fill_details(rows, fetch=fake, sleep=lambda s: None) == 1


def test_detail_url_is_derived_from_the_job_page_url():
    page = f"https://{HOST}/{SITE}/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2000499"
    assert workday.detail_url_for(page) == (
        f"https://{HOST}/wday/cxs/nvidia/{SITE}/job/US-CA-Santa-Clara/Senior-DFT-Engineer_JR2000499")
    assert workday.detail_url_for("https://boards.greenhouse.io/acme/jobs/1") == ""
    assert workday.detail_url_for(f"https://{HOST}/{SITE}") == ""
    assert workday.detail_url_for("") == ""


def test_jd_on_demand_for_a_stored_row_via_feedclient_and_module():
    fake = FakeWorkday(_nvidia(1))
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)
    assert "Build GPUs #0" in workday.jd_for_job(rows[0], fetch=fake)
    assert "Build GPUs #0" in fetch_board_jd(rows[0], fetch=fake)
    assert workday.jd_for_job({"url": "https://example.com/x"}, fetch=fake) == ""
    assert fetch_board_jd({"source_id": "workday:x.wd5/S:1", "url": ""}, fetch=fake) == ""


# -- the refresh: list-only rows, details for what is kept, nothing blanked --- #

def test_refresh_fills_details_only_for_kept_rows_and_a_blank_reingest_keeps_the_jd():
    fake = FakeWorkday(_nvidia(4))
    posts = fake.tenants[("nvidia", 5)]["sites"][SITE]["postings"]
    posts[1].update({"title": "Senior Software Engineer"})      # distinct roles (else dedup folds them)
    posts[2].update({"title": "ML Engineer"})
    posts[3].update({"title": "Office Manager"})               # fails the title criteria
    w = Watchlist(":memory:")
    w.add_company("NVIDIA", "workday", BOARD)
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any",
                    "aggregators": ["none"]})                  # boards only: no aggregator pulls
    s = refresh_watchlist(w, fetch=fake)
    board_errors = [e for e in s["errors"] if e["company"] == "NVIDIA"]   # aggregators 404 on this fake
    assert board_errors == [] and s["new"] == 3
    stored = w.get_jd_texts([j["source_id"] for j in w.list_jobs()])   # list_jobs() is slim: no jd
    assert len(stored) == 3 and all("Build GPUs" in jd for jd in stored.values())
    detail_urls = [u for u in fake.urls if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert len(detail_urls) == 3                                # not the Office Manager row
    # Second refresh: the details are already stored, so no detail request is made, and the
    # list-only rows (jd_text == "") do not blank what is stored.
    fake.detail_fail = True
    before = len(fake.urls)
    s2 = refresh_watchlist(w, fetch=fake)
    assert [e for e in s2["errors"] if e["company"] == "NVIDIA"] == [] and s2["new"] == 0
    assert not [u for u in fake.urls[before:] if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert all("Build GPUs" in jd for jd in w.get_jd_texts(list(stored)).values())
    assert w.source_ids_with_jd("workday") == set(stored)


def _distinct(fake, n):
    """Give a fake tenant's postings distinct titles (the dedup index folds same-title rows)."""
    posts = fake.tenants[("nvidia", 5)]["sites"][SITE]["postings"]
    for i, p in enumerate(posts[:n]):
        p["title"] = f"Software Engineer {i}"
    return posts


def _board_only_watchlist(path=":memory:"):
    w = Watchlist(path)
    w.add_company("NVIDIA", "workday", BOARD)
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any",
                    "aggregators": ["none"]})                  # boards only: no aggregator pulls
    return w


def test_refresh_keeps_a_workday_row_only_once_its_jd_is_checked_and_never_stores_an_unchecked_one():
    # A 1,000-posting tenant used to have its rows past the detail cap stored (and published) with
    # an empty jd_text, which is_intl_student_accessible() waves through. Now a row is kept only
    # after its description has been read and checked; what the budget did not reach is left for
    # the next refresh, and a posting that bars international candidates is dropped.
    fake = FakeWorkday(_nvidia(6))
    posts = _distinct(fake, 6)
    posts[1]["jobDescription"] = "<p>Must be a US citizen with an active TS/SCI clearance.</p>"
    w = _board_only_watchlist()
    s = refresh_watchlist(w, fetch=fake, workday_max_detail=3)
    assert [e for e in s["errors"] if e["company"] == "NVIDIA"] == []
    detail_urls = [u for u in fake.urls if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert len(detail_urls) == 3                                # the budget, in board order
    stored = w.get_jd_texts([j["source_id"] for j in w.list_jobs()])
    assert s["new"] == 2 and len(stored) == 2                   # #0 and #2: #1 excluded, #3-5 unread
    assert all("Build GPUs" in jd for jd in stored.values())    # every stored row carries its JD
    assert not any("JR1001" in sid for sid in stored)           # the citizenship-barred posting
    assert s["workday_unchecked"] == 3
    assert all(j["has_jd"] for j in w.list_jobs())
    # The next refresh skips what is stored (no detail request for those) and reads the next rows.
    before = len(fake.urls)
    s2 = refresh_watchlist(w, fetch=fake, workday_max_detail=3)
    new_details = [u for u in fake.urls[before:] if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert len(new_details) == 3 and not any("JR1000" in u or "JR1002" in u for u in new_details)
    assert s2["new"] == 2 and s2["workday_unchecked"] == 1      # #3, #4 kept; #1 dropped; #5 left
    # A detail endpoint that cannot be read leaves the row unchecked -> not stored.
    broken = FakeWorkday(_nvidia(2), detail_fail=True)
    _distinct(broken, 2)
    w2 = _board_only_watchlist()
    s3 = refresh_watchlist(w2, fetch=broken, sleep=lambda s: None)    # no real 5xx back-off waits
    assert s3["new"] == 0 and s3["workday_unchecked"] == 2 and w2.list_jobs() == []


def test_select_workday_rows_stops_at_the_company_cap_and_defaults_its_budget_from_the_env(monkeypatch):
    from sourcing.service import feed_workday_max_detail, select_workday_rows
    fake = FakeWorkday(_nvidia(8))
    _distinct(fake, 8)
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)
    crit = {"titles": ["engineer"], "locations": [], "remote": "any"}
    kept, fetched, left = select_workday_rows(rows, crit, fetch=fake, max_detail=50, cap=3)
    assert [len(kept), fetched, left] == [3, 3, 0]              # 3 confirmed -> stop, no waste
    monkeypatch.setenv("WORKDAY_MAX_DETAIL", "2")
    rows = workday.workday_jobs("nvidia", SITE, HOST, fetch=fake, sleep=lambda s: None)   # fresh, list-only
    kept, fetched, left = select_workday_rows(rows, crit, fetch=fake)
    assert [len(kept), fetched, left] == [2, 2, 6]
    monkeypatch.setenv("WORKDAY_MAX_DETAIL_FEED", "77")
    assert feed_workday_max_detail() == 77
    monkeypatch.delenv("WORKDAY_MAX_DETAIL_FEED")
    assert feed_workday_max_detail() == 2000


def test_feed_build_reads_every_kept_workday_jd_with_the_feed_budget(tmp_path, monkeypatch):
    import backend.feed as feed
    import sourcing.service as service
    from scripts import build_feed
    from sourcing.feedfile import loads_maybe_gz, shard_for
    from sourcing.sponsors import SponsorDB
    seed = tmp_path / "seed.csv"
    seed.write_text(f"company,ats,board_id\nNVIDIA,workday,{BOARD}\n")
    monkeypatch.setattr(service, "SEED_WATCHLIST", seed)
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    monkeypatch.setenv("WORKDAY_MAX_DETAIL", "1")             # the desktop cap would read one
    monkeypatch.setenv("WORKDAY_MAX_DETAIL_FEED", "50")       # the build reads them all
    fake = FakeWorkday(_nvidia(4))
    posts = _distinct(fake, 4)
    for p in posts:
        p["location"] = p["locationsText"] = "Santa Clara, CA"
    posts[3]["jobDescription"] = "<p>We are unable to sponsor visas for this role.</p>"
    m = build_feed.build(tmp_path / "out", tmp_path / "data", fetch=fake,
                         jobs_url="https://f.example.com/jobs.json.gz", log=lambda *a: None)
    head = loads_maybe_gz((tmp_path / "out" / "jobs.json.gz").read_bytes())
    rows = [j for j in head["jobs"] if j["source"] == "workday"]
    assert len(rows) == 3 and m["count"] == 3                  # #0-2 published, #3 (no sponsorship) not
    for r in rows:
        sh = loads_maybe_gz((tmp_path / "out" / "jd" / f"{shard_for(r['source_id'])}.json.gz").read_bytes())
        assert "Build GPUs" in sh[r["source_id"]]              # every published row has its JD


def test_the_board_and_the_feed_hide_a_legacy_unchecked_workday_row_until_it_is_opened_and_checked(
        tmp_path, monkeypatch):
    from sourcing.quality import is_jd_checked
    import backend.feed as feed
    import ui.app as app
    from sourcing.sponsors import SponsorDB
    db = str(tmp_path / "t.db")
    row = {"source": "workday", "source_id": f"workday:{BOARD}:JR1", "company": "NVIDIA",
           "title": "Software Engineer", "location": "Austin, TX", "remote": "",
           "url": f"https://{HOST}/{SITE}/job/Austin/Software-Engineer_JR1", "jd_text": "",
           "posted_at": "2026-09-29", "salary": ""}
    checked = {**row, "source_id": f"workday:{BOARD}:JR2", "title": "ML Engineer",
               "url": f"https://{HOST}/{SITE}/job/Austin/ML-Engineer_JR2", "jd_text": "Build GPUs"}
    w = Watchlist(db)
    w.upsert_jobs([row, checked])                               # a row stored list-only, pre-fix
    slim = {j["source_id"]: j for j in w.list_jobs()}
    assert slim[row["source_id"]]["has_jd"] is False and slim[checked["source_id"]]["has_jd"] is True
    assert not is_jd_checked(slim[row["source_id"]]) and is_jd_checked(slim[checked["source_id"]])
    assert is_jd_checked({"source": "greenhouse", "jd_text": ""})   # inline-JD feeds are always checked
    assert not is_jd_checked({"source": "workday", "jd_text": " "}) and is_jd_checked({**row, "jd_text": "x"})
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
    # Opening it fetches the JD and runs the check THEN: a barred role is dismissed, not shown.
    monkeypatch.setattr(workday, "jd_for_job", lambda job, fetch=None: "US citizenship required.")
    r = client.get(f"/api/jobs/detail?source_id={row['source_id']}")
    assert r.status_code == 404 and "not open to international" in r.get_json()["error"]
    w = Watchlist(db)
    assert w.get_job(row["source_id"])["dismissed"] == 1
    assert feed.feed_detail(row["source_id"]) is None           # the hosted detail route agrees
    # An accessible JD is shown and persisted, so the row is checked from now on.
    w.upsert_jobs([{**row, "source_id": f"workday:{BOARD}:JR3",
                    "url": f"https://{HOST}/{SITE}/job/Austin/Software-Engineer_JR3"}])
    w.close()
    monkeypatch.setattr(workday, "jd_for_job", lambda job, fetch=None: "Build GPUs with CUDA.")
    d = client.get(f"/api/jobs/detail?source_id=workday:{BOARD}:JR3").get_json()
    assert "CUDA" in d["jd"]
    w = Watchlist(db)
    assert {j["source_id"]: j["has_jd"] for j in w.list_jobs()}[f"workday:{BOARD}:JR3"] is True
    w.close()


def test_upsert_keeps_a_stored_jd_when_the_incoming_row_is_blank():
    w = Watchlist(":memory:")
    row = {"source": "workday", "source_id": "workday:a.wd5/S:1", "company": "A", "title": "T",
           "location": "", "remote": "", "url": "https://a.wd5.myworkdayjobs.com/S/job/x/T_1",
           "jd_text": "Full description", "posted_at": "2026-09-01", "salary": ""}
    w.upsert_jobs([row])
    w.upsert_jobs([{**row, "jd_text": "", "title": "T2"}])
    got = w.get_job(row["source_id"])
    assert got["jd_text"] == "Full description" and got["title"] == "T2"
    w.upsert_jobs([{**row, "jd_text": "Newer text"}])
    assert w.get_job(row["source_id"])["jd_text"] == "Newer text"


def test_list_only_reingest_never_reverts_the_detail_enriched_url_location_and_date():
    """The list row of a SmartRecruiters / Workday posting carries a synthetic url, a
    "3 Locations" placeholder and the "30+ Days Ago" posted_at floor. Once the detail fetch has
    stored the real values, the hourly list re-upsert (jd_text blank) must not revert them."""
    w = Watchlist(":memory:")
    listed = {"source": "smartrecruiters", "source_id": "smartrecruiters:Acme:p1", "company": "Acme",
              "title": "Software Engineer", "location": "3 Locations", "remote": "",
              "url": "https://jobs.smartrecruiters.com/Acme/p1", "jd_text": "",
              "posted_at": "2026-08-01", "salary": ""}
    enriched = {**listed, "location": "Austin, TX", "remote": "hybrid", "posted_at": "2026-09-20",
                "url": "https://jobs.smartrecruiters.com/Acme/p1-Software-Engineer",
                "jd_text": "Build the platform."}
    w.upsert_jobs([listed])
    w.upsert_jobs([enriched])                     # the detail view persists its enrichment
    for _ in range(2):
        w.upsert_jobs([{**listed, "title": "Software Engineer II", "salary": "$150k"}])  # list refresh
    got = w.get_job(listed["source_id"])
    assert got["url"] == enriched["url"] and got["location"] == "Austin, TX"
    assert got["posted_at"] == "2026-09-20" and got["remote"] == "hybrid"
    assert got["jd_text"] == "Build the platform."
    assert got["title"] == "Software Engineer II" and got["salary"] == "$150k"   # volatile fields do refresh
    # A later detail pass (a row WITH a body) still updates them.
    w.upsert_jobs([{**enriched, "posted_at": "2026-09-25", "location": "Austin, TX (Remote)"}])
    got = w.get_job(listed["source_id"])
    assert got["posted_at"] == "2026-09-25" and got["location"] == "Austin, TX (Remote)"
    # The list row still FILLS blanks on a row that never had the value.
    bare = {**listed, "source_id": "smartrecruiters:Acme:p2", "location": "", "posted_at": ""}
    w.upsert_jobs([bare])
    w.upsert_jobs([{**bare, "location": "Remote", "posted_at": "2026-09-01"}])
    got = w.get_job(bare["source_id"])
    assert got["location"] == "Remote" and got["posted_at"] == "2026-09-01"
    # Workday rows get the same protection.
    wd = {**listed, "source": "workday", "source_id": "workday:a.wd5/S:1",
          "url": "https://a.wd5.myworkdayjobs.com/S/job/x/T_1"}
    w.upsert_jobs([{**wd, "location": "Santa Clara, CA", "posted_at": "2026-09-22", "jd_text": "GPUs"}])
    w.upsert_jobs([wd])
    got = w.get_job(wd["source_id"])
    assert got["location"] == "Santa Clara, CA" and got["posted_at"] == "2026-09-22"


def test_a_feed_that_ships_its_body_inline_still_refreshes_every_field_on_reingest():
    """Greenhouse (and every non-list-only source) keeps the old behaviour: the feed row IS the
    truth, so a changed location / url / date overwrites what is stored."""
    w = Watchlist(":memory:")
    row = {"source": "greenhouse", "source_id": "greenhouse:acme:1", "company": "Acme", "title": "SE",
           "location": "New York, NY", "remote": "hybrid", "url": "https://boards.greenhouse.io/acme/jobs/1",
           "jd_text": "Build.", "posted_at": "2026-09-01", "salary": ""}
    w.upsert_jobs([row])
    w.upsert_jobs([{**row, "location": "Remote - US", "remote": "remote", "posted_at": "2026-09-10",
                    "url": "https://acme.example/careers/1", "jd_text": ""}])
    got = w.get_job(row["source_id"])
    assert got["location"] == "Remote - US" and got["remote"] == "remote"
    assert got["posted_at"] == "2026-09-10" and got["url"] == "https://acme.example/careers/1"
    assert got["jd_text"] == "Build."                           # the blank-body guard still holds


# -- dedup and the allowlist ---------------------------------------------------- #

def test_dedup_keys_a_workday_row_by_its_job_page_url_and_role():
    fake = FakeWorkday(_nvidia(1))
    row = workday.workday_jobs("nvidia", SITE, HOST, "NVIDIA", fetch=fake, sleep=lambda s: None)[0]
    keys = DedupIndex.keys_for(row)
    assert keys[0] == f"https://{HOST}/{SITE}/job/US-CA-Santa-Clara/Software-Engineer_JR1000"
    assert keys[1].startswith("nvidia::software engineer@@")
    index = DedupIndex([row])
    via_aggregator = {**row, "source": "freehire", "source_id": "freehire::abc",
                      "url": row["url"] + "?utm_source=feed"}
    assert index.is_duplicate(via_aggregator)
    assert not index.is_duplicate({**row, "source_id": row["source_id"]})      # a refresh of itself


def test_workday_is_assisted_the_allowlist_is_unchanged():
    from submit.allowlist import REGISTRY, auto_hosts
    from submit.policy import submission_policy
    assert submission_policy(f"https://{HOST}/{SITE}/job/US-CA-Santa-Clara/Software-Engineer_JR1000") == "assisted"
    assert not any("workday" in h for h in auto_hosts())
    assert auto_hosts() == frozenset({"recruitee.com"})         # the one verified AUTO entry, untouched
    assert "workday" not in {e.ats.lower() for e in REGISTRY}
    assert not re.search(r"workday", (ROOT / "submit" / "allowlist.py").read_text(), re.I)


# -- discovery ----------------------------------------------------------------- #

def test_robots_sites_reads_sitemap_and_allow_lines_and_honours_disallow():
    txt = ("Sitemap: https://visa.wd5.myworkdayjobs.com/Visa/siteMap.xml\n"
           "Sitemap: https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/siteMap.xml\n\n"
           "User-agent: *\nAllow: /Visa/\nAllow: /Visa_Early_Careers/\nAllow: /Internal_Only/\n"
           "Disallow: /Internal_Only/\nDisallow: /talentcommunity/\nDisallow: /refreshFacet/\n")
    assert workday.robots_sites(txt) == (["Visa", "Visa_Early_Careers"], ["Internal_Only"])
    # Lowe's shape: only a Disallow of its career site -> nothing crawlable, and we say why.
    assert workday.robots_sites("User-agent: *\nDisallow: /LWS_External_CS/\nDisallow: /refreshFacet/") == (
        [], ["LWS_External_CS"])
    assert workday.robots_sites("") == ([], [])
    assert workday.robots_sites('{"errorCode":"HTTP_422"}') == ([], [])


def test_site_from_redirect_parses_the_landing_location():
    assert workday.site_from_redirect(f"https://{HOST}/en-US/{SITE}") == SITE
    assert workday.site_from_redirect(f"https://{HOST}/{SITE}/") == SITE
    assert workday.site_from_redirect("https://intel.wd1.myworkdayjobs.com/fr/External/details") == "External"
    assert workday.site_from_redirect(f"https://{HOST}/") == ""
    assert workday.site_from_redirect("") == ""


def test_prefer_sites_puts_external_and_careers_first_and_drops_agency_or_internal_sites():
    assert workday.prefer_sites(["Research", "Visa_Early_Careers", "Visa"]) == ["Visa_Early_Careers", "Research", "Visa"]
    assert workday.prefer_sites(["Research", "NVIDIAExternalCareerSite"]) == ["NVIDIAExternalCareerSite", "Research"]
    assert workday.prefer_sites(["agency", "Careers", "Internal_Careers", "AlumniSite",
                                 "Relief-Veterinarian-Contractors"]) == ["Careers"]
    assert len(workday.prefer_sites([f"Site{i}" for i in range(20)])) == workday.MAX_SITES


def test_discover_picks_the_largest_applicant_facing_site_of_a_multi_site_tenant():
    # Visa runs its early-careers site next to the main one (plus an agency site): the main one
    # (most jobs) is the board, not the first "career" name in robots order.
    big = [_posting(i) for i in range(3)]
    fake = FakeWorkday({("visa", 5): {"robots": _robots("Visa_Early_Careers", "Visa", "agency"),
                                      "sites": {"Visa_Early_Careers": {"org": "Visa U.S.A. Inc", "postings": big[:1]},
                                                "Visa": {"org": "493 Visa Worldwide", "postings": big, "total": 764},
                                                "agency": {"org": "Visa", "postings": big, "total": 9000}}}})
    hit = workday.discover_workday("Visa Inc", fetch=fake)
    assert hit and hit.site == "Visa" and hit.jobs == 764 and hit.board_id == "visa.wd5/Visa"
    assert not any("/agency/" in u for u in fake.urls)        # agency sites are never even sized up
    # Both eligible sites were sized up (one list request each); only the larger needed a detail.
    assert len(fake.posts) == 2
    detail_urls = [u for u in fake.urls if "/wday/cxs/" in u and not u.endswith("/jobs")]
    assert detail_urls == [f"https://visa.wd5.myworkdayjobs.com/wday/cxs/visa/Visa{big[0]['externalPath']}"]
    assert workday.site_words("NVIDIAExternalCareerSite") == "NVIDIA External Career Site"
    assert workday.site_words("Cisco_Careers") == "Cisco Careers"
    assert workday.site_words("US_Bank_Careers") == "US Bank Careers"


def test_discover_finds_the_tenant_confirms_identity_and_counts_jobs():
    fake = FakeWorkday({**_nvidia(3, total=2000),
                        ("cisco", 5): {"robots": _robots("Cisco_Careers", "Internal"),
                                       "sites": {"Cisco_Careers": {"org": "020 Cisco Systems, Inc.",
                                                                   "postings": [_posting(0)]},
                                                 "Internal": {"org": "x", "postings": []}}}})
    hit = workday.discover_workday("Nvidia Corporation", fetch=fake)
    assert hit == ("nvidia", HOST, SITE, 2000, "2100 NVIDIA USA") and hit.board_id == BOARD
    # Instances are probed in order; after the hit nothing else is asked.
    robots = [u for u in fake.urls if u.endswith("/robots.txt")]
    assert robots[-1] == f"https://{HOST}/robots.txt" and len(robots) == workday.WD_INSTANCES.index(5) + 1
    hit = workday.discover_workday("Cisco Systems Inc", fetch=fake)
    assert hit and hit.board_id == "cisco.wd5/Cisco_Careers" and hit.jobs == 1
    # The whole probe stays on *.myworkdayjobs.com and never guesses a site on the cxs path blind.
    assert all(urlsplit(u).hostname.endswith(".myworkdayjobs.com") for u in fake.urls)


def test_discover_rejects_a_tenant_whose_identity_does_not_match():
    # "charles" exists on Workday but is Charles River Labs, not Charles Schwab.
    fake = FakeWorkday({("charles", 1): {"robots": _robots("External"),
                                         "sites": {"External": {"org": "Charles River Laboratories",
                                                                "postings": [_posting(0)]}}}})
    rej: list[tuple] = []
    assert workday.discover_workday("Charles Schwab & Company Inc", fetch=fake,
                                    reject=lambda a, s, r: rej.append((a, s, r))) is None
    assert rej and rej[0][0] == "workday" and rej[0][1] == "charles"
    assert "Charles River Laboratories" in rej[0][2] and "schwab" in rej[0][2]
    # A multi-brand name's leading token needs TWO brand words confirmed ("archer" is not ADM).
    fake = FakeWorkday({("archer", 5): {"robots": _robots("External"),
                                        "sites": {"External": {"org": "Archer Aviation",
                                                               "postings": [_posting(0)]}}}})
    assert workday.discover_workday("Archer Daniels Midland Company", fetch=fake) is None
    fake = FakeWorkday({("archer", 5): {"robots": _robots("External"),
                                        "sites": {"External": {"org": "Archer Daniels Midland Co",
                                                               "postings": [_posting(0)]}}}})
    assert workday.discover_workday("Archer Daniels Midland Company", fetch=fake).tenant == "archer"


def test_discover_reads_several_postings_organizations_and_squashed_spellings():
    # Amgen's first posting belongs to a subsidiary; the second names Amgen. BlackRock's site
    # spells itself "Black Rock". PayPal's three postings are all Paidy's: still turned down.
    posts = [_posting(i) for i in range(4)]
    fake = FakeWorkday({("amgen", 1): {"robots": _robots("Careers"),
                                       "sites": {"Careers": {"org": "1005 Immunex Rhode Island Corporation",
                                                             "postings": posts, "total": 500}}},
                        ("blackrock", 1): {"robots": _robots("BlackRock_Professional"),
                                           "sites": {"BlackRock_Professional": {"org": "Black Rock Professional",
                                                                                "postings": posts[:1]}}},
                        ("paypal", 1): {"robots": _robots("jobs"),
                                        "sites": {"jobs": {"org": "0578 Paidy Inc.", "postings": posts}}}})
    orgs = iter(["1005 Immunex Rhode Island Corporation", "Amgen Inc", "Amgen Inc"])
    real_call = fake.__call__

    def call(url, timeout=20, data=None, raw=False):
        out = real_call(url, timeout=timeout, data=data, raw=raw)
        if isinstance(out, dict) and "hiringOrganization" in out and "amgen" in url:
            out["hiringOrganization"] = {"name": next(orgs)}
        return out
    fake.__call__ = call                                    # not used: FakeWorkday is called directly
    hit = workday.discover_workday("Amgen Inc", fetch=call)
    assert hit and hit.board_id == "amgen.wd1/Careers" and hit.org == "1005 Immunex Rhode Island Corporation"
    details = [u for u in fake.urls if "/wday/cxs/amgen/" in u and not u.endswith("/jobs")]
    assert len(details) == 2                                # stopped as soon as Amgen was confirmed
    assert fake.posts[-1]["limit"] == workday.IDENTITY_POSTINGS
    assert workday.discover_workday("Blackrock Financial Management Inc", fetch=fake).tenant == "blackrock"
    rej: list[str] = []
    assert workday.discover_workday("Paypal Inc", fetch=fake, reject=lambda a, s, r: rej.append(r)) is None
    assert rej and "Paidy" in rej[0]
    assert len([u for u in fake.urls if "/wday/cxs/paypal/" in u and not u.endswith("/jobs")]) == 3
    assert workday.identity_match_count("Visa Early Careers advisable", ["visa"]) == 1   # token, not substring
    assert workday.identity_match_count("Careers advisable", ["visa"]) == 0


def test_discover_respects_a_robots_disallow_and_an_empty_site_and_a_missing_tenant():
    fake = FakeWorkday({("lowes", 5): {"robots": "User-agent: *\nDisallow: /LWS_External_CS/\n",
                                       "sites": {"LWS_External_CS": {"org": "Lowe's", "postings": [_posting(0)]}}},
                        ("qualcomm", 12): {"robots": _robots("External"),
                                           "sites": {"External": {"org": "Qualcomm", "postings": []}}}})
    rej: list[tuple] = []
    assert workday.discover_workday("Lowes Companies Inc", fetch=fake,
                                    reject=lambda a, s, r: rej.append((a, s, r))) is None
    assert rej and "robots.txt disallows" in rej[0][2]
    assert not any("/wday/cxs/" in u for u in fake.urls)       # the disallowed site was never crawled
    assert workday.discover_workday("Qualcomm Technologies Inc", fetch=fake) is None   # live but empty
    assert workday.discover_workday("Datadog Inc", fetch=fake) is None               # 422 everywhere
    assert workday.discover_workday("First National Group", fetch=fake) is None      # nothing to verify
    n = len(fake.urls)
    assert workday.discover_workday("First National Group", fetch=fake) is None and len(fake.urls) == n


def test_discover_falls_back_to_the_landing_redirect_when_robots_names_no_site():
    fake = FakeWorkday({("intel", 1): {"robots": "User-agent: *\nDisallow: /refreshFacet/\n",
                                       "sites": {"External": {"org": "500 Intel Corp",
                                                              "postings": [_posting(0)]}}}})
    redirects: list[str] = []

    def redirect(url):
        redirects.append(url)
        return "https://intel.wd1.myworkdayjobs.com/en-US/External"
    hit = workday.discover_workday("Intel Corporation", fetch=fake, redirect=redirect)
    assert hit and hit.board_id == "intel.wd1/External" and redirects == ["https://intel.wd1.myworkdayjobs.com/"]


def test_discovery_backs_off_for_real_honouring_retry_after_and_capped(monkeypatch):
    # discover_workday used to hand workday_detail a no-op sleep, so 429 / 5xx retries fired
    # instantly (MAX_RETRIES x IDENTITY_POSTINGS x sites). The pause is real now: Retry-After when
    # given, capped at DISCOVERY_MAX_BACKOFF; tests inject a recorder instead.
    fake = FakeWorkday(_nvidia(2))
    state = {"detail_429s": 0}

    def flaky(url, timeout=20, **kw):
        if "/wday/cxs/" in url and not url.endswith("/jobs") and state["detail_429s"] < 2:
            state["detail_429s"] += 1
            retry = "600" if state["detail_429s"] == 1 else "3"
            raise urllib.error.HTTPError(url, 429, "slow down", {"Retry-After": retry}, None)
        return fake(url, timeout, **kw)
    slept: list[float] = []
    monkeypatch.setattr(workday.time, "sleep", slept.append)
    hit = workday.discover_workday("Nvidia Corporation", fetch=flaky)
    assert hit and hit.tenant == "nvidia" and hit.jobs == 2
    assert slept == [pytest.approx(workday.DISCOVERY_MAX_BACKOFF), pytest.approx(3.0)]
    state["detail_429s"] = 0
    recorded: list[float] = []
    assert workday.discover_workday("Nvidia Corporation", fetch=flaky, backoff=recorded.append)
    assert recorded == [pytest.approx(workday.MAX_BACKOFF), pytest.approx(3.0)]   # uncapped by discovery


def test_discover_for_company_reaches_workday_after_the_other_ats_miss():
    fake = FakeWorkday(_nvidia(2))
    assert "workday" in DISCOVER_ATS and DISCOVER_ATS[-1] == "workday"
    hit = discover_for_company("Nvidia Corporation", fetch=fake)
    assert hit == {"company": "Nvidia Corporation", "ats": "workday", "board_id": BOARD, "jobs": 2}
    assert discover_for_company("Nvidia Corporation", atss=("greenhouse",), fetch=fake) is None


def test_a_hand_given_slug_is_still_identity_checked():
    fake = FakeWorkday({("usbank", 1): {"robots": _robots("US_Bank_Careers"),
                                        "sites": {"US_Bank_Careers": {"org": "U.S. Bank National Association",
                                                                      "postings": [_posting(0)]}}}})
    hit = workday.discover_workday("U S Bank National Association", fetch=fake, slugs=["usbank"])
    assert hit and hit.board_id == "usbank.wd1/US_Bank_Careers"
    assert workday.discover_workday("Datadog Inc", fetch=fake, slugs=["usbank"]) is None   # not them
    # A hand-given slug gets the full rule: a shared brand word is not enough for a two-word name.
    # (Live 2026-09-30: emerson.wd5 is Emerson College, not Emerson Electric.)
    fake = FakeWorkday({("emerson", 5): {"robots": _robots("Emerson_College_Staff"),
                                         "sites": {"Emerson_College_Staff": {"org": "Emerson College",
                                                                             "postings": [_posting(0)]}}}})
    assert workday.discover_workday("Emerson Electric Co", fetch=fake, slugs=["emerson"]) is None


def test_workday_slugs_are_hostname_labels_only():
    assert workday.workday_slugs("Nvidia Corporation") == [("nvidia", "brand")]
    assert workday.workday_slugs("Cisco Systems Inc") == [("cisco", "brand"), ("ciscosystems", "joined"),
                                                          ("cisco-systems", "joined")]
    assert workday.workday_slugs("First National Group") == []


# -- growth: the shared lane, and a new ATS is owed by old misses -------------- #

def test_growth_paces_every_workday_tenant_through_one_lane():
    assert growth.host_lane("nvidia.wd5.myworkdayjobs.com") == "myworkdayjobs.com"
    assert growth.host_lane("boards-api.greenhouse.io") == "boards-api.greenhouse.io"
    assert growth.ATS_HOSTS["myworkdayjobs.com"] == "workday"
    sleeps: list[float] = []
    pf = growth.PoliteFetch(fetch=lambda url, timeout, **kw: kw, sleep=sleeps.append, clock=lambda: 100.0)
    pf("https://a.wd1.myworkdayjobs.com/robots.txt", raw=True)
    assert pf("https://b.wd5.myworkdayjobs.com/robots.txt", raw=True) == {"raw": True}   # kwargs pass through
    assert sleeps == [pytest.approx(growth.HOST_DELAYS["myworkdayjobs.com"])]          # same lane: spaced


def test_a_legacy_miss_owes_workday_and_a_workday_only_run_completes_it(tmp_path):
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    cp.record("nvidia", name="Nvidia Corporation", status="miss", reason="no live board at any slug")
    cp.record("datadog", name="Datadog Inc", status="hit", ats="greenhouse", board_id="datadog", jobs=3)
    assert cp.pending() == {"nvidia": ["workday"]}
    assert cp.pending(growth.LEGACY_ATS) == {}
    rows = [{"norm_name": "nvidia", "display_name": "Nvidia Corporation", "h1b_approvals": 900, "h1b_last_fy": 2023},
            {"norm_name": "datadog", "display_name": "Datadog Inc", "h1b_approvals": 100, "h1b_last_fy": 2023},
            {"norm_name": "acme", "display_name": "Acme Widgets Inc", "h1b_approvals": 50, "h1b_last_fy": 2023}]
    ranked = growth.candidates(rows, cp, watched=set(), limit=None)
    assert [(r["display_name"], r.get("atss")) for r in ranked] == [("Nvidia Corporation", ["workday"]),
                                                                    ("Acme Widgets Inc", None)]
    fake = FakeWorkday(_nvidia(2))
    pf = growth.PoliteFetch(fetch=fake, sleep=lambda s: None, clock=lambda: 0.0)
    s = growth.grow(ranked, cp, pf, atss=("workday",), workers=1)
    assert s["hits"] == 1 and s["per_ats"] == {"workday": 1}
    assert s["new_boards"][0]["board_id"] == BOARD and s["new_boards"][0]["jobs"] == 2
    acme = cp.records["acme"]
    assert acme["status"] == "miss" and acme["probed_ats"] == ["workday"]
    assert cp.pending(("workday",)) == {} and cp.pending()["acme"] == list(growth.LEGACY_ATS)
    assert all(urlsplit(u).hostname.endswith(".myworkdayjobs.com") for u in fake.urls)


def test_grow_script_ats_filter_probes_only_workday(tmp_path, monkeypatch, capsys):
    sponsors = tmp_path / "sponsors.csv.gz"
    with gzip.open(sponsors, "wt", newline="") as f:
        f.write("norm_name,display_name,h1b_approvals,h1b_last_fy,h1b_first_fy,naics,state,"
                "cap_exempt,e_verify,perm_certs\n"
                "nvidia,Nvidia Corporation,1200,2023,2020,33,CA,0,0,0\n"
                "datadog,Datadog Inc,120,2023,2020,51,NY,0,0,0\n")
    seed = tmp_path / "watchlist.csv"
    seed.write_text("company,ats,board_id\nStripe Inc,greenhouse,stripe\n")
    fake = FakeWorkday(_nvidia(5))
    pf = growth.PoliteFetch(fetch=fake, sleep=lambda s: None, clock=lambda: 0.0)
    monkeypatch.setattr(growth, "PoliteFetch", lambda **kw: pf)
    spec = importlib.util.spec_from_file_location("grow_watchlist", ROOT / "scripts" / "grow_watchlist.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    cp = tmp_path / "cp.jsonl"
    rc = script.main(["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
                      "--limit", "10", "--workers", "1", "--ats", "workday"])
    assert rc == 0
    rows = growth.read_watchlist_seed(seed)
    assert ("Nvidia Corporation", "workday", BOARD) in [(r["company"], r["ats"], r["board_id"]) for r in rows]
    assert all(urlsplit(u).hostname.endswith(".myworkdayjobs.com") for u in fake.urls)
    out = capsys.readouterr().out
    assert "across workday" in out and "watchlist: 1 -> 2 boards" in out
    summary = json.loads(cp.with_suffix(".summary.json").read_text())
    assert summary["per_ats"] == {"workday": 1} and summary["after_ats"]["workday"] == 1
    with pytest.raises(SystemExit):
        script.main(["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
                     "--ats", "linkedin"])
