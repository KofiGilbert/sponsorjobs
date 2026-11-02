"""Is this posting still open? A free check: plain HTTP, no AI, no login (2026-11-02).

On the owner's real queue, 3 of 12 "Waiting on you" jobs had been closed by the employer
(Oracle Cloud HCM, US Bank on Workday, Amazon). The feed prunes rows its board stops listing,
but a job already in the person's queue was never looked at again, so the app tailored a resume
and ran the apply agent on a dead posting, spending the person's AI credits for nothing.

``posting_status(url)`` answers "open", "closed" or "unknown", from each ATS's own public JSON
where one exists, else the posting page itself:

  * Workday         GET https://{host}/wday/cxs/{tenant}/{site}/job/{path}: 200 with
                    jobPostingInfo = open; 404 = closed; a taken-down posting answers Workday's
                    own JSON 403 (errorCode S22, "permission denied") = closed.
  * Greenhouse      GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}: 404 = closed (the
                    board itself still answering, so a renamed board is never read as closed).
  * Lever           GET api.lever.co/v0/postings/{site}/{id}: 404 = closed (site still answers).
  * Ashby           the posting page jobs.ashbyhq.com/{org}/{id} (its robots.txt disallows
                    /api/): 404 with "posting":null = closed, when the org page still answers.
  * Workable        GET apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}: 404 =
                    closed (account still answers). A short link /j/{code} redirects to the
                    account (301) or to /oops (302 = closed).
  * Recruitee       GET {company}.recruitee.com/api/offers/{slug}: 404 = closed.
  * Oracle Cloud    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
    HCM             ?finder=ById;Id="{id}",siteNumber={site}: no item = closed, when the site's
                    own requisition search still lists jobs (a wrong site name is never closed).
  * Amazon          the amazon.jobs posting page: 404, or "the job you're looking for isn't
                    available" = closed.
  * anything else   (iCIMS, jibe, Phenom, SmartRecruiters, a company's own site): the page.
                    Closed only on HTTP 404 / 410, a redirect to a search or error page, or a
                    short line that clearly says so. SmartRecruiters' API is never asked: its
                    robots.txt disallows us (sourcing.ats.DISABLED_ATS).

"unknown" is never treated as closed: a network error, a 429, a bot wall or a page that says
nothing clear all leave the job alone. Polite: the repo's honest sourcing User-Agent, a 10 s
timeout, one request at a time per host, a few hosts at once, and each answer cached per URL
(open 6 hours, closed a week, unknown 1 hour).
"""

from __future__ import annotations

import html as _html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .ats import _UA, html_to_text

OPEN, CLOSED, UNKNOWN = "open", "closed", "unknown"
TIMEOUT = 10
MAX_BYTES = 1_500_000
WORKERS = 4
TTL = {OPEN: 6 * 3600, CLOSED: 7 * 86400, UNKNOWN: 3600}


@dataclass
class Resp:
    """One HTTP answer. ``status`` 0 means no answer at all (timeout, DNS, refused)."""
    status: int
    body: str = ""
    url: str = ""                       # the final URL after redirects
    location: str = ""                  # the redirect target, when redirects were not followed
    headers: dict = field(default_factory=dict)

    def json(self):
        try:
            return json.loads(self.body)
        except (TypeError, ValueError):
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):     # noqa: D401 - hand the 3xx back as an answer
        return None


def http_get(url: str, accept: str = "application/json", follow: bool = True,
             timeout: float = TIMEOUT) -> Resp:
    """GET one URL with the sourcing User-Agent. Never raises: a failure is Resp(0)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(MAX_BYTES).decode("utf-8", "replace")
            return Resp(r.status, body, r.geturl(), "", dict(r.headers or {}))
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BYTES).decode("utf-8", "replace")
        except Exception:                    # noqa: BLE001
            body = ""
        hdrs = dict(e.headers or {})
        return Resp(e.code, body, url, hdrs.get("Location") or hdrs.get("location") or "", hdrs)
    except Exception:                        # noqa: BLE001 - no answer: unknown, never closed
        return Resp(0, "", url)


# --------------------------------------------------------------------------- the words
# The same closed lines the apply agent reads in the pane (shell-electron/agent.js CLOSED_RE),
# matched only on a short line of its own, so a job description that mentions applications
# closing on some date is never read as a closed page.
CLOSED_RE = re.compile("|".join([
    r"(this|the) (job|position|posting|role|requisition|vacancy|opening)( you('|’| a)re looking for| you are looking for)? (is )?(no longer|not) (available|open|active|accepting)",
    r"(job|position|posting|role|page|opening) you('|’| a)re looking for (isn'?t|is not|doesn'?t|does not|no longer|can'?t|cannot)( be)? (available|exist|found)",
    r"page you are looking for (doesn'?t|does not|no longer) exist",
    r"no longer accepting applications",
    r"(job|position|posting|role|vacancy|requisition) (has|was) (been )?(filled|closed|expired|removed|taken down)",
    r"(job|posting|position|vacancy) (has )?expired",
    r"(job|position|posting) is (closed|no longer (open|available))",
    r"this (job|position|posting|requisition) (is )?(closed|unavailable)",
    r"applications? (for this (job|position|role) )?(are|is) (now )?closed",
]), re.I)
_APOS = str.maketrans({"‘": "'", "’": "'"})
# Where a closed posting is sent: a search, a job list, an error page.
SEARCH_PATH_RE = re.compile(r"(/search\b|/jobs?/?$|/careers?/?$|/job-?search|/joblist|/openings/?$|"
                            r"/404\b|not-?found|/oops\b|/error\b|/expired\b|/$)", re.I)


def closed_phrase(text: str) -> str:
    """The short line of a page that says the posting is gone, or ""."""
    for ln in str(text or "").translate(_APOS).splitlines():
        line = ln.strip().strip('"').strip()
        if line.lower().startswith("error:"):
            line = line[6:].strip().strip('"')
        if line and len(line) <= 200 and CLOSED_RE.search(line):
            return line[:160]
    return ""


def page_text(body: str) -> str:
    """The visible words of an HTML page (scripts and styles dropped)."""
    body = re.sub(r"(?is)<(noscript|template|head)[^>]*>.*?</\1>", " ", body or "")
    return html_to_text(body)


_JSONLD_RE = re.compile(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>')


def has_job_posting(body: str) -> bool:
    """Does the page carry a schema.org JobPosting block (an open job's page usually does)?"""
    for m in _JSONLD_RE.finditer(body or ""):
        if re.search(r'"@type"\s*:\s*"JobPosting"', m.group(1)):
            return True
    return False


# --------------------------------------------------------------------------- the adapters
# Each takes the split URL, the job's source_id ("ats:board:id", may be "") and a fetch(url,
# accept, follow) -> Resp, and returns (status, why), or None when the URL is not its kind.

def _segs(p) -> list[str]:
    return [s for s in (p.path or "").split("/") if s]


def _sid_parts(source_id: str, ats: str) -> list[str]:
    parts = str(source_id or "").split(":", 2)
    return parts if len(parts) == 3 and parts[0] == ats else []


_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")


def workday(p, source_id, fetch):
    host = (p.hostname or "").lower()
    m = re.match(r"^([a-z0-9][a-z0-9-]*)\.wd\d{1,3}\.myworkdayjobs\.com$", host)
    if not m:
        return None
    segs = _segs(p)
    if segs and _LOCALE_RE.match(segs[0]):
        segs = segs[1:]
    if len(segs) < 3 or "job" not in segs[1:]:
        return UNKNOWN, "not a Workday job page"
    i = segs.index("job", 1)
    site = segs[i - 1]
    api = f"https://{host}/wday/cxs/{m.group(1)}/{site}/" + "/".join(segs[i:])
    r = fetch(api, "application/json", True)
    d = r.json() if r.body else None
    if r.status == 200 and isinstance(d, dict) and isinstance(d.get("jobPostingInfo"), dict):
        if d["jobPostingInfo"].get("posted") is False:
            return CLOSED, "Workday: the posting is no longer posted"
        return OPEN, "Workday lists the posting"
    if r.status in (404, 410):
        return CLOSED, f"Workday answered {r.status} for the posting"
    # A posting the employer took down answers Workday's own JSON 403 "permission denied"
    # (errorCode S22; US Bank, 2026-11-02). A bot wall would not be Workday's JSON.
    if r.status == 403 and isinstance(d, dict) and d.get("errorCode") and d.get("httpStatus") == 403:
        return CLOSED, "Workday: the posting was taken down"
    return UNKNOWN, f"Workday answered {r.status or 'nothing'}"


def greenhouse(p, source_id, fetch):
    host = (p.hostname or "").lower()
    q = urllib.parse.parse_qs(p.query or "")
    segs = _segs(p)
    token = jid = ""
    if re.match(r"^(job-)?boards(\.eu)?\.greenhouse\.io$", host) and len(segs) >= 3 and segs[1] == "jobs":
        token, jid = segs[0], segs[2]
    sid = _sid_parts(source_id, "greenhouse")
    if sid:
        token = token or sid[1]
        jid = jid or sid[2]
    jid = jid or (q.get("gh_jid") or [""])[0]
    if not (token and jid):
        if "greenhouse.io" in host:
            return UNKNOWN, "no Greenhouse board or job id in the link"
        return None
    if not re.match(r"^\d+$", jid):
        return UNKNOWN, "not a Greenhouse job id"
    api = "https://boards-api.greenhouse.io/v1/boards/"
    r = fetch(f"{api}{token}/jobs/{jid}", "application/json", True)
    d = r.json()
    if r.status == 200 and isinstance(d, dict) and d.get("id"):
        return OPEN, "Greenhouse lists the job"
    if r.status == 404:
        b = fetch(f"{api}{token}", "application/json", True)
        if b.status == 200:
            return CLOSED, "Greenhouse: the board no longer has this job"
        return UNKNOWN, f"Greenhouse board answered {b.status or 'nothing'}"
    return UNKNOWN, f"Greenhouse answered {r.status or 'nothing'}"


def lever(p, source_id, fetch):
    host = (p.hostname or "").lower()
    m = re.match(r"^jobs(\.eu)?\.lever\.co$", host)
    segs = _segs(p)
    if not m:
        return None
    if len(segs) < 2:
        return UNKNOWN, "no Lever posting id in the link"
    site, pid, eu = segs[0], segs[1], bool(m.group(1))
    api = f"https://api{'.eu' if eu else ''}.lever.co/v0/postings/{site}"
    r = fetch(f"{api}/{pid}?mode=json", "application/json", True)
    d = r.json()
    if r.status == 200 and isinstance(d, dict) and d.get("id"):
        return OPEN, "Lever lists the posting"
    if r.status == 404:
        b = fetch(f"{api}?mode=json&limit=1", "application/json", True)
        if b.status == 200:
            return CLOSED, "Lever: the site no longer has this posting"
        return UNKNOWN, f"Lever site answered {b.status or 'nothing'}"
    return UNKNOWN, f"Lever answered {r.status or 'nothing'}"


_ASHBY_POSTING_RE = re.compile(r'"posting"\s*:\s*(null|\{\s*"id"\s*:\s*"([^"]+)")')
_ASHBY_ORG_RE = re.compile(r'"organization"\s*:\s*(null|\{)')


def ashby(p, source_id, fetch):
    if (p.hostname or "").lower() != "jobs.ashbyhq.com":
        return None
    segs = _segs(p)
    if len(segs) < 2:
        return UNKNOWN, "no Ashby posting id in the link"
    org, pid = segs[0], segs[1]
    r = fetch(f"https://jobs.ashbyhq.com/{org}/{pid}", "text/html", True)
    m = _ASHBY_POSTING_RE.search(r.body or "")
    if r.status == 200 and m and m.group(2):
        return OPEN, "Ashby shows the posting"
    if r.status in (404, 410) or (r.status == 200 and m and m.group(1) == "null"):
        o = fetch(f"https://jobs.ashbyhq.com/{org}", "text/html", True)
        om = _ASHBY_ORG_RE.search(o.body or "")
        if o.status == 200 and om and om.group(1) == "{":
            return CLOSED, "Ashby: the board no longer has this posting"
        return UNKNOWN, f"Ashby board answered {o.status or 'nothing'}"
    return UNKNOWN, f"Ashby answered {r.status or 'nothing'}"


def workable(p, source_id, fetch):
    host = (p.hostname or "").lower()
    segs = _segs(p)
    acct = code = ""
    if host == "apply.workable.com":
        if len(segs) >= 2 and segs[0] == "j":
            # The short link: 301 to the account's page, or 302 to /oops for a gone job.
            r = fetch(f"https://apply.workable.com/j/{segs[1]}", "text/html", False)
            loc = urllib.parse.urlsplit(urllib.parse.urljoin("https://apply.workable.com/", r.location or ""))
            ls = _segs(loc)
            if r.status in (301, 302, 303, 307, 308) and ls[:1] == ["oops"]:
                return CLOSED, "Workable: the link goes to its not-found page"
            if r.status in (301, 302, 303, 307, 308) and len(ls) >= 3 and ls[1] == "j":
                acct, code = ls[0], ls[2]
            elif r.status == 404:
                return CLOSED, "Workable answered 404 for the link"
            else:
                return UNKNOWN, f"Workable answered {r.status or 'nothing'}"
        elif len(segs) >= 3 and segs[1] == "j":
            acct, code = segs[0], segs[2]
    elif host.endswith(".workable.com") and len(segs) >= 2 and segs[0] == "j":
        acct, code = host.split(".")[0], segs[1]
    else:
        return None
    if not (acct and code):
        return UNKNOWN, "no Workable job code in the link"
    r = fetch(f"https://apply.workable.com/api/v2/accounts/{acct}/jobs/{code}", "application/json", True)
    d = r.json()
    if r.status == 200 and isinstance(d, dict) and (d.get("shortcode") or d.get("id")):
        return OPEN, "Workable lists the job"
    if r.status == 404:
        b = fetch(f"https://apply.workable.com/api/v1/accounts/{acct}", "application/json", True)
        if b.status == 200:
            return CLOSED, "Workable: the account no longer has this job"
        return UNKNOWN, f"Workable account answered {b.status or 'nothing'}"
    return UNKNOWN, f"Workable answered {r.status or 'nothing'}"


def recruitee(p, source_id, fetch):
    host = (p.hostname or "").lower()
    segs = _segs(p)
    sid = _sid_parts(source_id, "recruitee")
    if host.endswith(".recruitee.com"):
        base = f"https://{host}"
    elif sid:
        base = f"https://{sid[1]}.recruitee.com"
    else:
        return None
    if len(segs) >= 2 and segs[0] == "o":
        key = segs[1]
    elif sid:
        key = sid[2]
    else:
        return UNKNOWN, "no Recruitee offer in the link"
    r = fetch(f"{base}/api/offers/{key}", "application/json", True)
    d = r.json()
    if r.status == 200 and isinstance(d, dict) and isinstance(d.get("offer"), dict):
        return OPEN, "Recruitee lists the offer"
    if r.status == 404:
        b = fetch(f"{base}/api/offers/", "application/json", True)
        if b.status == 200:
            return CLOSED, "Recruitee: the company no longer has this offer"
        return UNKNOWN, f"Recruitee company answered {b.status or 'nothing'}"
    return UNKNOWN, f"Recruitee answered {r.status or 'nothing'}"


def oracle(p, source_id, fetch):
    host = (p.hostname or "").lower()
    if not (host.endswith(".oraclecloud.com") and "/hcmUI/CandidateExperience/" in (p.path or "")):
        return None
    segs = _segs(p)
    site = rid = ""
    if "sites" in segs and segs.index("sites") + 1 < len(segs):
        site = segs[segs.index("sites") + 1]
    for k in ("job", "preview"):
        if k in segs and segs.index(k) + 1 < len(segs):
            rid = segs[segs.index(k) + 1]
    if not (site and re.match(r"^\d+$", rid or "")):
        return UNKNOWN, "no Oracle requisition in the link"
    rest = f"https://{host}/hcmRestApi/resources/latest/"
    r = fetch(f'{rest}recruitingCEJobRequisitionDetails?onlyData=true&finder=ById;Id=%22{rid}%22,siteNumber={site}',
              "application/json", True)
    d = r.json()
    if r.status == 200 and isinstance(d, dict) and isinstance(d.get("items"), list):
        if d["items"]:
            return OPEN, "Oracle lists the requisition"
        # No item: closed only when the site's own search still lists jobs (a wrong site
        # name would answer no item too).
        s = fetch(f"{rest}recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                  f"&finder=findReqs;siteNumber={site},limit=1", "application/json", True)
        sd = s.json()
        items = sd.get("items") if isinstance(sd, dict) else None
        if s.status == 200 and items and (items[0].get("TotalJobsCount") or 0) > 0:
            return CLOSED, "Oracle: the site no longer lists this requisition"
        return UNKNOWN, "Oracle site search answered no jobs"
    if r.status == 404:
        return CLOSED, "Oracle answered 404 for the requisition"
    return UNKNOWN, f"Oracle answered {r.status or 'nothing'}"


def amazon(p, source_id, fetch):
    host = (p.hostname or "").lower()
    if host not in ("www.amazon.jobs", "amazon.jobs"):
        return None
    m = re.search(r"/jobs/(\d+)", p.path or "")
    if not m:
        return UNKNOWN, "no Amazon job id in the link"
    path = re.sub(r"\.json$", "", p.path or "")
    r = fetch(f"https://www.amazon.jobs{path}", "text/html", True)
    said = closed_phrase(page_text(r.body)) if r.body else ""
    if r.status in (404, 410) or said:
        return CLOSED, said or f"amazon.jobs answered {r.status}"
    if r.status == 200 and m.group(1) in (r.body or ""):
        return OPEN, "amazon.jobs shows the job"
    return UNKNOWN, f"amazon.jobs answered {r.status or 'nothing'}"


def _job_token(p) -> str:
    """The longest id-like token of a job link (a run of digits, or a long code), to tell a
    redirect to a search page from a redirect to the same job's canonical page."""
    toks = re.findall(r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", (p.path or "") + "?" + (p.query or ""))
    toks = [t for t in toks if sum(c.isdigit() for c in t) >= 4]
    return max(toks, key=len) if toks else ""


def page(p, source_id, fetch):
    """Any other careers page: closed only on a clear signal."""
    url = urllib.parse.urlunsplit(p)
    r = fetch(url, "text/html,application/xhtml+xml", True)
    if r.status in (404, 410):
        return CLOSED, f"the page answered {r.status}"
    if r.status != 200:
        return UNKNOWN, f"the page answered {r.status or 'nothing'}"
    final = urllib.parse.urlsplit(r.url or url)
    tok = _job_token(p)
    if tok and final.path != p.path and tok not in (r.url or "") and SEARCH_PATH_RE.search(final.path or "/"):
        return CLOSED, "the link now goes to a search page"
    said = closed_phrase(page_text(r.body))
    if said:
        return CLOSED, said
    if has_job_posting(r.body):
        return OPEN, "the page carries the job"
    return UNKNOWN, "the page does not say"


ADAPTERS = (workday, greenhouse, lever, ashby, workable, recruitee, oracle, amazon)


# --------------------------------------------------------------------------- the check
_CACHE: dict[str, tuple[str, str, float]] = {}
_CACHE_LOCK = threading.Lock()
_HOST_LOCKS: dict[str, threading.Lock] = {}


def _host_lock(host: str) -> threading.Lock:
    with _CACHE_LOCK:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _clean(url: str) -> str:
    """The link without tracking parameters (utm_*), so one posting caches once."""
    p = urllib.parse.urlsplit(str(url or "").strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def check(url: str, source_id: str = "", *, fetch=None, use_cache: bool = True,
          now: float | None = None) -> dict:
    """{status, why, at} for one posting. Never raises."""
    now = time.time() if now is None else now
    url = _clean(url)
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return {"status": UNKNOWN, "why": "no link to check", "at": now}
    key = url + "|" + str(source_id or "")
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[2] < TTL.get(hit[0], 3600):
            return {"status": hit[0], "why": hit[1], "at": hit[2], "cached": True}
    get = fetch or http_get
    try:
        with _host_lock(p.hostname.lower()):
            out = None
            for fn in ADAPTERS:
                out = fn(p, source_id, get)
                if out is not None:
                    break
            if out is None:
                out = page(p, source_id, get)
    except Exception as exc:                 # noqa: BLE001 - a broken page is unknown, not closed
        out = (UNKNOWN, f"check failed: {type(exc).__name__}")
    status, why = out
    with _CACHE_LOCK:
        _CACHE[key] = (status, why, now)
    return {"status": status, "why": why, "at": now}


def posting_status(url: str, source_id: str = "", *, fetch=None) -> str:
    """"open", "closed" or "unknown" for one posting link."""
    return check(url, source_id, fetch=fetch)["status"]


def check_many(items, *, fetch=None, workers: int = WORKERS) -> dict:
    """Check several postings a few at a time: items is [(key, url, source_id)], the answer
    {key: {status, why, at}}. One request at a time per host (the host lock in check)."""
    items = list(items or [])
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
        futs = {k: ex.submit(check, u, s, fetch=fetch) for k, u, s in items}
        return {k: f.result() for k, f in futs.items()}
