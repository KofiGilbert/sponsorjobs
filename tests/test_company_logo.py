"""Company-logo domain guessing (2026-08-09; tightened 2026-11-01).

The board shows a real logo when we can guess the employer's domain, and a clean monogram
otherwise. A WRONG logo is worse than a monogram (a foreign brand on the wrong row), so the
guesser only strips known legal/entity suffixes and never invents a brand. These pin that:
legal suffixes (incl. the newer "PBC") drop, schools resolve to .edu with the academic tail
removed, and companies resolve to .com.

2026-11-01: a test company "Umbrella Labs" showed Cisco's logo. "Labs" was stripped as a suffix,
umbrella.com is Cisco Umbrella's domain, and the favicon fallback followed its redirect. Now a
domain is used only on a confident match: a curated brand, the employer's own job host, or the
exact name minus its legal suffix; the favicon fallback also checks the domain is its own site.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def dom(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A._guess_company_domain


def test_strips_legal_suffixes(dom):
    assert dom("Waymo Llc") == "waymo.com"
    assert dom("Taboola Inc") == "taboola.com"
    assert dom("Insight Enterprises, Inc.") == "insightenterprises.com"


def test_strips_pbc_public_benefit_corp(dom):
    # The suffix that used to break: "Anthropic Pbc" -> anthropicpbc.com (a dead domain).
    assert dom("Anthropic Pbc") == "anthropic.com"


def test_schools_resolve_to_edu_without_the_academic_tail(dom):
    assert dom("DePaul University") == "depaul.edu"
    # Only "University" is dropped: northwestern.edu is Northwestern UNIVERSITY's domain.
    assert dom("Northwestern College") == "northwesterncollege.com"


def test_blank_company_is_no_domain(dom):
    assert dom("") == ""
    assert dom("   ") == ""


# ---- confident matching (2026-11-01) -------------------------------------------------------

def test_descriptive_words_stay_part_of_the_name(dom):
    # The bug: "Umbrella Labs" -> umbrella.com (Cisco Umbrella) -> Cisco's logo.
    assert dom("Umbrella Labs") == "umbrellalabs.com"
    assert dom("Acme Technologies Inc") == "acmetechnologies.com"
    assert dom("Globex Group") == "globexgroup.com"


def test_cisco_variants_resolve_to_cisco(dom):
    assert dom("Cisco") == "cisco.com"
    assert dom("Cisco Systems Inc") == "cisco.com"
    assert dom("Cisco Systems, Inc.") == "cisco.com"
    assert dom("Cisco Meraki") == "cisco.com"


def test_a_substring_is_not_a_match(dom):
    # "Discord" contains "cisco" as letters, not as a word.
    assert dom("Discord") == "discord.com"
    assert dom("Discord Inc") == "discord.com"
    assert dom("Franciscorp") == "franciscorp.com"


def test_brand_families_match_whole_leading_words_only(dom):
    assert dom("Amazon Web Services") == "amazon.com"
    assert dom("Amazon.com Services LLC") == "amazon.com"
    assert dom("Amazon") == "amazon.com"
    # Not a family brand: a name that merely starts with a famous word keeps its own guess.
    assert dom("Apple Leisure Group") == "appleleisuregroup.com"
    assert dom("Meta Financial Group") == "metafinancialgroup.com"


def test_display_brand_and_legal_name_agree(dom):
    # The card sends display_name "Kraken"; a row without it sends the legal "Payward Inc".
    assert dom("Kraken") == "kraken.com"
    assert dom("Payward Inc") == "kraken.com"
    assert dom("Payward, Inc.") == "kraken.com"


def test_shared_ats_hosts_never_become_the_employer(dom):
    # Workday, Greenhouse, Lever... carry many employers: the host must not decide the logo.
    assert dom("Umbrella Labs", "umbrellalabs.wd5.myworkdayjobs.com") == "umbrellalabs.com"
    assert dom("Initech", "initech.wd1.myworkdayjobs.com") == "initech.com"
    assert dom("Hooli", "boards.greenhouse.io") == "hooli.com"
    assert dom("Hooli", "job-boards.greenhouse.io") == "hooli.com"
    assert dom("Pied Piper", "jobs.lever.co") == "piedpiper.com"
    assert dom("Lever Brothers", "jobs.lever.co") == "leverbrothers.com"
    assert dom("Vandelay", "jobs.ashbyhq.com") == "vandelay.com"


def test_employer_own_host_is_used_when_it_names_the_company(dom):
    assert dom("Stark Industries", "careers.stark.com") == "stark.com"
    assert dom("Wayne Enterprises Inc", "jobs.wayneenterprises.co.uk") == "wayneenterprises.co.uk"
    # A host that is someone else's site is ignored.
    assert dom("Umbrella Labs", "jobs.cisco.com") == "umbrellalabs.com"
    assert dom("Discord", "jobs.cisco.com") == "discord.com"


def test_all_suffix_name_has_no_domain(dom):
    assert dom("Inc") == ""
    assert dom("The Company") == ""


def test_favicon_fallback_needs_the_domain_to_be_its_own_site(dom, monkeypatch):
    """umbrella.com redirects to umbrella.cisco.com; DuckDuckGo would return Cisco's icon."""
    import ui.app as A
    asked = []

    class _Resp:
        def __init__(self, url, body=b""):
            self._url, self._body = url, body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def geturl(self):
            return self._url
        def read(self, n=-1):
            return self._body

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        asked.append(url)
        if "apistemic" in url:
            return _Resp(url, b"x" * 400)                       # apistemic: a placeholder miss
        if "duckduckgo" in url:
            return _Resp(url, b"I" * 500)                       # a favicon of whatever is there
        if url == "https://umbrella.com/":
            return _Resp("https://umbrella.cisco.com/", b"<title>Cisco Umbrella</title>")
        if url == "https://parked.com/":
            return _Resp(url, b"<h1>This domain is for sale</h1>")
        return _Resp(url, b"<title>Home</title>")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert A._fetch_logo("umbrella.com") == (None, None)
    assert not any("duckduckgo" in u for u in asked)
    assert A._fetch_logo("parked.com") == (None, None)
    assert A._fetch_logo("ownsite.com") == (b"I" * 500, "image/x-icon")
    asked.clear()
    assert A._fetch_logo("cisco.com") == (b"I" * 500, "image/x-icon")   # curated: no probe
    assert "https://cisco.com/" not in asked


def test_logo_route_passes_the_host(dom, monkeypatch):
    import ui.app as A
    seen = []
    monkeypatch.setattr(A, "_fetch_logo", lambda d: (seen.append(d), (None, None))[1])
    A._LOGO_CACHE.clear()
    c = A.app.test_client()
    assert c.get("/api/logo?company=Umbrella%20Labs").status_code == 404
    c.get("/api/logo?company=Initech&host=initech.wd1.myworkdayjobs.com")
    c.get("/api/logo?company=Stark%20Industries&host=careers.stark.com")
    assert seen == ["umbrellalabs.com", "initech.com", "stark.com"]
    A._LOGO_CACHE.clear()
