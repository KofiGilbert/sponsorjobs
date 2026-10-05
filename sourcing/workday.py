"""Workday career sites: the public, keyless JSON behind every *.myworkdayjobs.com board
(GREEN lane -- CLAUDE.md §6). The biggest H-1B sponsors (NVIDIA, Intel, Cisco, Qualcomm, Visa,
Target, Adobe, US Bank...) hire through Workday, so this is the single largest lever on the
job count. Verified live 2026-09-30:

  * list   : POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
             body {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
             -> {"total": N, "jobPostings": [{"title", "externalPath", "locationsText",
                 "postedOn": "Posted Today|Yesterday|N Days Ago|30+ Days Ago", "bulletFields"}]}
             GET answers 400; limit maxes at 20; the list never pages past 2000, and an offset at
             or beyond `total` WRAPS to the first page, so paging must stop on a repeat.
  * detail : GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}
             -> {"jobPostingInfo": {"jobDescription" (HTML), "startDate", "location", "timeType",
                 "jobReqId", "externalUrl", "remoteType"?}, "hiringOrganization": {"name"}}
  * robots : GET  https://{host}/robots.txt names the tenant's career site(s) in its Sitemap /
             Allow lines (a non-existent tenant answers 422). This is how discovery learns the
             site name; the landing page redirect is a fallback (it answers 406 to non-browsers,
             and we do not pretend to be one).

Politeness: one small request at a time per tenant with a short gap, a bounded 429 / 5xx
back-off that honours Retry-After, hard timeouts, a per-board row cap (WORKDAY_MAX_JOBS), and
JDs fetched lazily -- only for rows the store does not already hold, capped per board per run
(WORKDAY_MAX_DETAIL). A site whose robots.txt disallows crawling is left alone. Submission on
Workday is ASSISTED only (submit/allowlist.py); this module reads postings, nothing more.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit

from .ats import (_WORKDAY_BOARD_ID_RE, _job, fetch_json, fetch_with_backoff, html_to_text,
                  retry_after_seconds)
from .discover import (PROBE_TIMEOUT, brand_slug, distinctive_tokens, joined_slugs,
                       name_match_count)

WD_DOMAIN = "myworkdayjobs.com"
# The Workday instances a tenant can live on: the ones whose wildcard DNS resolves (checked
# 2026-09-30; wd2/wd4/wd6-8 do not exist). wd1/wd3/wd5 carry most US enterprises; wd12 is real
# too (qualcomm.wd12). Each probe is one tiny robots.txt GET.
WD_INSTANCES = (1, 3, 5, 10, 12, 103)
PAGE = 20                       # Workday's maximum page size
LIST_CEILING = 2000             # the list endpoint never pages past this
DEFAULT_MAX_JOBS = 1000         # rows per board per refresh (env WORKDAY_MAX_JOBS)
DEFAULT_MAX_DETAIL = 200        # JD fetches per board per refresh (env WORKDAY_MAX_DETAIL)
REQUEST_GAP = 0.15              # seconds between consecutive requests to one tenant
MAX_RETRIES = 2                 # extra tries after a 429 / 5xx before giving up on the request
MAX_BACKOFF = 60.0
# Discovery reads up to IDENTITY_POSTINGS details per candidate site across many guessed tenants,
# so its 429 / 5xx back-off is a REAL pause (Retry-After when given) capped shorter than a
# board refresh's: long enough to honour the edge, short enough that one slow tenant cannot
# park a crawl. (It used to be a no-op lambda, which made the retries fire instantly.)
DISCOVERY_MAX_BACKOFF = 15.0

LIST_BODY = {"appliedFacets": {}, "limit": PAGE, "offset": 0, "searchText": ""}


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# -- ids and urls --------------------------------------------------------------- #

def parse_board_id(board_id: str) -> tuple[str, str, str]:
    """"{tenant}.wd{n}/{site}" -> (tenant, host, site). Raises ValueError on anything else, so
    a malformed id can never reach a URL (the host is always *.myworkdayjobs.com)."""
    m = _WORKDAY_BOARD_ID_RE.match(str(board_id or ""))
    if not m:
        raise ValueError(f"invalid workday board id {board_id!r}: expected 'tenant.wdN/Site'")
    tenant, n, site = m.groups()
    return tenant, f"{tenant}.wd{n}.{WD_DOMAIN}", site


def board_id_for(tenant: str, host: str, site: str) -> str:
    m = re.match(rf"^{re.escape(tenant)}\.(wd\d{{1,3}})\.{re.escape(WD_DOMAIN)}$", host)
    if not m:
        raise ValueError(f"{host!r} is not {tenant!r}'s Workday host")
    return f"{tenant}.{m.group(1)}/{site}"


def cxs_base(tenant: str, host: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}"


def job_page_url(host: str, site: str, external_path: str) -> str:
    """The human job page: https://{host}/{site}{externalPath}."""
    return f"https://{host}/{site}{external_path}"


def detail_url_for(job_url: str) -> str:
    """The cxs detail URL for a stored job page URL, or '' if it is not a Workday job page.
    The detail endpoint needs the posting slug ("Title_JR123") from the page path, which the
    source_id alone does not carry, so on-demand JD fetches derive it from the row's url."""
    try:
        parts = urlsplit(str(job_url or ""))
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    m = re.match(rf"^([a-z0-9][a-z0-9-]*)\.wd\d{{1,3}}\.{re.escape(WD_DOMAIN)}$", host)
    segs = [s for s in (parts.path or "").split("/") if s]
    if not m or len(segs) < 3 or segs[1] != "job":
        return ""
    tenant, site = m.group(1), segs[0]
    return cxs_base(tenant, host, site) + "/" + "/".join(segs[1:])


_REQ_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")


def req_id(posting: dict) -> str:
    """The requisition id: the suffix after the last '_' of externalPath ("..._JR2000499"),
    else the id-looking bulletField, else the posting slug. Always a safe slug."""
    path = str(posting.get("externalPath") or "")
    slug = path.rsplit("/", 1)[-1]
    if "_" in slug:
        cand = slug.rsplit("_", 1)[-1]
        if cand and _REQ_ID_RE.match(cand) and any(ch.isdigit() for ch in cand):
            return cand
    for b in reversed(posting.get("bulletFields") or []):
        b = str(b).strip()
        if _REQ_ID_RE.match(b) and any(ch.isdigit() for ch in b) and " " not in b:
            return b
    return slug if _REQ_ID_RE.match(slug) else ""


# -- dates and fields ----------------------------------------------------------- #

_POSTED_RE = re.compile(r"posted\s+(\d+)\+?\s+days?\s+ago", re.I)


def posted_on_to_date(text: str, today: date | None = None) -> str:
    """"Posted Today" / "Posted Yesterday" / "Posted N Days Ago" / "Posted 30+ Days Ago" -> an
    ISO date. "30+" is a floor, so it becomes exactly 30 days ago (the oldest Workday states).
    Unknown wording -> '' rather than a guess."""
    today = today or date.today()
    t = (text or "").strip().lower()
    if not t:
        return ""
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = _POSTED_RE.search(t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return ""


def _remote(location: str, remote_type: str = "") -> str:
    rt = (remote_type or "").lower()
    if "remote" in rt and "onsite" not in rt:
        return "remote"
    if "hybrid" in rt:
        return "hybrid"
    if "onsite" in rt or "on-site" in rt:
        return "onsite"
    return "remote" if "remote" in (location or "").lower() else ""


# -- requests ------------------------------------------------------------------- #

_retry_after = retry_after_seconds


def _request(fetch, url: str, sleep=time.sleep, **kw):
    """One request with a bounded back-off on 429 / 5xx (Retry-After when given, else
    exponential; the shared ats.fetch_with_backoff with this module's limits). Anything else
    propagates: a 404 is an answer, not a reason to retry."""
    return fetch_with_backoff(fetch, url, sleep, retries=MAX_RETRIES, max_backoff=MAX_BACKOFF, **kw)


def _bounded(fetch, timeout: int):
    """Give the real network fetch a short timeout; leave an injected fetch alone."""
    if fetch is fetch_json:
        return lambda url, **kw: fetch_json(url, timeout=timeout, **kw)
    return fetch


# -- the board adapter ------------------------------------------------------------ #

def list_postings(tenant: str, site: str, host: str, fetch=fetch_json, *, max_jobs: int | None = None,
                  sleep=time.sleep) -> tuple[int, list[dict]]:
    """Page through a site's postings (20 a page) up to `max_jobs` (env WORKDAY_MAX_JOBS,
    default 1000) and Workday's own 2000 ceiling. Returns (total_reported, postings). Stops
    on an empty or short page, and on the first repeated posting -- an offset past the end
    wraps to page one. `total` is only a hint (it is capped at 2000 and sometimes 0)."""
    cap = DEFAULT_MAX_JOBS if max_jobs is None else max_jobs
    cap = min(max(cap, 0) or 0, LIST_CEILING)
    url = cxs_base(tenant, host, site) + "/jobs"
    seen: set[str] = set()
    out: list[dict] = []
    total = 0
    offset = 0
    while offset < cap:
        data = _request(fetch, url, sleep, data={**LIST_BODY, "offset": offset,
                                                "limit": min(PAGE, cap - offset)}) or {}
        page = data.get("jobPostings") or []
        total = max(total, int(data.get("total") or 0))
        fresh = 0
        for p in page:
            key = str(p.get("externalPath") or p.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(p)
            fresh += 1
        if not page or fresh == 0 or len(page) < PAGE:
            break
        offset += len(page)
        if total and offset >= total:
            break
        sleep(REQUEST_GAP)
    return max(total, len(out)), out


def workday_jobs(tenant: str, site: str, host: str, company: str = "", fetch=fetch_json, *,
                 max_jobs: int | None = None, sleep=time.sleep, today: date | None = None,
                 **_ignored) -> list[dict]:
    """A Workday site's postings as the common job-row shape (sourcing/ats.py). LIST-ONLY:
    rows carry no jd_text, the service fills descriptions for the rows it keeps
    (``fill_details``) and the detail view fetches the rest on demand (``jd_for_job``).
    source_id = workday:{tenant}.wd{n}/{site}:{jobReqId}; url = the human job page."""
    board_id = board_id_for(tenant, host, site)
    if max_jobs is None:
        max_jobs = _env_int("WORKDAY_MAX_JOBS", DEFAULT_MAX_JOBS)
    _, postings = list_postings(tenant, site, host, fetch, max_jobs=max_jobs, sleep=sleep)
    out: list[dict] = []
    seen: set[str] = set()
    for p in postings:
        rid = req_id(p)
        path = str(p.get("externalPath") or "")
        if not rid or not path.startswith("/"):
            continue
        loc = str(p.get("locationsText") or "").strip()
        row = _job("workday", board_id, rid, company or tenant, p.get("title", ""), loc,
                   _remote(loc), job_page_url(host, site, path), "",
                   posted_on_to_date(p.get("postedOn", ""), today))
        if row["source_id"] in seen:
            continue
        seen.add(row["source_id"])
        out.append(row)
    return out


def workday_board_jobs(board_id: str, company: str = "", fetch=fetch_json, **kw) -> list[dict]:
    """ATS-registry entry point (ats.ADAPTERS['workday']): board_id is 'tenant.wdN/Site'."""
    tenant, host, site = parse_board_id(board_id)
    return workday_jobs(tenant, site, host, company, fetch=fetch, **kw)


def workday_detail(detail_url: str, fetch=fetch_json, sleep=time.sleep) -> dict:
    """One posting's detail: {jd_text, location, remote, posted_at, url, org, time_type}, or {}
    on any failure (a vanished posting is a 404). jd_text is plain text."""
    if not detail_url:
        return {}
    try:
        data = _request(fetch, detail_url, sleep) or {}
    except Exception:                                 # noqa: BLE001 - best-effort enrichment
        return {}
    info = data.get("jobPostingInfo") or {}
    if not isinstance(info, dict):
        return {}
    loc = str(info.get("location") or "").strip()
    extra = [str(x.get("descriptor") or x) if isinstance(x, dict) else str(x)
             for x in (info.get("additionalLocations") or [])]
    if extra:
        loc = "; ".join(x for x in [loc, *extra] if x)
    return {
        "jd_text": html_to_text(str(info.get("jobDescription") or "")),
        "location": loc,
        "remote": _remote(loc, str(info.get("remoteType") or "")),
        "posted_at": str(info.get("startDate") or "")[:10],
        "url": str(info.get("externalUrl") or ""),
        "org": str((data.get("hiringOrganization") or {}).get("name") or ""),
        "time_type": str(info.get("timeType") or ""),
    }


def fill_details(rows: list[dict], fetch=fetch_json, *, known_ids=(), max_detail: int | None = None,
                 sleep=time.sleep) -> int:
    """Fetch the JD (and exact location / date) for rows that still lack one and are not already
    held with a description (`known_ids`), at most `max_detail` per call (env WORKDAY_MAX_DETAIL,
    default 200). Rows left unfilled are still stored; the detail view fetches them on demand.
    Returns how many were fetched."""
    if max_detail is None:
        max_detail = _env_int("WORKDAY_MAX_DETAIL", DEFAULT_MAX_DETAIL)
    known = set(known_ids or ())
    n = 0
    for row in rows:
        if n >= max_detail:
            break
        if (row.get("jd_text") or "").strip() or row.get("source_id") in known:
            continue
        det = workday_detail(detail_url_for(row.get("url", "")), fetch, sleep)
        if not det:
            continue
        n += 1
        if det["jd_text"]:
            row["jd_text"] = det["jd_text"]
        if det["location"] and (not row.get("location") or
                                re.fullmatch(r"\d+ locations?", row["location"], re.I)):
            row["location"] = det["location"]
        if det["remote"]:
            row["remote"] = det["remote"]
        if det["posted_at"]:
            row["posted_at"] = det["posted_at"]
        sleep(REQUEST_GAP)
    return n


def jd_for_job(job: dict, fetch=fetch_json) -> str:
    """On-demand JD for a stored Workday row (feedclient.fetch_board_jd and the detail views):
    plain text, or '' when the posting is gone or the row is not a Workday job page."""
    return workday_detail(detail_url_for((job or {}).get("url", "")), fetch).get("jd_text", "")


# -- discovery --------------------------------------------------------------------- #

_ROBOTS_SITEMAP = re.compile(r"^\s*sitemap\s*:\s*https?://[^/\s]+/([^/\s]+)/sitemap\.xml", re.I)
_ROBOTS_RULE = re.compile(r"^\s*(allow|disallow)\s*:\s*/([^/\s*$]+)/?\s*$", re.I)
_NOT_SITES = {"refreshfacet", "talentcommunity", "wday", "login", "api", "robots.txt"}


def robots_sites(text: str) -> tuple[list[str], list[str]]:
    """Parse a tenant's robots.txt into (crawlable sites, disallowed sites). Sites come from
    Sitemap lines (…/{site}/siteMap.xml) and Allow lines (/{site}/), in that order, minus any
    site a Disallow names. We honour the file: a disallowed site is never crawled."""
    allowed: list[str] = []
    blocked: list[str] = []
    for line in str(text or "").splitlines():
        m = _ROBOTS_SITEMAP.match(line)
        if m:
            s = m.group(1)
            if s.lower() not in _NOT_SITES and s not in allowed:
                allowed.append(s)
            continue
        m = _ROBOTS_RULE.match(line)
        if not m:
            continue
        rule, s = m.group(1).lower(), m.group(2)
        if s.lower() in _NOT_SITES:
            continue
        if rule == "disallow":
            if s not in blocked:
                blocked.append(s)
        elif s not in allowed:
            allowed.append(s)
    return [s for s in allowed if s not in blocked], blocked


_LANG_SEG = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def site_from_redirect(location: str) -> str:
    """The site name in a landing-page redirect: https://{host}/{lang}/{site}[/...] or
    https://{host}/{site}. '' when there is none."""
    try:
        segs = [s for s in (urlsplit(str(location or "")).path or "").split("/") if s]
    except ValueError:
        return ""
    if segs and _LANG_SEG.match(segs[0]) and len(segs) > 1:
        segs = segs[1:]
    if not segs or segs[0].lower() in _NOT_SITES:
        return ""
    return segs[0]


# Sites that are not for outside applicants: staffing agencies, employees, alumni, contingent
# workers. A tenant's robots.txt lists them next to the real careers site (amgen: Careers +
# agency), so they are dropped before any site is chosen.
_NOT_FOR_APPLICANTS = re.compile(r"\b(agenc(y|ies)|internal|alumni|contractors?|contingent|vendors?|"
                                 r"employees?|relief)\b", re.I)
MAX_SITES = 8                   # sites of one tenant discovery will size up (one request each)


def prefer_sites(sites: list[str]) -> list[str]:
    """The sites worth sizing up, external / careers sites first (a tenant can also run campus
    or regional sites), otherwise the robots order; agency / internal / alumni sites are out."""
    def rank(s: str) -> int:
        t = s.lower()
        return 0 if ("external" in t or "career" in t) else 1
    keep = [s for s in sites if not _NOT_FOR_APPLICANTS.search(site_words(s))]
    return sorted(keep, key=lambda s: (rank(s), sites.index(s)))[:MAX_SITES]


def site_words(site: str) -> str:
    """"NVIDIAExternalCareerSite" -> "NVIDIA External Career Site"; "Cisco_Careers" -> "Cisco
    Careers": the words of a site name, so it can be matched against the sponsor's brand."""
    s = re.sub(r"[_\-.]+", " ", site or "")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return s


def workday_slugs(display_name: str) -> list[tuple[str, str]]:
    """Tenant slugs worth probing for an employer: the brand word and the joined forms, as
    (slug, kind). A tenant slug is a hostname label, so only lowercase letters, digits and
    hyphens qualify. Empty when the name has no distinctive brand word to verify against."""
    if not distinctive_tokens(display_name):
        return []
    cands = [(brand_slug(display_name), "brand"), *[(j, "joined") for j in joined_slugs(display_name)]]
    out: list[tuple[str, str]] = []
    for slug, kind in cands:
        if slug and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", slug) and slug not in [s for s, _ in out]:
            out.append((slug, kind))
    return out


IDENTITY_POSTINGS = 3           # postings whose hiring organization is read before giving up


def identity_match_count(evidence: str, distinctive: list[str]) -> int:
    """How many of the sponsor's brand words the evidence (site words + hiring organizations)
    confirms: discover.py's token match, plus a squashed-spelling match for long brand words so
    "Black Rock Professional" confirms "blackrock" (no short-word substrings: "visa" never
    matches inside "advisable")."""
    by_token = name_match_count(evidence, distinctive)
    squashed = re.sub(r"[^a-z0-9]", "", (evidence or "").lower())
    by_squash = sum(1 for t in distinctive if len(t) >= 6 and t in squashed)
    return max(by_token, by_squash)


class WorkdayHit(NamedTuple):
    tenant: str
    host: str
    site: str
    jobs: int
    org: str

    @property
    def board_id(self) -> str:
        return board_id_for(self.tenant, self.host, self.site)


def discovery_backoff(seconds: float) -> None:
    """The default back-off sleep for discovery's detail reads: Retry-After (or the exponential
    gap) honoured for real, capped at DISCOVERY_MAX_BACKOFF."""
    time.sleep(min(max(0.0, seconds), DISCOVERY_MAX_BACKOFF))


def discover_workday(display_name: str, fetch=fetch_json, reject=None, sleep=None,
                     instances=WD_INSTANCES, redirect=None, slugs=None,
                     backoff=discovery_backoff) -> WorkdayHit | None:
    """Find an employer's Workday career site, identity-confirmed, with at least one live job.

    For each slug guess and instance: GET robots.txt (a missing tenant answers 422). A tenant
    that answers names its site(s); every applicant-facing site is sized up with one list request
    and, LARGEST FIRST (a tenant often runs a main careers site next to small subsidiary or
    regional ones), checked against the sponsor's brand words using the SAME rule as discover.py's
    name lane -- the words of the site name plus the hiring organization on the first posting
    ("020 Cisco Systems, Inc.") must contain the brand word (two of them for a truncated
    multi-brand name). The first site that passes with >=1 posting wins. A tenant whose
    robots.txt disallows its career site is respected and reported, not crawled. `reject(ats,
    slug, reason)` is told about every live tenant turned down. `redirect(url) -> Location` is
    the landing-page fallback when robots.txt names no site. A tenant's first posting may belong
    to a subsidiary ("1005 Immunex Rhode Island Corporation" on amgen), so up to IDENTITY_POSTINGS
    postings' organizations are read before a site is turned down. `slugs` overrides the guessed
    tenant slugs (a maintainer verifying a known tenant such as "usbank" by hand); the identity
    check still applies at full strength -- a hand-given slug is a guess like any other, and
    "emerson" is Emerson College's tenant, not Emerson Electric's -- so a wrong slug is turned down.
    `backoff(seconds)` is the 429 / 5xx pause for the detail reads (a real, capped sleep by
    default; tests inject a recorder)."""
    dist = distinctive_tokens(display_name)
    cands = workday_slugs(display_name) if slugs is None else [(s, "brand") for s in slugs]
    if not dist or not cands:
        return None
    need_two = len(dist) >= 2
    f = _bounded(fetch, PROBE_TIMEOUT)
    quiet = sleep or (lambda: None)
    for slug, kind in cands:
        need = 2 if (kind == "brand" and need_two) else 1
        for n in instances:
            host = f"{slug}.wd{n}.{WD_DOMAIN}"
            try:
                robots = f(f"https://{host}/robots.txt", raw=True)
            except Exception:                         # noqa: BLE001 - 422/404/timeout: no tenant here
                quiet()
                continue
            quiet()
            if not isinstance(robots, str):
                continue
            sites, blocked = robots_sites(robots)
            if not sites and redirect:
                try:
                    sites = [s for s in [site_from_redirect(redirect(f"https://{host}/"))]
                             if s and s not in blocked]
                except Exception:                     # noqa: BLE001
                    sites = []
            if not sites:
                if blocked and reject:
                    reject("workday", slug, f"{host} robots.txt disallows its career site(s) "
                                            f"{blocked}; not crawled")
                break                                 # the tenant lives here; other instances won't
            sized: list[tuple[int, int, str, dict]] = []    # (-count, order, site, first posting)
            for i, site in enumerate(prefer_sites(sites)):
                try:
                    data = f(cxs_base(slug, host, site) + "/jobs",
                             data={**LIST_BODY, "limit": IDENTITY_POSTINGS}) or {}
                except Exception:                     # noqa: BLE001
                    quiet()
                    continue
                quiet()
                postings = data.get("jobPostings") or []
                count = max(int(data.get("total") or 0), len(postings))
                if postings and count > 0:
                    sized.append((-count, i, site, postings))
            for neg, _, site, postings in sorted(sized):
                orgs: list[str] = []
                for posting in postings[:IDENTITY_POSTINGS]:
                    det = workday_detail(cxs_base(slug, host, site) + str(posting.get("externalPath") or ""),
                                         f, sleep=backoff)
                    quiet()
                    if det.get("org") and det["org"] not in orgs:
                        orgs.append(det["org"])
                    if identity_match_count(f"{site_words(site)} {' '.join(orgs)}", dist) >= need:
                        break
                evidence = f"{site_words(site)} {' / '.join(orgs)}".strip()
                if identity_match_count(evidence, dist) < need:
                    if reject:
                        reject("workday", slug, f"{host}/{site} identifies as {evidence!r}; needs "
                                                f"{need} of {dist} to be {display_name!r}")
                    continue
                return WorkdayHit(slug, host, site, -neg, orgs[0] if orgs else "")
            break                                     # tenant found on this host; sites exhausted
    return None
