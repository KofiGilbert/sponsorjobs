"""Career-site page readers and the keep rule (sourcing/careersite_readers.py), and the hook in
sourcing/careersite.py: JSON-LD first, the site's reader when a page has none, the professional-
roles keep rule on the sitemap slug (no request) and on the page title, and dropped pages
remembered so the next run does not read them again. Fixtures are trimmed real pages
(careers.walmart.com R-2646349 and an accenture.com job, both fetched 2026-10-12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sourcing import careersite as cs
from sourcing import careersite_readers as rd
from sourcing.watchlist import Watchlist

FIX = Path(__file__).parent / "fixtures" / "careersite"
WALMART = (FIX / "walmart_R-2646349.html").read_text(encoding="utf-8")
ACCENTURE = (FIX / "accenture_R00352832.html").read_text(encoding="utf-8")

WM = "careers.walmart.com"
WM_CFG = cs.SITES[WM]


# --------------------------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------------------------- #

def test_walmart_reader_maps_the_real_next_data():
    p = rd.read_nextdata(WALMART)
    assert p["@type"] == "JobPosting"
    assert p["title"] == "Facilities Maintenance Manager, Licensed Master Electrician"   # "(USA) " cut
    assert "Licensed Master Electrician and 5 years" in p["description"]               # quals included
    assert p["identifier"] == {"value": "R-2646349"}
    assert p["datePosted"] == "2026-09-16"
    assert p["employmentType"] == "Full time"
    assert p["_department"] == "corporate / Home Office"
    job = cs.posting_to_job(WM, "Walmart", "https://careers.walmart.com/us/en/jobs/R-2646349", p)
    assert job["source_id"] == "careersite:careers.walmart.com:R-2646349"
    assert job["company"] == "Walmart"
    assert job["location"] == "Meridian, ID, US; Ammon, ID, US"
    assert job["posted_at"] == "2026-09-16"
    assert "Position Summary" in job["jd_text"] and "<span" not in job["jd_text"]


def test_walmart_reader_skips_a_closed_requisition():
    doc = json.loads(WALMART.split('type="application/json">', 1)[1].split("</script>", 1)[0])
    doc["props"]["pageProps"]["jobDetails"]["requisitionStatus"] = {"id": "FILLED", "value": "Filled"}
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(doc)}</script>'
    assert rd.read_nextdata(page) is None          # not handed to the generic walk either


def test_generic_reader_finds_the_posting_on_the_real_page():
    """The generic walk alone (no Walmart mapping) still finds title + longest description."""
    doc = rd.nextdata(WALMART)
    p = rd._generic(doc)
    assert p["title"].endswith("Facilities Maintenance Manager, Licensed Master Electrician")
    assert len(p["description"]) > 1000
    assert p["identifier"] == {"value": "R-2646349"}
    assert p["datePosted"] == "2026-09-16"
    assert p["jobLocation"]                                             # primaryLocation found


def test_generic_reader_other_shape_and_no_posting():
    doc = {"props": {"pageProps": {"seo": {"title": "Careers", "description": "short blurb"},
           "job": {"jobTitle": "Data Engineer", "jobDescription": "<p>" + "Build pipelines. " * 30 + "</p>",
                   "companyName": "Acme", "postedDate": "2026-10-01", "expirationDate": "2026-12-01",
                   "location": {"city": "Austin", "state": "TX", "country": "US"},
                   "employmentType": "FULL_TIME", "requisitionId": "JR-9"}}}}
    page = f"<script id='__NEXT_DATA__' type='application/json'>{json.dumps(doc)}</script>"
    p = rd.read_page("nextdata", page)
    assert p["title"] == "Data Engineer" and p["hiringOrganization"] == {"name": "Acme"}
    job = cs.posting_to_job("x.example", "X", "https://x.example/j/9", p)
    assert job["location"] == "Austin, TX, US" and job["source_id"].endswith(":JR-9")
    assert rd.read_page("nextdata", "<html>no next data</html>") is None
    seo_only = {"props": {"pageProps": {"seo": {"title": "Careers", "description": "short"}}}}
    assert rd.read_page("nextdata", f"<script id=__NEXT_DATA__>{json.dumps(seo_only)}</script>") is None
    assert rd.read_page("unknown-reader", page) is None


def test_jsonld_with_escaped_markup_inside_a_string_parses():
    """Accenture's description holds &quot; inside a JSON string: parse raw before unescaping."""
    p = cs.extract_jobposting(ACCENTURE)
    assert p and p["title"] == "Mulesoft Developer"
    job = cs.posting_to_job("www.accenture.com", "Accenture", "u", p)
    assert job and job["posted_at"] == "2026-09-29"


def test_us_style_date_posted():
    assert cs._date("10/09/2026") == "2026-10-09" and cs._date("2026-10-09T00:00") == "2026-10-09"


# --------------------------------------------------------------------------------------------- #
# the keep rule
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("title,want", [
    ("Staff, Software Engineer", True), ("Principal, Data Analyst- Sensory Science", True),
    ("Senior Product Manager", True), ("Manager, Marketing Analytics", True),
    ("Supply Chain Planning Manager", True), ("Director, Technical Program Management", True),
    ("Merchandise and Stocking Associate", False), ("Cashier", False), ("Truck Driver OTR", False),
    ("Personal Shopper - Sam's", False), ("Pharmacy Grad Intern (Hrly)", False),
    ("Data Center Technician (Part Time)", False), ("Member Assist Cart Attendant", False),
    ("Store Support Saint Cloud FL", None), ("Staff Pharmacist", None),
])
def test_decide_hourly_drops_first_then_professional_keeps(title, want):
    assert rd.decide({}, title) is want


def test_department_keeps_an_undecided_title_and_else_decides_the_rest():
    rule = WM_CFG["keep"]
    assert rd.keep_by_page(WM_CFG, {"title": "Realty Project Coach", "_department": "corporate / Home Office"})
    assert not rd.keep_by_page(WM_CFG, {"title": "Staff Pharmacist", "_department": "corporate / Retail"})
    assert not rd.keep_by_page(WM_CFG, {"title": "Technician, Facility Services", "_department": "field / Home Office"})
    assert rd.keep_by_page({"keep": {**rule, "else": "keep"}}, {"title": "Staff Pharmacist"})
    assert rd.keep_by_page({}, {"title": "Cashier"})                   # no rule: everything kept


def test_slug_rule_decides_before_any_request():
    hd = cs.SITES["careers.homedepot.com"]
    base = "https://careers.homedepot.com/job/"
    assert rd.keep_by_slug(hd, base + "23555314/senior-software-engineer-payroll-ai-remote/") is True
    assert rd.keep_by_slug(hd, base + "6946379/warehouse-associate-locust-grove-ga/") is False
    assert rd.keep_by_slug(hd, base + "6949886/customer-service-sales-marietta-ga/") is False
    assert rd.keep_by_slug(hd, base + "1/field-sales-consultant-interiors-whittier/") is False   # site word
    assert rd.keep_by_slug(hd, base + "2/store-support-saint-cloud-fl/") is False               # else: drop
    # Walmart's URL is only an id: the slug says nothing, so the page decides.
    assert rd.slug_words("https://careers.walmart.com/us/en/jobs/R-2646349") == ""
    assert rd.keep_by_slug(WM_CFG, "https://careers.walmart.com/us/en/jobs/R-2646349") is None
    assert rd.keep_by_slug({}, base + "6946379/warehouse-associate-locust-grove-ga/") is True


def test_order_urls_newest_requisition_first_without_lastmod():
    found = {f"https://careers.walmart.com/us/en/jobs/R-{n}": "" for n in (128100, 2646349, 1075582)}
    assert [u.rsplit("-", 1)[1] for u, _ in rd.order_urls(WM_CFG, found)] == ["2646349", "1075582", "128100"]
    dated = {"a": "2026-10-01", "b": "2026-10-09"}
    assert [u for u, _ in rd.order_urls({}, dated)] == ["b", "a"]


# --------------------------------------------------------------------------------------------- #
# the hook in careersite.crawl_site
# --------------------------------------------------------------------------------------------- #

H = "jobs.example.com"
ROBOTS = "User-agent: *\nDisallow: /private/\n"
SITEMAP = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://jobs.example.com/job/1/senior-data-scientist-austin-tx</loc></url>
  <url><loc>https://jobs.example.com/job/2/cashier-austin-tx</loc></url>
  <url><loc>https://jobs.example.com/job/R-3</loc></url>
  <url><loc>https://jobs.example.com/job/R-4</loc></url>
  <url><loc>https://jobs.example.com/job/R-5</loc></url>
</urlset>"""


def nd_page(title, dept="corporate / Home Office"):
    """A Next.js page in the Walmart shape."""
    jt, seg = dept.split(" / ")
    doc = {"props": {"pageProps": {"jobType": jt, "jobDetails": {
        "jobId": "R-" + title[:3], "jobPostingTitle": title, "requisitionStatus": {"id": "OPEN"},
        "jobPostingDescription": "<p>" + "Real work. " * 30 + "</p>", "businessSegment": {"value": seg},
        "primaryLocation": {"city": "BENTONVILLE", "stateCode": "AR", "countryCode": "US"},
        "jobPostingStartDate": "2026-10-01T00:00:00Z"}}}}
    return f"<html><script id=__NEXT_DATA__ type=\"application/json\">{json.dumps(doc)}</script></html>"


def jsonld_page(title):
    obj = {"@type": "JobPosting", "title": title, "description": "<p>" + "x " * 50 + "</p>",
           "identifier": {"value": "LD-" + title[:3]}, "datePosted": "2026-10-02"}
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


class Site:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, timeout=20, **_):
        import urllib.error
        self.calls.append(url)
        if url not in self.pages:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        return self.pages[url]


def pages():
    return {
        f"https://{H}/robots.txt": ROBOTS,
        f"https://{H}/sitemap.xml": SITEMAP,
        f"https://{H}/job/1/senior-data-scientist-austin-tx": jsonld_page("Senior Data Scientist"),
        f"https://{H}/job/2/cashier-austin-tx": jsonld_page("Cashier"),
        f"https://{H}/job/R-3": nd_page("Staff, Software Engineer"),          # reader, kept
        f"https://{H}/job/R-4": nd_page("Personal Shopper", "field / Retail"),   # reader, dropped
        f"https://{H}/job/R-5": "<html>no data</html>",                          # no posting
    }


def crawl(cfg, store=None, site=None):
    site = site or Site(pages())
    store = store or Watchlist(":memory:")
    s = cs.crawl_site(store, H, {"company": "Example", "sitemaps": [f"https://{H}/sitemap.xml"],
                                 "job_url": rf"^https://{H.replace('.', chr(92) + '.')}/job/", **cfg},
                      text_fetch=site, sleep=lambda _: None, clock=lambda: 0.0)
    return site, store, s


def test_no_reader_means_a_page_without_jsonld_is_skipped_as_before():
    site, store, s = crawl({})
    titles = sorted(j["title"] for j in store.list_jobs())
    assert titles == ["Cashier", "Senior Data Scientist"]
    assert s["no_posting"] == 3 and s["dropped"] == 0


def test_reader_is_the_fallback_and_jsonld_wins_when_present():
    site, store, s = crawl({"reader": "nextdata"})
    titles = sorted(j["title"] for j in store.list_jobs())
    assert titles == ["Cashier", "Personal Shopper", "Senior Data Scientist", "Staff, Software Engineer"]
    row = {j["title"]: j for j in store.list_jobs()}["Staff, Software Engineer"]
    assert row["location"] == "Bentonville, AR, US"
    assert s["no_posting"] == 1


def test_keep_rule_drops_by_slug_without_a_request_and_by_page_with_memory(tmp_path):
    cfg = {"reader": "nextdata", "keep": {"department": r"^corporate / home office\b", "else": "drop"}}
    store = Watchlist(tmp_path / "jobs.db")
    site, store, s = crawl(cfg, store=store)
    assert f"https://{H}/job/2/cashier-austin-tx" not in site.calls          # slug: no request
    assert sorted(j["title"] for j in store.list_jobs()) == ["Senior Data Scientist", "Staff, Software Engineer"]
    assert s["listed"] == 4 and s["dropped"] == 2 and s["no_posting"] == 1
    # Next run: kept pages are known, the dropped and the empty page are remembered: no page read.
    site2, _, s2 = crawl(cfg, store=store)
    assert [u for u in site2.calls if "/job/" in u] == []
    assert s2["fetched"] == 0 and s2["known"] == 2


def test_skip_memory_expires(tmp_path):
    store = Watchlist(tmp_path / "jobs.db")
    mem = rd.SkipMemory(store, days=30)
    mem.remember(H, "https://a", "Cashier")
    assert mem.recent(["https://a", "https://b"]) == {"https://a"}
    store._conn.execute("UPDATE careersite_skip SET at='2020-01-01'")
    store._conn.commit()
    assert rd.SkipMemory(store).recent(["https://a"]) == set()


def test_new_site_entries_are_well_formed():
    import re
    for host in ("careers.walmart.com", "careers.homedepot.com", "careers.bankofamerica.com", "www.accenture.com"):
        cfg = cs.SITES[host]
        assert cfg["job_url"].startswith("^https://" + re.escape(host) + "/"), host
        assert all(sm.startswith(f"https://{host}/") for sm in cfg["sitemaps"]), host
    assert WM_CFG["reader"] in rd.READERS
    assert re.match(WM_CFG["job_url"], "https://careers.walmart.com/us/en/jobs/R-2646349")
