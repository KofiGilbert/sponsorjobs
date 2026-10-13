"""scripts/discover_careersites.py: the maintainer's career-site finder. Pure classification,
the polite prober against canned hosts (no network), the checkpoint, and the table writer."""

from __future__ import annotations

import json

import pytest

from scripts import discover_careersites as d
from sourcing import careersite as cs

EIGHTFOLD_ROBOTS = """User-agent: *
Disallow: /
Allow: /careers
Allow: /api/pcsx
Allow: /careerhub/explore/jobs

Sitemap: https://careers.acme.com/careers/sitemap_index.xml?domain=acme.com
"""

PHENOM_ROBOTS = """User-agent: *
Disallow: */phenomtrack.min.js
Disallow: */apply
Disallow: */px-widgets
Sitemap: https://jobs.globex.com/global/en/sitemap_index.xml
Sitemap: https://jobs.globex.com/ca/fr/sitemap_index.xml
"""


def urlset(urls):
    body = "".join(f"<url><loc>{u}</loc><lastmod>2026-10-{10 - i % 9:02d}</lastmod></url>" for i, u in enumerate(urls))
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def index(urls):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


def job_page(title="Software Engineer"):
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "description": "x"}
    return f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'


class Net:
    """Canned (status, body, location) per URL; anything else is a DNS failure."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if url not in self.pages:
            raise OSError("nodename nor servname provided")
        v = self.pages[url]
        if isinstance(v, int):
            return v, "", ""
        if isinstance(v, tuple):
            return v
        return 200, v, ""


def prober(pages):
    net = Net(pages)
    return d.Prober(d.Gate(sleep=lambda s: None, clock=lambda: 0.0), fetch=net), net


def eightfold_pages(n=30):
    jobs = [f"https://careers.acme.com/careers/job/{1000 + i}?domain=acme.com" for i in range(n)]
    return {
        "https://careers.acme.com/robots.txt": EIGHTFOLD_ROBOTS,
        "https://careers.acme.com/careers/sitemap_index.xml?domain=acme.com": index([
            "https://careers.acme.com/careers/sitemap.xml?domain=acme.com",
            "https://careers.acme.com/careers/sitemap_cat.xml?domain=acme.com"]),
        "https://careers.acme.com/careers/sitemap.xml?domain=acme.com": urlset(jobs),
        "https://careers.acme.com/careers/sitemap_cat.xml?domain=acme.com": urlset(
            ["https://careers.acme.com/careers?category=Engineering"]),
        **{u: job_page() for u in jobs},
    }


# -- names -> hosts ---------------------------------------------------------------------- #

def test_brand_slugs_and_hosts():
    assert d.brand_slugs("Goldman Sachs Co Llc") == ["goldmansachs", "goldman"]
    assert d.brand_slugs("Bank Of America Corporation") == ["bankofamerica"]   # never bare "bank"
    assert d.brand_slugs("Mckinsey Company Inc United States") == ["mckinsey"]
    assert d.brand_slugs("Wal Mart Associates Inc") == ["walmart", "wal"]
    assert d.brand_slugs("Inc") == []
    hosts = d.candidate_hosts(["acme"])
    assert hosts == ["careers.acme.com", "jobs.acme.com", "acme.eightfold.ai",
                     "apply.careers.acme.com", "www.acmecareers.com"]
    assert len(d.candidate_hosts([f"s{i}x" for i in range(9)])) == d.MAX_HOSTS_PER_EMPLOYER
    assert d.registrable("x.eightfold.ai") == d.registrable("y.eightfold.ai") == "eightfold.ai"


def test_pick_employers_skips_watched_blocked_and_tabled():
    rows = [
        {"norm_name": "acme", "display_name": "Acme Inc", "h1b_approvals": 900, "h1b_last_fy": 2023},
        {"norm_name": "globex", "display_name": "Globex Corp", "h1b_approvals": 800, "h1b_last_fy": 2023},
        {"norm_name": "tesla", "display_name": "Tesla Inc", "h1b_approvals": 3000, "h1b_last_fy": 2023},
        {"norm_name": "initech", "display_name": "Initech Llc", "h1b_approvals": 700, "h1b_last_fy": 2023},
        {"norm_name": "umbrella", "display_name": "Umbrella Corporation", "h1b_approvals": 600, "h1b_last_fy": 2023},
    ]
    watch = [{"company": "Globex Corp", "ats": "greenhouse", "board_id": "globex"}]
    sites = {"careers.initech.com": {"company": "Initech", "job_url": r"^https://careers\.initech\.com/job/"}}
    picked = d.pick_employers(rows, watch, sites, limit=10)
    assert [p["name"] for p in picked] == ["Acme Inc", "Umbrella Corporation"]
    assert picked[0]["hosts"][0] == "careers.acme.com"
    assert len(d.pick_employers(rows, watch, sites, limit=1)) == 1


# -- classification ---------------------------------------------------------------------- #

def test_classify_platforms():
    assert d.classify("careers.acme.com", EIGHTFOLD_ROBOTS) == "eightfold"
    assert d.classify("acme.eightfold.ai") == "eightfold"
    assert d.classify("jobs.globex.com", PHENOM_ROBOTS) == "phenom"
    assert d.classify("x.com", "", ["https://x.com/us/en/job/R12345/Engineer"]) == "phenom"
    assert d.classify("acme.wd5.myworkdayjobs.com") == "workday"
    assert d.classify("careers-acme.icims.com") == "icims"
    assert d.classify("jobs.acme.com", "Sitemap: https://jobs.acme.com/sitemap_rmk.xml\nDisallow: /career_ns") == "successfactors"
    assert d.classify("careers.acme.com", "Sitemap: https://acme.fa.oraclecloud.com/x") == "oracle"
    assert d.classify("www.acmecareers.com", "Sitemap: https://www.acmecareers.com/jobs_sitemap.xml") == "radancy"
    assert d.classify("jobs.acme.com", "User-agent: *\nDisallow:/search-jobs/\n") == "radancy"
    assert d.classify("jobs.acme.com", "", ["https://jobs.acme.com/job/austin/data-engineer/27595/101812305488"]) == "radancy"
    assert d.classify("jobs.acme.com", "User-agent: *\nDisallow: /private") == "other"


def test_job_url_detection_and_pattern():
    h = "careers.acme.com"
    assert d.is_job_url(f"https://{h}/careers/job/123456?domain=acme.com", h)
    assert d.is_job_url(f"https://{h}/global/en/job/R-1234/Data-Engineer", h)
    assert d.is_job_url(f"https://{h}/job/austin/engineer/1234/5678", h)
    assert not d.is_job_url(f"https://{h}/jobs/search", h)
    assert not d.is_job_url(f"https://{h}/about/1234", h)
    assert not d.is_job_url(f"https://other.com/job/1234", h)
    assert not d.is_job_url(f"http://{h}/job/1234", h)
    pat = d.job_url_pattern(h, "eightfold", f"https://{h}/careers/job/123?domain=acme.com")
    assert pat == r"^https://careers\.acme\.com/careers/job/\d+"
    assert d.job_url_pattern(h, "phenom", f"https://{h}/global/en/job/R1/x") == r"^https://careers\.acme\.com/global/en/job/"
    # a title slug before the job segment varies per job: it becomes a wildcard segment
    assert d.job_prefix(f"https://{h}/de/athlete-iii/job/R-94223") == "/de/*/job/"
    assert d.job_prefix(f"https://{h}/en-us/project-manager/job/R1") == "/en-us/*/job/"
    import re
    pat = d.job_url_pattern(h, "other", f"https://{h}/en/project-manager/job/R0139637")
    assert pat == r"^https://careers\.acme\.com/en/[^/]+/job/"
    assert re.match(pat, f"https://{h}/en/data-engineer/job/R1") and not re.match(pat, f"https://{h}/de/x-y/job/R1")


def test_robots_text_that_forbids_collection_is_respected():
    meta_like = ("# Notice: Collection of data on Facebook through automated means is prohibited "
                 "unless you have express written permission from Facebook\nUser-agent: *\nAllow: /\n")
    assert d.robots_forbids_collection(meta_like)
    assert not d.robots_forbids_collection(EIGHTFOLD_ROBOTS)
    assert not d.robots_forbids_collection("Disallow: /written-permission\n")   # rules, not comments
    assert not d.looks_like_robots("<!DOCTYPE html><html>")
    assert d.looks_like_robots(EIGHTFOLD_ROBOTS)


# -- the prober (canned hosts, no network) ------------------------------------------------ #

def test_verifies_an_eightfold_site():
    p, net = prober(eightfold_pages())
    r = p.probe("careers.acme.com")
    assert r["status"] == "verified" and r["platform"] == "eightfold"
    assert r["jobs_seen"] == 30 and r["sample_title"] == "Software Engineer"
    assert r["sitemaps"] == ["https://careers.acme.com/careers/sitemap_index.xml?domain=acme.com"]
    assert r["job_url"] == r"^https://careers\.acme\.com/careers/job/\d+"
    assert net.calls[0] == "https://careers.acme.com/robots.txt"
    assert sum("/careers/job/" in u for u in net.calls) == 1        # ONE job page read
    entry = {"company": "Acme", "sitemaps": r["sitemaps"], "job_url": r["job_url"]}
    assert cs.site_entry_ok("careers.acme.com", entry)


def test_verifies_a_phenom_site_and_picks_one_locale():
    en = [f"https://jobs.globex.com/global/en/job/R{5000 + i}/Engineer" for i in range(40)]
    fr = [f"https://jobs.globex.com/ca/fr/job/R{5000 + i}/Ingenieur" for i in range(12)]
    pages = {
        "https://jobs.globex.com/robots.txt": PHENOM_ROBOTS,
        "https://jobs.globex.com/global/en/sitemap_index.xml": index(["https://jobs.globex.com/global/en/sitemap1.xml"]),
        "https://jobs.globex.com/global/en/sitemap1.xml": urlset(en),
        "https://jobs.globex.com/ca/fr/sitemap_index.xml": index(["https://jobs.globex.com/ca/fr/sitemap1.xml"]),
        "https://jobs.globex.com/ca/fr/sitemap1.xml": urlset(fr),
        **{u: job_page("Engineer") for u in en},
    }
    p, _ = prober(pages)
    r = p.probe("jobs.globex.com")
    assert r["status"] == "verified" and r["platform"] == "phenom"
    assert r["jobs_seen"] == 40
    assert r["sitemaps"] == ["https://jobs.globex.com/global/en/sitemap_index.xml"]
    assert r["job_url"] == r"^https://jobs\.globex\.com/global/en/job/"


@pytest.mark.parametrize("change, status, reason", [
    ({"https://careers.acme.com/robots.txt": "User-agent: *\nDisallow: /\n"}, "rejected", "disallows the sitemap"),
    ({"https://careers.acme.com/robots.txt": "<!doctype html><html>home</html>"}, "rejected", "HTML"),
    ({"https://careers.acme.com/robots.txt": 403}, "blocked", "403"),
    ({"https://careers.acme.com/robots.txt": 429}, "blocked", "429"),
    ({"https://careers.acme.com/robots.txt": (301, "", "https://www.acme.com/robots.txt")}, "redirect", "www.acme.com"),
    ({"https://careers.acme.com/careers/sitemap.xml?domain=acme.com": urlset([])}, "rejected", "no job pages"),
])
def test_rejections_and_blocks(change, status, reason):
    pages = {**eightfold_pages(), **change}
    p, net = prober(pages)
    r = p.probe("careers.acme.com")
    assert r["status"] == status and reason in (r["reason"] + r.get("redirect", ""))
    if status == "blocked":
        assert net.calls == ["https://careers.acme.com/robots.txt"]           # stop, no retry


def test_job_page_without_jsonld_or_behind_a_challenge():
    pages = eightfold_pages()
    for u in list(pages):
        if "/careers/job/" in u:
            pages[u] = "<html><div id=app></div></html>"
    r = prober(pages)[0].probe("careers.acme.com")
    assert r["status"] == "rejected" and "JobPosting" in r["reason"]
    pages = eightfold_pages()
    for u in list(pages):
        if "/careers/job/" in u:
            pages[u] = "<html><title>Just a moment...</title></html>"
    p, net = prober(pages)
    r = p.probe("careers.acme.com")
    assert r["status"] == "blocked" and sum("/careers/job/" in u for u in net.calls) == 1


def test_workday_and_absent_hosts():
    wd = {"https://acme.wd5.myworkdayjobs.com/robots.txt": "Sitemap: https://acme.wd5.myworkdayjobs.com/External/siteMap.xml\n"}
    p, net = prober(wd)
    assert p.probe("acme.wd5.myworkdayjobs.com")["status"] == "workday"
    assert len(net.calls) == 1
    assert prober({})[0].probe("careers.nowhere.com")["status"] == "absent"


def test_gate_spaces_requests_per_domain():
    sleeps: list[float] = []
    t = [0.0]
    g = d.Gate(gap=1.0, sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)), clock=lambda: t[0])
    g.run("a.eightfold.ai", lambda: None)
    g.run("b.eightfold.ai", lambda: None)                 # same registrable domain: waits
    g.run("careers.other.com", lambda: None)              # different domain: no wait
    g.run("c.eightfold.ai", lambda: None, gap=3.0)        # Crawl-delay raises the gap
    assert sleeps == [1.0, 3.0] and g.requests == 4


# -- the run, checkpoint and table writer ------------------------------------------------- #

def test_discover_is_resumable_and_writes_the_table(tmp_path):
    cp = d.Checkpoint(tmp_path / "cp.json")
    emps = [{"key": "acme", "name": "Acme Inc", "approvals": 900,
             "hosts": ["jobs.acme.com", "careers.acme.com", "acme.eightfold.ai"]},
            {"key": "nowhere", "name": "Nowhere Llc", "approvals": 10, "hosts": ["careers.nowhere.com"]}]
    p, net = prober(eightfold_pages())
    stats = d.discover(emps, cp, p, workers=2, log=lambda *_: None)
    assert stats == {"employers": 2, "verified": 1}
    assert cp.employers["acme"]["verified"] == "careers.acme.com"
    assert "acme.eightfold.ai" not in net.calls[-1]                     # stopped at the first hit
    calls = len(net.calls)
    again = d.Checkpoint(tmp_path / "cp.json")                          # a rerun probes nothing
    d.discover(emps, again, p, workers=2, log=lambda *_: None)
    assert len(net.calls) == calls
    table = tmp_path / "sites.json"
    table.write_text(json.dumps({"sites": {"careers.qualcomm.com": cs.load_sites()["careers.qualcomm.com"]}}))
    entries = d.table_entries(again, cs.load_sites(table))
    assert list(entries) == ["careers.acme.com"]
    assert entries["careers.acme.com"]["company"] == "Acme" and entries["careers.acme.com"]["jobs_seen"] == 30
    assert d.write_table(entries, table) == 1 and d.write_table(entries, table) == 0
    assert set(cs.load_sites(table)) == {"careers.qualcomm.com", "careers.acme.com"}


def test_deadline_leaves_employers_for_the_next_run(tmp_path):
    cp = d.Checkpoint(tmp_path / "cp.json")
    emps = [{"key": "acme", "name": "Acme Inc", "approvals": 1, "hosts": ["careers.acme.com"]}]
    p, net = prober(eightfold_pages())
    d.discover(emps, cp, p, deadline=0.0, clock=lambda: 1.0, log=lambda *_: None)
    assert cp.employers == {} and net.calls == []


def test_redirect_to_a_careers_host_is_followed_once(tmp_path):
    pages = {**eightfold_pages(),
             "https://jobs.acme.com/robots.txt": (301, "", "https://careers.acme.com/robots.txt")}
    cp = d.Checkpoint(tmp_path / "cp.json")
    emps = [{"key": "acme", "name": "Acme Inc", "approvals": 1, "hosts": ["jobs.acme.com"]}]
    d.discover(emps, cp, prober(pages)[0], log=lambda *_: None)
    assert cp.employers["acme"]["verified"] == "careers.acme.com"
    assert cp.employers["acme"]["tried"] == ["jobs.acme.com", "careers.acme.com"]
