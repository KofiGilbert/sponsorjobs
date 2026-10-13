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

import re
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
    # Weak descriptors (2026-10-12): words a legal name carries next to a brand that the
    # employer's own board usually leaves out ("Cadence Design Systems" -> a board that just
    # says "Cadence"; "Lowes Companies", "Comcast Cable", "Tata Consultancy Svcs"). They are
    # dropped only when a real brand word survives, like the rest of this set. Institution
    # words that DO tell same-brand employers apart (bank, hospital, clinic, university,
    # school, foundation...) are deliberately NOT here -- see _INSTITUTION.
    "design", "development", "companies", "consultancy", "consultants", "svcs", "intl",
    "infotech", "infosystems", "infosolutions", "info", "soft", "engineering", "engineers",
    "electronics", "computer", "computing", "wireless", "cable", "motor", "motors",
    "automation", "stores", "wholesale", "foods", "power", "games", "publishing", "aerospace",
    "trading", "resources", "resource", "professional", "professionals", "industry",
    "devices", "micro", "platforms", "mobility", "staffing", "telecom", "apps", "solution",
    "informatics", "subsidiary", "shared", "employees", "intelligence", "innovation",
    "innovations", "scientific", "sciences", "science", "pharmaceutical", "biomedical",
    "construction", "transportation", "supply", "chain", "commercial", "express", "travel",
    "related", "securities", "markets", "investment", "finance", "lab", "tech",
}

# Institution words: a brand word shared by two employers is told apart by these ("Cadence
# Bank" vs Cadence the chip company, "Ford Foundation" vs Ford Motor, "Emerson College" vs
# Emerson Electric). None of the new descriptors above is one of them; and whatever their
# filler status, on a board's name they count as a DIFFERENT employer's qualifier unless the
# sponsor's own name carries them ("Southwest" may stand for Southwest Airlines, but a board
# "Oracle Health" or "Hitachi Energy" names a sibling, not the sponsor).
_INSTITUTION = {
    "bank", "bancorp", "hospital", "hospitals", "clinic", "medical", "university", "college",
    "school", "schools", "foundation", "institute", "church", "county", "city", "district",
    "credit", "union", "mutual", "insurance", "academy", "museum", "society", "association",
    "center", "centre", "health", "healthcare", "dental", "pharmacy", "law", "realty",
    "aviation", "airlines", "airways", "air", "energy", "capital", "partners",
    "foods", "food", "grocery", "market", "staffing", "veterinary", "animal", "hotel", "hotels",
    "restaurant", "restaurants", "nursing", "church", "ministries", "trust", "savings",
    "federal", "motors", "power", "utilities", "electric", "oil", "gas", "petroleum",
}

# Words that may follow a brand on a board's own name without making it someone else:
# legal forms, careers-site words, and the corporate filler above. A brand word followed by
# anything else not in the sponsor's own name ("Cadence Bank", "Apple Bank", "Hitachi Energy",
# "Ford Foundation") is a different employer that shares the brand.
_LEGAL = {
    "inc", "incorporated", "llc", "llp", "lp", "plc", "corp", "corporation", "co", "company",
    "ltd", "limited", "gmbh", "ag", "sa", "nv", "bv", "pvt", "private", "pty", "the", "and",
    "of", "us", "usa", "na", "careers", "career", "jobs", "job", "external", "site", "hiring",
    "talent", "recruiting", "opportunities", "team", "teams", "official", "page", "board",
    "en", "professional", "experienced", "early", "campus", "university_careers",
}
# Corporate words that say nothing about the line of business, so they may follow the brand on
# a board's name ("Stripe Technologies", "DiDi Labs", "Virtu Financial"). Industry words are
# deliberately left out: "Lowes Foods" is a grocer, not Lowe's; "Hitachi Energy" is not Hitachi
# Vantara.
_CORPORATE = {
    "technologies", "technology", "tech", "systems", "solutions", "services", "service", "group",
    "holdings", "holding", "labs", "lab", "laboratories", "software", "digital", "interactive",
    "network", "networks", "global", "international", "worldwide", "enterprise", "enterprises",
    "consulting", "operations", "ventures", "brands", "research", "information", "innovation",
    "innovations", "professional", "professionals", "resources", "financial", "securities",
    "markets", "investments", "payments", "cloud", "data", "analytics", "platforms", "mobile",
    "online", "industries", "corporate", "management", "partners", "development", "design",
    "engineering", "companies", "intl", "infotech", "consultancy", "svcs", "employees",
}
_SAFE_QUALIFIERS = (_LEGAL | _GENERIC | _CORPORATE) - _INSTITUTION

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


# Known abbreviations (2026-10-12). Many of the biggest sponsors file under a legal name with
# no usable brand word ("Ibm Corporation", "Hcl America", "At&T Services", "Pwc Advisory
# Services") or are known by a short form their boards use ("ADM", "AMD", "HPE", "TCS"). Keyed
# by the leading words of the NORMALIZED sponsor name (sponsors.normalize_employer); each entry
# gives the slugs worth trying and the brand forms a board's own name may show instead of the
# legal name. Hand-maintained: an alias is a claim that this short form IS this employer, so
# add one only when it is unambiguous. A board must still show the brand form to be accepted.
ALIASES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ibm": (("ibm",), ("ibm",)),
    "hcl": (("hcltech", "hcl"), ("hcl", "hcltech")),
    "lt technology services": (("ltts",), ("ltts", "l&t technology services")),
    "larsen and toubro infotech": (("ltimindtree", "lti"), ("ltimindtree", "lti")),
    "ltimindtree": (("ltimindtree",), ("ltimindtree",)),
    "cgi": (("cgi",), ("cgi",)),
    "capital one": (("capitalone",), ("capital one",)),
    "ust global": (("ust", "ustglobal"), ("ust",)),
    "ust": (("ust",), ("ust",)),
    "morgan stanley": (("morganstanley", "ms"), ("morgan stanley",)),
    "pwc": (("pwc",), ("pwc", "pricewaterhousecoopers")),
    "pricewaterhousecoopers": (("pwc",), ("pwc", "pricewaterhousecoopers")),
    "adp": (("adp",), ("adp",)),
    "automatic data processing": (("adp",), ("adp",)),
    "boston consulting group": (("bcg",), ("bcg", "boston consulting group")),
    "sap": (("sap",), ("sap",)),
    "zs associates": (("zs", "zsassociates"), ("zs associates", "zs")),
    "at and t": (("att",), ("at&t", "at and t")),
    "fca us": (("stellantis",), ("stellantis", "fca")),
    "advanced micro devices": (("amd",), ("amd", "advanced micro devices")),
    "hewlett packard enterprise": (("hpe", "hewlettpackardenterprise"),
                                   ("hpe", "hewlett packard enterprise")),
    "hp": (("hp",), ("hp",)),
    "tata consultancy": (("tcs",), ("tcs", "tata consultancy")),
    "ernst and young": (("ey",), ("ey", "ernst and young", "ernst young")),
    "ernst young": (("ey",), ("ey", "ernst and young", "ernst young")),
    "general electric": (("ge",), ("ge", "general electric")),
    "archer daniels midland": (("adm",), ("adm", "archer daniels midland")),
    "johns hopkins": (("jhu", "johnshopkins"), ("johns hopkins", "jhu")),
    "american airlines": (("aa", "americanairlines"), ("american airlines",)),
    "wal mart": (("walmart",), ("walmart", "wal mart")),
    "t mobile": (("tmobile",), ("t mobile",)),
    "ntt data": (("nttdata",), ("ntt data",)),
    "bank of new york mellon": (("bnymellon", "bny"), ("bny", "bny mellon")),
    "3m": (("3m",), ("3m",)),
    "united parcel service": (("ups",), ("ups", "united parcel service")),
    "kla": (("kla",), ("kla",)),
    "nxp": (("nxp",), ("nxp",)),
    "sas institute": (("sas",), ("sas",)),
    "abb": (("abb",), ("abb",)),
    "ubs": (("ubs",), ("ubs",)),
    "emc": (("dell",), ("dell",)),
    "dfs corporate services": (("discover",), ("discover financial", "discover")),
    "massachusetts institute of technology": (("mit",), ("mit", "massachusetts institute of technology")),
    "new york university": (("nyu",), ("nyu", "new york university")),
    "univ of california san francisco": (("ucsf",), ("ucsf",)),
    "university of california san francisco": (("ucsf",), ("ucsf",)),
    "bnp paribas": (("bnpparibas",), ("bnp paribas",)),
    "td bank": (("td",), ("td bank",)),
    "eli lilly": (("lilly",), ("lilly", "eli lilly")),
}


def alias_for(display_name: str) -> tuple[list[str], list[str]]:
    """(slugs, brand forms) from ALIASES for this employer, or ([], []). The longest matching
    key wins, so "ernst and young" is not shadowed by a shorter key."""
    norm = normalize_employer(display_name)
    best = ""
    for key in ALIASES:
        if (norm == key or norm.startswith(key + " ")) and len(key) > len(best):
            best = key
    if not best:
        return [], []
    slugs, brands = ALIASES[best]
    return list(slugs), list(brands)


def has_brand(display_name: str) -> bool:
    """Is there anything a board's own name could confirm this employer by — a distinctive
    brand word, or a curated alias? Without one, discovery doesn't even guess."""
    return bool(distinctive_tokens(display_name) or alias_for(display_name)[1])


_TRIM = _FILLER | _GENERIC | _LEGAL


def core_tokens(display_name: str) -> list[str]:
    """The name with its trailing filler, legal form and geography trimmed ("Cognizant
    Technology Solutions Us" -> ["cognizant"], "Nokia Of America" -> ["nokia"]); the leading
    words are kept as they are, so a brand made of ordinary words survives."""
    toks = _tokens(display_name)
    while len(toks) > 1 and toks[-1] in _TRIM:
        toks = toks[:-1]
    return toks


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


MAX_SLUGS = 7                   # slug guesses per employer per ATS (each one is a request)


class SlugGuess(tuple):
    """(slug, kind, need): `kind` is "brand" (one word: a truncation or a lone brand word),
    "joined" (several words run together), or "alias" (a curated short form); `need` is how
    many of the sponsor's distinctive words a board's own name must show for it (a curated
    alias brand form on the board satisfies any need). `blind` says whether the slug is
    unambiguous enough to trust on an ATS that exposes no employer name (Ashby, Lever)."""
    __slots__ = ()

    def __new__(cls, slug: str, kind: str, need: int, blind: bool = False):
        return super().__new__(cls, (slug, kind, need, blind))

    slug = property(lambda self: self[0])
    kind = property(lambda self: self[1])
    need = property(lambda self: self[2])
    blind = property(lambda self: self[3])


def slug_guesses(display_name: str) -> list[SlugGuess]:
    """Every slug worth probing for an employer, most likely first, each with the identity
    bar a board at it must clear. Widened 2026-10-12 (the 2026-10-11 dry run accepted 2 of
    185): beside the leading word and the whole name run together, it now tries the curated
    abbreviation, the brand words joined without the filler ("thermofisher"), the name with
    its trailing filler cut ("nokia" from "Nokia Of America"), the first two words joined
    ("goldmansachs", "statestreet"), and a lone distinctive word that is not the first one
    ("schwab" from "Charles Schwab"). Only the whole name run together is trusted blind on
    Ashby/Lever: a shortened join can be an ordinary word (live 2026-10-12: "Health Care Service
    Corporation" trimmed to "healthcare", a Lever board of some other company). Empty when
    nothing could confirm a hit."""
    dist = distinctive_tokens(display_name)
    alias_slugs, alias_brands = alias_for(display_name)
    if not dist and not alias_brands:
        return []
    toks = _tokens(display_name)
    multi = min(2, len(dist)) if dist else 1

    def need_for(words: list[str]) -> int:
        if not dist:
            return 1
        return 1 if set(dist) <= set(words) else multi

    out: list[SlugGuess] = []
    seen: set[str] = set()

    def add(slug, kind, words, blind=False):
        if not slug or slug in seen or len(slug) < 2 or not valid_board_id(slug):
            return
        seen.add(slug)
        out.append(SlugGuess(slug, kind, need_for(words) if kind != "alias" else multi, blind))

    lead = brand_slug(display_name)
    if lead and dist:
        add(lead, "brand", [lead])
    for a in alias_slugs:
        add(a, "alias", [])
    if dist:
        core = core_tokens(display_name)
        if len(dist) >= 2:
            j = "".join(dist)
            add(j, "joined", dist)
        if len(core) >= 2:
            j = "".join(core)
            add(j, "joined", core)
        if len(toks) >= 3 and toks[1] not in _LEGAL:
            add("".join(toks[:2]), "joined", toks[:2])
        if len(toks) >= 2:
            add("".join(toks), "joined", toks, blind=len("".join(toks)) >= 8)
        for t in dist:                   # "schwab", "stanford": a brand word that isn't first
            if t != lead and len(t) >= 5 and t not in _FILLER and t not in _INSTITUTION:
                add(t, "brand", [t])
        if len(toks) >= 2:
            add("-".join(toks), "joined", toks, blind=len("-".join(toks)) >= 8)
    return out[:MAX_SLUGS]


def slug_candidates(display_name: str) -> list[str]:
    """Every slug worth probing for an employer, in probe order (see slug_guesses). Empty
    when the name has no brand word or alias to verify against. Which of these are actually
    TRUSTED depends on the ATS — see discover_for_company."""
    return [g.slug for g in slug_guesses(display_name)]


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


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _alias_hit(evidence: str, brands) -> bool:
    """Does the evidence show one of the curated brand forms? A one-word form must be a whole
    word ("ibm", never inside "ibmx"); a longer phrase may also appear squashed ("Capital One"
    in "CapitalOne_Careers")."""
    toks = _tokens(evidence)
    joined = " " + " ".join(toks) + " "
    sq = _squash(evidence)
    for b in brands:
        bt = _tokens(b)
        if not bt:
            continue
        if f" {' '.join(bt)} " in joined:
            return True
        if len(bt) > 1 and len("".join(bt)) >= 6 and "".join(bt) in sq:
            return True
    return False


def foreign_qualified(evidence: str, sponsor_name: str, brand_words=None) -> bool:
    """True when every mention of the sponsor's brand in a board's (short) name is qualified
    by a word that names a DIFFERENT employer: "Cadence Bank" for Cadence Design Systems,
    "Apple Bank" for Apple, "Hitachi Energy" for Hitachi Vantara, "Ford Foundation" for Ford
    Motor. Only an institution / line-of-business word (bank, hospital, college, foods,
    energy, staffing...) that the sponsor's own name lacks counts against it: a subsidiary's
    or region's wording is tolerated ("Netflix Animation Studios", "FactSet Brasil
    Consultoria" are Netflix's and FactSet's own postings). A board that never names the brand as a
    word (a squashed spelling) has nothing to qualify, so this says False."""
    brand = set(brand_words if brand_words is not None else distinctive_tokens(sponsor_name))
    for b in alias_for(sponsor_name)[1]:
        brand.update(_tokens(b)[:1])
    own = set(_tokens(sponsor_name))
    for b in alias_for(sponsor_name)[1]:
        own.update(_tokens(b))
    toks = _tokens(evidence)
    mentions = [i for i, t in enumerate(toks) if t in brand]
    if not mentions:
        return False
    for i in mentions:
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if nxt in own or nxt in brand or nxt not in _INSTITUTION:
            return False                         # one clean mention is enough
    return True


def confirms(evidence: str, sponsor_name: str, need: int = 1, *, squash: bool = False) -> bool:
    """Does a board's own name (or a posting's hiring organization) confirm it is this
    sponsor? It must show `need` of the sponsor's distinctive brand words (or one of its
    curated alias forms), and the brand must not be qualified as someone else's
    (foreign_qualified). `squash` also counts a long brand word spelled without its spaces
    ("Black Rock" for BlackRock), the Workday site-name rule."""
    dist = distinctive_tokens(sponsor_name)
    count = name_match_count(evidence, dist, sponsor_name) if dist else 0
    if squash and dist:
        sq = _squash(evidence)
        count = max(count, sum(1 for t in dist if len(t) >= 6 and t in sq))
    if count < need and not _alias_hit(evidence, alias_for(sponsor_name)[1]):
        return False
    return not foreign_qualified(evidence, sponsor_name)


_MENTION_NEXT = re.compile(r"[ \t]+([A-Z][A-Za-z&'-]*)")


def prose_confirms(text: str, sponsor_name: str, need: int = 1, min_mentions: int = 2) -> bool:
    """Does a job description's own prose say it is this sponsor's posting? For a Workday
    tenant whose postings carry no hiring-organization name (Cadence's do not), the JD is the
    only place the employer names itself ("At Cadence, we..."). Counts CAPITALIZED mentions of
    the sponsor's brand words (a lowercase "visa" or "target" in a sentence is not the brand):
    `need` distinct brand words (or an alias form) must appear, at least `min_mentions` of the
    mentions must be clean, and clean ones must outnumber those qualified by a different
    employer's word ("Cadence Bank is..."). Two clean mentions, not one, because a passing
    reference to another company is usually a single one."""
    if not text:
        return False
    dist = distinctive_tokens(sponsor_name)
    _, brands = alias_for(sponsor_name)
    words = list(dist) + [b for b in brands if len(_tokens(b)) == 1]
    own = set(_tokens(sponsor_name))
    for b in brands:
        own.update(_tokens(b))
    found: set[str] = set()
    clean = foreign = 0
    for w in words:
        for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", text, re.I):
            if not m.group(0)[:1].isupper():
                continue
            found.add(w)
            nxt = _MENTION_NEXT.match(text, m.end())
            nw = (nxt.group(1).lower() if nxt else "")
            nw = normalize_employer(nw) if nw else ""
            if nw and nw not in own and nw not in _SAFE_QUALIFIERS and nw not in words:
                foreign += 1
            else:
                clean += 1
    phrase = any(len(_tokens(b)) > 1 and re.search(r"(?<![A-Za-z])" + r"\W+".join(
        re.escape(t) for t in _tokens(b)) + r"(?![A-Za-z])", text, re.I) for b in brands)
    enough = len(found & set(dist)) >= need or phrase or bool(found - set(dist))
    return enough and clean >= min_mentions and clean > foreign


def discover_for_company(display_name: str, atss=DISCOVER_ATS, fetch=fetch_json,
                         sleep=None, reject=None) -> dict | None:
    """Find the first live, IDENTITY-CONFIRMED public ATS board for one employer, or None.
    On the name lane (Greenhouse/Workable/SmartRecruiters) every guessed slug is allowed but
    the hit must be confirmed by the board's own name (`confirms`, at the slug's own bar: a
    truncated leading word of a multi-brand name needs two brand words, so "Archer" is not
    Archer Daniels Midland); off it (Ashby/Lever) only the unambiguous long joined forms are
    tried, and a hit is trusted as-is (a concatenated full name is near-unique).
    `reject(ats, slug, reason)`, when given, is called for every LIVE board that was turned
    down, so a crawl can report what it rejected and why (sourcing/growth.py)."""
    guesses = slug_guesses(display_name)
    if not guesses:
        return None                          # no verifiable brand word -> don't guess
    dist = distinctive_tokens(display_name)
    for ats in atss:
        if ats == "workday":
            from .workday import discover_workday       # lazy: workday.py builds on this module
            quiet = sleep or None
            hit = discover_workday(display_name, fetch=fetch, reject=reject, sleep=quiet)
            if hit:
                return {"company": display_name, "ats": "workday", "board_id": hit.board_id,
                        "jobs": hit.jobs}
            continue
        cands = guesses if ats in NAME_LANE else [g for g in guesses if g.blind]
        for g in cands:
            n = probe(ats, g.slug, fetch=fetch)
            if sleep:
                sleep()
            if n <= 0:
                continue
            if ats in NAME_LANE:
                nm = board_name(ats, g.slug, fetch=fetch)
                if not confirms(nm, display_name, g.need):
                    if reject:
                        reject(ats, g.slug, f"board is named {nm or '(unnamed)'!r}; needs "
                                            f"{g.need} of {dist} to be {display_name!r}")
                    continue                 # board is a different company, or too weak a match
            return {"company": display_name, "ats": ats, "board_id": g.slug, "jobs": n}
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
