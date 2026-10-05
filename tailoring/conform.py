"""Conform user/LLM input to the reference template's conventions (CLAUDE.md §8).

The output CV must read like ``config/resume_shetty.tex``: full-month dates
("December 2022"), full degree names ("Master of Science …"), and generated prose
with no AI-tell em/en-dashes. These are pure, deterministic reformatters applied
in ``normalize_profile`` so every render conforms — they reformat, never invent.
"""

from __future__ import annotations

import re

_FULL = ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
_MONTH = {}
for _i, _m in enumerate(_FULL, 1):
    _MONTH[_m.lower()] = _m
    _MONTH[_m.lower()[:3]] = _m          # jan, feb, …
    _MONTH[str(_i)] = _m                 # 1, 2, …
    _MONTH[f"{_i:02d}"] = _m             # 01, 02, …
_MONTH["sept"] = "September"

# The template abbreviates LONG month names (>=6 letters) and keeps the short ones
# full — "Jan 2018", "Feb 2023", "Nov 2021" but "March/April/May/June/July" in full.
# Every rendered date is reduced to this form, even if the person typed it out.
_ABBR = {"January": "Jan", "February": "Feb", "March": "March", "April": "April",
         "May": "May", "June": "June", "July": "July", "August": "Aug",
         "September": "Sept", "October": "Oct", "November": "Nov", "December": "Dec"}


def _abbr(full_month: str) -> str:
    return _ABBR.get(full_month, full_month)


# Month -> 1..12 for both full and abbreviated forms, so a formatted range can be
# checked for chronological validity (start must not come after end).
_MONTH_NUM = {}
for _i, _m in enumerate(_FULL, 1):
    _MONTH_NUM[_m] = _i
    _MONTH_NUM[_ABBR[_m]] = _i


def _month_index(tok: str) -> int | None:
    """A comparable ordinal (year*12 + month) for a formatted token, or None when it
    isn't a concrete date (e.g. 'Present') — those never fail the ordering check."""
    tok = tok.strip()
    m = re.search(r"([A-Za-z]{3,9})\.?\s+((?:19|20)\d\d)", tok)
    if m and m.group(1) in _MONTH_NUM:
        return int(m.group(2)) * 12 + _MONTH_NUM[m.group(1)]
    if re.fullmatch(r"(?:19|20)\d\d", tok):
        return int(tok) * 12          # year-only: compare by year
    return None


def _range_is_chronological(result: str) -> bool:
    """False only when a two-ended range is provably impossible — its start comes AFTER
    its end (e.g. 'Dec 2025 - Feb 2020', a mis-attributed date). 'X - Present' and single
    dates are always fine."""
    if " - " not in result:
        return True
    start, _, end = result.partition(" - ")
    if end.strip() == "Present":
        return True
    si, ei = _month_index(start), _month_index(end)
    return not (si is not None and ei is not None and si > ei)


_PRESENT = {"present", "current", "now", "ongoing", "date", "today", "ongoing.",
            "presently", "day", "date.", "current."}
_DATE_PREFIX = re.compile(
    r"(?i)\b(graduated|graduating|expected|anticipated|class\s+of|completed|grad\.?)\b[:\s]*")
_RANGE_SPLIT = re.compile(r"\s*(?:--|—|–|-|\bto\b|\bthrough\b|\buntil\b)\s*", re.I)


def _fmt_token(tok: str) -> str:
    tok = tok.strip().strip(",.")
    if not tok:
        return ""
    if tok.lower() in _PRESENT:
        return "Present"
    # "December 2022" / "Dec 2022" / "Dec. 2022" -> "Dec 2022" (template abbreviation)
    m = re.search(r"([A-Za-z]{3,9})\.?\s+((?:19|20)\d\d)", tok)
    if m and m.group(1).lower() in _MONTH:
        return f"{_abbr(_MONTH[m.group(1).lower()])} {m.group(2)}"
    # numeric "12/2022" or "12-2022"
    m = re.search(r"\b(1[0-2]|0?[1-9])[/.\-]((?:19|20)\d\d)\b", tok)
    if m and m.group(1) in _MONTH:
        return f"{_abbr(_MONTH[m.group(1)])} {m.group(2)}"
    # year only
    y = re.search(r"\b(19|20)\d\d\b", tok)
    if y:
        return y.group(0)
    return tok


# The role is still held — end date should render as "Present". Covers explicit
# "present/current/ongoing" and natural phrasing ("still working there", "to date",
# "currently", "I'm here"). Anchored on word boundaries so "presented" won't match.
_ONGOING = re.compile(
    r"(?i)(\bstill\b|\bcurrent(?:ly)?\b|\bpresent\b|\bongoing\b|\bto\s+date\b|"
    r"\bto\s+now\b|\bto\s+present\b|\bpresently\b|i'?m\s+(?:still\s+)?(?:working|there|here|employed)|"
    r"\bwork\s+(?:there|here)\b|current\s+role)")


def format_dates(raw: str) -> str:
    """Reformat a date or date-range to the TEMPLATE style: abbreviated month + year
    ('Jan 2018', 'Feb 2023'; short months May/June/July kept full), ranges joined by
    ' - ', and an ongoing role rendered '… - Present'. Never invents a date."""
    raw0 = str(raw or "")
    raw = _DATE_PREFIX.sub("", raw0).strip()
    if not raw:
        return ""
    # The extractor sometimes crams contact info or a note into a date field
    # ("Jane Doe, jane@example.com, (555) 010 - December 2025"). Reject
    # such garbage so nothing broken renders and the gate asks for the real date.
    if ("@" in raw or "please provide" in raw.lower() or "corrupt" in raw.lower()
            or len(raw) > 55 or raw.count(",") >= 3):
        return ""
    ongoing = bool(_ONGOING.search(raw0))
    result = ""
    # An ongoing role given as "since Feb 2021" / "from 2020" (no explicit end)
    # renders as a range ending in Present. "from X to Y" falls through to the range.
    m = re.match(r"(?i)^(?:since|starting|from)\s+(.+)$", raw)
    if m:
        inner = m.group(1).strip()
        if not re.search(r"(?:--|—|–|-|\bto\b|\bthrough\b|\buntil\b|present|current|now|ongoing|date)",
                         inner, re.I):
            result = f"{_fmt_token(inner)} - Present"
        else:
            raw = inner
    if not result:
        parts = [p for p in _RANGE_SPLIT.split(raw) if p.strip()]
        result = _fmt_token(raw) if len(parts) <= 1 else " - ".join(_fmt_token(p) for p in parts[:2])
    # Still-current role but only a start date parsed -> end it at Present.
    if ongoing and result and " - " not in result and result != "Present":
        result = f"{result} - Present"
    # A provably impossible range (start after end) is a mis-attributed date — reject it
    # so nothing nonsensical renders and the gate asks for the real dates.
    if not _range_is_chronological(result):
        return ""
    return result


def extract_date_span(text: str) -> str:
    """Pull a date / date-range out of FREE TEXT ('I've been here since Feb 2023 to
    date') and return it in template form, or '' if none. Understands ongoing phrasing
    ('still there', 'currently') -> Present. Used to capture a role's dates from a
    conversational reply that names a date but not the role."""
    s = str(text or "")
    toks = re.findall(r"[A-Za-z]{3,9}\.?\s+(?:19|20)\d\d|\b(?:19|20)\d\d\b", s)
    if not toks:
        return ""
    if len(toks) >= 2:
        span = f"{toks[0]} to {toks[1]}"
    else:
        span = f"{toks[0]} to present" if _ONGOING.search(s) else toks[0]
    return format_dates(span)


def _token_has_month(tok: str) -> bool:
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(?:19|20)\d\d", tok)
    if m and m.group(1).lower() in _MONTH:
        return True
    return bool(re.search(r"\b(1[0-2]|0?[1-9])[/.\-](?:19|20)\d\d\b", tok))


def date_lacks_month(dates: str) -> bool:
    """True if a date/range has a YEAR but no MONTH (e.g. '2019 - 2020', '2019') —
    which breaks the template's full-month convention ('January 2021 - August 2023').
    'Present' and empty strings don't count; empty is the separate 'missing' case."""
    s = str(dates or "").strip()
    if not s:
        return False
    for tok in _RANGE_SPLIT.split(s):
        tok = tok.strip().strip(",.")
        if not tok or tok.lower() in _PRESENT:
            continue
        if re.search(r"(?:19|20)\d\d", tok) and not _token_has_month(tok):
            return True
    return False


def dummy_months(dates: str) -> str:
    """Fill placeholder months onto a year-only date so it fits the template's format:
    a start year gets 'Jan', an end year 'Dec' ('2019 - 2020' -> 'Jan 2019 - Dec 2020',
    template-abbreviated). Only years without a month are touched; use ONLY when the
    person opted to build without giving real months, and flag the result for review."""
    s = str(dates or "").strip()
    if not s:
        return s
    parts = [p for p in _RANGE_SPLIT.split(s) if p.strip()]
    # A lone bare year becomes a full-year RANGE placeholder ('2021' -> 'Jan 2021 - Dec
    # 2021') so every section shows a month range like the template, not a bare year.
    if len(parts) == 1 and re.fullmatch(r"(?:19|20)\d\d", parts[0].strip().strip(",.")):
        y = parts[0].strip().strip(",.")
        return f"Jan {y} - Dec {y}"
    out = []
    for i, tok in enumerate(parts[:2]):
        tok = tok.strip().strip(",.")
        if tok.lower() in _PRESENT:
            out.append("Present")
        elif re.fullmatch(r"(?:19|20)\d\d", tok):
            out.append(f"{'Jan' if i == 0 else 'Dec'} {tok}")
        else:
            out.append(_fmt_token(tok))
    return " - ".join(out) if len(out) > 1 else (out[0] if out else s)


_DEGREE = {
    "phd": "Doctor of Philosophy", "dphil": "Doctor of Philosophy",
    "btech": "Bachelor of Technology", "mtech": "Master of Technology",
    "be": "Bachelor of Engineering", "me": "Master of Engineering",
    "bs": "Bachelor of Science", "bsc": "Bachelor of Science",
    "ms": "Master of Science", "msc": "Master of Science",
    "ba": "Bachelor of Arts", "ma": "Master of Arts",
    "bcom": "Bachelor of Commerce", "mcom": "Master of Commerce",
    "bba": "Bachelor of Business Administration",
    "llb": "Bachelor of Laws", "llm": "Master of Laws",
}
_DEG_LEAD = re.compile(
    r"^\s*(ph\.?\s?d|d\.?phil|b\.?\s?tech|m\.?\s?tech|b\.?e|m\.?e|b\.?sc|m\.?sc|"
    r"b\.?s|m\.?s|b\.?a|m\.?a|b\.?com|m\.?com|b\.?b\.?a|ll\.?b|ll\.?m)\b\.?", re.I)
_ALREADY_FULL = re.compile(r"^\s*(bachelor|master|doctor|associate|mba|diploma)\b", re.I)


def format_degree(raw: str) -> str:
    """Expand an abbreviated degree to the template's full form
    ('M.S. in AI' -> 'Master of Science in AI'). MBA and already-full or unknown
    forms are left as written (so 'MBA - Business Analytics (STEM)' is preserved)."""
    raw = str(raw or "").strip()
    if not raw or _ALREADY_FULL.match(raw):
        return raw
    m = _DEG_LEAD.match(raw)
    if not m:
        return raw
    key = re.sub(r"[.\s]", "", m.group(1)).lower()
    full = _DEGREE.get(key)
    if not full:
        return raw
    rest = raw[m.end():].strip()
    rest = re.sub(r"^(?:in|of|,|:|-)\s*", "", rest, flags=re.I).strip()
    return f"{full} in {rest}" if rest else full


# --- Template-strict fields: the template is a fixed set of slots. We render only
# what it holds, so a sub-unit it has no slot for (a faculty/college/school-within)
# is trimmed, and the address slot expects a full mailing address, not a bare city.
_SUBUNIT_COMMA = re.compile(
    r",\s*(?:the\s+)?[\w&.' ]*\b(?:faculty|college|department|institute|division|"
    r"graduate\s+school|school\s+of)\b.*$", re.I)
_STREET_WORD = re.compile(
    r"\b(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ct|court|ln|lane|"
    r"way|pl|place|apt|suite|ste|unit|hwy|highway|pkwy|terrace|cir|circle)\b\.?", re.I)


def format_school(school: str) -> str:
    """Keep only the institution name the template renders — trim an appended
    faculty / college / graduate-school clause the template has no slot for
    ('DePaul University | Kellstadt Graduate School of Business' -> 'DePaul
    University'; 'University of Professional Studies, Faculty of Law' -> 'University
    of Professional Studies'). Never invents; only trims a sub-unit, and only after a
    separator so real names like 'London School of Economics' are left intact."""
    s = str(school or "").strip()
    if not s:
        return s
    s = s.split("|", 1)[0].strip()          # anything after '|' is a sub-unit
    m = _SUBUNIT_COMMA.search(s)            # ', Faculty/College/School of …'
    if m:
        s = s[:m.start()].strip()
    return s.strip().strip(",").strip()


def is_full_address(addr: str) -> bool:
    """The template header shows a full mailing address; a bare 'City, ST' isn't one.
    True only when a street number/ZIP or a street-type word is present."""
    a = str(addr or "").strip()
    if not a:
        return False
    return bool(re.search(r"\b\d{3,}\b", a) or _STREET_WORD.search(a))


_AI_DASH = re.compile(r"\s*[—–]\s*")


def strip_ai_dashes(text: str) -> str:
    """Remove AI-tell em/en-dashes from generated prose, using plain phrasing
    instead. Only U+2013/U+2014 are touched — literal hyphens (date ranges,
    'MBA - Business Analytics') are preserved."""
    if not text:
        return text
    t = _AI_DASH.sub(", ", str(text))
    t = re.sub(r",\s*,", ",", t)
    return t.strip().strip(",").strip() if t.strip().endswith(",") else t.strip()


def _clean_bullets(bullets) -> list:
    return [strip_ai_dashes(b) for b in bullets] if isinstance(bullets, list) else bullets


# --- Job titles: strip conversational fragments so a run-on answer like "before
# that I was an Analyst" becomes the real title "Analyst" (never invents one). ----
# Take the text after the LAST conversational lead-in ("… I'm a <title>", "… I was
# an <title>", "worked as a <title>") so a name/prefix can't leak into the title,
# while a real title with a comma ("Research Intern, Project Lab") is left intact.
_TITLE_INNER_LEAD = re.compile(
    r"^.*\b(?:i'?m|i\s+am|i\s+was|i\s+work(?:ed)?\s+as|work(?:ed|ing)?\s+as|"
    r"serv(?:ed|ing)\s+as|i\s+serve[d]?\s+as|my\s+role\s+(?:was|is))\s+(?:an?\s+|the\s+)?",
    re.I)
_TITLE_LEAD = re.compile(
    r"^(?:before\s+that|after\s+that|prior\s+to\s+that|previously|currently|now|then|next|"
    r"and|also|plus|i\s+was|i\s+am|i'?m|i\s+work(?:ed)?(?:\s+as)?|i\s+serve[d]?(?:\s+as)?|"
    r"i\s+held|i'?ve\s+been|i\s+have\s+been|my\s+role\s+(?:was|is)|working\s+as|serving\s+as|"
    r"as|role|position|title)\b[\s,:\-]*", re.I)
_TITLE_ARTICLE = re.compile(r"^(?:an?|the)\b\s+", re.I)


def clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip(",")
    m = _TITLE_INNER_LEAD.match(t)
    if m and t[m.end():].strip():
        t = t[m.end():].strip()
    prev = None
    while t and t != prev:
        prev = t
        t = _TITLE_LEAD.sub("", t).strip()
        t = _TITLE_ARTICLE.sub("", t).strip()
    return t


# --- Locations: template style is "City, XX" — City in full, then a two-letter
# region code (US state, else country). "Accra, Ghana" -> "Accra, GH". -----------
_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
}
_COUNTRIES = {
    "ghana": "GH", "nigeria": "NG", "kenya": "KE", "south africa": "ZA", "egypt": "EG",
    "morocco": "MA", "ethiopia": "ET", "tanzania": "TZ", "uganda": "UG", "rwanda": "RW",
    "ivory coast": "CI", "cote d'ivoire": "CI", "senegal": "SN", "cameroon": "CM",
    "india": "IN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK", "china": "CN",
    "japan": "JP", "south korea": "KR", "singapore": "SG", "malaysia": "MY", "indonesia": "ID",
    "philippines": "PH", "vietnam": "VN", "thailand": "TH", "united arab emirates": "AE",
    "uae": "AE", "saudi arabia": "SA", "qatar": "QA", "israel": "IL", "turkey": "TR",
    "united states": "US", "united states of america": "US", "usa": "US", "u.s.a.": "US",
    "u.s.": "US", "america": "US", "canada": "CA", "mexico": "MX", "brazil": "BR",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE",
    "united kingdom": "GB", "u.k.": "GB", "uk": "GB", "england": "GB", "scotland": "GB",
    "ireland": "IE", "germany": "DE", "france": "FR", "spain": "ES", "portugal": "PT",
    "italy": "IT", "netherlands": "NL", "belgium": "BE", "switzerland": "CH", "sweden": "SE",
    "norway": "NO", "denmark": "DK", "finland": "FI", "poland": "PL", "austria": "AT",
    "australia": "AU", "new zealand": "NZ",
}


def is_region(region: str) -> bool:
    """True if ``region`` is a real state/country — a 2-letter code, or a known US
    state or country name. Guards location capture against false positives like a
    course list ('Machine Learning, Statistics')."""
    r = str(region or "").strip().strip(".")
    if re.fullmatch(r"[A-Za-z]{2}", r):
        return True
    return r.lower() in _US_STATES or r.lower() in _COUNTRIES


def format_location(loc: str) -> str:
    """Reformat a 'City, Region' to the template's 'City, XX' — a two-letter region
    code (US state, else country). Unknown regions and non-'City, Region' strings
    (e.g. 'Remote') are left unchanged; never invents a location."""
    loc = re.sub(r"\s+", " ", str(loc or "")).strip()
    if "," not in loc:
        return loc
    city, region = loc.rsplit(",", 1)
    city, region = city.strip(), region.strip().strip(".")
    if not city or not region:
        return loc
    if re.fullmatch(r"[A-Za-z]{2}", region):        # already a 2-letter code
        return f"{city}, {region.upper()}"
    code = _US_STATES.get(region.lower()) or _COUNTRIES.get(region.lower())
    return f"{city}, {code}" if code else f"{city}, {region}"


def fit_courses(courses: str, budget: int = 175) -> str:
    """Keep the most-relevant courses that fit the template's ~2-line course line —
    dropping WHOLE trailing courses (courses are ordered most-relevant-first), never
    truncating a name, so the Education block never overflows to a 3rd line. Applied
    in conform so the live preview and the PDF show the SAME list (CLAUDE.md §8)."""
    items = [c.strip() for c in str(courses or "").split(",") if c.strip()]
    kept, total = [], 0
    for it in items:
        add = len(it) + (2 if kept else 0)   # account for the ", " separator
        if kept and total + add > budget:
            break
        kept.append(it)
        total += add
    return ", ".join(kept)


def conform_profile(p: dict) -> dict:
    """Apply the template conventions across a normalized profile, in place."""
    for e in p.get("education", []) or []:
        if isinstance(e, dict):
            e["date"] = format_dates(e.get("date", ""))
            e["degree"] = format_degree(e.get("degree", ""))
            if e.get("school"):
                e["school"] = format_school(e["school"])   # institution only, no faculty
            if e.get("courses"):
                e["courses"] = fit_courses(e["courses"])   # ≤ 2 lines, most-relevant kept
            if e.get("location"):
                e["location"] = format_location(e["location"])
    for section in ("experience", "projects"):
        for e in p.get(section, []) or []:
            if not isinstance(e, dict):
                continue
            if e.get("org"):
                e["org"] = format_school(e["org"])   # drop any faculty sub-unit on an org
            if e.get("dates"):
                e["dates"] = format_dates(e["dates"])
            if e.get("location"):
                e["location"] = format_location(e["location"])
            if e.get("title"):
                e["title"] = clean_title(e["title"])
            if isinstance(e.get("bullets"), list):
                e["bullets"] = _clean_bullets(e["bullets"])
            for r in e.get("roles", []) or []:
                if isinstance(r, dict):
                    r["dates"] = format_dates(r.get("dates", ""))
                    if r.get("title"):
                        r["title"] = clean_title(r["title"])
                    if r.get("title_original"):
                        r["title_original"] = clean_title(r["title_original"])
                    r["bullets"] = _clean_bullets(r.get("bullets"))
    for it in p.get("extracurricular", []) or []:
        if isinstance(it, dict):
            it["date"] = format_dates(it.get("date", ""))
            it["bullets"] = _clean_bullets(it.get("bullets"))
    skills = p.get("skills")
    if isinstance(skills, dict):
        p["skills"] = {k: strip_ai_dashes(v) for k, v in skills.items()}
    if isinstance(p.get("interests"), str):
        p["interests"] = strip_ai_dashes(p["interests"])
    return p
