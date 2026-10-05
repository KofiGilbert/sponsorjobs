"""Adzuna pagination — the volume lever for the aggregator lane (2026-08-08).

One Adzuna call returns 50 jobs; reaching aggregator-scale volume (the enterprise/Workday
tier our direct ATS crawl can't see) needs walking many pages. These tests pin that walk
offline: keys come from the env, the fetch is injected, so no network and no real key.
"""

from __future__ import annotations

import pytest

from sourcing.ats import ADZUNA_PER_PAGE, _adzuna_location, adzuna_jobs
from sourcing.sponsors import looks_us


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "test-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "test-key")
    monkeypatch.delenv("ADZUNA_PAGES", raising=False)


def _page(n_results, page_tag):
    return {"results": [{"id": f"{page_tag}-{i}", "title": "Engineer",
                         "location": {"display_name": "New York, NY"},
                         "company": {"display_name": "Acme"},
                         "redirect_url": "https://adzuna/x"} for i in range(n_results)]}


def test_no_ops_without_keys(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    assert adzuna_jobs("engineer", fetch=lambda u: {"results": []}) == []


def test_walks_multiple_full_pages():
    calls = []

    def fetch(url):
        calls.append(url)
        return _page(ADZUNA_PER_PAGE, f"p{len(calls)}")   # every page full -> keep going

    jobs = adzuna_jobs("engineer", fetch=fetch, pages=3)
    assert len(calls) == 3                                # walked all 3 pages
    assert len(jobs) == 3 * ADZUNA_PER_PAGE
    assert all(f"/search/{n}" in calls[n - 1] for n in (1, 2, 3))   # page in the path


def test_stops_early_on_a_partial_last_page():
    def fetch(url):
        page = int(url.split("/search/")[1].split("?")[0])
        return _page(ADZUNA_PER_PAGE if page == 1 else 7, f"p{page}")   # page 2 is partial

    jobs = adzuna_jobs("engineer", fetch=fetch, pages=10)
    assert len(jobs) == ADZUNA_PER_PAGE + 7               # only pages 1 and 2 fetched


def test_dedupes_listings_repeated_across_pages():
    def fetch(url):
        return {"results": [{"id": "dupe", "title": "Engineer",
                             "location": {"display_name": "Austin, TX"},
                             "company": {"display_name": "Acme"}}]}   # same id every page

    jobs = adzuna_jobs("engineer", fetch=fetch, pages=5)
    assert len(jobs) == 1                                 # collapsed to one


def test_us_location_is_enriched_so_badges_can_show():
    # Adzuna's display_name drops the state/country; without folding `area` back in, a
    # genuinely-US role reads as non-US and its visa badge is suppressed (Lockheed Martin bug).
    loc = _adzuna_location({"display_name": "Moorestown, Burlington County",
                            "area": ["US", "New Jersey", "Burlington County", "Moorestown"]})
    assert loc == "Moorestown, Burlington County, New Jersey, US"
    assert looks_us(loc)                                  # now detectable as US


def test_location_enrichment_does_not_duplicate_a_present_state():
    loc = _adzuna_location({"display_name": "Austin, Texas", "area": ["US", "Texas", "Austin"]})
    assert loc == "Austin, Texas, US" and loc.lower().count("texas") == 1


def test_non_us_area_is_never_labelled_us():
    loc = _adzuna_location({"display_name": "London, Greater London",
                            "area": ["UK", "England", "London"]})
    assert loc == "London, Greater London" and not looks_us(loc)


def test_adzuna_jobs_emits_us_detectable_locations():
    def fetch(url):
        return {"results": [{"id": "1", "title": "Engineer",
                             "company": {"display_name": "Lockheed Martin"},
                             "location": {"display_name": "Owego, Tioga County",
                                          "area": ["US", "New York", "Tioga County", "Owego"]}}]}
    jobs = adzuna_jobs("engineer", fetch=fetch, pages=1)
    assert looks_us(jobs[0]["location"])


def test_page_count_comes_from_env(monkeypatch):
    monkeypatch.setenv("ADZUNA_PAGES", "2")
    calls = []

    def fetch(url):
        calls.append(url)
        return _page(ADZUNA_PER_PAGE, "p")

    adzuna_jobs("engineer", fetch=fetch)                  # no explicit pages -> env wins
    assert len(calls) == 2
