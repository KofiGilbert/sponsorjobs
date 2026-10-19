"""Growing our OWN sources toward sponsoring employers (2026-10-18).

Discovery (sourcing/discover.py + sourcing/growth.py + scripts/grow_watchlist.py) now:
  * finds cap-exempt institutions whose names carry no brand word of their own ("University Of
    Michigan" was skipped outright), matching them on the WHOLE institution phrase, so a
    different school that shares a word never passes;
  * ranks by H-1B approvals plus PERM, with likely cap-exempt employers boosted and flagged;
  * marks a host that answers 401 / 403 / a challenge page as blocked and never asks it again,
    and obeys each ATS API's robots.txt (an ATS whose robots.txt disallows its API is left out);
  * records evidence for every verified board (URL, verified date, job count, cap-exempt).
Everything runs against fakes; nothing touches the network.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import re
import urllib.error
from pathlib import Path

import pytest

from sourcing import growth
from sourcing.discover import (confirms, discover_for_company, institution_alias,
                               institution_phrase, slug_candidates)

ROOT = Path(__file__).resolve().parents[1]
_JOB = [{"id": 1, "title": "Research Scientist", "location": {"name": "Ann Arbor, MI"}}]


def _http(code, url="https://x"):
    return urllib.error.HTTPError(url, code, "x", {}, None)


def _gh(jobs=None, names=None):
    """A fake Greenhouse (the name lane): unknown slugs answer 404."""
    jobs, names = jobs or {}, names or {}

    def fetch(url, timeout=20, **kw):
        m = re.search(r"boards-api\.greenhouse\.io/v1/boards/([^/?]+)(/jobs)?", url)
        if m and m.group(1) in jobs:
            return {"jobs": jobs[m.group(1)]} if m.group(2) else {"name": names.get(m.group(1), "")}
        raise _http(404, url)
    return fetch


# -- cap-exempt institutions: the whole phrase is the brand --------------------- #

def test_institution_phrase_cleans_filing_wrappers_and_abbreviations():
    assert institution_phrase("Regents Of The University Of Michigan") == "university of michigan"
    assert institution_phrase("Univ. of Michigan") == "university of michigan"
    assert institution_phrase("The University Of Chicago") == "university of chicago"
    assert institution_phrase("Massachusetts Inst Of Technology") == "massachusetts institute of technology"
    assert institution_phrase("Datadog Inc") == ""


def test_a_brandless_university_now_gets_guesses_it_never_had():
    # Generic word + a place: no distinctive token, so it used to be skipped without a request.
    slugs, brands = institution_alias("University Of Michigan")
    assert slugs[0] == "umich" and brands[0] == "university of michigan"
    assert slug_candidates("University Of Michigan")[:1] == ["umich"]
    assert "usc" in slug_candidates("University Of Southern California")
    assert "osu" in slug_candidates("Ohio State University")
    assert "mit" in slug_candidates("Massachusetts Inst Of Technology")
    assert growth.should_skip("University Of Michigan") is None
    # Names that already carry a brand word keep the ordinary rules.
    assert institution_alias("Yale University") == ([], [])
    assert institution_alias("Mayo Clinic") == ([], [])


def test_a_filing_body_word_is_never_guessed_alone():
    # "regents" / "trustees" alone could be any school's board.
    assert "regents" not in slug_candidates("Regents Of The University Of Michigan")
    assert "trustees" not in slug_candidates("The Trustees Of Princeton Univ")
    assert "princeton" in slug_candidates("The Trustees Of Princeton Univ")


@pytest.mark.parametrize("board,sponsor,ok", [
    ("The University of Chicago", "The University Of Chicago", True),
    ("Loyola University Chicago", "The University Of Chicago", False),
    ("City of Chicago", "The University Of Chicago", False),
    ("University of Washington", "Washington University In St Louis", False),
    ("Washington University in St. Louis", "Washington University In St Louis", True),
    ("Michigan State University", "University Of Michigan", False),
    ("University of Michigan", "Regents Of The University Of Michigan", True),
    ("Regents of the University of California", "Regents Of The University Of Michigan", False),
    ("University of Wisconsin-Madison", "Univ Of Wi System/Madison", True),
    ("Madison College", "Univ Of Wi System/Madison", False),
])
def test_institution_identity_needs_the_whole_phrase(board, sponsor, ok):
    assert confirms(board, sponsor) is ok


def test_discovery_accepts_the_right_school_and_rejects_a_namesake():
    fetch = _gh(jobs={"uchicago": _JOB, "chicago": _JOB},
                names={"uchicago": "The University of Chicago", "chicago": "City of Chicago"})
    hit = discover_for_company("The University Of Chicago", atss=("greenhouse",), fetch=fetch)
    assert hit and hit["board_id"] == "uchicago"
    only_city = _gh(jobs={"chicago": _JOB}, names={"chicago": "City of Chicago"})
    assert discover_for_company("The University Of Chicago", atss=("greenhouse",), fetch=only_city) is None


# -- cap-exempt flag and ranking ------------------------------------------------ #

def _row(name, h1b=100, last_fy=2023, perm=0, cap=0, naics=""):
    return {"norm_name": growth.normalize_employer(name), "display_name": name,
            "h1b_approvals": h1b, "h1b_last_fy": last_fy, "perm_certs": perm,
            "cap_exempt": cap, "naics": naics}


@pytest.mark.parametrize("row,expected", [
    (_row("University Of Michigan"), True),                       # the name says so
    (_row("Mayo Clinic", cap=1, naics="61"), True),               # flagged, no legal form
    (_row("Battelle Memorial Institute", cap=1, naics="54"), True),
    (_row("Microsoft Corporation", cap=1, naics="51"), False),    # flag set by a variant
    (_row("Snap Inc", cap=1, naics="51"), False),
    (_row("Acme Learning Inc", cap=1, naics="61"), False),        # for-profit school company
    (_row("Datadog Inc"), False),
])
def test_cap_exempt_likely(row, expected):
    assert growth.cap_exempt_likely(row) is expected


def test_ranking_counts_perm_and_boosts_cap_exempt_employers():
    rows = [_row("Big Corp Inc", h1b=250), _row("University Of Michigan", h1b=100),
            _row("Perm Heavy LLC", h1b=0, perm=220), _row("Small Shop LLC", h1b=10)]
    ranked = growth.rank_sponsors(rows)
    assert [r["display_name"] for r in ranked] == [
        "University Of Michigan", "Big Corp Inc", "Perm Heavy LLC", "Small Shop LLC"]
    assert ranked[0]["score"] == 300 and ranked[0]["cap_exempt_likely"] is True
    flat = growth.rank_sponsors(rows, cap_boost=1.0)
    assert flat[0]["display_name"] == "Big Corp Inc"
    only = growth.rank_sponsors(rows, cap_exempt_only=True)
    assert [r["display_name"] for r in only] == ["University Of Michigan"]


# -- blocked hosts: 401 / 403 / challenge, never asked again --------------------- #

class Recorder:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def __call__(self, url, timeout=20, **kw):
        self.urls.append(url)
        for pattern, answer in self.answers:
            if pattern in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise _http(404, url)


def _pf(fetch, **kw):
    return growth.PoliteFetch(fetch=fetch, sleep=lambda s: None, clock=lambda: 0.0, **kw)


def test_a_tenant_that_answers_403_is_blocked_at_once_and_never_asked_again():
    tenant = "https://acme.wd5.myworkdayjobs.com/robots.txt"
    rec = Recorder([("acme.wd5", _http(403))])
    pf = _pf(rec)
    pf.begin(("workday",))
    with pytest.raises(urllib.error.HTTPError):
        pf(tenant, raw=True)
    assert pf.refused_hosts == ["acme.wd5.myworkdayjobs.com"]
    assert pf.blocked_ats == set()        # one tenant refusing says nothing about the Workday lane
    with pytest.raises(growth.HostBlocked):
        pf(tenant, raw=True)
    assert len(rec.urls) == 1             # the second call never left the machine


def test_a_shared_api_host_is_blocked_only_after_repeated_refusals_and_its_ats_is_owed():
    rec = Recorder([("boards-api.greenhouse.io", _http(403))])
    pf = _pf(rec)
    pf.begin(("greenhouse",))
    with pytest.raises(urllib.error.HTTPError):
        pf("https://boards-api.greenhouse.io/v1/boards/a/jobs")
    assert "boards-api.greenhouse.io" not in pf.blocked_sites
    assert pf.blocked_ats == {"greenhouse"}          # owed for this employer, not a miss
    with pytest.raises(urllib.error.HTTPError):
        pf("https://boards-api.greenhouse.io/v1/boards/b/jobs")
    assert pf.blocked_sites["boards-api.greenhouse.io"] == "HTTP 403"
    with pytest.raises(growth.HostBlocked):
        pf("https://boards-api.greenhouse.io/v1/boards/c/jobs")
    assert len(rec.urls) == 2


def test_a_challenge_page_or_non_json_answer_counts_as_a_refusal():
    page = "<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>"
    rec = Recorder([("acme.wd1", page)])
    pf = _pf(rec)
    pf.begin(("workday",))
    with pytest.raises(growth.HostBlocked):
        pf("https://acme.wd1.myworkdayjobs.com/robots.txt", raw=True)
    assert pf.refused_hosts == ["acme.wd1.myworkdayjobs.com"]

    def html_not_json(url, timeout=20, **kw):
        raise json.JSONDecodeError("Expecting value", "<html>", 0)
    pf2 = _pf(html_not_json)
    pf2.begin(("workday",))
    with pytest.raises(json.JSONDecodeError):
        pf2("https://beta.wd3.myworkdayjobs.com/wday/cxs/beta/External/jobs")
    assert "beta.wd3.myworkdayjobs.com" in pf2.blocked_sites


def test_a_blocked_host_is_checkpointed_and_skipped_by_the_next_run(tmp_path):
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    rec = Recorder([("acme.wd5", _http(403))])
    pf = _pf(rec)

    def discover(name, atss, fetch, reject=None):
        try:
            fetch("https://acme.wd5.myworkdayjobs.com/robots.txt", raw=True)
        except Exception:
            pass
        return None

    s = growth.grow([_row("Acme Widgets Inc")], cp, pf, atss=("workday",), workers=1,
                    discover=discover)
    assert s["blocked"] == [{"company": "Acme Widgets Inc", "host": "acme.wd5.myworkdayjobs.com",
                             "why": "HTTP 403"}]
    assert growth.Checkpoint(cp.path).blocked_hosts() == {"acme.wd5.myworkdayjobs.com"}
    rec2 = Recorder([])
    pf2 = _pf(rec2, blocked_hosts=growth.Checkpoint(cp.path).blocked_hosts())
    with pytest.raises(growth.HostBlocked):
        pf2("https://acme.wd5.myworkdayjobs.com/robots.txt", raw=True)
    assert rec2.urls == []


# -- robots.txt of the ATS APIs --------------------------------------------------- #

_ROBOTS = [
    ("api.smartrecruiters.com/robots.txt",
     "User-agent: LinkedInBot\nAllow: /v1/companies/\nUser-agent: *\nDisallow: /\n"),
    ("api.lever.co/robots.txt", "User-agent: *\nAllow: /\nCrawl-delay: 1\n"),
    ("boards-api.greenhouse.io/robots.txt", "User-agent: *\nDisallow: /embed/\n"),
    ("api.ashbyhq.com/robots.txt", _http(401)),       # no robots file: everything allowed
    ("apply.workable.com/robots.txt", "User-agent: *\nDisallow: \n"),
]


def test_an_ats_whose_robots_txt_disallows_its_api_is_left_out_of_the_run():
    rec = Recorder(_ROBOTS)
    pf = _pf(rec, robots=True)
    # SmartRecruiters is also off in code (ats.DISABLED_ATS) and so not in DISCOVER_ATS; ask about
    # it explicitly to pin that the robots.txt read alone reaches the same answer.
    every = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday")
    ok, denied = pf.allowed_atss(every)
    assert ok == ["greenhouse", "lever", "ashby", "workable", "workday"]
    assert set(denied) == {"smartrecruiters"} and "disallows" in denied["smartrecruiters"]
    assert pf.delay_for("api.lever.co") >= 1.0       # Crawl-delay honoured
    n = len(rec.urls)
    pf.allowed_atss(every)                           # each robots.txt is read once per run
    assert len(rec.urls) == n
    with pytest.raises(growth.HostBlocked):          # and a stray request is never made
        pf("https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1")
    assert not any("companies/acme" in u for u in rec.urls)


def test_an_unreadable_robots_txt_owes_the_request_rather_than_guessing():
    rec = Recorder([("boards-api.greenhouse.io/robots.txt", _http(503))])
    pf = _pf(rec, robots=True)
    pf.begin(("greenhouse",))
    with pytest.raises(growth.HostBlocked):
        pf("https://boards-api.greenhouse.io/v1/boards/a/jobs")
    assert pf.blocked_ats == {"greenhouse"}
    assert not any("/v1/boards/a" in u for u in rec.urls)


# -- evidence ----------------------------------------------------------------------- #

def test_evidence_rows_merge_by_board_and_record_url_date_jobs_and_cap_exempt(tmp_path):
    p = tmp_path / "ev.csv"
    hit = {"ats": "workday", "board_id": "umich.wd5/Careers", "jobs": 812, "company": "University Of Michigan"}
    row = _row("University Of Michigan", h1b=1491, perm=19)
    ev = growth.evidence_row(hit, row, today="2026-10-18")
    assert ev == {"ats": "workday", "board_id": "umich.wd5/Careers", "company": "University Of Michigan",
                  "url": "https://umich.wd5.myworkdayjobs.com/Careers", "jobs": 812,
                  "verified": "2026-10-18", "cap_exempt": 1, "h1b_approvals": 1491, "perm_certs": 19}
    growth.write_evidence(p, [ev, {**ev, "board_id": "evil.com/x"}])
    growth.write_evidence(p, [{**ev, "jobs": 900, "verified": "2026-11-01"}])   # re-verified
    rows = growth.read_evidence(p)
    assert len(rows) == 1 and rows[0]["jobs"] == "900" and rows[0]["verified"] == "2026-11-01"
    assert growth.board_url("greenhouse", "datadog") == "https://boards-api.greenhouse.io/v1/boards/datadog/jobs"


def test_committed_evidence_only_names_boards_on_the_watchlist():
    seed = {(r["ats"], r["board_id"]) for r in growth.read_watchlist_seed(growth.WATCHLIST_SEED)}
    for r in growth.read_evidence(growth.WATCHLIST_EVIDENCE):
        assert (r["ats"], r["board_id"]) in seed
        assert r["url"].startswith("https://") and int(r["jobs"]) >= 1
        assert r["cap_exempt"] in ("0", "1") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["verified"])


def test_the_script_writes_the_seed_and_the_evidence_with_the_cap_exempt_flag(tmp_path, monkeypatch, capsys):
    sponsors = tmp_path / "sponsors.csv.gz"
    with gzip.open(sponsors, "wt", newline="") as f:
        f.write("norm_name,display_name,h1b_approvals,h1b_last_fy,h1b_first_fy,naics,state,"
                "cap_exempt,e_verify,perm_certs\n"
                "datadog,Datadog Inc,300,2023,2020,51,NY,0,0,0\n"
                "university of chicago,The University Of Chicago,120,2023,2020,61,IL,1,0,10\n")
    seed = tmp_path / "watchlist.csv"
    seed.write_text("company,ats,board_id\n")
    evidence = tmp_path / "evidence.csv"
    fetch = growth.PoliteFetch(fetch=_gh(jobs={"datadog": _JOB, "uchicago": _JOB},
                                         names={"datadog": "Datadog",
                                                "uchicago": "The University of Chicago"}),
                               sleep=lambda s: None)
    monkeypatch.setattr(growth, "PoliteFetch", lambda **kw: fetch)
    spec = importlib.util.spec_from_file_location("grow_watchlist", ROOT / "scripts" / "grow_watchlist.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    rc = script.main(["--sponsors", str(sponsors), "--seed", str(seed), "--evidence", str(evidence),
                      "--checkpoint", str(tmp_path / "cp.jsonl"), "--limit", "5", "--workers", "1",
                      "--ats", "greenhouse"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 of them likely cap-exempt" in out
    assert "1 of the new boards are likely cap-exempt" in out
    assert {(r["ats"], r["board_id"]) for r in growth.read_watchlist_seed(seed)} == {
        ("greenhouse", "datadog"), ("greenhouse", "uchicago")}
    ev = {r["board_id"]: r for r in growth.read_evidence(evidence)}
    assert ev["uchicago"]["cap_exempt"] == "1" and ev["datadog"]["cap_exempt"] == "0"
    assert ev["uchicago"]["url"] == "https://boards-api.greenhouse.io/v1/boards/uchicago/jobs"
    assert ev["uchicago"]["jobs"] == "1" and ev["uchicago"]["perm_certs"] == "10"


def test_a_phrase_that_names_no_particular_institution_is_not_an_alias():
    assert institution_alias("Childrens Hospital") == ([], [])
    assert institution_alias("Community Medical Center") == ([], [])
    assert slug_candidates("Childrens Hospital") == []


def test_a_k12_school_district_is_not_boosted_as_cap_exempt():
    assert growth.cap_exempt_likely(_row("Dallas Independent School District", cap=1, naics="61")) is False
    assert growth.cap_exempt_likely(_row("Savannah Chatham County Public Sch", cap=1, naics="61")) is False


def test_a_universitys_student_jobs_site_is_not_a_board_for_outside_applicants():
    from sourcing.workday import prefer_sites
    assert prefer_sites(["UVAStudentJobs", "UVAJobs"]) == ["UVAJobs"]
    assert prefer_sites(["Student_Employment", "External"]) == ["External"]


def test_a_workday_posting_at_a_campus_building_is_placed_by_its_own_country():
    # Live 2026-10-18: upenn.wd1 lists "Franklin Building", which the feed's US rule cannot place.
    from sourcing.workday import fill_details, workday_detail
    detail = {"jobPostingInfo": {"location": "Franklin Building", "jobDescription": "<p>Role</p>",
                                 "country": {"descriptor": "United States of America", "alpha2Code": "US"}},
              "hiringOrganization": {"name": "University of Pennsylvania"}}
    fetch = lambda url, **kw: detail
    assert workday_detail("https://upenn.wd1.myworkdayjobs.com/wday/cxs/upenn/x/job/1", fetch,
                          sleep=lambda s: None)["location"] == "Franklin Building, United States"
    row = {"url": "https://upenn.wd1.myworkdayjobs.com/careers-at-penn/job/Franklin-Building/X_1",
           "location": "Franklin Building", "source_id": "workday:x"}
    fill_details([row], fetch, sleep=lambda s: None)
    assert row["location"] == "Franklin Building, United States"
    abroad = {"jobPostingInfo": {"location": "Dortmund", "country": {"alpha2Code": "DE"}}}
    assert workday_detail("https://x.wd1.myworkdayjobs.com/wday/cxs/x/y/job/1", lambda u, **k: abroad,
                          sleep=lambda s: None)["location"] == "Dortmund"
