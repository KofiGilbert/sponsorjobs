"""The career-site lane (sourcing/careersite.py): robots.txt obeyed, sitemap read newest first,
JobPosting JSON-LD turned into rows, and a hard stop on a 403 / 429 / challenge page."""

from __future__ import annotations

import json
import urllib.error

import pytest

from sourcing import careersite as cs
from sourcing.watchlist import Watchlist

HOST = "jobs.example.com"
CFG = {"company": "Example Corp", "job_url": r"^https://jobs\.example\.com/job/\d+"}

ROBOTS = """User-agent: *
Disallow: /internal/
Crawl-delay: 2
Sitemap: https://jobs.example.com/sitemap-index.xml
"""

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://jobs.example.com/sitemap-images.xml</loc></sitemap>
  <sitemap><loc>https://jobs.example.com/sitemap-jobs-1.xml</loc><lastmod>2026-10-10</lastmod></sitemap>
</sitemapindex>"""

JOBS_SM = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://jobs.example.com/job/100</loc><lastmod>2026-10-01T10:00:00+00:00</lastmod></url>
  <url><loc>https://jobs.example.com/job/200</loc><lastmod>2026-10-09</lastmod></url>
  <url><loc>https://jobs.example.com/job/300</loc><lastmod>2026-10-05</lastmod></url>
  <url><loc>https://jobs.example.com/internal/job/400</loc><lastmod>2026-10-11</lastmod></url>
  <url><loc>https://jobs.example.com/about</loc></url>
</urlset>"""


def posting_page(jid, title="Software Development Engineer", city="Seattle", region="WA",
                 country="US", desc="<p>Build <b>things</b>.</p><ul><li>Python</li></ul>",
                 extra=None):
    obj = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
           "description": desc, "datePosted": "2026-10-08T00:00:00Z",
           "validThrough": "2027-01-01", "identifier": {"@type": "PropertyValue", "value": f"REQ-{jid}"},
           "hiringOrganization": {"@type": "Organization", "name": "Example Corp Services LLC"},
           "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
                           "addressLocality": city, "addressRegion": region, "addressCountry": country}},
           "employmentType": "FULL_TIME", "url": f"https://jobs.example.com/job/{jid}"}
    if extra:
        obj.update(extra)
    return (f"<html><head><title>{title}</title><script type=\"application/ld+json\">"
            f"{json.dumps(obj)}</script></head><body>...</body></html>")


class Site:
    """Canned pages for one host; records the order of requests."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url, timeout=20, **_):
        self.calls.append(url)
        page = self.pages.get(url)
        if page is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(page, int):
            raise urllib.error.HTTPError(url, page, "err", {}, None)
        return page


def base_pages():
    return {
        f"https://{HOST}/robots.txt": ROBOTS,
        f"https://{HOST}/sitemap-index.xml": INDEX,
        f"https://{HOST}/sitemap-jobs-1.xml": JOBS_SM,
        f"https://{HOST}/job/100": posting_page(100, title="Data Analyst", city="Arlington", region="Virginia"),
        f"https://{HOST}/job/200": posting_page(200),
        f"https://{HOST}/job/300": "<html><body>No structured data here</body></html>",
        f"https://{HOST}/internal/job/400": posting_page(400),
    }


def run(pages, store=None, **kw):
    site = Site(pages)
    store = store or Watchlist(":memory:")
    sleeps: list[float] = []
    summary = cs.crawl_site(store, HOST, CFG, text_fetch=site, sleep=sleeps.append,
                            clock=lambda: 0.0, **kw)
    return site, store, summary, sleeps


def test_parse_sitemap_index_and_urlset():
    assert cs.parse_sitemap(INDEX)[0] == "index"
    kind, rows = cs.parse_sitemap(JOBS_SM)
    assert kind == "urlset"
    assert rows[0] == ("https://jobs.example.com/job/100", "2026-10-01")
    assert cs.parse_sitemap("<not xml") == ("urlset", [])


def test_extract_jobposting_handles_graph_and_lists():
    page = posting_page(1)
    assert cs.extract_jobposting(page)["title"] == "Software Development Engineer"
    graph = ('<script type="application/ld+json">{"@graph": [{"@type": "WebPage"}, '
             '{"@type": ["JobPosting", "Thing"], "title": "QA"}]}</script>')
    assert cs.extract_jobposting(graph)["title"] == "QA"
    assert cs.extract_jobposting("<script type='application/ld+json'>{bad</script>") is None
    assert cs.extract_jobposting("<html>none</html>") is None


def test_posting_to_job_normalizes_location_company_and_text():
    job = cs.posting_to_job(HOST, "Example Corp", "https://jobs.example.com/job/1",
                            cs.extract_jobposting(posting_page(1)))
    assert job["source"] == "careersite"
    assert job["source_id"] == "careersite:jobs.example.com:REQ-1"
    assert job["company"] == "Example Corp Services LLC"
    assert job["location"] == "Seattle, WA, US"
    assert job["posted_at"] == "2026-10-08"
    assert "Build things" in job["jd_text"] and "<b>" not in job["jd_text"]
    assert job["employment_type"] == "FULL_TIME"


def test_posting_to_job_drops_closed_and_untitled():
    closed = cs.extract_jobposting(posting_page(1, extra={"validThrough": "2020-01-01"}))
    assert cs.posting_to_job(HOST, "X", "u", closed) is None
    assert cs.posting_to_job(HOST, "X", "u", {"@type": "JobPosting"}) is None


def test_remote_posting_location():
    p = cs.extract_jobposting(posting_page(1, extra={
        "jobLocation": None, "jobLocationType": "TELECOMMUTE",
        "applicantLocationRequirements": {"@type": "Country", "name": "USA"}}))
    job = cs.posting_to_job(HOST, "X", "u", p)
    assert job["remote"] == "remote" and job["location"] == "Remote, US"


def test_crawl_obeys_robots_reads_newest_first_and_stores_rows():
    site, store, summary, sleeps = run(base_pages())
    assert summary["blocked"] == "" and summary["error"] == ""
    # robots first, then the index, then only the jobs child (images sitemap skipped by order
    # and budget is plenty, but the disallowed /internal/ page is never requested).
    assert site.calls[0] == f"https://{HOST}/robots.txt"
    assert f"https://{HOST}/internal/job/400" not in site.calls
    assert f"https://{HOST}/about" not in site.calls
    # newest lastmod first: 200 (10-09) before 300 (10-05) before 100 (10-01)
    job_calls = [u for u in site.calls if "/job/" in u]
    assert job_calls == [f"https://{HOST}/job/200", f"https://{HOST}/job/300", f"https://{HOST}/job/100"]
    assert summary == {**summary, "listed": 3, "fetched": 3, "kept": 2, "no_posting": 1}
    rows = {r["source_id"]: r for r in store.list_jobs()}
    assert set(rows) == {"careersite:jobs.example.com:REQ-200", "careersite:jobs.example.com:REQ-100"}
    assert rows["careersite:jobs.example.com:REQ-100"]["location"] == "Arlington, Virginia, US"
    # Crawl-delay: 2 raised the gap from the 1 s default; every request after the first waited.
    assert sleeps and all(s == pytest.approx(2.0) for s in sleeps)


def test_second_run_touches_known_rows_without_refetching():
    pages = base_pages()
    site, store, _, _ = run(pages)
    store._conn.execute("UPDATE sourced_job SET last_seen='2020-01-01 00:00:00'")
    store._conn.commit()
    site2, _, summary, _ = run(pages, store=store)
    assert summary["known"] == 2 and summary["fetched"] == 1      # only the no-posting page again
    assert f"https://{HOST}/job/200" not in site2.calls
    seen = store._conn.execute("SELECT last_seen FROM sourced_job").fetchall()
    assert all(r[0] > "2025" for r in seen)


def test_detail_budget_and_deadline_stop_the_crawl():
    _, _, summary, _ = run(base_pages(), max_detail=1)
    assert summary["fetched"] == 1 and summary["kept"] == 1
    site = Site(base_pages())
    ticks = iter([0.0] * 6 + [10_000.0] * 50)
    summary = cs.crawl_site(Watchlist(":memory:"), HOST, CFG, text_fetch=site, sleep=lambda s: None,
                            clock=lambda: next(ticks), deadline=100.0)
    assert summary["fetched"] < 3


@pytest.mark.parametrize("code", [403, 429])
def test_forbidden_or_rate_limited_stops_the_host(code):
    pages = base_pages()
    pages[f"https://{HOST}/job/200"] = code
    site, _, summary, _ = run(pages)
    assert summary["blocked"] and str(code) in summary["blocked"]
    assert site.calls.count(f"https://{HOST}/job/200") == 1          # no retry, no second identity
    assert f"https://{HOST}/job/300" not in site.calls


def test_challenge_page_stops_the_host():
    pages = base_pages()
    pages[f"https://{HOST}/job/200"] = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
    site, _, summary, _ = run(pages)
    assert "challenge" in summary["blocked"]
    assert f"https://{HOST}/job/300" not in site.calls


def test_robots_disallow_all_means_nothing_is_read():
    pages = base_pages()
    pages[f"https://{HOST}/robots.txt"] = "User-agent: *\nDisallow: /\nSitemap: https://jobs.example.com/sitemap-index.xml\n"
    site, _, summary, _ = run(pages)
    assert summary["listed"] == 0 and summary["fetched"] == 0
    assert site.calls == [f"https://{HOST}/robots.txt"]


def test_missing_robots_allows_the_table_sitemap():
    pages = base_pages()
    del pages[f"https://{HOST}/robots.txt"]
    site = Site(pages)
    cfg = {**CFG, "sitemaps": [f"https://{HOST}/sitemap-jobs-1.xml"]}
    summary = cs.crawl_site(Watchlist(":memory:"), HOST, cfg, text_fetch=site, sleep=lambda s: None,
                            clock=lambda: 0.0)
    assert summary["kept"] == 2 and summary["error"] == ""


def test_host_never_leaves_its_domain():
    host = cs.Host(HOST, CFG, text_fetch=Site({}), sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(ValueError):
        host.get("https://evil.example.net/x")


def test_excluded_roles_are_not_stored():
    pages = base_pages()
    pages[f"https://{HOST}/job/200"] = posting_page(200, desc="Must be a U.S. citizen. Active TS/SCI clearance required.")
    _, store, summary, _ = run(pages)
    assert summary["excluded"] == 1
    assert not [r for r in store.list_jobs() if r["source_id"].endswith("REQ-200")]


def test_crawl_sites_runs_hosts_in_threads_with_own_connections(tmp_path):
    db = tmp_path / "jobs.db"
    Watchlist(db).close()
    pages = base_pages()
    sites = {HOST: CFG, "jobs2.example.com": {"company": "Two", "job_url": r"^https://jobs2\.example\.com/job/\d+"}}
    pages2 = {k.replace(HOST, "jobs2.example.com"): (v.replace(HOST, "jobs2.example.com") if isinstance(v, str) else v)
              for k, v in pages.items()}
    site = Site({**pages, **pages2})
    out = cs.crawl_sites(db, sites, minutes=1, workers=2, text_fetch=site, sleep=lambda s: None, log=None)
    assert sorted(x["host"] for x in out) == sorted(sites)
    assert sum(x["kept"] for x in out) == 4
    w = Watchlist(db)
    assert len(w.list_jobs()) == 4
    w.close()


def test_site_table_entries_are_well_formed():
    """Every curated site names a company and a job-url pattern on its own host."""
    import re
    for host, cfg in cs.SITES.items():
        assert cfg.get("company") and cfg.get("job_url"), host
        assert host == host.lower() and "/" not in host
        pat = re.compile(cfg["job_url"])
        assert pat.pattern.startswith("^https://"), host
        for sm in cfg.get("sitemaps", []):
            assert sm.startswith(f"https://{host}/") or sm.startswith("https://"), (host, sm)


def test_robots_longest_match_wins_like_search_engines():
    """Eightfold's file: "Disallow: /" then "Allow: /careers". Python's robotparser takes the
    first rule and allows nothing; the web standard (RFC 9309) takes the longest match."""
    r = cs.Robots("User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /api/pcsx\nDisallow: /careers/*/apply$\n"
                  "Sitemap: https://x.example/careers/sitemap_index.xml?domain=x.com\n")
    assert r.can_fetch("https://x.example/careers/job/123-slug?domain=x.com")
    assert r.can_fetch("https://x.example/careers/sitemap.xml?domain=x.com")
    assert not r.can_fetch("https://x.example/careerhub/x")
    assert not r.can_fetch("https://x.example/careers/123/apply")
    assert r.can_fetch("https://x.example/careers/123/apply/thanks")
    assert r.sitemaps == ["https://x.example/careers/sitemap_index.xml?domain=x.com"]


def test_robots_picks_our_group_tie_goes_to_allow_and_empty_file_allows_all():
    r = cs.Robots("User-agent: Googlebot\nDisallow: /g\n\nUser-agent: SponsorJobsBot\nDisallow: /s\nCrawl-delay: 3\n\n"
                  "User-agent: *\nDisallow: /\n")
    assert r.can_fetch("https://h/g") and not r.can_fetch("https://h/s/x") and r.can_fetch("https://h/other")
    assert r.delay == 3.0
    tie = cs.Robots("User-agent: *\nDisallow: /jobs\nAllow: /jobs\n")
    assert tie.can_fetch("https://h/jobs/1")
    assert cs.Robots("").can_fetch("https://h/anything")
    assert cs.Robots("User-agent: *\nDisallow:\n").can_fetch("https://h/anything")


def test_an_unreadable_page_is_skipped_not_fatal():
    pages = base_pages()
    pages[f"https://{HOST}/job/200"] = 404
    site, _, summary, _ = run(pages)
    assert summary["errors"] == 1 and summary["kept"] == 1 and summary["blocked"] == ""
    assert f"https://{HOST}/job/100" in site.calls


def test_country_objects_and_codes_become_readable_locations():
    """Eightfold writes addressCountry as {"@type": "Country", "name": "IN"}; spelled out so the
    US detector never reads IN as Indiana."""
    from sourcing.sponsors import looks_us
    p = cs.extract_jobposting(posting_page(1, city="Bengaluru", region="KA",
                                           extra={"jobLocation": {"@type": "Place", "address": {
                                               "addressLocality": "Bengaluru", "addressRegion": "KA",
                                               "addressCountry": {"@type": "Country", "name": "IN"}}}}))
    job = cs.posting_to_job(HOST, "X", "u", p)
    assert job["location"] == "Bengaluru, KA, India" and not looks_us(job["location"])
    p = cs.extract_jobposting(posting_page(1, extra={"jobLocation": {"@type": "Place", "address": {
        "addressLocality": "Mountain View", "addressRegion": "CA", "addressCountry": {"@type": "Country", "name": "US"}}}}))
    job = cs.posting_to_job(HOST, "X", "u", p)
    assert job["location"] == "Mountain View, CA, US" and looks_us(job["location"])
