"""Server-side facet filtering + pagination for the jobs feed.

Used by BOTH the hosted kitchen (backend/feed.py) and the local app (ui/app.py) so the two
behave identically. Once the feed is paginated, filtering MUST live server-side: client-side
filtering would only ever see the current page. This mirrors the board's facets exactly:
keyword (title/company), date-posted, work-type, sponsorship visa (multi-select), entry-level.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Compact pay string -> annual multiplier, so an hourly or monthly figure compares on one axis.
_PERIOD_MULT = {"/yr": 1, "/mo": 12, "/wk": 52, "/day": 260, "/hr": 2080}


def salary_value(salary: str) -> int:
    """Annualized numeric value of a compact salary string, for filtering + sorting by pay.
    "$120k-$160k/yr" -> 160000, "$45-$60/hr" -> 124800, "$135k/yr" -> 135000. Uses the TOP of a
    range (what a candidate screens on). Returns 0 when there's no figure, so a row with unknown
    pay reads as "unknown" and drops out when a minimum-pay filter is active, LinkedIn-style."""
    s = (salary or "").strip().lower()
    if not s:
        return 0
    mult = next((m for suf, m in _PERIOD_MULT.items() if s.endswith(suf)), 1)
    vals = []
    for num, k in re.findall(r"([\d,]+(?:\.\d+)?)\s*(k)?", s):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        vals.append(v * 1000 if k else v)
    return int(max(vals) * mult) if vals else 0


# Coarse role family from a title, for salary benchmarking. Mirrors the card's category chips
# (app.js ROLE_CATS) but lives here because the benchmark is computed server-side. First match
# wins, most-specific first. "" when nothing matches (that role just gets no pay comparison).
# NOTE: no forced trailing \b on the group -- several tokens are PREFIXES ("data scien" must match
# "Data Scientist", "financ" -> "Financial"), and a trailing boundary would kill them mid-word.
# Boundaries live on the short/ambiguous tokens only (\bml\b, \bhr\b), and Engineering is checked
# before Sales/Marketing so "Salesforce Developer" lands in Engineering, not Sales.
_ROLE_FAMILIES = [
    (re.compile(r"data scien|data eng|machine learning|\bml\b|\bai\b|analytics|data analyst", re.I), "Data & AI"),
    (re.compile(r"software|developer|\bengineer|programmer|full[- ]?stack|back[- ]?end|front[- ]?end|devops|\bsre\b|platform|cloud", re.I), "Engineering"),
    (re.compile(r"product manager|product owner|\bpm\b|program manager", re.I), "Product & Program"),
    (re.compile(r"\bdesign|\bux\b|\bui\b|creative", re.I), "Design"),
    (re.compile(r"\bsales|account executive|business development|\bbdr\b|\bsdr\b|account manager", re.I), "Sales"),
    (re.compile(r"marketing|\bseo\b|growth|demand gen|content strateg", re.I), "Marketing"),
    (re.compile(r"nurse|clinical|\bhealth|medical|pharma|physician|therapist|patient care", re.I), "Healthcare"),
    (re.compile(r"financ|account(?:ant|ing)|\baudit|\btax\b|controller|treasury", re.I), "Finance & Accounting"),
    (re.compile(r"recruit|talent|\bhr\b|people ops|human resources", re.I), "People / HR"),
    (re.compile(r"operations|logistics|supply chain|warehouse|procurement", re.I), "Operations"),
]


def role_family(title: str) -> str:
    """Coarse role family for a job title ('Senior Data Analyst' -> 'Data & AI'), for grouping
    salaries into comparable buckets. '' when nothing matches."""
    t = title or ""
    for rx, label in _ROLE_FAMILIES:
        if rx.search(t):
            return label
    return ""


def salary_benchmarks(jobs: list[dict], min_samples: int = 5) -> dict[str, tuple[int, int]]:
    """{family: (median_annual_pay, sample_count)} over the jobs that state pay, so a single role's
    pay can be read against 'typical for similar roles here'. Families with fewer than min_samples
    priced roles are omitted (too thin to be a fair benchmark)."""
    import statistics
    from collections import defaultdict
    buckets: dict[str, list[int]] = defaultdict(list)
    for j in jobs:
        v = salary_value(j.get("salary"))
        if v <= 0:
            continue
        fam = role_family(j.get("title") or "")
        if fam:
            buckets[fam].append(v)
    return {fam: (int(statistics.median(vs)), len(vs))
            for fam, vs in buckets.items() if len(vs) >= min_samples}


def salary_insight(job: dict, benchmarks: dict) -> dict | None:
    """How this role's pay compares to its family's median on the board: above / around / below,
    with the median and sample size so the claim is inspectable. None when the role has no pay or
    its family has no benchmark. Bands are +/-15% so 'around' means genuinely typical."""
    v = salary_value(job.get("salary"))
    fam = role_family(job.get("title") or "")
    if v <= 0 or fam not in (benchmarks or {}):
        return None
    median, count = benchmarks[fam]
    if median <= 0:
        return None
    ratio = v / median
    verdict = "above" if ratio >= 1.15 else "below" if ratio <= 0.85 else "around"
    return {"family": fam, "median": median, "count": count, "verdict": verdict}


def _visa_codes(job: dict) -> list[str]:
    return ([v.get("code") for v in (job.get("visa") or [])]
            + [v.get("code") for v in (job.get("nationality_visas") or [])])


def _parse_posted(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt, cut in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:cut], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def apply_facets(jobs: list[dict], *, q: str = "", loc: str = "", days=0, remote: str = "",
                 visa=(), level: str = "", pay=0, sort: str = "", sponsored=False) -> list[dict]:
    """Return the subset matching EVERY active facet (AND across facets; OR within the
    multi-select visa facet, LinkedIn-style). Undated rows survive a date filter (some feeds omit
    a posting date), but a minimum-pay filter DROPS rows with no readable pay (you asked for a
    floor; an unknown can't clear it). `sponsored` keeps ONLY roles whose posting itself STATES visa
    sponsorship (the strongest signal for an international student, distinct from the visa-history
    badge). `sort="pay"` re-orders the survivors highest-pay-first (unknown-pay last); default keeps
    the feed's recency order. Cheap in-memory pass."""
    q = (q or "").strip().lower()
    loc = (loc or "").strip().lower()
    remote = (remote or "").strip().lower()
    level = (level or "").strip().lower()
    sort = (sort or "").strip().lower()
    visa = [v for v in (visa or []) if v]
    # Accept a bool or any string/int form a client sends ("1"/"true"/"on").
    sponsored = str(sponsored).strip().lower() in ("1", "true", "yes", "on")
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    try:
        pay = int(pay or 0)
    except (TypeError, ValueError):
        pay = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days > 0 else None

    def keep(j: dict) -> bool:
        if q and q not in (j.get("title") or "").lower() and q not in (j.get("company") or "").lower():
            return False
        if loc:
            jl = (j.get("location") or "").lower()
            if loc not in jl and not (loc == "remote" and (j.get("remote") or "").lower() == "remote"):
                return False
        if remote and (j.get("remote") or "").lower() != remote:
            return False
        if level == "entry" and not j.get("entry_level"):
            return False
        if visa:
            codes = _visa_codes(j)
            if not any((j.get("visa") or []) if v == "SPONSORED" else (v in codes) for v in visa):
                return False
        # "Ad offers sponsorship": what the ad SAYS (sourcing/adstance.py), with the aggregator's
        # flag as a fallback for rows built before the ad reader existed.
        if sponsored and j.get("ad_stance") != "offered" and not (
                j.get("ad_stance") is None and j.get("sponsorship_stated") is True):
            return False
        if cutoff is not None:
            dt = _parse_posted(j.get("posted_at") or "")
            if dt is not None and dt < cutoff:      # keep undated rows (dt is None)
                return False
        if pay > 0 and salary_value(j.get("salary")) < pay:
            return False
        return True

    result = [j for j in jobs if keep(j)]
    if sort == "pay":                               # highest pay first; unknown-pay (0) sinks last
        result.sort(key=lambda j: salary_value(j.get("salary")), reverse=True)
    return result


def paginate(jobs: list[dict], page=1, per_page=30):
    """Slice one page out of the (already filtered) list. Returns (page_jobs, total, page,
    per_page). per_page is clamped so a client can't ask for an unbounded payload."""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(100, int(per_page)))
    except (TypeError, ValueError):
        per_page = 30
    total = len(jobs)
    start = (page - 1) * per_page
    return jobs[start:start + per_page], total, page, per_page
