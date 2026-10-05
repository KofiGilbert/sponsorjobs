"""Tests for Phase 2 Stage 1 — watchlist job sourcing (GREEN lane).

Adapters are exercised with canned JSON (an injected fetch), so no network is
touched. Covers normalization, filtering, dedupe/new flags, and the compliance
guard that no RED-lane code exists in the sourcing package.
"""

from __future__ import annotations

import re
from pathlib import Path

from sourcing.ats import (adzuna_jobs, ashby_jobs, greenhouse_jobs, html_to_text,
                          jsearch_jobs, lever_jobs, recruitee_jobs, remotive_jobs,
                          smartrecruiters_jobs, workable_jobs)
from sourcing.service import matches_criteria, refresh_watchlist
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]

# -- canned feed payloads ------------------------------------------------ #
GH = {"jobs": [
    {"id": 1, "title": "Senior Software Engineer", "location": {"name": "Remote - US"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
     "content": "<p>Build <b>AI</b> systems.</p><ul><li>Python</li></ul>",
     "first_published": "2026-07-01"},
    {"id": 2, "title": "Office Manager", "location": {"name": "New York, NY"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "content": "Manage the office"},
]}
LEVER = [
    {"id": "abc", "text": "Backend Engineer", "categories": {"location": "Toronto"},
     "workplaceType": "remote", "descriptionPlain": "Backend role.",
     "hostedUrl": "https://jobs.lever.co/foo/abc", "createdAt": 1751328000000},
]
ASHBY = {"jobs": [
    {"id": "z1", "title": "ML Engineer", "location": "Chicago, IL", "isRemote": False,
     "workplaceType": "Hybrid", "jobUrl": "https://jobs.ashbyhq.com/bar/z1",
     "descriptionPlain": "Train models.", "publishedAt": "2026-06-15T00:00:00Z"},
]}


# Real Workable widget shape: flat city/state/country + a `telecommuting` flag.
WORKABLE = {"jobs": [
    {"title": "AI Engineer", "shortcode": "AB12", "telecommuting": True,
     "country": "Pakistan", "city": "", "state": "",
     "url": "https://apply.workable.com/j/AB12", "published_on": "2026-06-10",
     "description": "<p>Do <b>AI</b>.</p>"},
]}
RECRUITEE = {"offers": [
    {"id": 7, "title": "ML Engineer", "city": "Berlin", "country": "Germany",
     "remote": True, "careers_url": "https://acme.recruitee.com/o/ml-engineer",
     "published_at": "2026-05-20T00:00:00.000Z", "description": "<p>Train models.</p>"},
]}
SR_LIST = {"content": [
    {"id": "p1", "name": "Data Engineer",
     "location": {"city": "Chicago", "region": "IL", "country": "us", "remote": False},
     "releasedDate": "2026-06-01T00:00:00.000Z",
     # In the real API this is the self-link (raw JSON), NOT a page to show the person.
     "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/p1"},
]}
SR_DETAIL = {"postingUrl": "https://jobs.smartrecruiters.com/Acme/p1-data-engineer",
             "jobAd": {"sections": {
                 "jobDescription": {"text": "<p>Build pipelines.</p>"},
                 "qualifications": {"text": "<ul><li>SQL</li></ul>"}}}}
REMOTIVE = {"jobs": [
    {"id": 555, "title": "Senior Software Engineer", "company_name": "RemoteCo",
     "candidate_required_location": "USA", "publication_date": "2026-07-02T10:00:00",
     "url": "https://remotive.com/remote-jobs/software-dev/senior-swe-555",
     "description": "<p>Remote engineering.</p>"},
]}
ADZUNA = {"results": [
    {"id": "az1", "title": "Data Engineer", "company": {"display_name": "Acme Data"},
     "location": {"display_name": "Chicago, IL"}, "created": "2026-07-03T09:00:00Z",
     "redirect_url": "https://www.adzuna.com/land/ad/az1",
     "description": "<p>Build <b>pipelines</b>.</p>"},
]}


JSEARCH = {"data": [
    {"job_id": "js1", "job_title": "Data Scientist", "employer_name": "Acme AI",
     "job_city": "Austin", "job_state": "Texas", "job_country": "US", "job_is_remote": False,
     "job_apply_link": "https://acme.com/apply/1", "job_description": "<p>Do <b>ML</b>.</p>",
     "job_posted_at_datetime_utc": "2026-08-01T00:00:00.000Z"},
]}


def _fetch(payload):
    return lambda url, **k: payload


def _dispatch(mapping):
    """A url-aware fake fetch: returns the first payload whose key is a substring of
    the requested URL (insertion order — put more specific keys first)."""
    def f(url, **k):
        for key, payload in mapping.items():
            if key in url:
                return payload
        return {}
    return f


# -- adapters ------------------------------------------------------------ #

def test_greenhouse_adapter_normalizes():
    jobs = greenhouse_jobs("acme", "Acme", fetch=_fetch(GH))
    j = jobs[0]
    assert j["source"] == "greenhouse"
    assert j["source_id"] == "greenhouse:acme:1"
    assert j["title"] == "Senior Software Engineer"
    assert j["location"] == "Remote - US" and j["remote"] == "remote"
    assert j["url"].endswith("/jobs/1")
    assert "AI systems" in j["jd_text"] and "<" not in j["jd_text"]   # HTML stripped


def test_lever_adapter_normalizes():
    j = lever_jobs("foo", "Foo", fetch=_fetch(LEVER))[0]
    assert j["source_id"] == "lever:foo:abc"
    assert j["title"] == "Backend Engineer" and j["location"] == "Toronto"
    assert j["remote"] == "remote"
    assert j["url"].endswith("/abc") and j["jd_text"].startswith("Backend")
    assert j["posted_at"]   # epoch -> iso date


def test_ashby_adapter_normalizes():
    j = ashby_jobs("bar", "Bar", fetch=_fetch(ASHBY))[0]
    assert j["source_id"] == "ashby:bar:z1"
    assert j["title"] == "ML Engineer" and j["remote"] == "hybrid"
    assert j["url"].endswith("/z1") and j["jd_text"] == "Train models."


def test_workable_adapter_normalizes():
    j = workable_jobs("acme", "Acme", fetch=_fetch(WORKABLE))[0]
    assert j["source_id"] == "workable:acme:AB12"
    assert j["title"] == "AI Engineer"
    assert j["remote"] == "remote"           # from the telecommuting flag
    assert j["location"] == "Pakistan"       # flat city/state/country joined
    assert j["url"].endswith("/AB12") and j["posted_at"] == "2026-06-10"
    assert "AI" in j["jd_text"] and "<" not in j["jd_text"]


def test_recruitee_adapter_normalizes():
    j = recruitee_jobs("acme", "Acme", fetch=_fetch(RECRUITEE))[0]
    assert j["source_id"] == "recruitee:acme:7"
    assert j["location"] == "Berlin, Germany" and j["remote"] == "remote"
    assert j["jd_text"] == "Train models." and j["url"].endswith("/ml-engineer")


def test_smartrecruiters_adapter_pulls_detail_jd():
    fetch = _dispatch({"postings/p1": SR_DETAIL, "postings": SR_LIST})
    j = smartrecruiters_jobs("acme", "Acme", fetch=fetch, details=True)[0]
    assert j["source_id"] == "smartrecruiters:acme:p1"
    assert j["title"] == "Data Engineer" and j["location"] == "Chicago, IL, us"
    assert "Build pipelines" in j["jd_text"] and "SQL" in j["jd_text"]   # detail sections
    assert "<" not in j["jd_text"]
    # The "View" link is the PUBLIC posting page, never the API self-link.
    assert j["url"] == "https://jobs.smartrecruiters.com/Acme/p1-data-engineer"
    assert "api.smartrecruiters.com" not in j["url"]


def test_smartrecruiters_list_only_is_one_request_and_defers_jd():
    # The DEFAULT (details=False) must not touch the per-posting detail endpoint — that's
    # what keeps a wide crawl fast. JD is empty (fetched lazily later); URL is the public one.
    calls = []
    def fetch(url):
        calls.append(url)
        return SR_LIST
    j = smartrecruiters_jobs("acme", "Acme", fetch=fetch)[0]
    assert len(calls) == 1 and "postings/p1" not in calls[0]     # list only, no detail fetch
    assert j["jd_text"] == "" and j["title"] == "Data Engineer"
    assert j["url"] == "https://jobs.smartrecruiters.com/acme/p1"  # constructed public URL


def test_adzuna_adapter_normalizes(monkeypatch):
    monkeypatch.setattr("sourcing.ats._cred",
                        lambda n: {"ADZUNA_APP_ID": "x", "ADZUNA_APP_KEY": "y"}.get(n, ""))
    jobs = adzuna_jobs("data engineer", fetch=_fetch(ADZUNA))
    assert len(jobs) == 1
    j = jobs[0]
    assert j["source"] == "adzuna" and j["source_id"] == "adzuna::az1"
    assert j["company"] == "Acme Data" and j["title"] == "Data Engineer"
    assert j["location"] == "Chicago, IL" and j["url"] == "https://www.adzuna.com/land/ad/az1"
    assert "pipelines" in j["jd_text"] and "<" not in j["jd_text"]
    assert j["posted_at"] == "2026-07-03"


def test_jsearch_adapter_normalizes(monkeypatch):
    monkeypatch.setattr("sourcing.ats._cred", lambda n: {"RAPIDAPI_KEY": "k"}.get(n, ""))
    jobs = jsearch_jobs("data scientist", fetch=_fetch(JSEARCH))
    assert len(jobs) == 1
    j = jobs[0]
    assert j["source"] == "jsearch" and j["source_id"] == "jsearch::js1"
    assert j["company"] == "Acme AI" and j["title"] == "Data Scientist"
    assert j["location"] == "Austin, Texas, US"
    assert j["url"] == "https://acme.com/apply/1" and j["posted_at"] == "2026-08-01"
    assert "ML" in j["jd_text"] and "<" not in j["jd_text"]


def test_jsearch_noops_without_key(monkeypatch):
    """No RapidAPI key -> returns [] with NO network call, so it's opt-in and harmless."""
    monkeypatch.setattr("sourcing.ats._cred", lambda n: "")
    called = []
    assert jsearch_jobs("x", fetch=lambda u, **k: called.append(u) or {"data": []}) == []
    assert not called


def test_adzuna_noops_without_keys(monkeypatch):
    """Unconfigured (no app_id/app_key) -> returns [] WITHOUT any network call, so the
    aggregator is opt-in and harmless by default."""
    monkeypatch.setattr("sourcing.ats._cred", lambda n: "")
    called = []
    assert adzuna_jobs("engineer", fetch=lambda url, **k: called.append(url) or {}) == []
    assert called == []


def test_remotive_aggregator_normalizes():
    j = remotive_jobs("engineer", fetch=_fetch(REMOTIVE))[0]
    assert j["source"] == "remotive" and j["source_id"] == "remotive::555"
    assert j["company"] == "RemoteCo" and j["remote"] == "remote"
    assert j["title"] == "Senior Software Engineer" and j["jd_text"] == "Remote engineering."


def test_refresh_pulls_company_boards_and_aggregators():
    w = Watchlist(":memory:")
    w.add_company("Acme", "greenhouse", "acme")
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any"})
    fetch = _dispatch({"greenhouse.io": GH, "remotive.com": REMOTIVE})
    summary = refresh_watchlist(w, fetch=fetch)
    sources = {j["source"] for j in w.list_jobs()}
    assert "greenhouse" in sources and "remotive" in sources   # both lanes contributed
    titles = {j["title"] for j in w.list_jobs()}
    assert "Senior Software Engineer" in titles   # from Remotive keyword feed
    assert "Office Manager" not in titles          # filtered out by criteria
    assert summary["new"] >= 2


def test_aggregators_skip_when_onsite_only():
    w = Watchlist(":memory:")
    w.add_company("Acme", "greenhouse", "acme")
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "onsite"})
    fetch = _dispatch({"greenhouse.io": GH, "remotive.com": REMOTIVE})
    refresh_watchlist(w, fetch=fetch)
    # Remotive is remote-only, so an onsite-only search must not include it.
    assert all(j["source"] != "remotive" for j in w.list_jobs())


def test_refresh_freehire_only_upserts_the_fast_feed():
    from sourcing.service import refresh_freehire_only
    w = Watchlist(":memory:")
    payload = {"data": [
        {"public_slug": "acme-eng", "company": "Acme", "title": "Engineer",
         "location": "Austin, TX", "url": "https://x/1", "description": "Build systems.",
         "posted_at": "2026-08-10"},
        {"public_slug": "beta-ds", "company": "Beta", "title": "Data Scientist",
         "location": "New York, NY", "url": "https://x/2", "description": "Train models.",
         "posted_at": "2026-08-10"},
    ]}
    # Only the first page carries rows; later offsets are empty so pagination stops after one page.
    fetch = lambda url, **k: (payload if "offset=0" in url else {"data": []})
    summary = refresh_freehire_only(w, fetch=fetch, cap=200)
    assert {j["source"] for j in w.list_jobs()} == {"freehire"}
    assert summary["new"] >= 2


def test_freehire_bulk_cap_bounds_the_number_of_requests():
    # A small cap must page shallowly: cap=250 at 100/page hits offsets 0, 100, 200 only -- the
    # fast loop relies on this to stay a handful of requests per tick, not ~60.
    from sourcing.ats import freehire_bulk
    full_page = {"data": [{"public_slug": f"r{i}", "company": "C", "title": "T",
                           "location": "US", "url": "u", "description": "d"} for i in range(100)]}
    calls = []
    def fetch(url, **k):
        calls.append(url)
        return full_page                    # always a full page -> never early-breaks on short page
    freehire_bulk(fetch=fetch, cap=250)
    assert len(calls) == 3


def test_html_to_text_strips_and_bullets():
    t = html_to_text("<p>Hi</p><ul><li>One</li><li>Two &amp; three</li></ul>")
    assert "<" not in t and "Hi" in t and "One" in t and "Two & three" in t


def test_html_to_text_folds_lone_marker_lists():
    # Some feeds (Abbott's postings) format a list as "* \n\ntext": a lone marker on its own line,
    # the item's text a blank line below. It must render as one bullet, not a stray dot + orphan.
    raw = "Benefits:\n\n* \n\nCareer development.\n\n* \n\nMedical coverage."
    t = html_to_text(raw)
    assert "• Career development." in t
    assert "• Medical coverage." in t
    assert "\n•\n" not in t                      # no orphan bullet on its own line


def test_html_to_text_decodes_literal_newline_escapes():
    # Some feeds double-encode newlines as the LITERAL two chars "\n" (backslash + n), so a JD
    # reads "Job Description \n \n Address:". They must become real line breaks, not gibberish text.
    raw = "Job Description \\n \\n \\n Address: 320 S Canal Street\\n \\n Job Family Group: Data"
    t = html_to_text(raw)
    assert "\\n" not in t                          # no literal escapes left in the visible text
    assert "Address: 320 S Canal Street" in t
    assert "Job Family Group: Data" in t


def test_looks_truncated_flags_previews_only():
    from sourcing.ats import looks_truncated
    assert looks_truncated({"source": "adzuna"}, "short snippet") is True   # Adzuna preview, short
    assert looks_truncated({"source": "greenhouse"}, "ends abruptly…") is True
    assert looks_truncated({"source": "greenhouse"}, "A short but complete JD.") is False
    # An Adzuna row already ENRICHED from freehire is full -> must NOT re-flag as a preview,
    # else a complete JD wrongly shows the "preview only" note (Kofi's IBM screenshot).
    assert looks_truncated({"source": "adzuna"}, "x" * 8000) is False


def test_freehire_fulltext_matches_company_and_title_only():
    # Enrichment must match the SAME employer (never show a different company's JD) and a related
    # title, then take the fullest. Uses an injected fetch (no network).
    from sourcing.ats import freehire_fulltext
    payload = {"data": [
        {"company": "Kelly Services", "title": "Mechanical Design Engineer",
         "public_slug": "k1", "description": "FULL Kelly JD. " * 40, "url": "u1"},
        {"company": "Acme Corp", "title": "Mechanical Design Engineer",
         "public_slug": "a1", "description": "Different employer JD. " * 40, "url": "u2"},
        {"company": "Kelly Services", "title": "Warehouse Picker",
         "public_slug": "k2", "description": "Unrelated Kelly role. " * 40, "url": "u3"},
    ]}
    fetch = lambda url, **k: payload
    out = freehire_fulltext("Mechanical Design Engineer", "Kelly Services", fetch=fetch)
    assert "FULL Kelly JD." in out                # matched employer + title
    assert "Different employer" not in out        # never another company's JD
    assert "Unrelated Kelly role" not in out      # not a different role at the same employer
    # No company to verify against -> refuse to guess (keep the snippet).
    assert freehire_fulltext("Mechanical Design Engineer", "", fetch=fetch) == ""


def test_html_to_text_decodes_entity_encoded_markup():
    # Greenhouse ships entity-encoded HTML: the tags arrive as &lt;div&gt; with obfuscated
    # classes. Decoding must happen BEFORE stripping so no literal tags survive in the text.
    raw = ('&lt;div&gt;&lt;span class="author-d-4z65zz66"&gt;Dropbox is looking for a '
           'strategic leader&lt;/span&gt;&lt;/div&gt;&lt;ul&gt;&lt;li&gt;Build enablement '
           'programs&lt;/li&gt;&lt;/ul&gt;')
    t = html_to_text(raw)
    assert "<" not in t and "author-d" not in t and "class=" not in t
    assert "Dropbox is looking for a strategic leader" in t
    assert "Build enablement programs" in t


def test_html_to_text_handles_double_encoding():
    t = html_to_text("&amp;lt;p&amp;gt;Hello&amp;lt;/p&amp;gt;")
    assert "<" not in t and "Hello" in t


# -- filtering ----------------------------------------------------------- #

def test_criteria_title_is_whole_word():
    # "ai" must not match "Affairs"; must match "AI"
    ai_job = {"title": "AI Architect", "location": "NY", "remote": ""}
    aff_job = {"title": "Legislative Affairs Manager", "location": "NY", "remote": ""}
    crit = {"titles": ["ai"], "locations": [], "remote": "any"}
    assert matches_criteria(ai_job, crit) is True
    assert matches_criteria(aff_job, crit) is False


def test_criteria_title_matches_symbol_keywords():
    """Keywords with symbol edges — c++, c#, .net — must match (a \\b boundary silently
    never would), while still not matching a larger alphanumeric token."""
    def job(t):
        return {"title": t, "location": "NY", "remote": ""}
    assert matches_criteria(job("Senior C++ Engineer"), {"titles": ["c++"]}) is True
    assert matches_criteria(job("C# Backend Developer"), {"titles": ["c#"]}) is True
    assert matches_criteria(job(".NET Developer"), {"titles": [".net"]}) is True
    # ".net" is a whole token — it should not fire on an unrelated word like "planetary".
    assert matches_criteria(job("Planetary Scientist"), {"titles": ["net"]}) is False


def test_criteria_remote_and_location():
    remote_job = {"title": "Engineer", "location": "Remote - US", "remote": "remote"}
    onsite_ny = {"title": "Engineer", "location": "New York, NY", "remote": "onsite"}
    assert matches_criteria(remote_job, {"titles": [], "locations": [], "remote": "remote"})
    assert not matches_criteria(onsite_ny, {"titles": [], "locations": [], "remote": "remote"})
    # location filter — remote always passes; NY matches "new york"
    assert matches_criteria(onsite_ny, {"titles": [], "locations": ["new york"], "remote": "any"})
    assert not matches_criteria(onsite_ny, {"titles": [], "locations": ["chicago"], "remote": "any"})
    assert matches_criteria(remote_job, {"titles": [], "locations": ["chicago"], "remote": "any"})  # remote passes


# -- store: dedupe + new flag ------------------------------------------- #

def test_watchlist_store_dedupe_and_new_flag():
    w = Watchlist(":memory:")
    jobs = greenhouse_jobs("acme", "Acme", fetch=_fetch(GH))
    new, total = w.upsert_jobs(jobs)
    assert new == 2 and total == 2
    assert all(j["is_new"] == 1 for j in w.list_jobs())
    # second upsert of the same jobs adds nothing new and doesn't duplicate
    new2, _ = w.upsert_jobs(jobs)
    assert new2 == 0
    assert len(w.list_jobs()) == 2
    # dismiss hides it
    sid = w.list_jobs()[0]["source_id"]
    w.dismiss(sid)
    assert all(j["source_id"] != sid for j in w.list_jobs())


def test_watchlist_companies_and_criteria():
    w = Watchlist(":memory:")
    w.add_company("Acme", "greenhouse", "acme")
    w.add_company("Acme", "greenhouse", "acme")   # dedupe on (ats, board)
    assert len(w.companies()) == 1
    w.set_criteria({"titles": ["engineer", ""], "locations": [], "remote": "remote"})
    assert w.get_criteria()["titles"] == ["engineer"]
    assert w.get_criteria()["remote"] == "remote"


def test_refresh_service_with_injected_feeds():
    w = Watchlist(":memory:")
    w.add_company("Acme", "greenhouse", "acme")
    w.set_criteria({"titles": ["engineer"], "locations": [], "remote": "any"})
    # Scope the fetch to the company board only (Remotive returns nothing here) so this
    # test isolates the company-board path; the aggregator path is covered separately.
    summary = refresh_watchlist(w, fetch=_dispatch({"greenhouse.io": GH}))
    assert summary["fetched"] == 2 and summary["matched"] == 1   # only the engineer role
    assert summary["new"] == 1
    jobs = w.list_jobs()
    assert len(jobs) == 1 and jobs[0]["title"] == "Senior Software Engineer"


def test_prune_source_removes_only_that_feed():
    # Retiring Adzuna (preview-only JDs) must drop its rows and leave every other source intact.
    w = Watchlist(":memory:")
    w.upsert_jobs([
        {"source_id": "adzuna::1", "source": "adzuna", "company": "A", "title": "T",
         "location": "", "remote": "", "url": "", "jd_text": "preview…", "posted_at": ""},
        {"source_id": "freehire::2", "source": "freehire", "company": "B", "title": "T2",
         "location": "", "remote": "", "url": "", "jd_text": "full jd", "posted_at": ""},
    ])
    assert w.prune_source("adzuna") == 1
    left = w.list_jobs()
    assert [j["source"] for j in left] == ["freehire"]      # adzuna gone, freehire kept


# -- compliance guard ---------------------------------------------------- #

def test_no_red_lane_code_in_sourcing():
    """Sourcing must never contain scraping/anti-detection code (CLAUDE.md §7)."""
    red = [
        r"undetected[_-]?chromedriver", r"selenium", r"playwright",
        r"jobspy", r"2captcha", r"captcha[_ ]?solv", r"rotating[_ ]?prox",
        r"proxy[_ ]?rotat", r"indeed\.com", r"linkedin\.com/jobs", r"glassdoor",
        r"beautifulsoup|bs4",
    ]
    offenders = []
    for py in (ROOT / "sourcing").rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace").lower()
        for pat in red:
            if re.search(pat, text):
                offenders.append(f"{py.name}: /{pat}/")
    assert not offenders, "RED-lane code in sourcing:\n" + "\n".join(offenders)


def test_seed_if_empty_loads_the_committed_watchlist_seed(tmp_path, monkeypatch):
    import sourcing.service as svc
    seed = tmp_path / "wl.csv"
    seed.write_text("company,ats,board_id\nStripe,greenhouse,stripe\nRamp,ashby,ramp\n")
    monkeypatch.setattr(svc, "SEED_WATCHLIST", seed)
    w = Watchlist(str(tmp_path / "db.sqlite"))
    svc.seed_if_empty(w)
    boards = {(c["ats"], c["board_id"]) for c in w.companies()}
    assert ("greenhouse", "stripe") in boards and ("ashby", "ramp") in boards


def test_seed_if_empty_falls_back_to_defaults_without_seed(tmp_path, monkeypatch):
    import sourcing.service as svc
    monkeypatch.setattr(svc, "SEED_WATCHLIST", tmp_path / "missing.csv")
    w = Watchlist(str(tmp_path / "db2.sqlite"))
    svc.seed_if_empty(w)
    assert len(w.companies()) == len(svc.SEED_COMPANIES)   # the built-in example set
