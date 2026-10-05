"""Discover which visa-sponsoring employers run a PUBLIC ATS board, so the sourcing lane
can pull their live jobs (GREEN lane — CLAUDE.md §6). This is the job-VOLUME engine.

Migrate-Mate-style boards reach 500k+ "sponsorship jobs" by ingesting a huge general job
feed and filtering it down to employers that appear in the public H-1B/PERM data. We
already own the harder half of that recipe — the sponsor list (sourcing/sponsors.py,
582k employers from official USCIS/DOL data). This module builds the OTHER half, but
inverted and legally: starting FROM the biggest sponsors, it probes each one's own public
ATS endpoints and records the boards that actually exist. Every board it finds is therefore
a pre-vetted sponsor — higher precision than buying a general feed and filtering down. No
scraping, no keys, no LinkedIn/Indeed; only the same official feeds sourcing/ats.py reads.

Correctness (CLAUDE.md §8, "a wrong badge is worse than a missing one"): a board slug is
only a GUESS from the employer's name, and a live board at a guessed slug is often a
DIFFERENT company — Greenhouse "charles" is a board literally named "charles", not Charles
Schwab; "linkedin" is "LI Test Company". So a hit is accepted only when its identity is
confirmed:
  * Greenhouse / Workable / SmartRecruiters expose the board's own name -> require it to
    share a DISTINCTIVE word with the sponsor (a brand word, not a generic word, a place,
    or a common first name). A legal name's corporate filler ("Robinhood Markets", "Twitch
    Interactive") is stripped first, so the one brand word left is what the board must show;
    a multi-brand-word name ("Archer Daniels Midland") still needs two of them, because its
    leading word alone collides with other companies.
  * Ashby / Lever expose NO name -> trust only a long joined multi-word form
    ("americanexpress", "meta-platforms"): a concatenation of a company's whole name is a
    near-unique string, so a live board at it is almost certainly them. A bare single word
    is NOT trusted here even for a one-word brand — a common brand word can be squatted
    (a live Lever board "linkedin" is not LinkedIn, which doesn't use Lever), and a
    truncated first token ("applied", "leland") is a different company outright. One-word
    brands are only discoverable on the name lane, where the board's name confirms them.
An unconfirmed hit is reported but never watched, so a discovered board's visa badge
(re-derived per employer by sponsors.tag_jobs at serve time) stays honest.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .ats import fetch_json, normalize_jobs, valid_board_id
from .sponsors import normalize_employer

# The big, keyless, US-heavy ATSs, tried in this order. Each probe is a SINGLE public
# request. SmartRecruiters is last (larger payloads) and now cheap to probe: its adapter
# defaults to a list-only fetch (no per-posting detail), so one request gives the count and
# the employer name to verify against. It reaches enterprises Greenhouse doesn't (Experian,
# Visa, Bosch, Canon, NBCUniversal...).
# Workday is last: it is the most expensive probe (a robots.txt GET per slug per instance) and
# reaches the enterprises none of the others do (NVIDIA, Intel, Cisco, Target, Adobe, Visa...).
# Its probe lives in sourcing/workday.py; identity is confirmed the same way as the name lane.
DISCOVER_ATS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "workday")

# ATSs whose public feed also exposes the EMPLOYER's own name, so a guessed slug can be
# identity-verified. Ashby/Lever expose only department/location, so they can't.
NAME_LANE = ("greenhouse", "workable", "smartrecruiters")

# Single words too collision-prone to identify an employer on their own — geography,
# industry, and corporate filler. A joined multi-word form containing one is still fine;
# it's the LONE word that's untrustworthy.
_GENERIC = {
    "group", "holding", "holdings", "global", "america", "american", "national",
    "international", "systems", "technologies", "technology", "solutions", "services",
    "consulting", "company", "corporation", "industries", "enterprises", "partners",
    "associates", "digital", "health", "financial", "capital", "management", "university",
    "college", "first", "general", "standard", "premier", "advanced", "central",
    "northern", "southern", "eastern", "western", "pacific", "atlantic", "united",
    "state", "states", "north", "south", "east", "west", "great", "greater", "new", "the", "and",
    "applied", "integrated", "unified", "allied", "associated", "consolidated",
    # Places: a board named after a state or city ("OH.io" at the slug "ohio") is not
    # evidence of any particular sponsor from there.
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "hampshire",
    "jersey", "mexico", "york", "carolina", "dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode", "island", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "wisconsin", "wyoming", "boston", "chicago", "houston", "dallas",
    "austin", "seattle", "atlanta", "denver", "phoenix", "miami", "detroit", "philadelphia",
    "angeles", "francisco", "diego", "jose", "vegas", "orlando", "tampa", "charlotte",
    "nashville", "columbus", "pittsburgh", "baltimore", "portland", "minneapolis",
    "canada", "india", "china", "japan", "europe", "european", "asia", "london",
    "usa", "americas",
    # legal-entity filler seen on the big sponsors' filing names ("Visa Technology & Operations
    # Llc", "Fedex Corporate Services Inc", "Target Enterprise Inc", "Pwc Advisory Services Llc")
    "operations", "corporate", "enterprise", "advisory", "worldwide", "ventures", "limited",
    "incorporated", "investments",
}

# Corporate FILLER: words a legal name carries that say nothing about which company it is
# ("Robinhood Markets", "Twitch Interactive", "Experian Information Solutions"). Unlike
# _GENERIC they are only stripped when a real brand word is left over — so "Robinhood
# Markets" reduces to the one brand word "robinhood" (and a board named "Robinhood" is
# then enough), while "United Airlines", whose only non-generic word is filler, keeps
# "airlines" as the word a board must show. Maintained by hand; extend it when a live
# rejection shows a real board was turned down over one of these.
_FILLER = {
    "markets", "interactive", "network", "networks", "information", "solutions",
    "airlines", "airways", "surgical", "technologies", "technology", "systems",
    "holdings", "group", "labs", "laboratories", "inc", "incorporated", "llc", "corp",
    "corporation", "company", "co", "ltd", "limited", "international", "global",
    "services", "enterprise", "enterprises", "industries", "partners", "worldwide", "usa",
    "america", "research",
    "north", "americas", "software", "ventures", "operations", "brands", "media",
    "entertainment", "communications", "pharmaceuticals", "therapeutics", "biosciences",
    "financial", "capital", "health", "healthcare", "digital", "online", "mobile",
    "studios", "products", "logistics", "manufacturing", "automotive", "energy",
    "payments", "analytics", "cloud", "data", "security", "robotics", "semiconductor",
    "pharma", "biologics", "biotherapeutics", "microsystems", "avionics", "aviation",
    "rail", "asset", "operating", "annuity", "administrative", "strategic", "advisors",
    "business", "rehabilitation", "service", "life",
}

# Common English words that companies also use as their whole brand ("Smart", "Pulse",
# "Bloom", "Squad"). Caught live (2026-09-30): the SmartRecruiters board "smart" is a
# one-posting account that is not Smart ERP Solutions, Greenhouse "pulse" is Pulse
# Healthcare (not Pulse Network), "equinox" is the gym (not Equinox IT Solutions), "bloom"
# is a Beirut NGO (not Bloom Energy). Like a first name, such a word can't identify a
# sponsor on its own, so it is never the distinctive word — the sponsor's OTHER words are.
# Maintained by hand from live collisions; a proper noun that is also a word but names
# one well-known company (Robinhood, Twitch, Intuitive, Southwest) does not belong here.
_COMMON_WORDS = {
    "smart", "pulse", "squad", "bloom", "equinox", "nextgen", "spectrum", "universal",
    "reliant", "techno", "keystone", "encore", "fetch", "wing", "plume", "wise", "clear",
    "super", "remote", "alliance", "cornerstone", "align", "system", "sunrise", "genius",
    "insurance", "goodman", "point", "logos",
}

# Common personal first names: a company's leading token is often a founder's first name
# (Charles Schwab, Leland Stanford, Morgan Stanley), and an unrelated board can carry the
# same bare name. Excluding these from the DISTINCTIVE set stops a "charles" board from
# verifying as "Charles Schwab" — the distinctive word there is the surname, not the name.
_FIRST_NAMES = {
    "charles", "william", "james", "robert", "john", "michael", "david", "richard",
    "thomas", "george", "henry", "edward", "frank", "joseph", "walter", "arthur",
    "samuel", "benjamin", "harold", "morgan", "stanley", "leland", "oscar", "victor",
    "martin", "dean", "grant", "mason", "chase", "murphy", "howard", "franklin",
    "warren", "marcus", "julian", "leo", "max", "oliver", "ellen", "grace", "mary",
}


def _tokens(name: str) -> list[str]:
    return [t for t in normalize_employer(name).split() if t]


def distinctive_tokens(display_name: str) -> list[str]:
    """The brand words of an employer name — long enough, not generic filler, not a common
    first name, place, or everyday English word. These are what a board's own name must
    contain to be trusted as this employer. Empty means we have nothing we can confidently
    verify against, so we don't even guess (e.g. "First National Group").

    Corporate filler (_FILLER) is dropped only when a stronger word survives it: "Robinhood
    Markets" -> ["robinhood"], "Experian Information Solutions" -> ["experian"], so a legal
    name's filler no longer demands a second matching word from the board. But "United
    Airlines" -> ["airlines"]: with nothing stronger left, the filler word is still the one
    thing the board's name can be checked against."""
    brand = [t for t in _tokens(display_name)
             if len(t) >= 4 and t not in _GENERIC and t not in _FIRST_NAMES
             and t not in _COMMON_WORDS]
    strong = [t for t in brand if t not in _FILLER]
    return strong or brand


def brand_slug(display_name: str) -> str | None:
    """The leading-token slug (a guess at the board name). Usable only on the NAME_LANE,
    where a hit is identity-verified — for a multi-word name this is a truncation and can't
    be trusted blind."""
    toks = _tokens(display_name)
    if toks and len(toks[0]) >= 3 and valid_board_id(toks[0]):
        return toks[0]
    return None


def joined_slugs(display_name: str) -> list[str]:
    """Long joined multi-word forms ("americanexpress", "meta-platforms") — unambiguous by
    length, so trusted on any ATS. Only for multi-word names with a real brand word."""
    toks = _tokens(display_name)
    if len(toks) < 2 or not distinctive_tokens(display_name):
        return []
    out: list[str] = []
    for s in ("".join(toks), "-".join(toks)):
        if len(s) >= 8 and valid_board_id(s) and s not in out:
            out.append(s)
    return out


def slug_candidates(display_name: str) -> list[str]:
    """Every slug worth probing for an employer, strongest first (leading brand token, then
    long joined forms). De-duped and host-safe. Empty when the name has no distinctive brand
    word to verify against. Which of these are actually TRUSTED depends on the ATS — see
    discover_for_company."""
    if not distinctive_tokens(display_name):
        return []
    ordered = [brand_slug(display_name), *joined_slugs(display_name)]
    seen: set[str] = set()
    return [s for s in ordered if s and not (s in seen or seen.add(s))]


# Most probes hit a slug that doesn't exist; a dead or slow host must not stall a crawl of
# thousands, so probes use a short timeout, not the 20s adapters default.
PROBE_TIMEOUT = 8


def _bounded(fetch, timeout: int):
    """Apply the probe timeout to the real network fetch only; leave an injected test
    fetch (which takes no timeout) untouched."""
    return (lambda url: fetch_json(url, timeout=timeout)) if fetch is fetch_json else fetch


def probe(ats: str, slug: str, fetch=fetch_json, timeout: int = PROBE_TIMEOUT) -> int:
    """Number of live jobs on (ats, slug), or 0 if the board doesn't exist / is empty /
    errored. Never raises — a 404, timeout, or network blip just means 'no board here'.
    Uses the LIGHTEST official endpoint that reveals the count: a guessed slug often lands
    on some other company's huge board, and pulling every description (what the feed
    adapters do) made a single guess time out on a slow link. Same public hosts, less data."""
    if not valid_board_id(slug):
        return 0
    f = _bounded(fetch, timeout)
    try:
        if ats == "greenhouse":            # job list without ?content=true (titles only)
            return len((f(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
                        or {}).get("jobs") or [])
        if ats == "workable":              # widget list without ?details=true
            return len((f(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
                        or {}).get("jobs") or [])
        if ats == "smartrecruiters":       # one posting + the board's own total
            data = f(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1") or {}
            content = data.get("content") or []
            return int(data.get("totalFound") or len(content)) if content else 0
        return len(normalize_jobs(ats, slug, "", fetch=f))   # lever / ashby: no lighter form
    except Exception:
        return 0


def board_name(ats: str, slug: str, fetch=fetch_json, timeout: int = PROBE_TIMEOUT) -> str:
    """The board's own employer name where the ATS exposes one (Greenhouse board meta,
    Workable account, SmartRecruiters posting), else "". Verifies a guessed slug is really
    this employer."""
    f = _bounded(fetch, timeout)
    try:
        if ats == "greenhouse":
            return str((f(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                        or {}).get("name") or "")
        if ats == "workable":
            return str((f(f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
                          "?details=true") or {}).get("name") or "")
        if ats == "smartrecruiters":
            content = (f(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                         "?limit=1") or {}).get("content") or []
            return str((content[0].get("company") or {}).get("name") or "") if content else ""
    except Exception:
        return ""
    return ""


def name_match_count(board_nm: str, distinctive: list[str], sponsor_name: str = "") -> int:
    """How many of the sponsor's distinctive brand words appear in a board's own name, as
    whole words (case-insensitive). When none do, the names are also compared with their
    spaces removed — a board "Value Labs" is the sponsor "Valuelabs", and a board
    "Valuelabs" is the sponsor "Value Labs Inc" — and a no-space match counts as every word
    matching. (A place or generic word never gets this far: it isn't distinctive, so a board
    "OH.io" can't confirm an "Ohio ..." sponsor.)"""
    if not board_nm or not distinctive:
        return 0
    board_toks = _tokens(board_nm)
    board_set = set(board_toks)
    count = sum(1 for t in distinctive if t in board_set)
    if count:
        return count
    # The no-space comparison only trusts REAL word pieces: a name split by punctuation
    # into fragments ("Co–Star" -> "co star", "OH.io" -> "oh io") happens to join to
    # another company's one word ("costar", "ohio") and must not pass as it.
    if any(len(t) < 3 for t in board_toks):
        return 0
    joined_board = "".join(board_toks)
    sponsor_joined = {"".join(distinctive)}
    if sponsor_name:
        sponsor_joined.add("".join(_tokens(sponsor_name)))
    sponsor_joined.discard("")
    if joined_board and (joined_board in sponsor_joined or joined_board in distinctive
                         or sponsor_joined & board_set):
        return len(distinctive)
    return 0


def name_matches(board_nm: str, distinctive: list[str], sponsor_name: str = "") -> bool:
    """True when a board's own name shares a distinctive brand word with the sponsor."""
    return name_match_count(board_nm, distinctive, sponsor_name) > 0


def discover_for_company(display_name: str, atss=DISCOVER_ATS, fetch=fetch_json,
                         sleep=None, reject=None) -> dict | None:
    """Find the first live, IDENTITY-CONFIRMED public ATS board for one employer, or None.
    On the name lane (Greenhouse/Workable) any guessed slug is allowed but the hit must
    verify against the board's own name; off it (Ashby/Lever) only long joined multi-word
    slugs are tried, and a hit is trusted as-is (a concatenated full name is unambiguous).
    `reject(ats, slug, reason)`, when given, is called for every LIVE board that was turned
    down, so a crawl can report what it rejected and why (sourcing/growth.py)."""
    dist = distinctive_tokens(display_name)
    if not dist:
        return None                          # no verifiable brand word -> don't guess
    single = brand_slug(display_name)
    joined = joined_slugs(display_name)
    # A leading-token slug for a MULTI-brand-word name ("archer" from Archer Daniels
    # Midland) is a truncation that collides with unrelated single-brand companies (Archer
    # Aviation). So the board's own name must confirm at least TWO of the sponsor's brand
    # words — one shared word isn't enough. A single-brand-word company ("Stripe") and the
    # unambiguous joined forms only need the one match.
    need_two = len(dist) >= 2
    for ats in atss:
        if ats == "workday":
            from .workday import discover_workday       # lazy: workday.py builds on this module
            quiet = sleep or None
            hit = discover_workday(display_name, fetch=fetch, reject=reject, sleep=quiet)
            if hit:
                return {"company": display_name, "ats": "workday", "board_id": hit.board_id,
                        "jobs": hit.jobs}
            continue
        if ats in NAME_LANE:
            cands = [(single, "brand"), *[(j, "joined") for j in joined]]
        else:
            cands = [(j, "joined") for j in joined]   # no name to check — joined only
        for slug, kind in dict.fromkeys((c, k) for c, k in cands if c):
            n = probe(ats, slug, fetch=fetch)
            if sleep:
                sleep()
            if n <= 0:
                continue
            if ats in NAME_LANE:
                need = 2 if (kind == "brand" and need_two) else 1
                nm = board_name(ats, slug, fetch=fetch)
                if name_match_count(nm, dist, display_name) < need:
                    if reject:
                        reject(ats, slug, f"board is named {nm or '(unnamed)'!r}; needs "
                                          f"{need} of {dist} to be {display_name!r}")
                    continue                 # board is a different company, or too weak a match
            return {"company": display_name, "ats": ats, "board_id": slug, "jobs": n}
    return None


def discover_and_watch(sponsor_db, watchlist, limit: int = 2000, min_approvals: int = 1,
                       atss=DISCOVER_ATS, fetch=fetch_json, sleep=None, on_progress=None,
                       dry_run: bool = False, max_workers: int = 8) -> dict:
    """Walk the biggest sponsors, discover each one's public ATS board, and add the
    confirmed ones to the watchlist (which the app then refreshes like any other board).
    Idempotent: boards already watched are counted as hits but not re-added. `dry_run`
    discovers without writing. `on_progress(i, name, hit)` fires per employer, in ranking
    order. Discovery is network-bound, so it runs across `max_workers` threads; the
    watchlist write stays on this thread, so SQLite is only ever touched serially."""
    rows = sponsor_db.top_sponsors(limit, min_approvals)
    existing = {(w["ats"], w["board_id"]) for w in watchlist.companies(active_only=False)}
    boards: list[dict] = []
    probed = 0

    def work(row):
        return row, discover_for_company(row["display_name"], atss=atss, fetch=fetch, sleep=sleep)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        for row, hit in ex.map(work, rows):        # map preserves ranking order
            probed += 1
            if on_progress:
                on_progress(probed, row["display_name"], hit)
            if hit:
                boards.append(hit)

    added = 0
    for hit in boards:
        key = (hit["ats"], hit["board_id"])
        if key in existing or dry_run:
            continue
        try:
            watchlist.add_company(hit["company"], hit["ats"], hit["board_id"])
            existing.add(key)
            added += 1
        except ValueError:
            pass                                   # a slug that fails the board-id guard
    return {"probed": probed, "hits": len(boards), "added": added, "boards": boards}
