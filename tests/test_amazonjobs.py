"""Amazon's public search JSON adapter (sourcing/amazonjobs.py)."""

from __future__ import annotations

import pytest

from sourcing import amazonjobs as az
from sourcing.ats import ADAPTERS, normalize_jobs


def hit(i, **over):
    h = {"id_icims": str(10000 + i), "title": f"Software Development Engineer {i}",
         "company_name": "Amazon.com Services LLC", "city": "Seattle", "state": "WA",
         "normalized_location": "Seattle, Washington, USA", "job_path": f"/en/jobs/{10000 + i}/sde",
         "posted_date": "October 10, 2026", "description": "<p>Build <b>AWS</b>.</p>",
         "basic_qualifications": "- 3+ years", "preferred_qualifications": "- Rust",
         "job_schedule_type": "full-time"}
    h.update(over)
    return h


def test_row_from_hit_builds_a_full_text_us_row():
    job = az.row_from_hit("USA", hit(1))
    assert job["source_id"] == "amazonjobs:USA:10001"
    assert job["company"] == "Amazon.com Services LLC"
    assert job["location"] == "Seattle, WA, US"
    assert job["url"] == "https://www.amazon.jobs/en/jobs/10001/sde"
    assert job["posted_at"] == "2026-10-10"
    assert "Build AWS" in job["jd_text"] and "Basic qualifications" in job["jd_text"] and "Rust" in job["jd_text"]
    assert job["employment_type"] == "full-time"
    assert az.row_from_hit("USA", hit(2, title="")) is None


def test_pages_newest_first_with_a_gap_and_stops_at_the_cap():
    calls, sleeps = [], []

    def fetch(url, timeout=20):
        calls.append(url)
        off = int(url.split("offset=")[1].split("&")[0])
        return {"hits": 250, "jobs": [hit(off + i) for i in range(min(100, 250 - off))]}

    rows = az.amazon_jobs("USA", fetch=fetch, max_jobs=1000, sleep=sleeps.append)
    assert len(rows) == 250 and len(calls) == 3
    assert "sort=recent" in calls[0] and "country=USA" in calls[0] and "offset=200" in calls[2]
    assert sleeps == [az.REQUEST_GAP, az.REQUEST_GAP]
    rows = az.amazon_jobs("USA", fetch=fetch, max_jobs=120, sleep=lambda s: None)
    assert len(rows) == 120


def test_country_code_is_the_only_thing_that_reaches_the_url():
    with pytest.raises(ValueError):
        az.search_url("usa/../x", 0)
    with pytest.raises(ValueError):
        normalize_jobs("amazonjobs", "evil.example", fetch=lambda u, timeout=20: {})


def test_registered_as_an_adapter():
    assert "amazonjobs" in ADAPTERS
    rows = normalize_jobs("amazonjobs", "USA", "Amazon", fetch=lambda u, timeout=20: {"hits": 1, "jobs": [hit(1)]},
                          sleep=lambda s: None)
    assert rows[0]["source"] == "amazonjobs"
