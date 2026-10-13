"""Employer career sites (GREEN lane since 2026-10-11, CLAUDE.md §6): the biggest H-1B sponsors
(Amazon, Apple, JPMorgan, IBM, Oracle, Capital One...) run their own career sites, not one of
the ATS feeds, so the ATS-only crawl never saw them. This lane reads such a site the way Google
for Jobs does:

  1. robots.txt   -- fetched first and OBEYED (urllib.robotparser, our own user agent): a
                     disallowed path is never fetched, a Crawl-delay is honoured, and the lane
                     only follows the job sitemap(s) robots.txt or the site table names.
  2. job sitemap  -- the site's own list of job-page URLs (with lastmod), newest first.
  3. job page     -- the schema.org JobPosting JSON-LD the page embeds for search engines:
                     title, description, hiring organization, location, datePosted, validThrough.
                     Pages WITHOUT a JobPosting block are not jobs (or not for us) and are skipped.

Politeness, no exceptions: one request at a time per host with a gap (max(REQUEST_GAP,
Crawl-delay)), hard timeouts, a per-run page budget per site, honest identification (UA), and
a hard STOP for the host this run on a 403 / 429 / challenge page -- never a retry loop, never
a different identity, never a proxy. A host that answers like that has said no for now.

Only hosts in SITES are ever fetched (the table is the allowlist; a board id must name one of
them), so no stored value can point the crawler at an arbitrary host. Submission on these sites
is ASSISTED (submit/allowlist.py); this module reads postings, nothing more.
"""

from __future__ import annotations

import gzip
import hashlib
import html as _html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

from .ats import _job, html_to_text

SOURCE = "careersite"
UA = "SponsorJobsBot/1.0 (+https://sponsorjobs.ai; jobs crawler, obeys robots.txt)"
REQUEST_GAP = 1.0               # seconds between requests to one host (Crawl-delay may raise it)
TIMEOUT = 20
DEFAULT_MAX_URLS = 6000         # sitemap entries considered per site per run (newest lastmod first)
DEFAULT_MAX_DETAIL = 400        # job pages read per site per run (env CAREERSITE_MAX_DETAIL)
DEFAULT_MINUTES = 60.0          # the lane's wall-clock cap in the feed build (CAREERSITE_MINUTES)
DEFAULT_WORKERS = 4             # hosts crawled at once (one request at a time EACH)
DEFAULT_PAGE_BUDGET = 4000      # job pages read across ALL sites per run (CAREERSITE_PAGE_BUDGET)
MAX_CHILD_SITEMAPS = 40         # child sitemaps of an index read per site per run
MAX_SERVER_ERRORS = 3           # consecutive 5xx before the host is left alone this run
MAX_PAGE_ERRORS = 25            # unreadable job pages (404, timeout) before the site is left this run


# host -> how to crawl it. `company` is the display name (JobPosting.hiringOrganization wins
# when present); `sitemaps` overrides the robots.txt Sitemap lines when a site lists its jobs
# somewhere specific; `job_url` must match a job-page URL (everything else in the sitemap is
# skipped); `platform`, `verified` and `jobs_seen` record what the maintainer checked.
# The table lives in config/careersites.json (written by scripts/discover_careersites.py after it
# has verified each site: robots allows the sitemap and job pages, the sitemap lists job URLs, and
# a job page carries JobPosting JSON-LD with a title). A site goes in ONLY that way or by hand
# after the same checks (docs/feed.md, "Career sites"). The four below are the hand-verified
# originals (2026-10-11) and the fallback if the data file is missing or unreadable.
# Checked and left OUT: JPMorgan and Oracle (client-rendered Oracle Recruiting Cloud, dead
# sitemaps, WAF), IBM and Deloitte (Avature answers an empty body to crawlers), Infosys and
# Tesla (Akamai 403 on robots.txt itself), Meta (robots.txt header forbids automated collection
# without written permission), Amazon and Apple and Google (no sitemap, no JobPosting block;
# Amazon's public search JSON is a separate adapter). Intel is a Workday tenant (sourcing/workday.py).
SITES_FILE = Path(__file__).resolve().parents[1] / "config" / "careersites.json"

_BUILTIN_SITES: dict[str, dict] = {
    "apply.careers.microsoft.com": {
        "company": "Microsoft", "platform": "eightfold",
        "sitemaps": ["https://apply.careers.microsoft.com/careers/sitemap.xml?domain=microsoft.com"],
        "job_url": r"^https://apply\.careers\.microsoft\.com/careers/job/\d+",
    },
    "careers.qualcomm.com": {
        "company": "Qualcomm", "platform": "eightfold",
        "sitemaps": ["https://careers.qualcomm.com/careers/sitemap.xml?domain=qualcomm.com"],
        "job_url": r"^https://careers\.qualcomm\.com/careers/job/\d+",
    },
    "www.capitalonecareers.com": {
        "company": "Capital One", "platform": "radancy",
        "sitemaps": ["https://www.capitalonecareers.com/jobs_sitemap.xml"],
        "job_url": r"^https://www\.capitalonecareers\.com/job/",
    },
    "careers.cisco.com": {
        "company": "Cisco", "platform": "phenom",
        "sitemaps": ["https://careers.cisco.com/global/en/sitemap_index.xml"],
        "job_url": r"^https://careers\.cisco\.com/global/en/job/",
    },
}


def site_entry_ok(host: str, cfg) -> bool:
    """A table entry is usable only if it names a company and a job-URL pattern anchored to its
    OWN host, and every sitemap it lists is on that host (the table is the allowlist)."""
    if not isinstance(cfg, dict) or not host or host != host.lower() or "/" in host:
        return False
    if not cfg.get("company") or not isinstance(cfg.get("job_url"), str):
        return False
    if not cfg["job_url"].startswith("^https://" + re.escape(host) + "/"):
        return False
    try:
        re.compile(cfg["job_url"])
    except re.error:
        return False
    sitemaps = cfg.get("sitemaps") or []
    return isinstance(sitemaps, list) and all(
        isinstance(sm, str) and urlsplit(sm).scheme == "https" and urlsplit(sm).netloc.lower() == host
        for sm in sitemaps)


def load_sites(path=None, log=None) -> dict[str, dict]:
    """The site table from config/careersites.json ({"sites": {host: cfg}}), malformed entries
    dropped; the built-in four if the file is missing or unreadable."""
    path = Path(path) if path else SITES_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        table = raw.get("sites") if isinstance(raw, dict) else None
        if not isinstance(table, dict):
            raise ValueError("no 'sites' object")
    except (OSError, ValueError) as exc:
        if log:
            log(f"[careersite] {path}: {exc}; using the built-in sites")
        return {h: dict(c) for h, c in _BUILTIN_SITES.items()}
    out: dict[str, dict] = {}
    for host, cfg in table.items():
        if site_entry_ok(host, cfg):
            out[host] = cfg
        elif log:
            log(f"[careersite] {path}: skipping malformed entry {host!r}")
    return out


SITES: dict[str, dict] = load_sites()


class Blocked(Exception):
    """The host answered 403 / 429 / a bot-challenge page: stop crawling it this run."""


class Robots:
    """robots.txt the way search engines read it (RFC 9309): the group for our product token
    (else `*`), the LONGEST matching rule wins and Allow wins a tie, `*` and `$` wildcards.
    (Python's urllib.robotparser takes the FIRST matching rule, which turns the common Eightfold
    file "Disallow: / ... Allow: /careers" into "nothing allowed".)"""

    TOKEN = "sponsorjobsbot"

    def __init__(self, text: str = ""):
        self.rules: list[tuple[bool, str]] = []
        self.delay: float = 0.0
        self.sitemaps: list[str] = []
        groups: dict[str, list[tuple[bool, str]]] = {}
        delays: dict[str, float] = {}
        agents: list[str] = []
        fresh = True
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if not fresh:
                    agents = []
                fresh = True
                agents.append(value.lower())
                groups.setdefault(value.lower(), [])
            elif field == "sitemap":
                if value:
                    self.sitemaps.append(value)
            elif field in ("allow", "disallow"):
                fresh = False
                for a in agents:
                    if value or field == "disallow":
                        groups[a].append((field == "allow", value))
            elif field == "crawl-delay":
                fresh = False
                try:
                    for a in agents:
                        delays[a] = float(value)
                except ValueError:
                    pass
        mine = [a for a in groups if a != "*" and a in self.TOKEN]
        key = mine[0] if mine else ("*" if "*" in groups else None)
        if key is not None:
            self.rules = groups[key]
            self.delay = delays.get(key, 0.0)

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        if not pattern:
            return False
        anchored = pattern.endswith("$")
        core = pattern[:-1] if anchored else pattern
        rx = ".*".join(re.escape(part) for part in core.split("*"))
        return re.match(rx + ("$" if anchored else ""), path) is not None

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, bool] | None = None
        for allow, pattern in self.rules:
            if self._matches(pattern, path):
                score = (len(pattern), allow)
                if best is None or score > best:
                    best = score
        return True if best is None else best[1]


# ----------------------------------------------------------------------------------------- #
# fetching
# ----------------------------------------------------------------------------------------- #

_CHALLENGE = re.compile(r"(<title>\s*just a moment|cf-chl-|cf_chl_|px-captcha|_pxhc|"
                        r"incapsula|request unsuccessful|<title>\s*access denied|"
                        r"distil_r_captcha|are you a (human|robot)|akamai.*?bot ?manager)", re.I)


def fetch_page(url: str, timeout: int = TIMEOUT, **_) -> str:
    """GET one URL as text (gzip sitemaps decoded), identifying honestly. Raises urllib's
    HTTPError on any non-2xx; the caller decides what a 403 / 429 means (Blocked)."""
    url = quote(url, safe="/:?=&%#+@!$,;'()*[]~")              # a slug with an accent is fine
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")


def looks_challenged(text: str) -> bool:
    """A 200 that is really a bot wall (Cloudflare / PerimeterX / Akamai / Imperva interstitial)."""
    return bool(_CHALLENGE.search(text[:4000]))


class Host:
    """One site's politeness state for a run: request spacing, robots rules, error counters."""

    def __init__(self, host: str, cfg: dict, text_fetch=fetch_page, sleep=time.sleep,
                 clock=time.monotonic):
        self.host, self.cfg = host, cfg
        self._fetch, self._sleep, self._clock = text_fetch, sleep, clock
        self._last = None
        self.gap = REQUEST_GAP
        self.robots: Robots | None = None
        self.requests = 0
        self.server_errors = 0

    def get(self, url: str) -> str:
        """One polite request. Blocked on 403 / 429 / a challenge page; other HTTP errors
        propagate (a 404 is an answer). MAX_SERVER_ERRORS consecutive 5xx also block."""
        if urlsplit(url).netloc.lower() != self.host:
            raise ValueError(f"{url!r} is not on {self.host}")      # the table is the allowlist
        if self._last is not None:
            wait = self.gap - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()
        self.requests += 1
        try:
            text = self._fetch(url, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise Blocked(f"{self.host} answered {exc.code} for {url}") from exc
            if 500 <= exc.code < 600:
                self.server_errors += 1
                if self.server_errors >= MAX_SERVER_ERRORS:
                    raise Blocked(f"{self.host}: {self.server_errors} server errors in a row") from exc
            raise
        self.server_errors = 0
        if looks_challenged(text):
            raise Blocked(f"{self.host} answered a bot challenge for {url}")
        return text

    def load_robots(self) -> None:
        try:
            self.robots = Robots(self.get(f"https://{self.host}/robots.txt"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.robots = Robots("")                       # no robots.txt: everything allowed
            else:
                raise
        if self.robots.delay:
            self.gap = max(self.gap, self.robots.delay)

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(url)

    def sitemaps(self) -> list[str]:
        """The sitemaps to read: the table's override, else robots.txt's Sitemap lines."""
        if self.cfg.get("sitemaps"):
            return list(self.cfg["sitemaps"])
        return list(self.robots.sitemaps if self.robots else [])


# ----------------------------------------------------------------------------------------- #
# sitemaps
# ----------------------------------------------------------------------------------------- #

def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(xml: str) -> tuple[str, list[tuple[str, str]]]:
    """("index" | "urlset", [(loc, lastmod)]) for one sitemap document. Tolerant of the
    namespace, of missing lastmod, and of junk around the XML."""
    xml = xml.strip()
    if xml.startswith("﻿"):
        xml = xml[1:]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return "urlset", []
    kind = "index" if _tag(root) == "sitemapindex" else "urlset"
    out: list[tuple[str, str]] = []
    for entry in root:
        if _tag(entry) not in ("url", "sitemap"):
            continue
        loc = lastmod = ""
        for child in entry:
            t = _tag(child)
            if t == "loc":
                loc = (child.text or "").strip()
            elif t == "lastmod":
                lastmod = (child.text or "").strip()[:10]
        if loc:
            out.append((loc, lastmod))
    return kind, out


def _prefer_job_sitemaps(children: list[tuple[str, str]], job_hint: re.Pattern | None) -> list[str]:
    """Child sitemaps worth reading: those whose URL mentions jobs first (a big site's index
    lists images, pages, news...), newest lastmod first within each group."""
    def key(c):
        loc, lastmod = c
        hinted = bool(job_hint and job_hint.search(loc)) or "job" in loc.lower() or "career" in loc.lower()
        digits = re.sub(r"\D", "", lastmod)[:8]
        return (0 if hinted else 1, -int(digits) if digits else 0)
    return [loc for loc, _ in sorted(children, key=key)]


def job_urls(host: Host, *, max_urls: int = DEFAULT_MAX_URLS, log=None) -> list[tuple[str, str]]:
    """The job-page URLs a site's sitemap(s) list, newest lastmod first, robots-allowed only,
    matching the site's `job_url` pattern. Reads sitemap indexes one level deep."""
    pat = re.compile(host.cfg["job_url"])
    found: dict[str, str] = {}
    queue = list(host.sitemaps())
    children_read = 0
    while queue and len(found) < max_urls:
        sm = queue.pop(0)
        if not host.allowed(sm):
            continue
        try:
            kind, entries = parse_sitemap(host.get(sm))
        except urllib.error.HTTPError as exc:
            if log:
                log(f"[careersite] {host.host}: sitemap {sm} answered {exc.code}")
            continue
        if kind == "index":
            kids = _prefer_job_sitemaps(entries, re.compile(host.cfg.get("sitemap_hint", "")) if host.cfg.get("sitemap_hint") else None)
            room = MAX_CHILD_SITEMAPS - children_read
            queue.extend(kids[:max(0, room)])
            children_read += min(len(kids), max(0, room))
            continue
        for loc, lastmod in entries:
            if pat.match(loc) and host.allowed(loc) and loc not in found:
                found[loc] = lastmod
    rows = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    return rows[:max_urls]


# ----------------------------------------------------------------------------------------- #
# JobPosting JSON-LD
# ----------------------------------------------------------------------------------------- #

_LD = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)


def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _is_posting(obj) -> bool:
    return isinstance(obj, dict) and "JobPosting" in _as_list(obj.get("@type"))


def _walk(obj):
    """Every dict reachable from a JSON-LD document (handles @graph, lists, nesting)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def extract_jobposting(page_html: str) -> dict | None:
    """The first schema.org JobPosting object embedded in a page, or None."""
    for block in _LD.findall(page_html):
        text = _html.unescape(block).strip()
        text = re.sub(r"^\s*<!--|-->\s*$", "", text).strip()
        try:
            doc = json.loads(text, strict=False)
        except ValueError:
            continue
        for obj in _walk(doc):
            if _is_posting(obj):
                return obj
    return None


# ISO 3166 codes the big career sites put in addressCountry, spelled out so the US detector
# (sourcing.sponsors.looks_us) reads "Bengaluru, KA, India" and never mistakes IN for Indiana.
_COUNTRIES = {
    "US": "US", "USA": "US", "IN": "India", "CA": "Canada", "GB": "United Kingdom", "UK": "United Kingdom",
    "DE": "Germany", "IE": "Ireland", "IL": "Israel", "MX": "Mexico", "BR": "Brazil", "CN": "China",
    "JP": "Japan", "SG": "Singapore", "AU": "Australia", "TW": "Taiwan", "KR": "South Korea",
    "FR": "France", "ES": "Spain", "PL": "Poland", "NL": "Netherlands", "SE": "Sweden", "CZ": "Czechia",
    "HU": "Hungary", "RO": "Romania", "PT": "Portugal", "IT": "Italy", "CH": "Switzerland",
    "AT": "Austria", "BE": "Belgium", "DK": "Denmark", "FI": "Finland", "NO": "Norway", "AE": "UAE",
    "SA": "Saudi Arabia", "ZA": "South Africa", "NZ": "New Zealand", "PH": "Philippines",
    "VN": "Vietnam", "ID": "Indonesia", "MY": "Malaysia", "TH": "Thailand", "HK": "Hong Kong",
    "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "CR": "Costa Rica", "EG": "Egypt",
    "KE": "Kenya", "NG": "Nigeria", "PK": "Pakistan", "BD": "Bangladesh", "LK": "Sri Lanka",
    "TR": "Turkey", "UA": "Ukraine", "RS": "Serbia", "HR": "Croatia", "SK": "Slovakia",
    "BG": "Bulgaria", "GR": "Greece", "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia",
    "LU": "Luxembourg", "PR": "Puerto Rico",
}


def _named(v) -> str:
    """A schema.org value that may be a string or a {name: ...} object, as text."""
    if isinstance(v, dict):
        v = v.get("name") or v.get("value") or ""
    return str(v or "").strip()


def _country(v) -> str:
    text = _named(v)
    return _COUNTRIES.get(text.upper(), text)


def _location_text(posting: dict) -> tuple[str, str]:
    """("City, ST, US" style text, remote flag) from JobPosting.jobLocation / jobLocationType."""
    remote = "remote" if "TELECOMMUTE" in str(posting.get("jobLocationType") or "").upper() else ""
    parts_all: list[str] = []
    for loc in _as_list(posting.get("jobLocation"))[:3]:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") if isinstance(loc.get("address"), dict) else loc
        bits = [_named(addr.get("addressLocality")), _named(addr.get("addressRegion")),
                _country(addr.get("addressCountry"))]
        if not any(bits) and isinstance(addr.get("name"), str):
            bits = [addr["name"]]
        text = ", ".join(b for b in bits if b)
        if text:
            parts_all.append(text)
    if not parts_all and remote:
        country = _as_list(posting.get("applicantLocationRequirements"))
        for c in country:
            if _named(c):
                parts_all.append(f"Remote, {_country(c)}")
                break
        else:
            parts_all.append("Remote")
    return "; ".join(dict.fromkeys(parts_all)), remote


def _date(s) -> str:
    s = str(s or "").strip()
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else ""


def _job_key(url: str, posting: dict) -> str:
    ident = posting.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    ident = re.sub(r"[^A-Za-z0-9._-]", "", str(ident or ""))[:60]
    return ident or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def posting_to_job(host: str, company: str, url: str, posting: dict) -> dict | None:
    """A normalized job dict from a JobPosting, or None when the posting is closed
    (validThrough in the past) or has no title."""
    title = str(posting.get("title") or "").strip()
    if not title:
        return None
    valid = _date(posting.get("validThrough"))
    if valid and valid < date.today().isoformat():
        return None
    org = posting.get("hiringOrganization")
    org_name = (org.get("name") if isinstance(org, dict) else org) or company
    location, remote = _location_text(posting)
    jd = html_to_text(str(posting.get("description") or ""))
    job = _job(SOURCE, host, _job_key(url, posting), str(org_name).strip() or company, title,
               location, remote, str(posting.get("url") or url), jd, _date(posting.get("datePosted")))
    emp = ", ".join(str(x) for x in _as_list(posting.get("employmentType")) if x)
    if emp:
        job["employment_type"] = emp
    return job


# ----------------------------------------------------------------------------------------- #
# the crawl
# ----------------------------------------------------------------------------------------- #

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class PageBudget:
    """Job pages the whole lane may read this run, shared by every worker thread. Sitemaps and
    robots.txt are not counted: a site reached after the budget is spent still has its stored
    rows stamped "still listed" from its sitemap, it just reads no new pages."""

    def __init__(self, pages: int | None):
        self.left = pages
        self._lock = threading.Lock()

    def take(self) -> bool:
        if self.left is None:
            return True
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


def crawl_site(store, host_name: str, cfg: dict, *, text_fetch=fetch_page, sleep=time.sleep,
               clock=time.monotonic, deadline: float | None = None, max_detail: int | None = None,
               max_urls: int = DEFAULT_MAX_URLS, budget: PageBudget | None = None, log=None) -> dict:
    """Crawl one site into `store` (a sourcing.watchlist.Watchlist): robots, sitemap(s), then
    job pages newest first for URLs the store does not hold yet, until `max_detail` pages, the
    shared `budget` or the `deadline` (by `clock`). URLs already stored are stamped seen (still
    listed) without a request. `stopped` in the summary says why new pages were left unread
    ("max_detail" | "budget" | "deadline", "" = none left). Returns a summary dict; never raises
    for a blocked or broken host."""
    from .quality import is_intl_student_accessible
    if max_detail is None:
        max_detail = _env_int("CAREERSITE_MAX_DETAIL", DEFAULT_MAX_DETAIL)
    host = Host(host_name, cfg, text_fetch=text_fetch, sleep=sleep, clock=clock)
    summary = {"host": host_name, "listed": 0, "known": 0, "fetched": 0, "kept": 0,
               "no_posting": 0, "excluded": 0, "errors": 0, "requests": 0, "blocked": "", "error": "",
               "stopped": ""}
    try:
        host.load_robots()
        urls = job_urls(host, max_urls=max_urls, log=log)
        summary["listed"] = len(urls)
        known = store.source_ids_by_url(SOURCE, [u for u, _ in urls])
        if known:
            store.touch_seen(list(known.values()))
            summary["known"] = len(known)
        kept: list[dict] = []
        for url, lastmod in urls:
            if url in known:
                continue
            if summary["fetched"] >= max_detail:
                summary["stopped"] = "max_detail"
                break
            if deadline is not None and clock() >= deadline:
                summary["stopped"] = "deadline"
                break
            if budget is not None and not budget.take():
                summary["stopped"] = "budget"
                break
            summary["fetched"] += 1
            try:
                page = host.get(url)
            except Blocked:
                raise
            except Exception as exc:                       # noqa: BLE001 - 404, timeout, bad bytes
                summary["errors"] += 1                     # gone or unreadable: skip it
                if summary["errors"] >= MAX_PAGE_ERRORS:
                    raise Blocked(f"{host_name}: {summary['errors']} unreadable pages ({exc!s:.60})") from exc
                continue
            posting = extract_jobposting(page)
            job = posting_to_job(host_name, cfg.get("company", host_name), url, posting) if posting else None
            if job is None:
                summary["no_posting"] += 1
                continue
            if not job["posted_at"]:
                job["posted_at"] = lastmod
            if not is_intl_student_accessible(job):
                summary["excluded"] += 1
                continue
            kept.append(job)
            if len(kept) >= 50:
                store.upsert_jobs(kept)
                summary["kept"] += len(kept)
                kept = []
        if kept:
            store.upsert_jobs(kept)
            summary["kept"] += len(kept)
    except Blocked as exc:
        summary["blocked"] = str(exc)
    except Exception as exc:                               # noqa: BLE001 - one site never kills the lane
        summary["error"] = f"{type(exc).__name__}: {exc}"[:200]
    summary["requests"] = host.requests
    if log:
        log(f"[careersite] {host_name}: listed={summary['listed']} known={summary['known']} "
            f"fetched={summary['fetched']} kept={summary['kept']} no_posting={summary['no_posting']} "
            f"excluded={summary['excluded']} errors={summary['errors']} requests={summary['requests']}"
            + (f" stopped={summary['stopped']}" if summary["stopped"] else "")
            + (f" BLOCKED: {summary['blocked']}" if summary["blocked"] else "")
            + (f" error: {summary['error']}" if summary["error"] else ""))
    return summary


def rotate_sites(sites: dict, cursor_path) -> tuple[list[tuple[str, dict]], int]:
    """The sites in crawl order for this run, starting where the last run stopped (the cursor
    file under the feed's data dir), and that start index. Hosts are sorted first so the order
    is stable when the table grows."""
    items = sorted(sites.items())
    if not items:
        return [], 0
    start = 0
    if cursor_path:
        try:
            start = int(json.loads(Path(cursor_path).read_text())["next"]) % len(items)
        except (OSError, ValueError, KeyError, TypeError):
            start = 0
    return items[start:] + items[:start], start


def next_cursor(order: list[str], start: int, summaries: list[dict]) -> int:
    """Where the next run starts: the first site (in this run's order) that never started or
    was cut short by the page budget or the deadline. If every site finished, move on by one so
    the head of the list does not always go first."""
    total = len(order)
    if not total:
        return 0
    by_host = {s["host"]: s for s in summaries}
    for i, host in enumerate(order):
        s = by_host.get(host)
        if s is None or s.get("stopped") in ("budget", "deadline"):
            return (start + i) % total
    return (start + 1) % total


def crawl_sites(db_path, sites: dict | None = None, *, minutes: float | None = None,
                workers: int | None = None, page_budget: int | None = None, cursor_path=None,
                text_fetch=fetch_page, sleep=time.sleep, clock=time.monotonic, log=None) -> list[dict]:
    """Crawl the sites in `sites` (default SITES) with `workers` threads, one host per thread at
    a time (each host still sees one request at a time), stopping at `minutes`. Job pages read
    across all sites are capped by `page_budget` (env CAREERSITE_PAGE_BUDGET; 0 = no cap). The
    order rotates: it starts at the cursor in `cursor_path` and the cursor is saved for the next
    run, so a capped run never starves the end of the table. Each thread opens its own Watchlist
    connection (SQLite WAL). Returns the per-site summaries."""
    from .watchlist import Watchlist
    sites = SITES if sites is None else sites
    minutes = float(os.environ.get("CAREERSITE_MINUTES", DEFAULT_MINUTES)) if minutes is None else minutes
    workers = _env_int("CAREERSITE_WORKERS", DEFAULT_WORKERS) if workers is None else workers
    if page_budget is None:
        page_budget = _env_int("CAREERSITE_PAGE_BUDGET", DEFAULT_PAGE_BUDGET)
    budget = PageBudget(page_budget if page_budget and page_budget > 0 else None)
    deadline = clock() + minutes * 60 if minutes else None
    ordered, start = rotate_sites(sites, cursor_path)
    order = [h for h, _ in ordered]
    items = list(ordered)
    out: list[dict] = []
    lock = threading.Lock()

    def worker():
        store = Watchlist(db_path)
        try:
            while True:
                with lock:
                    if not items or (deadline is not None and clock() >= deadline):
                        return
                    host_name, cfg = items.pop(0)
                s = crawl_site(store, host_name, cfg, text_fetch=text_fetch, sleep=sleep, clock=clock,
                               deadline=deadline, budget=budget, log=log)
                with lock:
                    out.append(s)
        finally:
            store.close()

    threads = [threading.Thread(target=worker, name=f"careersite-{i}", daemon=True)
               for i in range(max(1, min(workers, len(items))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if cursor_path and order:
        nxt = next_cursor(order, start, out)
        try:
            Path(cursor_path).write_text(json.dumps({"next": nxt}) + "\n")
        except OSError as exc:
            if log:
                log(f"[careersite] could not save the cursor {cursor_path}: {exc}")
        if log:
            log(f"[careersite] sites {len(out)}/{len(order)} from #{start}; next run starts at #{nxt}"
                + (f"; page budget left {budget.left}" if budget.left is not None else ""))
    return out
