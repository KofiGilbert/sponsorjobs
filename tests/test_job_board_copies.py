"""Job-board copies stay off the board (2026-11-24). FreeHire mixes in postings it copied from
LinkedIn (its row's `url` is linkedin.com/jobs/view/...?utm_source=freehire.me): they opened on a
LinkedIn sign-in wall the apply agent must never drive (CLAUDE.md section 7). The board shows the
employer's own posting or nothing, so such a row is dropped when the feed is built
(ats.freehire_employer_url_ok) and when the app reads a feed built before this rule
(feedclient.StaticFeed._set)."""
from __future__ import annotations

import pytest

from sourcing.quality import is_job_board_link


@pytest.mark.parametrize("url,board", [
    ("https://www.linkedin.com/jobs/view/4494057873/?utm_source=freehire.me", True),
    ("https://linkedin.com/jobs/view/1", True),
    ("https://ca.indeed.com/viewjob?jk=1", True),
    ("https://www.glassdoor.com/job-listing/x", True),
    ("https://www.ziprecruiter.com/c/x", True),
    ("https://powertofly.com/jobs/detail/1?utm_source=freehire.me", True),
    ("https://workforcenow.adp.com/mascsr/default/careercenter/x", False),
    ("https://jobs.lever.co/acme/1", False),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", False),
    ("https://notlinkedin.com/jobs/1", False),
    ("https://www.linkedin.com.example.io/jobs/1", False),
    ("", False),
    (None, False),
])
def test_job_board_links_are_recognised(url, board):
    assert is_job_board_link(url) is board


def _raw(slug, url):
    return {"public_slug": slug, "company": "Acme", "title": "Data Analyst",
            "location": "Austin, TX, United States", "url": url, "description": "Analyze data.",
            "posted_at": "2026-11-24T00:00:00Z"}


def test_freehire_drops_a_linkedin_copy_when_the_feed_is_built():
    from sourcing.ats import freehire_bulk, freehire_employer_url_ok, freehire_jobs
    assert not freehire_employer_url_ok({"url": "https://www.linkedin.com/jobs/view/1/?utm_source=freehire.me"})
    data = {"data": [_raw("a", "https://workforcenow.adp.com/jobs/a"),
                     _raw("b", "https://www.linkedin.com/jobs/view/2/?utm_source=freehire.me")]}
    assert [r["source_id"] for r in freehire_bulk(fetch=lambda u, **k: data, cap=100)] == ["freehire::a"]
    assert [r["source_id"] for r in freehire_jobs("x", fetch=lambda u, **k: data)] == ["freehire::a"]


def test_a_feed_built_before_the_rule_hides_its_linkedin_copies(tmp_path):
    from sourcing.feedclient import StaticFeed
    feed = StaticFeed("", tmp_path)
    feed._set({"generated_at": "2026-11-24T00:00:00Z", "jobs": [
        {"source_id": "freehire::a", "url": "https://workforcenow.adp.com/jobs/a"},
        {"source_id": "freehire::b", "url": "https://www.linkedin.com/jobs/view/2/"},
        {"source_id": "greenhouse::c", "url": "https://boards.greenhouse.io/acme/jobs/3"},
    ]}, 0.0)
    assert [j["source_id"] for j in feed._jobs] == ["freehire::a", "greenhouse::c"]
    assert feed._header == {"generated_at": "2026-11-24T00:00:00Z"}
