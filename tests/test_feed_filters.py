"""Server-side facet filtering + pagination (2026-08-09).

The board browses tens of thousands of roles, so filtering + paging moved to the server. These
pin the shared filter helper and the paginated /feed contract (page shape, filtered count vs
unfiltered total, and that facets actually narrow).
"""

from __future__ import annotations

import datetime

from flask import Flask

import backend.feed as feed
from sourcing.filters import apply_facets, paginate

_TODAY = datetime.date.today().isoformat()
_OLD = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()

_JOBS = [
    {"source_id": "a", "title": "Data Engineer", "company": "Acme", "location": "New York, NY",
     "remote": "remote", "posted_at": _TODAY, "entry_level": False,
     "visa": [{"code": "H-1B"}], "nationality_visas": []},
    {"source_id": "b", "title": "New Grad Nurse", "company": "Beta Health", "location": "Austin, TX",
     "remote": "onsite", "posted_at": _TODAY, "entry_level": True,
     "visa": [], "nationality_visas": [{"code": "TN"}]},
    {"source_id": "c", "title": "Senior Data Scientist", "company": "Gamma", "location": "Remote, US",
     "remote": "hybrid", "posted_at": _OLD, "entry_level": False,
     "visa": [{"code": "GREEN-CARD"}], "nationality_visas": []},
]


def test_apply_facets_each_axis():
    f = lambda **k: sorted(j["source_id"] for j in apply_facets(_JOBS, **k))
    assert f(q="data") == ["a", "c"]                     # title keyword
    assert f(loc="austin") == ["b"]                      # location substring
    assert f(remote="remote") == ["a"]                   # work type
    assert f(level="entry") == ["b"]                     # experience
    assert f(visa=["H-1B"]) == ["a"]                     # approval-backed badge
    assert f(visa=["TN"]) == ["b"]                       # by-nationality option counts too
    assert f(visa=["SPONSORED"]) == ["a", "c"]           # any visa at all
    assert f(days=7) == ["a", "b"]                       # drops the 40-day-old role
    assert f(days=7, visa=["H-1B"]) == ["a"]             # facets AND together


def test_sponsored_facet_keeps_only_postings_that_state_it():
    jobs = [
        {"source_id": "yes", "title": "Engineer", "sponsorship_stated": True},
        {"source_id": "no", "title": "Engineer", "sponsorship_stated": False},
        {"source_id": "unknown", "title": "Engineer"},          # silent posting -> excluded
    ]
    ids = lambda **k: sorted(j["source_id"] for j in apply_facets(jobs, **k))
    assert ids() == ["no", "unknown", "yes"]                    # off by default: everything stays
    assert ids(sponsored=True) == ["yes"]                       # only the stated-sponsor posting
    assert ids(sponsored="1") == ["yes"]                        # string form (from a query param)
    assert ids(sponsored="") == ["no", "unknown", "yes"]        # empty/false -> no filter


_PAID = [
    {"source_id": "hi", "title": "Staff Engineer", "salary": "$180k-$220k/yr"},
    {"source_id": "mid", "title": "Analyst", "salary": "$95k/yr"},
    {"source_id": "hr", "title": "Support", "salary": "$45-$60/hr"},        # ~$124.8k annualized
    {"source_id": "none", "title": "Intern", "salary": ""},                 # unknown pay
]


def test_pay_floor_hides_unknown_and_below():
    ids = lambda **k: sorted(j["source_id"] for j in apply_facets(_PAID, **k))
    assert ids(pay=100000) == ["hi", "hr"]               # mid ($95k) and unknown drop out
    assert ids(pay=200000) == ["hi"]                     # only the $180-220k role clears $200k
    assert ids(pay=0) == ["hi", "hr", "mid", "none"]     # no floor -> everyone stays


def test_sort_by_pay_orders_high_to_low_unknown_last():
    order = [j["source_id"] for j in apply_facets(_PAID, sort="pay")]
    assert order == ["hi", "hr", "mid", "none"]          # 220k, 124.8k, 95k, unknown(0) last


def test_paginate_slices_and_reports_total():
    page1, total, page, per = paginate(_JOBS, page=1, per_page=2)
    assert total == 3 and page == 1 and per == 2 and [j["source_id"] for j in page1] == ["a", "b"]
    page2, total2, *_ = paginate(_JOBS, page=2, per_page=2)
    assert total2 == 3 and [j["source_id"] for j in page2] == ["c"]
    assert paginate(_JOBS, per_page=999)[3] == 100       # per_page clamped


def _client(monkeypatch):
    monkeypatch.delenv("JOBS_AUTOUPDATE", raising=False)
    feed._FEED_CACHE.clear()
    monkeypatch.setattr(feed, "feed_jobs", lambda level=None: list(_JOBS))
    app = Flask(__name__)
    feed.register_feed(app)
    return app.test_client()


def test_feed_route_paginates_and_filters(monkeypatch):
    c = _client(monkeypatch)
    d = c.get("/feed?per_page=2&page=1").get_json()
    assert d["total"] == 3 and d["count"] == 3 and d["page"] == 1 and len(d["jobs"]) == 2
    d2 = c.get("/feed?per_page=2&page=2").get_json()
    assert len(d2["jobs"]) == 1 and d2["jobs"][0]["source_id"] == "c"
    # a facet narrows count but total stays the whole board
    dv = c.get("/feed?visa=H-1B").get_json()
    assert dv["count"] == 1 and dv["total"] == 3 and dv["jobs"][0]["source_id"] == "a"


def test_on_demand_search_augments_thin_results(monkeypatch):
    # A keyword the pre-built board can't satisfy triggers a live freehire query, folded in.
    feed._FEED_CACHE.clear()
    monkeypatch.delenv("JOBS_AUTOUPDATE", raising=False)
    monkeypatch.setattr(feed, "feed_jobs", lambda level=None: [])          # board has nothing
    monkeypatch.setattr(feed, "_freehire_live", lambda q: [
        {"source_id": "freehire::n1", "title": "Nurse Practitioner", "company": "Cigna",
         "location": "New York, NY", "remote": "", "posted_at": _TODAY, "entry_level": False,
         "visa": [], "nationality_visas": []}])
    app = Flask(__name__)
    feed.register_feed(app)
    c = app.test_client()
    d = c.get("/feed?q=nurse").get_json()
    assert d["count"] == 1 and d["jobs"][0]["company"] == "Cigna"      # live result folded in
    feed._FEED_CACHE.clear()
    assert c.get("/feed").get_json()["count"] == 0                     # no keyword -> no live query
