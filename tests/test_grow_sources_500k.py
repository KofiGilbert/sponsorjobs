"""Growing the board toward 500k honestly (2026-10-23): more rows from the sources we already
read, under the same quality rules. Pinned here, all offline (fakes and trimmed fixtures):

* Phenom career sites whose JSON-LD script is empty are read from the page's own `phApp.ddo`
  (sourcing/careersite_readers.read_phenom), and every `platform: phenom` site gets that reader
  without a per-site `reader` key.
* Workday boards are listed US-only when the board offers a US country facet, so the per-board
  cap and the description budget go to roles the US-only feed can publish; rows the board's own
  country filter returned get ", United States" when their campus / city name reads as nothing.
* A stored location the US rule cannot place gives way to a list location it can.
* A short Workday description budget is spent on US-readable rows before clearly foreign ones.
* The feed lists Workday boards up to Workday's 2000-row ceiling.
* Career-site discovery can continue past the ranks an earlier run probed (`--start`).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from sourcing import careersite as cs
from sourcing import careersite_readers as rd
from sourcing import workday
from sourcing.service import select_checked_rows
from sourcing.sponsors import looks_us, names_foreign
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "careersite"
STATESTREET = (FIX / "statestreet_R-800179.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------- #
# Phenom reader
# --------------------------------------------------------------------------------------------- #

def test_phenom_page_has_an_empty_jsonld_block_and_the_reader_finds_the_posting():
    assert cs.extract_jobposting(STATESTREET) is None            # the empty ld+json script
    p = rd.read_phenom(STATESTREET)
    assert p["@type"] == "JobPosting"
    assert p["title"] == "Alternative Ops Fund Accounting, Officer"
    assert p["hiringOrganization"]["name"] == "State Street"
    assert p["datePosted"] == "2026-10-23"
    assert p["identifier"]["value"] == "R-800179"
    job = cs.posting_to_job("careers.statestreet.com", "State Street",
                            "https://careers.statestreet.com/global/en/job/STSTGLOBALR800179EXTERNALENGLOBAL/x", p)
    assert job["location"] == "Hangzhou, Zhejiang, China" and not looks_us(job["location"])
    assert job["source_id"] == "careersite:careers.statestreet.com:R-800179"
    assert "fund account" in job["jd_text"].lower()


def _ddo_page(job: dict, status=200) -> str:
    ddo = {"jobDetail": {"status": status, "data": {"job": job}}}
    return f'<script type="application/ld+json"></script><script>phApp.ddo = {json.dumps(ddo)}; phApp.x = 1;</script>'


def test_phenom_reader_us_job_and_the_misses():
    page = _ddo_page({"title": "Data Engineer", "description": "<p>" + "Build pipelines. " * 20 + "</p>",
                      "companyName": "Merck", "city": "Rahway", "state": "New Jersey",
                      "country": "United States of America", "postedDate": "2026-10-20T00:00:00.000+0000",
                      "jobId": "R1"})
    job = cs.posting_to_job("jobs.merck.com", "Merck", "https://jobs.merck.com/us/en/job/R1/x", rd.read_phenom(page))
    assert job["location"].startswith("Rahway, New Jersey") and looks_us(job["location"])
    assert rd.read_phenom(_ddo_page({"title": "X", "description": "d"}, status=404)) is None
    assert rd.read_phenom(_ddo_page({"description": "no title"})) is None
    assert rd.read_phenom("<html>no ddo</html>") is None
    assert rd.read_phenom("<script>phApp.ddo = {broken</script>") is None


def test_every_phenom_site_gets_the_reader_and_an_explicit_reader_wins():
    assert rd.reader_for({"platform": "phenom"}) == "phenom"
    assert rd.reader_for({"platform": "phenom", "reader": "nextdata"}) == "nextdata"
    assert rd.reader_for({"platform": "eightfold"}) == ""
    phenom_sites = [h for h, c in cs.SITES.items() if c.get("platform") == "phenom"]
    assert "careers.statestreet.com" in phenom_sites


def test_crawl_site_reads_a_phenom_page_without_a_reader_key():
    H = "careers.statestreet.com"
    url = f"https://{H}/global/en/job/STSTGLOBALR800179EXTERNALENGLOBAL/x"
    pages = {f"https://{H}/robots.txt": "User-agent: *\nAllow: /\n",
             f"https://{H}/sitemap.xml": f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{url}</loc></url></urlset>',
             url: STATESTREET}

    def fetch(u, timeout=20, **_):
        return pages[u]

    store = Watchlist(":memory:")
    cfg = {"company": "State Street", "platform": "phenom", "sitemaps": [f"https://{H}/sitemap.xml"],
           "job_url": f"^https://{H}/global/en/job/"}
    s = cs.crawl_site(store, H, cfg, text_fetch=fetch, sleep=lambda *_: None, max_detail=5)
    assert (s["fetched"], s["kept"], s["no_posting"]) == (1, 1, 0)
    assert store.list_jobs()[0]["title"] == "Alternative Ops Fund Accounting, Officer"


# --------------------------------------------------------------------------------------------- #
# Workday: the US country facet
# --------------------------------------------------------------------------------------------- #

US_ID = "bc33aa3152ec42d4995f4791a106ed09"


def _facets(us_count, param="locationCountry", nested=False):
    country = {"facetParameter": param, "values": [
        {"descriptor": "India", "id": "in1", "count": 50},
        {"descriptor": "United States of America", "id": US_ID, "count": us_count}]}
    if nested:
        return [{"facetParameter": "locationMainGroup", "values": [country]}]
    return [{"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Eng", "id": "e", "count": 9}]}, country]


def test_us_country_facet_finds_the_value_flat_nested_and_hierarchy():
    assert workday.us_country_facet(_facets(30)) == ("locationCountry", US_ID, 30)
    assert workday.us_country_facet(_facets(30, "Location_Country", nested=True)) == ("Location_Country", US_ID, 30)
    nv = [{"facetParameter": "locationHierarchy1", "values": [{"descriptor": "United States", "id": "u", "count": 1425}]}]
    assert workday.us_country_facet(nv) == ("locationHierarchy1", "u", 1425)
    # DuPont's `locations` facet has one OFFICE called "United States": not a country filter.
    dupont = [{"facetParameter": "locations", "values": [{"descriptor": "United States", "id": "x", "count": 1}]}]
    assert workday.us_country_facet(dupont) is None
    assert workday.us_country_facet(None) is None


class FacetBoard:
    """A Workday list endpoint with 6 rows (4 in the US) and a country facet."""

    ROWS = [("Austin, TX", True), ("Pune, India", False), ("Springfield", True),
            ("3 Locations", True), ("London, UK", False), ("MN-Champlin", True)]

    def __init__(self, offer_facet=True):
        self.offer_facet, self.bodies = offer_facet, []

    def __call__(self, url, data=None, **_):
        self.bodies.append(data)
        applied = (data or {}).get("appliedFacets") or {}
        rows = [(i, loc) for i, (loc, us) in enumerate(self.ROWS) if us or not applied]
        off, lim = data["offset"], data["limit"]
        page = [{"title": f"Role {i}", "externalPath": f"/job/x/Role_JR{i}", "locationsText": loc,
                 "postedOn": "Posted Today", "bulletFields": [f"JR{i}"]} for i, loc in rows[off:off + lim]]
        out = {"total": len(rows), "jobPostings": page}
        if self.offer_facet:
            out["facets"] = _facets(4)
        return out


def test_workday_list_is_us_only_when_the_board_offers_the_facet(monkeypatch):
    monkeypatch.delenv("WORKDAY_US_ONLY", raising=False)
    board = FacetBoard()
    rows = workday.workday_jobs("acme", "External", "acme.wd1.myworkdayjobs.com", "Acme", fetch=board,
                                sleep=lambda *_: None)
    locs = [r["location"] for r in rows]
    assert locs == ["Austin, TX", "Springfield, United States", "3 Locations", "MN-Champlin, United States"]
    assert board.bodies[0]["appliedFacets"] == {}
    assert board.bodies[1]["appliedFacets"] == {"locationCountry": [US_ID]}
    assert all(looks_us(l) for l in locs if l != "3 Locations")


def test_workday_list_unchanged_without_a_facet_or_when_switched_off(monkeypatch):
    board = FacetBoard(offer_facet=False)
    rows = workday.workday_jobs("acme", "External", "acme.wd1.myworkdayjobs.com", fetch=board, sleep=lambda *_: None)
    assert [r["location"] for r in rows] == [loc for loc, _ in FacetBoard.ROWS]
    assert len(board.bodies) == 1
    monkeypatch.setenv("WORKDAY_US_ONLY", "0")
    board = FacetBoard()
    rows = workday.workday_jobs("acme", "External", "acme.wd1.myworkdayjobs.com", fetch=board, sleep=lambda *_: None)
    assert len(rows) == 6 and all(b["appliedFacets"] == {} for b in board.bodies)


def test_place_in_us_leaves_foreign_us_and_multi_location_rows_alone():
    assert workday.place_in_us("101 Corsair Hall") == "101 Corsair Hall, United States"
    assert workday.place_in_us("Tokyo, Japan") == "Tokyo, Japan"          # matched via another office
    assert workday.place_in_us("Seattle, WA") == "Seattle, WA"
    assert workday.place_in_us("12 Locations") == "12 Locations"
    assert workday.place_in_us("") == "United States"
    assert names_foreign("Bengaluru") and not names_foreign("Springfield")


# --------------------------------------------------------------------------------------------- #
# stored locations and the description budget
# --------------------------------------------------------------------------------------------- #

def _wd_row(rid, loc, jd=""):
    return {"source_id": f"workday:acme.wd1/External:{rid}", "source": "workday", "company": "Acme",
            "title": "Engineer", "location": loc, "remote": "", "url": f"https://acme.wd1.myworkdayjobs.com/External/job/Austin/Engineer_{rid}",
            "jd_text": jd, "posted_at": "2026-10-20", "salary": ""}


def test_a_us_list_location_replaces_a_stored_one_the_rule_cannot_place():
    store = Watchlist(":memory:")
    store.upsert_jobs([_wd_row("A", "Springfield", "full description"),
                       _wd_row("B", "Austin, TX; Remote", "full description")])
    store.upsert_jobs([_wd_row("A", "Springfield, United States"), _wd_row("B", "Austin")])
    got = {j["source_id"][-1]: j["location"] for j in store.list_jobs()}
    assert got == {"A": "Springfield, United States", "B": "Austin, TX; Remote"}   # B keeps the richer one


def test_short_budget_reads_us_rows_before_foreign_ones():
    rows = [_wd_row("1", "London, UK"), _wd_row("2", "Pune, India"), _wd_row("3", "Austin, TX"),
            _wd_row("4", "Springfield")]
    rows[0]["posted_at"] = "2026-10-22"                         # the newest is abroad
    read = []

    def fetch(url, **_):
        read.append(url.rsplit("_", 1)[-1])
        return {"jobPostingInfo": {"jobDescription": "<p>Fine role.</p>", "location": "x"}}

    kept, fetched, left = select_checked_rows("workday", rows, {}, fetch=fetch, max_detail=2, sleep=lambda *_: None)
    assert fetched == 2 and left == 2
    assert sorted(read) == ["3", "4"] and {k["source_id"][-1] for k in kept} == {"3", "4"}


# --------------------------------------------------------------------------------------------- #
# feed size and discovery
# --------------------------------------------------------------------------------------------- #

def test_feed_lists_workday_boards_to_the_ceiling():
    wf = (ROOT / ".github" / "workflows" / "feed.yml").read_text()
    assert "vars.WORKDAY_MAX_JOBS || 25000" in wf and "vars.JOBS_MAX_PER_COMPANY || 25000" in wf


def test_discovery_start_skips_the_ranks_already_probed(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("dcs", ROOT / "scripts" / "discover_careersites.py")
    dcs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dcs)
    names = [{"key": f"e{i}", "name": f"E{i}", "approvals": 1, "hosts": []} for i in range(10)]
    seen = {}
    monkeypatch.setattr(dcs, "pick_employers", lambda rows, watch, sites, limit: names[:limit])
    monkeypatch.setattr(dcs, "discover", lambda todo, *a, **k: seen.setdefault("todo", todo) and {})
    monkeypatch.setattr(dcs, "load_sites", lambda: {})
    import sourcing.growth as g
    monkeypatch.setattr(g, "load_sponsor_seed", lambda: [])
    monkeypatch.setattr(g, "read_watchlist_seed", lambda: [])
    dcs.main(["--start", "6", "--limit", "3", "--checkpoint", str(tmp_path / "cp.json"), "--minutes", "0"])
    assert [e["key"] for e in seen["todo"]] == ["e6", "e7", "e8"]


def test_discovery_treats_a_cut_off_body_as_a_network_error(monkeypatch):
    """A chunked sitemap cut off mid-read (http.client.IncompleteRead) killed the whole 2026-10-23
    discovery run; it is now an OSError, which the prober records for that host only."""
    import http.client

    import pytest
    spec = importlib.util.spec_from_file_location("dcs2", ROOT / "scripts" / "discover_careersites.py")
    dcs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dcs)

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(dcs._OPENER, "open", lambda req, timeout=0: Resp())
    with pytest.raises(OSError):
        dcs.http_get("https://careers.example.com/sitemap.xml")
