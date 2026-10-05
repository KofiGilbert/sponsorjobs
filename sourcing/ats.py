"""Adapters for official public ATS job feeds (GREEN lane — CLAUDE.md §6).

Each adapter hits a company's own public board API (no login, no key, no proxy)
and returns a list of normalized job dicts. The network call is injectable
(``fetch``) so tests run offline against canned JSON.

Official endpoints used:
  * Greenhouse : https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  * Lever      : https://api.lever.co/v0/postings/{token}?mode=json
  * Ashby      : https://api.ashbyhq.com/posting-api/job-board/{name}
  * Workday    : https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
                 (sourcing/workday.py; the one adapter that POSTs and pages)
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_UA = "resume-agent/1.0 (personal job search; +local)"


def _cred(name: str) -> str:
    """Read a credential from the environment or the git-ignored config/credentials.env
    (same file the LLM key lives in). Returns '' if unset, so an unconfigured optional
    feed no-ops instead of erroring."""
    v = os.environ.get(name)
    if v:
        return v
    env_file = Path(__file__).resolve().parents[1] / "config" / "credentials.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    return ""


def fetch_json(url: str, timeout: int = 20, data=None, raw: bool = False):
    """GET a public JSON endpoint. No auth, no proxy — official feeds only.

    `data` (a dict) turns the request into a JSON POST: Workday's public job list is
    POST-only (a GET answers 400). `raw=True` returns the decoded body as text instead of
    parsed JSON, for the one non-JSON document discovery reads: a tenant's robots.txt."""
    headers = {"User-Agent": _UA, "Accept": "text/plain, */*" if raw else "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if raw:
            return resp.read().decode("utf-8", "replace")
        return json.load(resp)


def retry_after_seconds(exc) -> float:
    """The Retry-After header of an HTTPError as seconds, 0.0 when absent or not a number."""
    try:
        return float((getattr(exc, "headers", None) or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_with_backoff(fetch, url: str, sleep=time.sleep, *, retries: int = 2,
                       max_backoff: float = 60.0, **kw):
    """One request with a bounded back-off on 429 / 5xx (Retry-After when given, else
    exponential), shared by the per-posting detail reads of the list-only boards (Workday,
    SmartRecruiters). Anything else propagates: a 404 is an answer, not a reason to retry."""
    for attempt in range(retries + 1):
        try:
            return fetch(url, **kw)
        except urllib.error.HTTPError as exc:
            if not (exc.code == 429 or 500 <= exc.code < 600) or attempt >= retries:
                raise
            sleep(min(max_backoff, retry_after_seconds(exc) or 2.0 * (2 ** attempt)))
    raise RuntimeError("unreachable")                 # pragma: no cover


def html_to_text(s: str) -> str:
    """Strip an HTML job description down to readable plain text.

    Some feeds (notably Greenhouse's `content`) deliver the markup HTML-ENTITY-ENCODED, so it
    arrives as `&lt;div class="author-d-..."&gt;...` with no real tags. Entities MUST be decoded
    BEFORE tags are stripped: stripping first finds nothing to strip, and a later decode then turns
    the entities into literal `<div class="author-d-...">` tags left inside the text, which showed
    up as raw HTML in the job description. Decode up to a few times to also catch double-encoding
    (`&amp;lt;`), then strip, then decode once more for any entities the markup wrapped as text.
    """
    if not s:
        return ""
    # Some feeds double-encode newlines as the LITERAL two characters "\n" (backslash + n) rather
    # than a real newline, so a JD reads 'Job Description \n \n \n Address:'. Decode the common
    # literal escapes to real whitespace so the text renders as structured lines, not gibberish.
    # Guarded so it only touches affected text (a stray real backslash in prose is left alone).
    if "\\n" in s or "\\t" in s or "\\r" in s:
        s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n").replace("\\t", " ")
    prev = None
    for _ in range(3):                      # decode entities to stable (handles single + double)
        if s == prev:
            break
        prev, s = s, html.unescape(s)
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    # Feeds wrap each bullet's text in its own paragraph: <li><p>text</p></li>. Left alone, the
    # inner </p> AND the </li> each become a newline, so every bullet ends with a blank line and
    # the list renders double-spaced. Unwrap the paragraph that sits directly inside a list item
    # first, so one <li> yields exactly one line.
    s = re.sub(r"(?i)<li[^>]*>\s*<p[^>]*>", "<li>", s)
    s = re.sub(r"(?i)</p>\s*</li>", "</li>", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|ul|ol|tr)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    # Markdown cleanup: aggregator descriptions (freehire etc.) arrive as MARKDOWN, so strip the
    # emphasis markers and horizontal-rule lines that would otherwise render as literal asterisks
    # ("*Location:*", a line of "*****"). Bullets ("* item", "- item") are kept, normalised to "• ".
    s = re.sub(r"(?m)^[ \t]*([*_=-])\1{2,}[ \t]*$", "", s)          # ***/---/___ rules -> gone
    s = re.sub(r"\*\*+([^*\n]+?)\*\*+", r"\1", s)                    # **bold** / ***strong***
    s = re.sub(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])", r"\1", s)   # *emphasis* (not "* bullet")
    s = re.sub(r"(?m)^[ \t]*[*\-·▪‣][ \t]+", "• ", s)              # markdown bullet marker -> "• "
    s = re.sub(r"[ \t]+", " ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))              # no stray leading/trailing spaces
    # Some feeds format a list as "* \n\ntext" -- a lone marker on its own line, the item's text a
    # blank line below (Abbott's postings do this). Pull the text back up so it renders as one
    # bullet, not a stray "•" followed by an orphan paragraph.
    s = re.sub(r"(?m)^•[ \t]*\n(?:[ \t]*\n)*(?=[^\s•])", "• ", s)
    s = re.sub(r"\n\n+(?=• )", "\n", s)                             # a list hugs its items (no blank gaps)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _remote_from_text(loc: str) -> str:
    return "remote" if "remote" in (loc or "").lower() else ""


def _epoch_iso(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _job(source, board_id, job_id, company, title, location, remote, url, jd, posted):
    return {
        "source": source,
        "source_id": f"{source}:{board_id}:{job_id}",
        "company": company,
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "remote": remote or "",
        "url": url or "",
        "jd_text": jd or "",
        "posted_at": posted or "",
        # Compact pay string ("$120k–$160k/yr") when a feed publishes one, else "" -> the UI
        # shows an honest "Salary TBD". Only the aggregators (Adzuna/JSearch) carry pay; the
        # ATS boards don't expose it, so most rows stay blank, same as the competition.
        "salary": "",
    }


# How a feed labels the pay period -> the compact suffix we render.
_SAL_PERIOD = {"year": "/yr", "yr": "/yr", "annual": "/yr", "annually": "/yr",
               "month": "/mo", "monthly": "/mo", "week": "/wk", "weekly": "/wk",
               "day": "/day", "daily": "/day", "hour": "/hr", "hourly": "/hr", "hr": "/hr"}


def _fmt_salary(lo, hi, currency="USD", period="year") -> str:
    """Format a min/max pay range into a compact, scannable string. Yearly figures collapse to
    'k' ($120k), hourly/daily stay whole ($45/hr). Empty string when there's no usable number,
    so the caller can fall back to 'Salary TBD'."""
    def num(x):
        try:
            v = float(x)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    lo, hi = num(lo), num(hi)
    if lo is None and hi is None:
        return ""
    per = _SAL_PERIOD.get((period or "year").strip().lower(), "/yr")
    hourly = per in ("/hr", "/day")
    sym = "$" if (currency or "USD").upper() == "USD" else (currency or "").upper() + " "

    def money(v):
        return f"{sym}{round(v / 1000)}k" if (not hourly and v >= 1000) else f"{sym}{v:,.0f}"

    body = f"{money(lo)}-{money(hi)}" if (lo and hi and hi >= lo) else money(hi or lo)
    return f"{body}{per}"


# A dollar figure with optional k/thousand suffix: "$78,000.00", "$120k", "$156,000", "$45".
_MONEY = r"\$\s*([\d,]+(?:\.\d+)?)\s*(k|thousand)?"
# Words near a figure that tell us the pay PERIOD, so an hourly "$45" isn't rendered as "$45/yr".
# \b-anchored so "Working Conditions/Hours" doesn't read as an hourly rate via its "/Hour".
_PER_HR = re.compile(r"(per\s*hour\b|/\s*hour\b|/\s*hr\b|an\s*hour\b|hourly\b|per\s*hr\b)", re.I)
_PER_YR = re.compile(r"(per\s*year|/\s*year|/\s*yr|annual|annually|a\s*year|per\s*annum)", re.I)
# The figure only counts as PAY when a compensation cue sits close by, so "$2B in revenue" or a
# "$500 referral bonus" in the body never masquerades as the salary. Conservative on purpose:
# a missing range ("Salary TBD") is honest, a wrong one is not (CLAUDE.md §8).
_PAY_CUE = re.compile(
    r"(salary|salaries|base\s*pay|base\s*salary|pay\s*range|pay\s*rate|compensation|"
    r"total\s*comp|the\s*base\s*pay|hourly\s*rate|annual\s*(?:base|salary|pay)|"
    r"(?:pay|compensation)\s*(?:is|of|range))", re.I)


def salary_from_text(jd: str) -> str:
    """Pull a pay range out of the JD prose when the feed didn't publish one. Employers often
    state pay in the body ("The base pay for this position is $78,000.00 - $156,000.00"); LinkedIn
    surfaces it, our ATS feeds mostly don't. We only trust a figure that has a compensation cue
    (salary/base pay/compensation/hourly rate) within ~80 chars, so numbers elsewhere in the
    posting can't be mistaken for pay. Returns a compact string ("$78k-$156k/yr") or "" when no
    trustworthy figure is present."""
    if not jd:
        return ""
    text = " ".join(jd.split())
    def scale(num_s, suffix):
        v = float(num_s.replace(",", ""))
        # "$120k" -> 120000, but a stray k after an already-large number ("$185,000k") is spurious:
        # nobody writes a salary in thousands-of-thousands, so only scale genuinely small numbers.
        return v * 1000 if (suffix and v < 1000) else v
    # No one is paid hundreds of dollars an hour in a posted range; a big number tagged "hourly"
    # is a mislabel ("Hourly Pay Rate Range $197,800"), so treat any value this large as annual.
    HOURLY_CEIL = 2000
    best = None                                        # (lo, hi, hourly) for the widest cued range
    # Ranges first: "$A - $B", "$A to $B". Both ends share one period, judged from nearby words.
    for m in re.finditer(_MONEY + r"\s*(?:-|\u2013|\u2014|to|through)\s*" + _MONEY, text, re.I):
        window = text[max(0, m.start() - 80): m.end() + 40]
        if not _PAY_CUE.search(window):
            continue
        lo, hi = scale(m.group(1), m.group(2)), scale(m.group(3), m.group(4))
        if lo <= 0 or hi <= 0 or hi < lo:
            continue
        hourly = (bool(_PER_HR.search(window)) or (hi < 1000 and not _PER_YR.search(window))) \
            and hi < HOURLY_CEIL
        if best is None or (hi - lo) > (best[1] - best[0]):
            best = (lo, hi, hourly)
    if best:
        return _fmt_salary(best[0], best[1], period="hour" if best[2] else "year")
    # No range: accept a single cued figure ("salary is $120,000").
    for m in re.finditer(_MONEY, text):
        window = text[max(0, m.start() - 80): m.end() + 40]
        if not _PAY_CUE.search(window):
            continue
        v = scale(m.group(1), m.group(2))
        if v <= 0:
            continue
        hourly = (bool(_PER_HR.search(window)) or (v < 1000 and not _PER_YR.search(window))) \
            and v < HOURLY_CEIL
        if (hourly and v > 200) or (not hourly and v < 10000):
            continue                                   # implausible: skip rather than mislead
        return _fmt_salary(v, None, period="hour" if hourly else "year")
    return ""


# --------------------------------------------------------------------------- #
# Per-ATS adapters
# --------------------------------------------------------------------------- #

def greenhouse_jobs(board_id, company="", fetch=fetch_json):
    data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true")
    out = []
    for j in (data or {}).get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_job(
            "greenhouse", board_id, j.get("id"), company or board_id.title(),
            j.get("title", ""), loc, _remote_from_text(loc),
            j.get("absolute_url", ""), html_to_text(j.get("content", "")),
            j.get("first_published") or j.get("updated_at") or "",
        ))
    return out


def lever_jobs(board_id, company="", fetch=fetch_json):
    data = fetch(f"https://api.lever.co/v0/postings/{board_id}?mode=json")
    out = []
    for j in (data if isinstance(data, list) else []):
        cat = j.get("categories") or {}
        wt = (j.get("workplaceType") or "").lower().replace("on-site", "onsite").replace("on site", "onsite")
        remote = wt if wt in ("remote", "hybrid", "onsite") else _remote_from_text(cat.get("location", ""))
        jd = j.get("descriptionPlain", "") or html_to_text(j.get("description", ""))
        extra = j.get("additionalPlain", "") or ""
        out.append(_job(
            "lever", board_id, j.get("id"), company or board_id.title(),
            j.get("text", ""), cat.get("location", ""), remote,
            j.get("hostedUrl", ""), (jd + ("\n\n" + extra if extra else "")).strip(),
            _epoch_iso(j.get("createdAt")),
        ))
    return out


def ashby_jobs(board_id, company="", fetch=fetch_json):
    data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{board_id}")
    out = []
    for j in (data or {}).get("jobs", []):
        wt = (j.get("workplaceType") or "").lower()
        remote = "remote" if j.get("isRemote") else (
            wt if wt in ("hybrid", "onsite") else _remote_from_text(j.get("location", "")))
        jd = j.get("descriptionPlain", "") or html_to_text(j.get("descriptionHtml", ""))
        out.append(_job(
            "ashby", board_id, j.get("id"), company or board_id.title(),
            j.get("title", ""), j.get("location", ""), remote,
            j.get("jobUrl", "") or j.get("applyUrl", ""), jd,
            (j.get("publishedAt", "") or "")[:10],
        ))
    return out


def _norm_workplace(wp: str) -> str:
    wp = (wp or "").lower().replace("on-site", "onsite").replace("on site", "onsite")
    return wp if wp in ("remote", "hybrid", "onsite") else ""


def workable_jobs(board_id, company="", fetch=fetch_json):
    # Workable's public widget returns FLAT job objects: city/state/country plus a
    # `telecommuting` flag (and sometimes a nested `location`/`locations[]`).
    data = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{board_id}?details=true")
    out = []
    for j in (data or {}).get("jobs", []):
        loc = j.get("location")
        if isinstance(loc, dict):
            loc_str = loc.get("location_str") or ", ".join(
                x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        else:
            loc_str = ", ".join(x for x in (j.get("city"), j.get("state"),
                                            j.get("country")) if x)
        remote = "remote" if j.get("telecommuting") else (
            _norm_workplace(j.get("workplace") or "") or _remote_from_text(loc_str))
        out.append(_job(
            "workable", board_id, j.get("shortcode") or j.get("id") or j.get("code"),
            company or board_id.title(), j.get("title", ""), loc_str, remote,
            j.get("url") or j.get("shortlink") or j.get("application_url", ""),
            html_to_text(j.get("description", "")),
            (j.get("published_on") or j.get("created_at") or "")[:10],
        ))
    return out


def recruitee_jobs(board_id, company="", fetch=fetch_json):
    data = fetch(f"https://{board_id}.recruitee.com/api/offers/")
    out = []
    for j in (data or {}).get("offers", []):
        loc = j.get("location") or ", ".join(
            x for x in (j.get("city"), j.get("country")) if x)
        remote = "remote" if j.get("remote") else _remote_from_text(loc)
        out.append(_job(
            "recruitee", board_id, j.get("id"), company or board_id.title(),
            j.get("title", ""), loc, remote,
            j.get("careers_url") or j.get("careers_apply_url", ""),
            html_to_text(j.get("description", "")),
            (j.get("published_at") or "")[:10],
        ))
    return out


# SmartRecruiters publishes its Posting API limit as 10 requests a second (developers.
# smartrecruiters.com/docs/rate-limiting, read 2026-10-01; a 429 past it, with X-RateLimit-*
# headers). A crawl reading one board's postings in a row stays well under it, and a 429 / 5xx
# backs off (fetch_with_backoff) before one more try.
SMARTRECRUITERS_REQUEST_GAP = 0.2   # seconds between consecutive detail reads of one board


def smartrecruiters_detail(board_id, posting_id, fetch=fetch_json, sleep=time.sleep):
    """One posting's PUBLIC url + JD text. The list endpoint omits the description body, so
    it lives here on a detail endpoint. Returns (public_url, jd_text); ('', '') on error."""
    try:
        det = fetch_with_backoff(
            fetch, f"https://api.smartrecruiters.com/v1/companies/{board_id}/postings/{posting_id}",
            sleep, retries=1, max_backoff=30.0)
    except Exception:
        return "", ""
    posting_url = (det or {}).get("postingUrl") or ""            # the PUBLIC job page
    secs = ((det or {}).get("jobAd") or {}).get("sections") or {}
    parts = [(secs.get(k) or {}).get("text", "") for k in
             ("companyDescription", "jobDescription", "qualifications",
              "additionalInformation")]
    return posting_url, html_to_text("\n".join(x for x in parts if x))


def smartrecruiters_fill(job: dict, fetch=fetch_json, sleep=time.sleep) -> bool:
    """Give a list-only SmartRecruiters row its description (and the posting's real public
    page, which the detail carries) from the per-posting endpoint. The one place the detail is
    folded into a row: the `details=True` crawl, the check-before-keep refresh
    (service.select_checked_rows) and the detail views all go through here. Returns whether a
    description was read; a blank / failed detail leaves the row untouched."""
    parts = str(job.get("source_id") or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "smartrecruiters":
        return False
    durl, jd = smartrecruiters_detail(parts[1], parts[2], fetch, sleep)
    if durl:
        job["url"] = durl
    if not jd.strip():
        return False
    job["jd_text"] = jd
    return True


def smartrecruiters_jobs(board_id, company="", fetch=fetch_json, details=False):
    """A company's SmartRecruiters postings. The list endpoint carries title, company, and
    location but NOT the JD body, so by DEFAULT we build fast, JD-less rows (one request per
    board) and let the detail view pull the description lazily when a role is opened. Pass
    details=True to also fetch each posting's JD + public URL (N+1 requests) — used for a
    single role, not a wide crawl."""
    data = fetch(f"https://api.smartrecruiters.com/v1/companies/{board_id}/postings?limit=100")
    out = []
    for p in (data or {}).get("content", []):
        loc = p.get("location") or {}
        loc_str = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        remote = "remote" if loc.get("remote") else (
            "hybrid" if loc.get("hybrid") else _remote_from_text(loc_str))
        pid = p.get("id")
        # p["ref"] is the API self-link (raw JSON) — never a URL to show the person; the
        # public page is jobs.smartrecruiters.com/{company}/{id}, refined by detail if fetched.
        url = f"https://jobs.smartrecruiters.com/{board_id}/{pid}"
        row = _job(
            "smartrecruiters", board_id, pid,
            company or (p.get("company") or {}).get("name") or board_id.title(),
            p.get("name", ""), loc_str, remote, url, "",
            (p.get("releasedDate") or "")[:10],
        )
        if details:
            smartrecruiters_fill(row, fetch)
        out.append(row)
    return out


def remotive_jobs(search="", company="", fetch=fetch_json):
    """Aggregator feed (not a single company board): Remotive's keyless public API
    of vetted remote jobs, searched by keyword. GREEN lane -- official public JSON."""
    q = f"?search={urllib.parse.quote(search)}" if search else ""
    data = fetch(f"https://remotive.com/api/remote-jobs{q}")
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(_job(
            "remotive", "", j.get("id"), j.get("company_name", ""),
            j.get("title", ""), j.get("candidate_required_location", ""), "remote",
            j.get("url", ""), html_to_text(j.get("description", "")),
            (j.get("publication_date") or "")[:10],
        ))
    return out


ADZUNA_PER_PAGE = 50            # Adzuna's max page size
ADZUNA_DEFAULT_PAGES = 10       # pages per search term unless ADZUNA_PAGES overrides

_US_MARKERS = {"US", "USA", "UNITED STATES"}


def _adzuna_location(locobj: dict) -> str:
    """Build a readable, country-aware location from an Adzuna location object.

    Adzuna's `display_name` is only 'City, County' (e.g. 'Moorestown, Burlington County') —
    it drops the state and country, so downstream US detection (sponsors.looks_us, which
    keys off a state code or a US marker) misses genuinely-US roles and their visa badges
    never show. The `area` array carries the full trail ['US','New Jersey',...], so we fold
    the state and a 'US' marker back in when the area says the role is in the US. Driven by
    the DATA (area[0]), not the query country, so a gb/ca search is never mislabelled US."""
    display = (locobj or {}).get("display_name", "") or ""
    area = (locobj or {}).get("area") or []
    if area and str(area[0]).strip().upper() in _US_MARKERS:
        state = area[1] if len(area) > 1 else ""
        parts = [display] if display else []
        if state and state.lower() not in display.lower():
            parts.append(state)
        parts.append("US")
        return ", ".join(p for p in parts if p)
    return display


def adzuna_jobs(search="", company="", fetch=fetch_json, pages: int | None = None):
    """Aggregator feed: Adzuna's keyed public API (free dev app_id/app_key) aggregating many
    boards across ~20 countries — the biggest single coverage boost, and the one feed that
    reaches the enterprise/Workday-hosted roles our direct ATS crawl can't. GREEN lane,
    official public JSON. No-ops quietly if ADZUNA_APP_ID / ADZUNA_APP_KEY aren't set in
    credentials.env, so it's opt-in. Country defaults to 'us' (override ADZUNA_COUNTRY).

    PAGINATED: Adzuna returns 50 results per page, so one call is only 50 jobs. We walk up
    to `pages` pages (env ADZUNA_PAGES, default 10 -> up to 500 jobs per search term),
    stopping early on the last partial page. Mind the free tier's ~1,000 calls/month: each
    page is one call, and refresh runs one search per title term. Raise ADZUNA_PAGES (and/or
    a paid plan) to push toward full 500k-scale volume."""
    app_id, app_key = _cred("ADZUNA_APP_ID"), _cred("ADZUNA_APP_KEY")
    if not (app_id and app_key and search):
        return []
    country = (_cred("ADZUNA_COUNTRY") or "us").lower()
    if pages is None:
        try:
            pages = int(_cred("ADZUNA_PAGES") or ADZUNA_DEFAULT_PAGES)
        except ValueError:
            pages = ADZUNA_DEFAULT_PAGES
    pages = max(1, pages)
    out = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        # sort_by=date -> FRESHEST first (default is relevance, which buries today's postings
        # under older popular ones); max_days_old bounds it to recent roles. Same call budget,
        # far fresher results -- the single biggest free freshness win on this feed.
        max_days = _cred("ADZUNA_MAX_DAYS") or "7"
        url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
               f"?app_id={urllib.parse.quote(app_id)}&app_key={urllib.parse.quote(app_key)}"
               f"&results_per_page={ADZUNA_PER_PAGE}&what={urllib.parse.quote(search)}"
               f"&sort_by=date&max_days_old={urllib.parse.quote(str(max_days))}"
               f"&content-type=application/json")
        try:
            results = (fetch(url) or {}).get("results", [])
        except Exception:
            break                                    # a rate-limit / blip ends this term's paging
        for j in results:
            loc = _adzuna_location(j.get("location") or {})
            job = _job(
                "adzuna", "", j.get("id"), (j.get("company") or {}).get("display_name", ""),
                j.get("title", ""), loc, _remote_from_text(loc),
                j.get("redirect_url", ""), html_to_text(j.get("description", "")),
                (j.get("created") or "")[:10],
            )
            # Adzuna publishes salary_min/max (already annualised, country currency). Mark
            # model-estimated figures with '~' so we never present a guess as a quoted number.
            sal = _fmt_salary(j.get("salary_min"), j.get("salary_max"),
                              "USD" if country == "us" else country.upper(), "year")
            if sal and str(j.get("salary_is_predicted")) == "1":
                sal = "~" + sal
            job["salary"] = sal
            if job["source_id"] not in seen:        # Adzuna can repeat a listing across pages
                seen.add(job["source_id"])
                out.append(job)
        if len(results) < ADZUNA_PER_PAGE:
            break                                    # last page reached
    return out


def arbeitnow_jobs(search="", company="", fetch=fetch_json):
    """Aggregator feed: Arbeitnow's FREE, KEYLESS public job board API. Its whole angle is
    visa-friendly / sponsorship roles, so it directly grows the sponsor-friendly half of the feed
    (many are remote, some US). The public API has no server-side search, so we fetch the recent
    feed and filter by the keyword client-side. GREEN lane, official public JSON, no key."""
    data = fetch("https://www.arbeitnow.com/api/job-board-api")
    term = (search or "").strip().lower()
    out = []
    for j in (data or {}).get("data", []):
        title = j.get("title", "") or ""
        if term and term not in title.lower():
            continue
        created = j.get("created_at")
        posted = _epoch_iso(created * 1000) if isinstance(created, (int, float)) else ""
        loc = j.get("location", "")
        remote = "remote" if j.get("remote") else _remote_from_text(loc)
        out.append(_job(
            "arbeitnow", "", j.get("slug"), j.get("company_name", ""),
            title, loc, remote, j.get("url", ""), html_to_text(j.get("description", "")), posted,
        ))
    return out


def remoteok_jobs(search="", company="", fetch=fetch_json):
    """Aggregator feed: RemoteOK's KEYLESS public API of remote jobs (many US-remote). The API
    returns a JSON ARRAY whose first element is a legal/notice object (no 'position'), which we
    skip. No server-side search, so we filter by keyword client-side. GREEN lane, no key."""
    data = fetch("https://remoteok.com/api")
    term = (search or "").strip().lower()
    out = []
    for j in (data or []):
        if not isinstance(j, dict) or not (j.get("position") or j.get("title")):
            continue                                  # skip the leading legal-notice element
        title = j.get("position") or j.get("title") or ""
        if term and term not in title.lower():
            continue
        out.append(_job(
            "remoteok", "", j.get("id") or j.get("slug"), j.get("company", ""),
            title, j.get("location", "") or "Remote", "remote",
            j.get("url", ""), html_to_text(j.get("description", "")), (j.get("date") or "")[:10],
        ))
    return out


def _rapidapi_get(url: str, timeout: int = 20):
    """GET a RapidAPI endpoint with the key in HEADERS (not the URL). Honest client, no
    proxy/masquerade (CLAUDE.md §7). Key from RAPIDAPI_KEY / JSEARCH_API_KEY."""
    key = _cred("RAPIDAPI_KEY") or _cred("JSEARCH_API_KEY")
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        "Accept": "application/json", "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def jsearch_jobs(search="", company="", fetch=fetch_json, pages: int = 1):
    """Aggregator feed: JSearch (RapidAPI) is a LICENSED aggregator of the big consumer job
    boards via Google for Jobs — the coverage our ATS crawl and Adzuna under-serve, so it's
    the complement, not a duplicate. It's an official paid API (no scraping on our side; §7).
    Opt-in: no-ops without RAPIDAPI_KEY / JSEARCH_API_KEY in credentials.env. Country 'us'.

    Auth is a HEADER, so unlike Adzuna we can't ride the plain fetch_json passed by refresh;
    when that default is passed we swap in _rapidapi_get (which sets the key header). An
    injected test fetch is used as-is, so tests stay offline."""
    key = _cred("RAPIDAPI_KEY") or _cred("JSEARCH_API_KEY")
    if not (key and search):
        return []
    country = (_cred("JSEARCH_COUNTRY") or "us").lower()
    getter = _rapidapi_get if fetch is fetch_json else fetch
    out, seen = [], set()
    for page in range(1, max(1, pages) + 1):
        # v5 renamed the search endpoint to /search-v2 (the old /search now 404s). Page 1
        # works with query+country; deeper pages use a cursor (added when needed).
        # date_posted bounds results to recent postings (freshness); overridable via env.
        recency = _cred("JSEARCH_DATE_POSTED") or "week"
        url = (f"https://jsearch.p.rapidapi.com/search-v2?query={urllib.parse.quote(search)}"
               f"&page={page}&num_pages=1&country={country}"
               f"&date_posted={urllib.parse.quote(recency)}")
        try:
            results = (getter(url) or {}).get("data", []) or []
        except Exception:
            break
        for j in results:
            loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"),
                                        j.get("job_country")) if x)
            job = _job(
                "jsearch", "", j.get("job_id"), j.get("employer_name", ""),
                j.get("job_title", ""), loc,
                "remote" if j.get("job_is_remote") else _remote_from_text(loc),
                j.get("job_apply_link", ""), html_to_text(j.get("job_description", "")),
                (j.get("job_posted_at_datetime_utc") or "")[:10],
            )
            # JSearch carries min/max with an explicit currency + period (YEAR/HOUR/...).
            job["salary"] = _fmt_salary(j.get("job_min_salary"), j.get("job_max_salary"),
                                        j.get("job_salary_currency") or "USD",
                                        j.get("job_salary_period") or "year")
            if job["source_id"] not in seen:
                seen.add(job["source_id"])
                out.append(job)
        if len(results) < 10:                            # JSearch returns ~10/page; short = last
            break
    return out


def freehire_jobs(search="", company="", fetch=fetch_json):
    """Aggregator feed: freehire.me's FREE, KEYLESS public JSON API. It aggregates 1M+ postings
    across many markets (tech-heavy) and is MINUTE-fresh, so querying it US-only + recent gives
    the sponsorship board fresh, high-volume roles at no cost -- the single biggest free coverage
    win. GREEN lane: we're an honest client of a public, documented API (no scraping on our
    side). It's a best-effort community service (no SLA), so it's one source among several -- a
    hiccup degrades to the others via refresh_watchlist's per-feed error handling.

    US-only (this product is US visa sponsorship) and bounded to recent postings for freshness.
    Overridable: FREEHIRE_MAX_DAYS (recency window), FREEHIRE_LIMIT (rows per term)."""
    term = (search or "").strip()
    days = _cred("FREEHIRE_MAX_DAYS") or "14"
    try:
        limit = max(1, min(200, int(_cred("FREEHIRE_LIMIT") or "100")))
    except ValueError:
        limit = 100
    params = urllib.parse.urlencode({
        "q": term, "limit": limit, "offset": 0, "countries": "US",
        "posted_within_days": days, "include_description": "true",
        "description_format": "text", "semantic_ratio": "0",
    })
    data = fetch(f"https://freehire.me/api/v1/agent/jobs/search?{params}") or {}
    out = []
    for j in (data.get("data") or []):
        out.append(_freehire_row(j))
    return out


def looks_truncated(job: dict, jd: str) -> bool:
    """True when a stored JD is only a preview, not the full posting. Adzuna's API returns a
    ~500-char snippet (their terms forbid redistributing the full text) that ends in an ellipsis.
    Judge the ACTUAL text, not just the source: an Adzuna row we've already enriched from freehire
    is full and must NOT re-flag as a preview. So: a trailing ellipsis, or a preview-only source
    whose text is still short (< 900 chars)."""
    jd = (jd or "").rstrip()
    if jd.endswith(("…", "...")):
        return True
    return (job or {}).get("source") == "adzuna" and len(jd) < 900


def _norm_co(name: str) -> str:
    """Loose company key for matching across feeds: lowercase alnum, legal suffixes dropped."""
    n = re.sub(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|group|plc|pbc|"
               r"gmbh|lp|llp|the)\b", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", n)


def freehire_fulltext(title: str, company: str = "", fetch=fetch_json) -> str:
    """Full JD text for a role, from freehire, matched by COMPANY + title -- used to enrich a
    truncated snippet (Adzuna's API returns only a ~500-char preview ending in '…'). Conservative:
    requires the company to match (one being a substring of the other, after normalising) so we
    never show a different company's description; among those, takes the longest (fullest) JD whose
    title shares a real word with ours. '' when there's no confident match, so the caller keeps the
    snippet rather than risk the wrong posting."""
    title = (title or "").strip()
    want_co = _norm_co(company)
    if not title or not want_co:
        return ""                                   # need BOTH to match safely -> else keep snippet
    try:
        rows = freehire_jobs(f"{title} {company}".strip(), fetch=fetch)
    except Exception:                               # noqa: BLE001 - enrichment is best-effort
        return ""
    stop = {"the", "and", "for", "with", "of", "in", "at", "a", "an", "to", "senior", "sr", "jr",
            "junior", "lead", "principal", "staff", "i", "ii", "iii", "iv"}
    want_words = {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in stop and len(w) > 2}
    best = ""
    for r in rows:
        co = _norm_co(r.get("company", ""))
        if not co or not (want_co in co or co in want_co):
            continue                                # unknown or different employer -> never use its JD
        rw = {w for w in re.findall(r"[a-z0-9]+", (r.get("title") or "").lower()) if w not in stop}
        if want_words and not (want_words & rw):
            continue                                # unrelated role at the same employer
        jd = r.get("jd_text") or ""
        if len(jd) > len(best):
            best = jd
    return best


FREEHIRE_BULK_MAX = 6000        # freshest-US rows pulled per refresh (env FREEHIRE_BULK). Back to
# 6000 now that #287 moved the visa-sponsor lookup off RAM: the ~290MB in-memory index build (the
# multi-second boot CPU spike that starved the 1-CPU broker's health check) is gone, and per-job
# tagging is a fast indexed SQL lookup. #281 cut this to 3000 as an emergency stopgap during the
# crash-loop; with that root cause fixed the box has plenty of headroom for the fuller board again.


def _freehire_salary(j, jd_text) -> str:
    """freehire's pay, if it published a structured range, else pulled from the JD prose. Most
    freehire rows carry no pay field, so the text fallback is what fills the board's salaries."""
    lo, hi = j.get("salary_min") or j.get("min_salary"), j.get("salary_max") or j.get("max_salary")
    if lo or hi:
        s = _fmt_salary(lo, hi, j.get("salary_currency") or "USD", j.get("salary_period") or "year")
        if s:
            return s
    return salary_from_text(jd_text)


def _freehire_row(j) -> dict:
    loc = j.get("location") or ", ".join(j.get("cities") or [])
    jd = html_to_text(j.get("description", ""))
    # Keep freehire's FULL posting timestamp (e.g. "2026-08-11T02:04:05Z"), do NOT truncate to a
    # date. It's the employer's real per-job post time, to the second, which is what lets the board
    # show an honest "New 11m ago" that differs per role. Truncating to [:10] threw that away and
    # forced the label back onto our uniform crawl time, so every card read the same age.
    row = _job(
        "freehire", "", j.get("public_slug") or j.get("external_id"),
        j.get("company", ""), j.get("title", ""), loc,
        _remote_from_text(loc), j.get("url", ""), jd,
        (j.get("posted_at") or j.get("created_at") or "").strip(),
    )
    row["salary"] = _freehire_salary(j, jd)
    # freehire's own enrichment states whether THIS posting offers visa sponsorship. It's the
    # strongest, most current signal for an international student -- the employer's declaration on
    # this exact role, distinct from our DOL sponsor-HISTORY badge. True when stated, None when the
    # posting is silent (we never infer "no" from silence). See sponsorship_stated in the board.
    vs = (j.get("enrichment") or {}).get("visa_sponsorship")
    row["sponsorship_stated"] = vs if isinstance(vs, bool) else None
    return row


def freehire_bulk(search="", company="", fetch=fetch_json, cap=None, visa_sponsorship=False):
    """Bulk-pull the FRESHEST US jobs from freehire.me (newest-first), paginated across all
    fields -- this is the free high-volume feed. freehire carries tens of thousands of US roles
    posted in the last week, so rather than a few per search term we grab the newest ~1,200 and
    let the board's own filters narrow from there. Keyless, GREEN lane (honest client of a public
    API). Signature matches the aggregator contract; the `search` arg is ignored (bulk is
    all-fields). `cap` overrides the row budget for a call (the fast freshness loop passes a SMALL
    cap so each tick is a handful of requests, not ~60). `visa_sponsorship=True` asks freehire for
    ONLY roles whose posting states visa sponsorship (its own verified server-side filter) -- used
    to guarantee the board carries a healthy slice of sponsor-friendly roles for our users.
    Tunable: FREEHIRE_BULK (rows), FREEHIRE_MAX_DAYS (recency)."""
    try:
        cap = max(100, min(20000, int(cap if cap is not None
                                      else (_cred("FREEHIRE_BULK") or FREEHIRE_BULK_MAX))))
    except (ValueError, TypeError):
        cap = FREEHIRE_BULK_MAX
    days = _cred("FREEHIRE_MAX_DAYS") or "14"
    out, seen, per = [], set(), 100
    for offset in range(0, cap, per):
        p = {
            "q": "", "limit": per, "offset": offset, "countries": "US",
            "posted_within_days": days, "include_description": "true",
            "description_format": "text", "semantic_ratio": "0",
        }
        if visa_sponsorship:
            p["visa_sponsorship"] = "true"
        params = urllib.parse.urlencode(p)
        try:
            data = fetch(f"https://freehire.me/api/v1/agent/jobs/search?{params}") or {}
        except Exception:
            break                                        # transient upstream: keep what we have
        if not isinstance(data, dict):
            break                                        # unexpected shape: stop, keep what we have
        rows = data.get("data") or []
        for j in rows:
            key = j.get("public_slug") or j.get("external_id")
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(_freehire_row(j))
        if len(rows) < per:
            break                                        # last page
    return out


def workday_jobs(board_id, company="", fetch=fetch_json, **kw):
    """Workday adapter (sourcing/workday.py). board_id is "{tenant}.wd{n}/{site}", e.g.
    "nvidia.wd5/NVIDIAExternalCareerSite". Rows come back list-only (no JD) -- the service
    layer fills descriptions for the rows it keeps (``workday.fill_details``), and the detail
    view fetches the rest on demand. Imported lazily: workday.py builds on this module."""
    from .workday import workday_board_jobs
    return workday_board_jobs(board_id, company, fetch=fetch, **kw)


ADAPTERS = {
    "greenhouse": greenhouse_jobs,
    "lever": lever_jobs,
    "ashby": ashby_jobs,
    "workable": workable_jobs,
    "smartrecruiters": smartrecruiters_jobs,
    "recruitee": recruitee_jobs,
    "workday": workday_jobs,
}

# Keyword feeds that aren't tied to one company board. Signature: fn(search, company, fetch).
AGGREGATORS = {
    "remotive": remotive_jobs,
    "adzuna": adzuna_jobs,
    "arbeitnow": arbeitnow_jobs,
    "remoteok": remoteok_jobs,
    "jsearch": jsearch_jobs,
    "freehire": freehire_jobs,
}


_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")


def valid_board_id(board_id: str) -> bool:
    """A board id is a plain ATS board SLUG — it becomes a path segment (or, for Recruitee, a
    subdomain LABEL) in the feed URL. Restrict it to a conservative slug charset so a value
    like 'evil.com/' or 'x@host' can never escape the official host (SSRF) or inject extra
    path/query segments — recruitee_jobs interpolates board_id into the HOST position (§6)."""
    return bool(_BOARD_ID_RE.match(str(board_id or "")))


# A Workday board id names BOTH halves of the tenant's public URL: "{tenant}.wd{n}/{site}"
# (host label / path segment). Each half is a plain slug; the tenant half must be a Workday
# instance ("nvidia.wd5"), so the fetch can only ever land on *.myworkdayjobs.com.
_WORKDAY_BOARD_ID_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})\.wd(\d{1,3})/([A-Za-z0-9][A-Za-z0-9._-]{0,98})$")


def valid_workday_board_id(board_id: str) -> bool:
    return bool(_WORKDAY_BOARD_ID_RE.match(str(board_id or "")))


def valid_board_id_for(ats: str, board_id: str) -> bool:
    """The board-id guard for a given ATS: Workday's two-part id, a plain slug for the rest."""
    if (ats or "").strip().lower() == "workday":
        return valid_workday_board_id(board_id)
    return valid_board_id(board_id)


def normalize_jobs(ats: str, board_id: str, company: str = "", fetch=fetch_json, **kw):
    adapter = ADAPTERS.get(ats)
    if adapter is None:
        raise ValueError(f"unsupported ATS: {ats!r}")
    if not valid_board_id_for(ats, board_id):
        # Hard stop before the value reaches a URL — never fetch an off-platform host.
        raise ValueError(f"invalid board id {board_id!r}: must be a plain board slug "
                         "(letters, digits, '.', '_', '-'), not a URL or host")
    return adapter(board_id, company, fetch=fetch, **kw)
