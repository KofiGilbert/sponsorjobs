"""Which kind of job is this? The labels a choice is matched against (docs/applicant-brain.md,
section 6), and which choices win when two disagree.

Every job gets three labels once:

* **job family** from the title (``sourcing/filters.py role_family``), sharpened by the top tag
  of ``sourcing/jobtags.py job_tags`` when the title says nothing. No model;
* **industry** of the employer as a NAICS prefix: the sponsor record's code, else the JD's tags
  (banking, lending or investing mean Finance & Insurance, ``52``), else one small model call per
  new company, made only when a choice needs the industry to decide;
* **company type**: startup, scaleup, established, public sector, nonprofit, academic or unknown,
  from the same small model call, also only when a choice needs it.

The industry and the company type are cached per company in ``company_class``. The person's own
correction (source ``person``) wins for good: no later lookup or model call replaces it.

The model call (``classify_company``) reads the company name and the first 1,500 characters of
the ad. It never sees anything about the person. Unknown is a real answer: a choice for
startups never matches a company whose type is unknown.

Precedence: company, then company type, then industry (a longer NAICS prefix beats a shorter
one), then job family, then all jobs. Inside one level, the newer choice wins.
"""
from __future__ import annotations

import json
import re
import time

from applicant.store import Store

ALL, FAMILY, INDUSTRY, COMPANY_TYPE, COMPANY = "all", "job_family", "industry", "company_type", "company"
SCOPE_KINDS = (ALL, FAMILY, INDUSTRY, COMPANY_TYPE, COMPANY)

# The job families of sourcing/filters.py role_family, in its order.
FAMILIES = ("Data & AI", "Engineering", "Product & Program", "Design", "Sales", "Marketing",
            "Healthcare", "Finance & Accounting", "People / HR", "Operations")

# A job tag (sourcing/jobtags.py) that names a family when the title does not.
_TAG_FAMILY = {
    "Software Engineering": "Engineering", "Cloud & Infrastructure": "Engineering",
    "Cybersecurity": "Engineering", "QA & Testing": "Engineering", "Systems & Architecture": "Engineering",
    "Database Administration": "Engineering", "Mechanical Engineering": "Engineering",
    "Data Science & Analytics": "Data & AI", "Machine Learning & AI": "Data & AI",
    "Data Engineering": "Data & AI", "Business Analysis": "Data & AI",
    "Product Management": "Product & Program", "Project & Program Management": "Product & Program",
    "UX & Design": "Design", "Sales": "Sales", "Marketing": "Marketing",
    "Finance & Accounting": "Finance & Accounting", "Banking & Lending": "Finance & Accounting",
    "Investment & Trading": "Finance & Accounting", "Human Resources": "People / HR",
    "Operations": "Operations", "Supply Chain & Logistics": "Operations",
}

# JD tags that settle the employer's industry when no sponsor record does.
_TAG_INDUSTRY = {"Banking & Lending": "52", "Investment & Trading": "52"}

COMPANY_TYPES = ("startup", "scaleup", "established", "public_sector", "nonprofit", "academic")
UNKNOWN = "unknown"

# How a scope reads in a sentence ("Hide Viva for other bank and finance jobs too?").
_INDUSTRY_WORDS = {
    "11": "farming", "21": "energy and mining", "22": "utilities", "23": "construction",
    "31": "manufacturing", "32": "manufacturing", "33": "manufacturing", "42": "wholesale",
    "44": "retail", "45": "retail", "48": "transport and logistics", "49": "transport and logistics",
    "51": "tech and media", "52": "bank and finance", "53": "real estate",
    "54": "consulting and professional services", "55": "holding company", "56": "staffing and support services",
    "61": "education", "62": "health care", "71": "arts and entertainment", "72": "hospitality",
    "81": "services", "92": "government",
    "522": "bank and lending", "523": "investment", "524": "insurance", "5415": "software and IT services",
}
_FAMILY_WORDS = {
    "Data & AI": "data and AI", "Engineering": "engineering", "Product & Program": "product and program",
    "Design": "design", "Sales": "sales", "Marketing": "marketing", "Healthcare": "healthcare",
    "Finance & Accounting": "finance and accounting", "People / HR": "people and HR",
    "Operations": "operations",
}
_TYPE_WORDS = {"startup": "startup", "scaleup": "scaleup", "established": "large company",
               "public_sector": "public sector", "nonprofit": "nonprofit", "academic": "university"}

# The fixed table that turns the person's scope words in chat into a scope (section 8.2).
# Longest phrases first; a word must stand alone.
_SCOPE_WORDS: list[tuple[str, str, str]] = [
    (r"investment bank\w*|asset manag\w*|hedge funds?|private equity", INDUSTRY, "52"),
    (r"banks?|banking|financ\w*|fintech|insurance|insurers?|lenders?|lending", INDUSTRY, "52"),
    (r"health ?care|hospitals?|clinical|medical|pharma\w*", INDUSTRY, "62"),
    (r"government|public sector|federal|state agenc\w*", COMPANY_TYPE, "public_sector"),
    (r"non-?profits?|ngos?|charit\w*", COMPANY_TYPE, "nonprofit"),
    (r"universit\w*|academi\w*|research labs?", COMPANY_TYPE, "academic"),
    (r"start-?ups?|early[- ]stage|seed[- ]stage", COMPANY_TYPE, "startup"),
    (r"scale-?ups?|growth[- ]stage", COMPANY_TYPE, "scaleup"),
    (r"big (?:companies|firms|corporates?)|large (?:companies|firms)|enterprises?|corporates?|fortune 500",
     COMPANY_TYPE, "established"),
    (r"consulting|consultanc\w*", INDUSTRY, "54"),
    (r"data|analytics|analyst|machine learning|\bai\b", FAMILY, "Data & AI"),
    (r"software|engineering|developer|tech", FAMILY, "Engineering"),
    (r"product|program", FAMILY, "Product & Program"),
    (r"design", FAMILY, "Design"),
    (r"sales", FAMILY, "Sales"),
    (r"marketing", FAMILY, "Marketing"),
    (r"accounting|audit", FAMILY, "Finance & Accounting"),
    (r"\bhr\b|recruiting|people", FAMILY, "People / HR"),
    (r"operations|supply chain|logistics", FAMILY, "Operations"),
    (r"every|all|any", ALL, ""),
]
_SCOPE_RX = [(re.compile(r"\b(?:" + rx + r")\b", re.I), k, v) for rx, k, v in _SCOPE_WORDS]

PROMPT_CHARS = 1500                     # how much of the ad the company call reads

# Where the visa sponsor records come from (ui/app.py sets it to its sponsor store at start-up).
_SPONSOR_SOURCE = None


def set_sponsor_source(fn) -> None:
    """``fn()`` returns an object with ``lookup(company)`` (sourcing/sponsors.py SponsorDB)."""
    global _SPONSOR_SOURCE
    _SPONSOR_SOURCE = fn


def sponsor_source():
    try:
        return _SPONSOR_SOURCE() if callable(_SPONSOR_SOURCE) else None
    except Exception:                          # noqa: BLE001 - no sponsor data: the JD tags still work
        return None


# ---------------------------------------------------------------------------- companies
def company_key(name) -> str:
    from applicant.facts import company_key as _ck
    return _ck(name)


def _digits(code) -> str:
    return re.sub(r"\D", "", str(code or ""))


def cached_class(st: Store | None, company: str) -> dict | None:
    """The company's cached industry and type (or None)."""
    key = company_key(company)
    if st is None or not key:
        return None
    rows = st.q("SELECT * FROM company_class WHERE company=?", (key,))
    return dict(rows[0]) if rows else None


def set_class(st: Store, company: str, *, industry: str | None = None, industry_label: str = "",
              company_type: str | None = None, source: str = "") -> None:
    """Cache what we learned about a company. A value the person set (source ``person``) is never
    replaced by a lookup or a model call; the person's own correction always lands."""
    key = company_key(company)
    if not key:
        return
    with st.tx() as c:
        cur = c.execute("SELECT * FROM company_class WHERE company=?", (key,)).fetchone()
        row = dict(cur) if cur else {"company": key, "industry": "", "industry_label": "", "industry_source": "",
                                     "company_type": UNKNOWN, "type_source": ""}
        if industry is not None and (row["industry_source"] != "person" or source == "person"):
            row["industry"], row["industry_label"], row["industry_source"] = _digits(industry), industry_label, source
        if company_type is not None and (row["type_source"] != "person" or source == "person"):
            ct = company_type if company_type in COMPANY_TYPES else UNKNOWN
            row["company_type"], row["type_source"] = ct, source
        c.execute("INSERT OR REPLACE INTO company_class (company, industry, industry_label, industry_source,"
                  " company_type, type_source, updated_at) VALUES (?,?,?,?,?,?,?)",
                  (key, row["industry"], row["industry_label"], row["industry_source"], row["company_type"],
                   row["type_source"], time.time()))


COMPANY_SYSTEM = (
    "You label an employer for a job seeker's resume choices. From the company name and the start "
    "of its job ad, give (1) its industry as a 2-digit NAICS sector code (52 finance and insurance, "
    "51 information and software, 54 professional services and consulting, 62 health care, 61 "
    "education, 92 public administration, 31 manufacturing, 44 retail), or \"\" when the ad does not "
    "say, and (2) its company type: startup (seed to Series B, a small young company), scaleup "
    "(Series C or later, fast growing), established (a large or long-standing company), "
    "public_sector, nonprofit, academic, or unknown. Answer unknown when the ad does not say: a "
    "wrong guess is worse than unknown. Return ONLY JSON: {\"naics\": \"52\", \"type\": \"startup\"}."
)


def _company_user(company: str, jd: str) -> str:
    return f"<company>{company}</company>\n<ad>{(jd or '')[:PROMPT_CHARS]}</ad>"


def parse_company(raw: str) -> tuple[str, str]:
    """(naics digits, company type) from the model's reply; ("", "unknown") when unreadable."""
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    naics = _digits(d.get("naics"))[:4] if isinstance(d, dict) else ""
    ct = str(d.get("type") or "").strip().lower() if isinstance(d, dict) else ""
    return naics, (ct if ct in COMPANY_TYPES else UNKNOWN)


def classify_company(st: Store, llm, company: str, jd: str) -> dict:
    """ONE small model call for a new company (industry and type together), cached. Never called
    for a company already labelled. Returns the cached row."""
    row = cached_class(st, company) or {}
    if row.get("type_source") and row.get("industry_source") or llm is None or not hasattr(llm, "classify_company"):
        return row
    try:
        raw = llm.classify_company(COMPANY_SYSTEM, _company_user(company, jd))
    except Exception:                          # noqa: BLE001 - no label then; unknown matches nothing
        raw = ""
    naics, ct = parse_company(raw)
    set_class(st, company, industry=None if row.get("industry_source") else naics,
              company_type=None if row.get("type_source") else ct, source="model")
    try:
        st.set_meta("company_calls", str(int(st.meta("company_calls") or 0) + 1))
    except Exception:                          # noqa: BLE001
        pass
    return cached_class(st, company) or {}


# ---------------------------------------------------------------------------- the job's labels
def family_of(title: str, jd: str = "") -> str:
    from sourcing.filters import role_family
    fam = role_family(title or "")
    if fam:
        return fam
    try:
        from sourcing.jobtags import job_tags
        for tag in job_tags(title or "", jd or "", limit=3):
            if tag in _TAG_FAMILY:
                return _TAG_FAMILY[tag]
    except Exception:                          # noqa: BLE001
        pass
    return ""


def industry_from_tags(title: str, jd: str) -> str:
    try:
        from sourcing.jobtags import job_tags
        for tag in job_tags(title or "", jd or "", limit=4):
            if tag in _TAG_INDUSTRY:
                return _TAG_INDUSTRY[tag]
    except Exception:                          # noqa: BLE001
        pass
    return ""


def labels(st: Store | None, job: dict, *, sponsors=None, llm=None, rules=None) -> dict:
    """{family, industry, company_type, company} for one job. ``job`` = {title, company, jd}.
    ``sponsors`` has ``lookup(company)`` (the visa sponsor data). The model is called only when a
    choice in ``rules`` needs a label nothing else settled."""
    title = str(job.get("title") or "")
    company = str(job.get("company") or "").strip()
    jd = str(job.get("jd") or job.get("jd_text") or "")
    out = {"family": family_of(title, jd), "industry": "", "company_type": UNKNOWN, "company": company}
    row = cached_class(st, company) if company else None
    if row:
        out["industry"] = row.get("industry") or ""
        out["company_type"] = row.get("company_type") or UNKNOWN
    if not out["industry"] and company and sponsors is not None:
        try:
            rec = sponsors.lookup(company)
        except Exception:                      # noqa: BLE001
            rec = None
        naics = _digits(getattr(rec, "naics", "")) if rec else ""
        if naics:
            out["industry"] = naics
            if st is not None:
                from sourcing.sponsors import naics_industry
                set_class(st, company, industry=naics, industry_label=naics_industry(naics), source="naics")
    if not out["industry"]:
        tagged = industry_from_tags(title, jd)
        if tagged:
            out["industry"] = tagged
            if st is not None and company:
                set_class(st, company, industry=tagged, source="jobtags")
    need = {r.get("scope_kind") for r in (rules or []) if r.get("status", "active") == "active"}
    want_type = COMPANY_TYPE in need and out["company_type"] == UNKNOWN
    want_industry = INDUSTRY in need and not out["industry"]
    if (want_type or want_industry) and st is not None and company and llm is not None:
        row = cached_class(st, company) or {}
        if not row.get("type_source") or (want_industry and not row.get("industry_source")):
            row = classify_company(st, llm, company, jd)
        out["industry"] = out["industry"] or row.get("industry") or ""
        out["company_type"] = row.get("company_type") or UNKNOWN
    return out


# ---------------------------------------------------------------------------- matching
def matches(rule: dict, lab: dict) -> bool:
    """Does this choice's scope cover this job?"""
    kind, val = rule.get("scope_kind") or ALL, str(rule.get("scope_value") or "")
    if kind == ALL:
        return True
    if kind == FAMILY:
        return bool(val) and val == lab.get("family")
    if kind == INDUSTRY:
        ind = _digits(lab.get("industry"))
        return bool(val) and bool(ind) and ind.startswith(_digits(val))
    if kind == COMPANY_TYPE:
        ct = lab.get("company_type") or UNKNOWN
        return ct != UNKNOWN and ct == val
    if kind == COMPANY:
        # The company the choice names, or a longer name of it ("Stanbic" covers "Stanbic Bank
        # Ghana"); a shorter name ("Acme" for an "Acme Bank" choice) is another employer.
        x, y = company_key(val), company_key(lab.get("company") or "")
        return bool(x) and bool(y) and (x == y or (len(x) >= 4 and y.startswith(x + " ")))
    return False


_LEVEL = {ALL: 0, FAMILY: 1, INDUSTRY: 2, COMPANY_TYPE: 3, COMPANY: 4}


def specificity(rule: dict) -> tuple:
    """A sort key: higher is more specific. A longer NAICS prefix beats a shorter one; inside a
    level, the newer choice wins."""
    kind = rule.get("scope_kind") or ALL
    depth = len(_digits(rule.get("scope_value"))) if kind == INDUSTRY else 0
    return (_LEVEL.get(kind, 0), depth, float(rule.get("created_at") or 0), int(rule.get("id") or 0))


def applicable(rules: list[dict], lab: dict) -> list[dict]:
    """The active choices whose scope covers this job, most specific first."""
    hit = [r for r in rules or [] if (r.get("status") or "active") == "active" and matches(r, lab)]
    return sorted(hit, key=specificity, reverse=True)


# ---------------------------------------------------------------------------- words
def words(kind: str, value: str = "") -> str:
    """How a scope reads: "bank and finance jobs", "startup jobs", "Acme Bank", "every job"."""
    if kind == ALL:
        return "every job"
    if kind == COMPANY:
        return str(value or "this company")
    if kind == FAMILY:
        return f"{_FAMILY_WORDS.get(value, str(value).lower())} jobs"
    if kind == COMPANY_TYPE:
        return f"{_TYPE_WORDS.get(value, str(value).replace('_', ' '))} jobs"
    if kind == INDUSTRY:
        d = _digits(value)
        for n in (len(d), 3, 2):
            w = _INDUSTRY_WORDS.get(d[:n])
            if w:
                return f"{w} jobs"
        return "jobs in this industry"
    return "these jobs"


def offer(lab: dict) -> tuple[str, str] | None:
    """The scope the builder offers after a change: the job's most specific known label, the
    industry (as its sector) when known, else the company type, else the job family."""
    ind = _digits(lab.get("industry"))
    if ind:
        return INDUSTRY, ind[:2]
    if (lab.get("company_type") or UNKNOWN) != UNKNOWN:
        return COMPANY_TYPE, lab["company_type"]
    if lab.get("family"):
        return FAMILY, lab["family"]
    return None


def from_words(text: str, companies=()) -> tuple[str, str] | None:
    """The person's scope words in chat ("for banks", "on startup jobs", "at Acme") as a scope,
    by the fixed table. A company the person named that is one of ``companies`` is a company
    scope. None when the words name no scope we know (then nothing is proposed)."""
    t = str(text or "").strip()
    if not t:
        return ALL, ""
    from applicant.facts import same_company
    for name in companies or ():
        if name and re.search(r"\b" + re.escape(str(name).lower()) + r"\b", t.lower()):
            return COMPANY, str(name)
    m = re.search(r"\bat\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,3})", t)
    if m and not any(same_company(m.group(1), c) for c in companies or ()):
        return COMPANY, m.group(1).strip()
    for rx, kind, val in _SCOPE_RX:
        if rx.search(t):
            return kind, val
    return None
