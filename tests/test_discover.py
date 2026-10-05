"""Sponsor-board discovery — the job-VOLUME engine (2026-08-08).

Discovery turns our 582k-employer sponsor list into a watchlist of live public ATS boards,
so the feed grows from a few seed companies to thousands of pre-vetted sponsors. The thing
that matters most is PRECISION (CLAUDE.md §8, "a wrong badge is worse than a missing one"):
a board slug is only a GUESS from the employer name, and a live board at that slug is often
a different company. These tests pin the identity checks that keep discovery honest —
verified against real failures caught live: Greenhouse "charles" is a board named "charles"
(not Charles Schwab), "linkedin" is "LI Test Company", and Ashby exposes no name at all, so
a truncated "applied"/"leland" can never be trusted there. A fake fetch stands in for the
live ATS endpoints, so nothing touches the network.
"""

from __future__ import annotations

import re

import pytest

from sourcing.discover import (discover_and_watch, discover_for_company,
                               distinctive_tokens, name_match_count, probe,
                               slug_candidates)
from sourcing.watchlist import Watchlist


# -- slug generation & distinctive tokens: precision over recall ---------- #

def test_distinctive_tokens_drop_generic_and_first_names():
    assert distinctive_tokens("Charles Schwab & Company Inc") == ["schwab"]  # not "charles"
    assert distinctive_tokens("First National Group") == []                  # all generic
    assert distinctive_tokens("Datadog Inc") == ["datadog"]
    # Legal-entity filler on a filing name is not a brand word to verify against.
    assert distinctive_tokens("Visa Technology & Operations Llc") == ["visa"]
    assert distinctive_tokens("Fedex Corporate Services Inc") == ["fedex"]


def test_single_word_brand_is_a_candidate():
    assert slug_candidates("Datadog Inc") == ["datadog"]
    assert slug_candidates("Cloudflare, Inc.") == ["cloudflare"]


def test_multiword_names_add_joined_forms_strongest_first():
    cands = slug_candidates("Meta Platforms Inc")
    assert cands[0] == "meta"                        # leading token (name-lane only)
    assert "metaplatforms" in cands and "meta-platforms" in cands


def test_generic_or_all_filler_names_yield_nothing():
    assert slug_candidates("United Global Systems") == []   # every token is filler
    assert slug_candidates("Global Holdings Corp") == []
    assert slug_candidates("") == []


# -- a fake fetch that mimics the four ATS endpoints ---------------------- #

def _fetch(gh_jobs=None, gh_names=None, ashby=None, lever=None, workable=None):
    gh_jobs, gh_names = gh_jobs or {}, gh_names or {}
    ashby, lever, workable = ashby or {}, lever or {}, workable or {}

    def fetch(url, timeout=20):
        m = re.search(r"boards-api\.greenhouse\.io/v1/boards/([^/?]+)(/jobs)?", url)
        if m:
            slug, is_jobs = m.group(1), m.group(2)
            if is_jobs:
                if slug not in gh_jobs:
                    raise Exception("404")
                return {"jobs": gh_jobs[slug]}
            return {"name": gh_names.get(slug, slug)}     # board meta (identity)
        m = re.search(r"api\.ashbyhq\.com/posting-api/job-board/([^/?]+)", url)
        if m:
            if m.group(1) not in ashby:
                raise Exception("404")
            return {"apiVersion": "1", "jobs": ashby[m.group(1)]}
        m = re.search(r"api\.lever\.co/v0/postings/([^/?]+)", url)
        if m:
            if m.group(1) not in lever:
                raise Exception("404")
            return lever[m.group(1)]
        m = re.search(r"apply\.workable\.com/api/v1/widget/accounts/([^/?]+)", url)
        if m:
            if m.group(1) not in workable:
                raise Exception("404")
            return workable[m.group(1)]
        raise Exception("404")
    return fetch


_JOB_US = [{"id": 1, "title": "Engineer", "location": {"name": "Remote - US"}}]
_ASHBY_JOB = [{"id": "a1", "title": "Engineer", "location": "New York"}]


# -- probing -------------------------------------------------------------- #

def test_probe_counts_live_jobs_and_swallows_missing_boards():
    fetch = _fetch(gh_jobs={"datadog": _JOB_US})
    assert probe("greenhouse", "datadog", fetch=fetch) == 1
    assert probe("greenhouse", "doesnotexist", fetch=fetch) == 0
    assert probe("greenhouse", "bad slug!", fetch=fetch) == 0        # invalid id -> 0


# -- identity verification on the name lane ------------------------------- #

def test_name_lane_accepts_only_when_the_board_name_confirms_the_employer():
    # Real Stripe: board named "Stripe" -> accepted.
    fetch = _fetch(gh_jobs={"stripe": _JOB_US}, gh_names={"stripe": "Stripe"})
    hit = discover_for_company("Stripe Inc", fetch=fetch)
    assert hit["ats"] == "greenhouse" and hit["board_id"] == "stripe"


def test_name_lane_rejects_a_squatted_or_mismatched_board():
    # The live failures: "charles" board is named "charles"; "linkedin" board is a test co.
    fetch = _fetch(gh_jobs={"charles": _JOB_US, "linkedin": _JOB_US},
                   gh_names={"charles": "charles", "linkedin": "LI Test Company"})
    assert discover_for_company("Charles Schwab & Company Inc", fetch=fetch) is None
    assert discover_for_company("Linkedin Corporation", fetch=fetch) is None


def test_smartrecruiters_is_a_verified_name_lane():
    # SmartRecruiters exposes the employer name, so it's a NAME_LANE: a single-word brand is
    # probed (list only) and accepted ONLY when the board's company.name confirms it.
    def ok(url, timeout=20):
        if "smartrecruiters.com/v1/companies/experian/postings" in url:
            return {"totalFound": 5, "content": [{"id": "1", "name": "Data Analyst",
                    "location": {"fullLocation": "Austin, Texas, United States"},
                    "company": {"name": "Experian"}}]}
        raise Exception("404")
    hit = discover_for_company("Experian PLC", atss=("smartrecruiters",), fetch=ok)
    assert hit and hit["ats"] == "smartrecruiters" and hit["board_id"] == "experian"

    def mismatch(url, timeout=20):
        if "companies/experian/postings" in url:
            return {"content": [{"id": "1", "name": "X", "location": {},
                    "company": {"name": "Unrelated Holdings"}}]}
        raise Exception("404")
    assert discover_for_company("Experian PLC", atss=("smartrecruiters",), fetch=mismatch) is None


def test_shared_brand_word_across_two_companies_is_rejected():
    # Archer Daniels Midland's leading token "archer" matches a Greenhouse board named
    # "Archer" — but that's Archer Aviation, a different company. One shared word from a
    # multi-brand-word name isn't enough; it needs two.
    fetch = _fetch(gh_jobs={"archer": _JOB_US}, gh_names={"archer": "Archer"})
    assert discover_for_company("Archer Daniels Midland Company", fetch=fetch) is None
    # If the board's name actually confirms the full company, it's accepted.
    ok = _fetch(gh_jobs={"archer": _JOB_US}, gh_names={"archer": "Archer Daniels Midland"})
    assert discover_for_company("Archer Daniels Midland Company", fetch=ok)["board_id"] == "archer"


def test_offline_ats_never_trusts_a_truncated_or_bare_single_word():
    # The false-positive class on Ashby/Lever (no name to verify): a truncated first token
    # ("applied"/"leland"), AND even a distinctive lone brand word ("linkedin" — a live
    # Lever board that is NOT LinkedIn). None may be trusted without a name.
    fetch = _fetch(ashby={"applied": _ASHBY_JOB, "leland": _ASHBY_JOB},
                   lever={"linkedin": _ASHBY_JOB})
    assert discover_for_company("Applied Materials Inc", fetch=fetch) is None
    assert discover_for_company("The Leland Stanford Jr University", fetch=fetch) is None
    assert discover_for_company("Linkedin Corporation", fetch=fetch) is None


def test_offline_ats_trusts_a_joined_multiword_form():
    # A concatenation of the whole name is near-unique, so it's trusted without a name.
    fetch = _fetch(ashby={"americanexpress": _ASHBY_JOB})
    assert discover_for_company("American Express Co", fetch=fetch)["board_id"] == "americanexpress"
    # ...but a one-word company has no joined form, so it isn't discoverable off the name lane.
    assert discover_for_company("Datadog Inc", fetch=_fetch(ashby={"datadog": _ASHBY_JOB})) is None


# -- corporate filler: a legal name's padding must not demand a second word ---- #
#
# The 2026-09 crawl turned down real boards because the sponsor's LEGAL name carries filler
# ("Robinhood Markets" vs a board named "Robinhood"): both words counted as brand words, so
# the leading-token slug needed two matches and the board only had one. Filler is now
# stripped when a real brand word survives it, so the brand word alone confirms the board.
# Every live case from that run is pinned here, in both directions: the real boards it must
# now accept, and the squatted/unrelated boards it must keep rejecting.

_REAL_BOARDS = [
    # sponsor legal name,                   slug,          the board's own name
    ("Robinhood Markets Inc",               "robinhood",   "Robinhood"),
    ("Twitch Interactive Inc",              "twitch",      "Twitch"),
    ("Peloton Interactive Inc",             "peloton",     "Peloton"),
    ("Upstart Network Inc",                 "upstart",     "Upstart"),
    ("Experian Information Solutions Inc",  "experian",    "Experian"),
    ("Southwest Airlines Co",               "southwest",   "Southwest"),
    ("Intuitive Surgical Inc",              "intuitive",   "Intuitive"),
    ("Valuelabs LLC",                       "valuelabs",   "Value Labs"),     # no-space match
    ("Axon Enterprise Inc",                 "axon",        "Axon"),
    ("Didi Research America Llc",           "didi",        "DiDi Labs"),
    ("Kite Pharma Inc",                     "kite",        "Kite"),
    ("Point72 Asset Management Lp",         "point72",     "Point72 "),
    ("Virtu Financial Operating Llc",       "virtu",       "Virtu Financial"),
    ("Vonage Business Inc",                 "vonage",      "Vonage"),
    ("Wabtec Us Rail Inc",                  "wabtec",      "Wabtec"),
    ("Thales Avionics Inc",                 "thales",      "Thales"),
    ("Abbvie Biotherapeutics Inc",          "abbvie",      "AbbVie"),
    ("Schonfeld Strategic Advisors Llc",    "schonfeld",   "Schonfeld"),
    ("Agero Administrative Service Corp",   "agero",       "Agero"),
    ("Athene Annuity And Life Company",     "athene",      "Athene"),
]

_WRONG_BOARDS = [
    # sponsor legal name,                   slug,          the (different) board at that slug
    ("Linkedin Corporation",                "linkedin",    "LI Test Company"),
    ("Charles Schwab & Company Inc",        "charles",     "charles"),
    ("General Motors Company",              "general",     "General Interest"),
    ("National Instruments Corporation",    "national",    "NATIONAL"),
    ("Ohio Farmers Insurance Company",      "ohio",        "OH.io"),
    ("Pure Storage Inc",                    "purestorage", "Everpure"),
    ("New Relic Inc",                       "new",         "Sonja Inc."),
    ("Archer Daniels Midland Company",      "archer",      "Archer Veterinary Clinic"),
    ("Costar Realty Information Inc",       "costar",      "Co–Star"),
    # Everyday English words used as a whole brand collide with unrelated boards (caught
    # live on SmartRecruiters/Greenhouse); the sponsor's other words must confirm instead.
    ("Smart Erp Solutions Inc",             "smart",       "Smart"),
    ("Pulse Network Llc",                   "pulse",       "Pulse Healthcare"),
    ("Equinox It Solutions Llc",            "equinox",     "Equinox"),
    ("Bloom Energy Corporation",            "bloom",       "Bloom"),
    ("Squad Software Inc",                  "squad",       "SQUAD"),
    ("Alight Solutions Llc",                "alight",      "A Light"),            # fragment join
    ("Goodman Manufacturing Company L P",   "goodman",     "Goodman"),
]


@pytest.mark.parametrize("sponsor,slug,board", _REAL_BOARDS)
def test_filler_reduced_name_accepts_the_board_named_for_its_one_brand_word(sponsor, slug, board):
    assert len(distinctive_tokens(sponsor)) == 1          # the filler is gone, one brand word left
    fetch = _fetch(gh_jobs={slug: _JOB_US}, gh_names={slug: board})
    hit = discover_for_company(sponsor, atss=("greenhouse",), fetch=fetch)
    assert hit and hit["board_id"] == slug and hit["jobs"] == 1


@pytest.mark.parametrize("sponsor,slug,board", _WRONG_BOARDS)
def test_generic_place_and_first_name_slugs_stay_rejected(sponsor, slug, board):
    rejected = []
    fetch = _fetch(gh_jobs={slug: _JOB_US}, gh_names={slug: board})
    assert discover_for_company(sponsor, atss=("greenhouse",), fetch=fetch,
                                reject=lambda a, s, why: rejected.append((s, why))) is None
    # ...and whichever slug was probed (if any) was turned down on identity, never accepted.
    assert all(s == slug for s, _ in rejected)


def test_the_distinctive_word_is_never_a_place_or_filler():
    # Places and generic words are not brand words (so "OH.io" can't confirm "Ohio ...").
    assert distinctive_tokens("Ohio Farmers Insurance Company") == ["farmers"]
    assert distinctive_tokens("New Relic Inc") == ["relic"]
    assert distinctive_tokens("General Motors Company") == ["motors"]
    assert distinctive_tokens("National Instruments Corporation") == ["instruments"]
    # Filler is stripped only when a real brand word survives it...
    assert distinctive_tokens("Robinhood Markets Inc") == ["robinhood"]
    assert distinctive_tokens("Experian Information Solutions Inc") == ["experian"]
    # ...otherwise the filler word is still what the board has to show (no regression).
    assert distinctive_tokens("United Airlines Inc") == ["airlines"]
    # A multi-brand-word name keeps all its words, so its leading token still needs two.
    assert distinctive_tokens("Archer Daniels Midland Company") == ["archer", "daniels", "midland"]


def test_a_common_word_brand_is_confirmed_by_its_other_words():
    # "bloom" alone can't identify Bloom Energy, but a board named "Bloom Energy" can.
    assert distinctive_tokens("Bloom Energy Corporation") == ["energy"]
    fetch = _fetch(gh_jobs={"bloom": _JOB_US}, gh_names={"bloom": "Bloom Energy"})
    assert discover_for_company("Bloom Energy Corporation", atss=("greenhouse",), fetch=fetch)["board_id"] == "bloom"


def test_a_board_named_for_a_single_filler_word_is_not_enough():
    # "United Airlines" reduces to the filler word "airlines"; a board that only says
    # "United" shares nothing distinctive, while "United Airlines" does.
    fetch = _fetch(gh_jobs={"united": _JOB_US}, gh_names={"united": "United"})
    assert discover_for_company("United Airlines Inc", atss=("greenhouse",), fetch=fetch) is None
    ok = _fetch(gh_jobs={"united": _JOB_US}, gh_names={"united": "United Airlines"})
    assert discover_for_company("United Airlines Inc", atss=("greenhouse",), fetch=ok)["board_id"] == "united"


def test_no_space_comparison_runs_both_ways_but_never_through_a_generic_word():
    assert name_match_count("Value Labs", ["valuelabs"], "Valuelabs LLC") == 1
    assert name_match_count("Valuelabs", ["value"], "Value Labs Inc") == 1
    assert name_match_count("Snow Flake", ["snowflake"], "Snowflake Inc") == 1
    # "OH.io" joins to "ohio", but "ohio" is a place, not a distinctive word -> no match.
    assert name_match_count("OH.io", ["farmers", "insurance"], "Ohio Farmers Insurance Company") == 0
    assert name_match_count("Everpure", ["pure", "storage"], "Pure Storage Inc") == 0
    assert name_match_count("Sonja Inc.", ["relic"], "New Relic Inc") == 0
    # Caught live: the Greenhouse board "costar" is "Co–Star" (an astrology app), which
    # joins to "costar" — but a punctuation split into fragments isn't a spelling variant,
    # so it must not confirm CoStar Realty.
    assert name_match_count("Co–Star", ["costar"], "Costar Realty Information Inc") == 0


# -- end-to-end walk over the sponsor list -------------------------------- #

class _FakeSponsorDB:
    def __init__(self, names):
        self._names = names

    def top_sponsors(self, limit=2000, min_approvals=1):
        return [{"display_name": n, "h1b_approvals": 100} for n in self._names][:limit]


def test_discover_and_watch_adds_only_confirmed_boards(tmp_path):
    wl = Watchlist(tmp_path / "wl.db")
    fetch = _fetch(gh_jobs={"datadog": _JOB_US, "charles": _JOB_US},
                   gh_names={"datadog": "Datadog", "charles": "charles"})
    sponsors = _FakeSponsorDB(["Datadog Inc",                       # confirmed
                               "Charles Schwab & Company Inc",      # squatted -> rejected
                               "First National Group"])             # no brand -> not probed
    summary = discover_and_watch(sponsors, wl, fetch=fetch)
    assert summary["probed"] == 3 and summary["hits"] == 1 and summary["added"] == 1
    assert {(w["ats"], w["board_id"]) for w in wl.companies()} == {("greenhouse", "datadog")}


def test_discover_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    wl = Watchlist(tmp_path / "wl.db")
    fetch = _fetch(gh_jobs={"datadog": _JOB_US}, gh_names={"datadog": "Datadog"})
    sponsors = _FakeSponsorDB(["Datadog Inc"])
    dry = discover_and_watch(sponsors, wl, fetch=fetch, dry_run=True)
    assert dry["hits"] == 1 and dry["added"] == 0 and wl.is_empty()
    first = discover_and_watch(sponsors, wl, fetch=fetch)
    again = discover_and_watch(sponsors, wl, fetch=fetch)
    assert first["added"] == 1 and again["added"] == 0 and again["hits"] == 1
