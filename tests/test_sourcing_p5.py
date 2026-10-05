"""P5: the new KEYLESS GREEN aggregators (Arbeitnow, RemoteOK) parse fixture payloads offline and
the watchlist fills from them. No network, no keys.
"""

from __future__ import annotations

from sourcing.ats import AGGREGATORS, arbeitnow_jobs, remoteok_jobs
from sourcing.service import DEFAULT_AGGREGATORS, refresh_watchlist
from sourcing.watchlist import Watchlist

ARBEITNOW = {"data": [
    {"slug": "senior-data-engineer-acme-123", "company_name": "Acme",
     "title": "Senior Data Engineer",
     "description": "<p>Build pipelines. <b>Visa sponsorship</b> available.</p>",
     "remote": True, "location": "Berlin",
     "url": "https://www.arbeitnow.com/jobs/senior-data-engineer-acme-123",
     "tags": ["visa sponsorship"], "job_types": ["full_time"], "created_at": 1719792000},
    {"slug": "barista-cafe-9", "company_name": "Cafe", "title": "Barista",
     "description": "Make coffee.", "remote": False, "location": "Munich",
     "url": "https://www.arbeitnow.com/jobs/barista-cafe-9", "tags": [], "created_at": 1719792000},
]}

REMOTEOK = [
    {"legal": "By accessing this data you agree to the RemoteOK terms."},   # leading notice, no job
    {"id": "999", "company": "Beta", "position": "Backend Engineer", "location": "US only",
     "tags": ["backend", "python"], "url": "https://remoteok.com/remote-jobs/999",
     "date": "2026-07-18T00:00:00+00:00", "description": "Remote backend role."},
]


def _fetch(payload):
    return lambda url, **k: payload


def test_arbeitnow_adapter_normalizes_and_filters_by_keyword():
    jobs = arbeitnow_jobs("engineer", fetch=_fetch(ARBEITNOW))
    assert len(jobs) == 1                                  # "Barista" filtered out by the keyword
    j = jobs[0]
    assert j["source"] == "arbeitnow" and j["title"] == "Senior Data Engineer"
    assert j["remote"] == "remote" and j["company"] == "Acme"
    assert "visa sponsorship" in j["jd_text"].lower() and j["url"].endswith("acme-123")
    # no keyword -> the whole feed
    assert len(arbeitnow_jobs("", fetch=_fetch(ARBEITNOW))) == 2


def test_remoteok_adapter_skips_the_legal_notice_element():
    jobs = remoteok_jobs("", fetch=_fetch(REMOTEOK))
    assert len(jobs) == 1                                  # the notice element is not a job
    j = jobs[0]
    assert j["source"] == "remoteok" and j["title"] == "Backend Engineer" and j["remote"] == "remote"
    assert j["company"] == "Beta"


def test_keyless_defaults_and_arbeitnow_is_registered_but_not_a_us_default():
    # remotive + remoteok run keyless out of the box (defaults).
    assert "remotive" in DEFAULT_AGGREGATORS and "remoteok" in DEFAULT_AGGREGATORS
    # arbeitnow is a registered adapter but deliberately NOT a US default (a German/EU board that
    # surfaced Munich/EU roles); it stays available for explicit use, just off the default path.
    assert "arbeitnow" in AGGREGATORS and "arbeitnow" not in DEFAULT_AGGREGATORS
    # adzuna is registered but NOT a default: its API only licenses a ~500-char preview (truncated
    # JDs), so we lean on freehire (full text) for volume instead.
    assert "adzuna" in AGGREGATORS and "adzuna" not in DEFAULT_AGGREGATORS


def test_new_aggregator_sources_route_to_assisted_never_auto():
    """P5 grows GREEN sourcing (what ASSISTED can help with), never the AUTO lane: a job from a new
    aggregator classifies ASSISTED because its host isn't on the verified allowlist (§7)."""
    from submit.service import classify_record
    for url in ("https://www.arbeitnow.com/jobs/senior-data-engineer-acme-123",
                "https://remoteok.com/remote-jobs/999"):
        plan = classify_record({"source_job": {"url": url}})
        assert plan["tier"] == "assisted" and plan["can_auto"] is False


def test_watchlist_fills_from_a_keyless_default_aggregator():
    # refresh_watchlist runs the KEYLESS default aggregators; remoteok is one, so its fixture
    # should land in the store (keyword-matched + accessible). (arbeitnow is covered as an adapter
    # above; it isn't a default, and set_criteria doesn't persist a per-run aggregator override.)
    store = Watchlist(":memory:")
    try:
        store.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any"})
        summary = refresh_watchlist(store, fetch=_fetch(REMOTEOK))
        assert summary["new"] >= 1 and summary["matched"] >= 1
        titles = [j.get("title", "") for j in store.list_jobs()]
        assert any("Engineer" in t for t in titles)
    finally:
        store.close()
