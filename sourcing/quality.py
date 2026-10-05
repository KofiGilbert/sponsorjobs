"""Keep the feed to roles an INTERNATIONAL STUDENT can actually take (CLAUDE.md §8).

A sponsor badge is an EMPLOYER-level signal: it says the company has sponsored an H-1B
before, not that a given opening is open to a non-citizen. Two big categories of role slip
through that gap and are dead ends for an F-1/OPT candidate, even at a genuine sponsor:

  1. Roles that state a hard bar — "U.S. citizenship required", "active security clearance",
     "no visa sponsorship". Detected from the job description.
  2. Defense / intelligence / government-IT contractors. They appear in H-1B data (they do
     sponsor a few commercial roles), so the badge is technically valid, but their PUBLIC
     postings are overwhelmingly clearance/citizen-only — a live Adzuna pull was 27% Lockheed
     Martin alone. For this audience that's noise, so we drop these employers from the feed.

This is the quality layer that makes our board feel like Migrate Mate's — jobs students
actually care about — rather than a raw sponsor-tagged dump. It is deliberately conservative
about #1 (only unambiguous negative phrasing) so it never hides a role that DOES sponsor, and
the employer list in #2 is a short, curated set of well-known primes, not a broad guess.
"""

from __future__ import annotations

import re

from .sponsors import normalize_employer

# Unambiguous "you must be a US person" phrasing. Kept tight so a positive line like
# "we are happy to sponsor visas" is never caught — every branch needs an explicit bar.
_EXCLUDE_RE = re.compile(
    r"(u\.?s\.?\s*citizens?(hip)?\s*(is\s*)?(required|only|mandatory)"
    r"|must\s+be\s+(a\s+)?(u\.?s\.?\s*)?citizen"
    r"|sole\s+u\.?s\.?\s*citizen"
    r"|security\s+clearance|active\s+clearance|obtain\s+(a\s+)?(security\s+)?clearance"
    r"|ts/sci|top\s+secret|secret\s+clearance|dod\s+secret|polygraph|\bq\s+clearance\b"
    r"|no\s+(visa\s+)?sponsorship|not\s+(able|eligible)\s+to\s+sponsor"
    r"|unable\s+to\s+sponsor|do(es)?\s+not\s+(offer|provide|sponsor)\s+(visa\s+)?sponsor"
    r"|will\s+not\s+sponsor|without\s+(visa\s+)?sponsorship"
    r"|sponsorship\s+is\s+not\s+(available|offered|provided)"
    r"|not\s+(provide|offer)\s+(visa\s+)?sponsorship)",
    re.I,
)

# Curated defense / intelligence / government-services contractors whose public listings are
# overwhelmingly clearance- or citizenship-gated. Normalized names; subsidiaries match by
# prefix ("general dynamics information technology" -> "general dynamics"). Short and
# well-known on purpose — a wrong exclusion loses a real sponsor, so we only list primes.
_CLEARANCE_EMPLOYERS = {normalize_employer(x) for x in (
    # Primes
    "Lockheed Martin", "Northrop Grumman", "Raytheon", "RTX", "General Dynamics",
    "L3Harris Technologies", "L3 Technologies", "SAIC", "Booz Allen Hamilton", "Leidos",
    "Huntington Ingalls Industries", "BAE Systems", "CACI International", "ManTech",
    "Peraton", "Parsons", "MITRE", "Battelle", "Draper", "The Aerospace Corporation",
    "GDIT", "Nightwing",
    # National labs / FFRDCs (US-citizen-gated)
    "Sandia National Laboratories", "Los Alamos National Laboratory",
    "Lawrence Livermore National Laboratory", "MIT Lincoln Laboratory",
    "Johns Hopkins Applied Physics Laboratory", "Idaho National Laboratory",
    # Mid-tier defense / intel / gov-services (clearance-dominant public postings)
    "ENSCO", "KBR", "Amentum", "V2X", "Cubic", "SimVentions", "Sierra Nevada Corporation",
    "Anduril", "Noblis", "Vectrus", "Torch Technologies", "Riverside Research",
    "Two Six Technologies", "BlueHalo", "Systems Planning and Analysis", "Sierra Space",
    "Ball Aerospace", "Perspecta", "Engility", "Vencore", "ECS Federal",
    "Radiance Technologies", "Applied Signal Technology",
)}


def employer_is_clearance_heavy(company: str) -> bool:
    """True for a curated defense/intel/gov-services prime (or a subsidiary of one), whose
    public roles are overwhelmingly clearance/citizen-only."""
    norm = normalize_employer(company)
    if not norm:
        return False
    return any(norm == e or norm.startswith(e + " ") for e in _CLEARANCE_EMPLOYERS)


def role_excludes_international(company: str = "", jd_text: str = "") -> bool:
    """True when a role is a dead end for an F-1/OPT candidate: a clearance-heavy employer,
    or a description that states a citizenship / clearance / no-sponsorship bar."""
    if employer_is_clearance_heavy(company):
        return True
    return bool(jd_text and _EXCLUDE_RE.search(jd_text))


def is_intl_student_accessible(job: dict) -> bool:
    """Convenience for filtering a sourced-job dict: keep only roles a student can take.
    Judges the description it is GIVEN: for a row from a list-only feed whose description has
    not been fetched yet this can only judge the employer, so pair it with `is_jd_checked`
    before presenting such a row as accessible."""
    return not role_excludes_international(job.get("company", ""), job.get("jd_text", ""))


# Feeds whose rows arrive WITHOUT a description (it is fetched per posting, later): Workday's
# cxs list and SmartRecruiters' postings list both omit the body. Until that fetch has happened
# the citizenship / clearance / no-sponsorship check above has had nothing to read, so such a
# row is "unchecked", not "accessible". The refresh reads the detail before keeping a row
# (service.select_checked_rows); a row stored earlier without one is hidden until it is opened
# and checked (feedclient.fill_list_only_detail).
LIST_ONLY_SOURCES = frozenset({"workday", "smartrecruiters"})


def is_list_only(job: dict) -> bool:
    """Is this row from a feed that delivers no description inline (LIST_ONLY_SOURCES)?"""
    return (job.get("source") or "") in LIST_ONLY_SOURCES


def is_jd_checked(job: dict) -> bool:
    """Has the accessibility check actually seen this row's description? True for every feed
    that delivers descriptions inline. For a list-only feed (Workday, SmartRecruiters) it is true
    only once the row holds one: `has_jd` on a slim list_jobs row, else a non-blank `jd_text`. A
    row that is not checked must never be shown or published as accessible (CLAUDE.md §6 quality
    rule)."""
    if not is_list_only(job):
        return True
    if "has_jd" in job:
        return bool(job.get("has_jd"))
    return bool((job.get("jd_text") or "").strip())


# --------------------------------------------------------------------------- #
# Entry-level / new-grad detection — most international students ARE new grads,
# so surfacing early-career roles is a quality signal (Migrate Mate has a whole
# "Graduate Jobs" page). Title-first, since a role's level is almost always in the
# title and the list view carries no JD.
# --------------------------------------------------------------------------- #

# Seniority in the title overrides everything: a "Senior" or "Staff" role is not entry
# even if some other word looks junior. \b-guarded; roman-numeral II/III/IV catch levelled
# titles ("Engineer II"); "sr"/"jr" only as whole tokens.
_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|\blead\b|director|head\s+of|"
    r"vp|vice\s+president|chief|c[te]o|distinguished|expert|architect|"
    r"ii|iii|iv|10\+|[6-9]\+\s*years?)\b", re.I)

_ENTRY_TITLE_RE = re.compile(
    r"\b(intern|internship|new\s*grad(uate)?|recent\s+grad(uate)?|entry[\s-]*level|"
    r"junior|jr\.?|early\s*career|associate|apprentice|trainee|rotational|"
    r"campus|university\s+grad(uate)?|graduate\s+(program|engineer|analyst|developer|scheme)|"
    r"level\s*1|grad\s+role)\b", re.I)

# In a JD, a low required-experience line marks early-career even if the title is neutral.
_ENTRY_JD_RE = re.compile(
    r"(0\s*[-to]+\s*2\s*years|1\s*[-to]+\s*2\s*years|no\s+(prior\s+)?experience\s+(required|necessary)"
    r"|new\s*grad|recent\s+graduate|entry[\s-]*level|early[\s-]*career)", re.I)


def is_entry_level(title: str, jd_text: str = "") -> bool:
    """True for an early-career / new-grad role. Precise by design (a senior signal in the
    title always wins), so the 'Entry-level' filter surfaces genuinely junior roles rather
    than everything unlabelled."""
    t = title or ""
    if _SENIOR_RE.search(t):
        return False
    if _ENTRY_TITLE_RE.search(t):
        return True
    return bool(jd_text and _ENTRY_JD_RE.search(jd_text))


# Sources that are aggregators, not a company's own board. A board on the watchlist is there
# because the company has a sponsorship record, so its rows always fit the product's promise;
# an aggregator row fits only when the employer carries a sponsor badge or the posting itself
# states sponsorship (decided 2026-10-01: sponsor-relevant only, like niche boards such as
# H1BGrader, rather than an Indeed-style "everything, ranked" list).
AGGREGATOR_SOURCES = frozenset({"remotive", "adzuna", "arbeitnow", "remoteok", "jsearch", "freehire"})


def is_sponsor_relevant(job: dict) -> bool:
    """Does this row belong on SponsorJobs at all? Board rows: yes. Aggregator rows: only with
    a sponsor badge (`visa`, set by SponsorDB.tag_jobs for US roles) or a posting that states
    sponsorship (`sponsorship_stated` is True)."""
    if (job.get("source") or "") not in AGGREGATOR_SOURCES:
        return True
    if job.get("visa"):
        return True
    return job.get("sponsorship_stated") is True
