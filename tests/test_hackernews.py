"""Hacker News "Ask HN: Who is hiring?" source (sourcing/hackernews.py). No network: the two API
answers are recorded fixtures in tests/fixtures/hackernews (the real Algolia response shape; the
employers are invented, so no poster's text is committed)."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from sourcing import hackernews as hn
from sourcing.quality import is_sponsor_relevant

FIX = Path(__file__).parent / "fixtures" / "hackernews"
SEARCH = json.loads((FIX / "search_by_date.json").read_text(encoding="utf-8"))
ITEMS = json.loads((FIX / "items_49922569.json").read_text(encoding="utf-8"))
NOW = 1790866927 + 10 * 86400          # ten days after the October thread opened


class Api:
    """Serves the recorded answers and counts requests."""

    def __init__(self, fail: bool = False):
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url, timeout=20, **_):
        self.urls.append(url)
        if self.fail:
            raise urllib.error.URLError("offline")
        if "/search_by_date" in url:
            return SEARCH
        if url.endswith("/items/49922569"):
            return ITEMS
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)


def rows(**kw):
    return {r["company"]: r for r in hn.hn_jobs(fetch=Api(), now=NOW, sleep=lambda s: None, **kw)}


# -- the thread --------------------------------------------------------------------- #

def test_find_thread_picks_the_newest_who_is_hiring_story():
    t = hn.find_thread(SEARCH, now=NOW)
    assert t["id"] == 49922569 and t["title"] == "Ask HN: Who is hiring? (October 2026)"
    # not "Who wants to be hired?" / "Freelancer?", and nothing once the thread is stale
    assert hn.find_thread(SEARCH, now=NOW + 60 * 86400) is None
    assert hn.find_thread({"hits": []}, now=NOW) is None


def test_only_top_level_comments_with_text_are_read():
    ids = {c["id"] for c in hn.top_level(ITEMS)}
    assert 49930008 not in ids                         # deleted: no text
    assert 49930090 not in ids                         # a reply under a post, not a post
    assert len(ids) == 8


def test_two_requests_both_to_the_official_search_api():
    api = Api()
    hn.hn_jobs(fetch=api, now=NOW, sleep=lambda s: None)
    assert len(api.urls) == 2
    assert all(u.startswith("https://hn.algolia.com/api/v1/") for u in api.urls)


# -- parsing ------------------------------------------------------------------------ #

def test_a_conventional_header_becomes_a_facts_only_row():
    r = rows()["Acme Robotics"]
    assert r["source"] == "hackernews" and r["source_id"] == "hackernews::49930001"
    assert r["title"] == "Senior Backend Engineer"
    assert r["location"] == "San Francisco, CA" and r["remote"] == "onsite"
    assert r["sponsorship_stated"] is True                       # the "VISA" header tag
    assert r["url"] == "https://news.ycombinator.com/item?id=49930001"
    assert r["posted_at"] == "2026-10-02"
    # the description is our own sentences, never the poster's text
    assert "warehouse robots" not in r["jd_text"]
    assert "https://news.ycombinator.com/item?id=49930001" in r["jd_text"]
    assert "https://acmerobotics.example/jobs" in r["jd_text"]
    assert "visa sponsorship is available" in r["jd_text"]


def test_a_no_is_read_from_the_body_and_pay_from_the_header():
    r = rows()["Birchwood Analytics"]
    assert r["sponsorship_stated"] is False
    assert r["title"] == "Data Engineer, Analytics Engineer"
    assert r["location"] == "Remote (US)" and r["remote"] == "remote"
    assert r["salary"]
    assert "will not sponsor" in r["jd_text"]


def test_a_no_beats_a_yes():
    assert rows()["Fjord Payments"]["sponsorship_stated"] is False


def test_role_from_the_body_when_the_header_names_none():
    r = rows()["Cobalt Health"]
    assert r["title"] == "Senior Platform Engineers"
    assert r["location"] == "New York, NY" and r["remote"] == "hybrid"
    assert r["sponsorship_stated"] is True


def test_dash_separated_header_and_silence_is_none():
    r = rows()["Ember Labs"]
    assert r["title"] == "Founding Product Engineer" and r["location"] == "London, UK"
    assert r["sponsorship_stated"] is None


def test_h1b_sponsorship_in_the_body_is_a_yes():
    assert rows()["Grove AI"]["sponsorship_stated"] is True


def test_citizenship_or_clearance_posts_and_chatter_are_dropped():
    got = rows()
    assert "Dunmore Defense" not in got                  # U.S. citizenship / clearance required
    assert not any("800" in c for c in got)              # a complaint, not a job post
    assert len(got) == 6


@pytest.mark.parametrize("header, text, want", [
    (["VISA"], "", True),
    (["Visa sponsorship"], "", True),
    ([], "We are happy to sponsor visas.", True),
    ([], "No visa sponsorship offered.", False),
    ([], "We cannot sponsor at this time.", False),
    (["VISA"], "Visa sponsorship not available for this role.", False),
    ([], "Join us in Berlin.", None),
])
def test_sponsorship_signal(header, text, want):
    assert hn.sponsorship_signal(header, text) is want


def test_search_filters_titles_client_side():
    got = hn.hn_jobs("data engineer", fetch=Api(), now=NOW, sleep=lambda s: None)
    assert [r["company"] for r in got] == ["Birchwood Analytics"]


# -- the feed's sponsor gating ------------------------------------------------------- #

def test_rows_go_through_the_aggregator_sponsor_gate():
    got = rows()
    assert is_sponsor_relevant(got["Acme Robotics"])                 # stated yes
    assert not is_sponsor_relevant(got["Ember Labs"])                # silent, no badge
    assert is_sponsor_relevant({**got["Ember Labs"], "visa": ["H-1B"]})   # a sponsor badge


def test_ad_stance_reads_our_sentences():
    from sourcing.adstance import ad_stance
    got = rows()
    assert ad_stance(got["Acme Robotics"]["jd_text"])["stance"] == "offered"
    assert ad_stance(got["Birchwood Analytics"]["jd_text"])["stance"] == "not_offered"


# -- caching (HN API etiquette) ------------------------------------------------------ #

def test_the_thread_is_cached_and_reused_within_cache_hours(tmp_path):
    api = Api()
    hn.hn_jobs(fetch=api, cache_dir=tmp_path, now=NOW, sleep=lambda s: None)
    assert len(api.urls) == 2 and (tmp_path / hn.CACHE_FILE).exists()
    again = hn.hn_jobs(fetch=api, cache_dir=tmp_path, now=NOW + 3600, sleep=lambda s: None)
    assert len(api.urls) == 2 and len(again) == 6              # no new request
    hn.hn_jobs(fetch=api, cache_dir=tmp_path, now=NOW + 7 * 3600, sleep=lambda s: None)
    assert len(api.urls) == 4                                  # past HN_CACHE_HOURS: refreshed


def test_a_failed_refresh_serves_the_older_cache(tmp_path):
    hn.hn_jobs(fetch=Api(), cache_dir=tmp_path, now=NOW, sleep=lambda s: None)
    got = hn.hn_jobs(fetch=Api(fail=True), cache_dir=tmp_path, now=NOW + 86400, sleep=lambda s: None)
    assert len(got) == 6
    with pytest.raises(urllib.error.URLError):
        hn.hn_jobs(fetch=Api(fail=True), cache_dir=tmp_path / "empty", now=NOW, sleep=lambda s: None)


# -- the crawl and the public file ---------------------------------------------------- #

def test_refresh_stores_hn_rows_and_skips_employers_whose_board_we_watch(tmp_path, monkeypatch):
    import time as _time
    from sourcing.service import refresh_watchlist
    from sourcing.watchlist import Watchlist
    monkeypatch.setattr(_time, "time", lambda: NOW)
    api = Api()
    w = Watchlist(tmp_path / "jobs.db")
    try:
        w.add_company("Grove Ai Inc", "greenhouse", "groveai")
        w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": ["hackernews"]})
        summary = refresh_watchlist(w, fetch=api, boards=[], sleep=lambda s: None)
        assert not [e for e in summary["errors"] if e["company"] == "hackernews"], summary["errors"]
        got = {j["company"]: j for j in w.list_jobs(include_dismissed=True) if j["source"] == "hackernews"}
        assert "Acme Robotics" in got and "Cobalt Health" in got
        assert "Grove AI" not in got                     # its own board is on the watchlist
        assert "Birchwood Analytics" not in got          # says it will not sponsor
        assert (tmp_path / hn.CACHE_FILE).exists()
        w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})
        api.urls.clear()
        refresh_watchlist(w, fetch=api, boards=[], sleep=lambda s: None)
        assert api.urls == []                            # switched off with the other aggregators
    finally:
        w.close()


def test_the_switch_turns_the_source_off(monkeypatch):
    from sourcing.service import refresh_watchlist
    from sourcing.watchlist import Watchlist
    monkeypatch.setenv("HN_WHOISHIRING", "0")
    api = Api()
    w = Watchlist(":memory:")
    try:
        w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": ["hackernews"]})
        refresh_watchlist(w, fetch=api, boards=[], sleep=lambda s: None)
        assert api.urls == []
    finally:
        w.close()


def test_public_feed_includes_and_credits_hacker_news():
    import importlib.util
    from sourcing import feedfile
    spec = importlib.util.spec_from_file_location(
        "build_feed_hn", Path(__file__).resolve().parents[1] / "scripts" / "build_feed.py")
    bf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bf)
    assert "hackernews" in bf.aggregators_for_feed(env={})
    assert feedfile.ATTRIBUTION["hackernews"]["name"] == "Hacker News"


def test_a_board_row_wins_over_the_same_opening_from_hn():
    from sourcing.feedfile import dedupe_rows
    board = {"source_id": "greenhouse:acme:1", "source": "greenhouse", "company": "Acme Robotics",
             "title": "Senior Backend Engineer", "location": "San Francisco, CA",
             "url": "https://boards.greenhouse.io/acme/jobs/1"}
    hn_row = rows()["Acme Robotics"]
    kept = dedupe_rows([hn_row, board])
    assert [r["source"] for r in kept] == ["greenhouse"]
