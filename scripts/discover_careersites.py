#!/usr/bin/env python3
"""Find and verify the career sites of the biggest H-1B sponsors for the career-site lane
(sourcing/careersite.py, config/careersites.json). A MAINTAINER script: it hits the live
internet, so run it by hand now and then, read what it found, and commit the table it writes.

For the top sponsors (H-1B approvals, recent years weighted; seed/sponsors_seed.csv.gz) that
are not already on the watchlist (seed/watchlist_seed.csv) or in the site table, it guesses a
few careers hosts from the brand name (careers.<brand>.com, jobs.<brand>.com,
<brand>.eightfold.ai, apply.careers.<brand>.com, www.<brand>careers.com) and, per host:

  1. reads robots.txt FIRST (a 404 means "no rules"; an HTML page, a redirect off the host or
     a file whose text forbids automated collection means "leave it alone"),
  2. names the platform from what robots.txt and the sitemap show (Eightfold, Phenom, Workday,
     iCIMS, SuccessFactors, Oracle, Avature, Radancy, other),
  3. reads the job sitemap robots.txt names (or the platform's usual one) if robots.txt allows
     it, and counts the job-page URLs it lists,
  4. reads ONE job page (robots-allowed) and checks it carries a schema.org JobPosting with a
     title.

A host that passes all four is VERIFIED and, with --write, is added to config/careersites.json
(never overwriting an entry already there). Workday tenants are reported, not added: Workday
boards belong on the watchlist (sourcing/workday.py reads them through their JSON API).

Polite, no exceptions: our honest user agent (sourcing.careersite.UA), one request at a time
per registrable domain with at least --gap seconds between them (robots.txt Crawl-delay raises
it), short timeouts, and a hard stop for a host on 401 / 403 / 429 or a bot-challenge page --
no retry, no other identity. The hosts docs/feed.md records as blocked are never probed.

Resumable: every finished employer and host is written to a checkpoint (default
data/careersite_discovery.json, git-ignored), so stop it any time (--minutes, Ctrl-C) and rerun
to continue; nothing is probed twice.

    python scripts/discover_careersites.py --limit 600 --minutes 60 [--workers 8] [--write] [--ipv4]
    python scripts/discover_careersites.py --report          # what the checkpoint holds
    python scripts/discover_careersites.py --start 600 --limit 300 --minutes 30   # the next ranks
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing.careersite import (  # noqa: E402
    SITES_FILE, UA, Robots, extract_jobposting, load_sites, looks_challenged, parse_sitemap,
    site_entry_ok,
)
from sourcing.sponsors import normalize_employer  # noqa: E402

CHECKPOINT = ROOT / "data" / "careersite_discovery.json"
TIMEOUT = 12
MAX_BYTES = 40_000_000           # one sitemap file; the biggest job sitemaps are a few MB
MAX_CHILDREN = 12                # child sitemaps read per index while counting
MIN_JOBS = 10                    # fewer job URLs than this is not worth a table entry
MAX_HOSTS_PER_EMPLOYER = 12

# Recorded in docs/feed.md as blocked or unusable (WAF / Akamai 403 on robots.txt, Avature empty
# body, robots.txt forbids collection, no sitemap or JSON-LD), plus brands already served by
# another lane (the four original sites, Amazon's adapter, Walmart's __NEXT_DATA__ site).
SKIP_BRANDS = {
    "jpmorgan", "jpmorganchase", "chase", "oracle", "ibm", "deloitte", "infosys", "tesla", "meta",
    "metaplatforms", "facebook", "apple", "google", "alphabet",
    "amazon", "microsoft", "qualcomm", "capitalone", "cisco", "walmart",
}

# Name words that say nothing about the brand when they END a filing name.
_TRAILING = {
    "services", "service", "svcs", "associates", "technologies", "technology", "solutions",
    "group", "holdings", "holding", "united", "states", "america", "americas", "north",
    "international", "global", "enterprises", "consulting", "com", "of", "and", "the", "usa",
    "us", "na", "inc", "llc", "corp", "corporation", "co", "company", "ltd", "limited", "lp", "llp",
}
# Leading words too generic to stand alone as a host (careers.bank.com is not Bank of America).
_GENERIC_LEAD = {
    "bank", "first", "general", "american", "united", "national", "global", "the", "new", "data",
    "advanced", "applied", "international", "systems", "health", "medical", "university", "info",
    "tech", "digital", "smart", "best", "next", "one", "north", "south", "east", "west", "us",
}

PLATFORMS_ADDED = ("eightfold", "phenom", "radancy", "successfactors", "icims", "other")


# ----------------------------------------------------------------------------------------- #
# names -> hosts
# ----------------------------------------------------------------------------------------- #

def brand_tokens(display_name: str) -> list[str]:
    toks = normalize_employer(display_name).split()
    while toks and toks[-1] in _TRAILING:
        if len(toks) >= 2 and toks[-2] in ("of", "and"):
            break                                # "Bank of America" keeps its America
        toks.pop()
    return toks


def brand_slugs(display_name: str) -> list[str]:
    """Host labels worth trying for an employer, strongest first: the joined first two words,
    the lead word (if distinctive), the whole brand joined (up to four words)."""
    toks = brand_tokens(display_name)
    if not toks:
        return []
    out: list[str] = []
    if len(toks) >= 2 and toks[1] not in ("of", "and"):
        out.append(toks[0] + toks[1])
    if len(toks[0]) >= 3 and toks[0] not in _GENERIC_LEAD:
        out.append(toks[0])
    if 2 < len(toks) <= 4:
        out.append("".join(toks))
    seen: set[str] = set()
    return [s for s in out if re.fullmatch(r"[a-z0-9]{2,40}", s) and not (s in seen or seen.add(s))]


def candidate_hosts(slugs: list[str]) -> list[str]:
    hosts: list[str] = []
    for s in slugs:
        hosts += [f"careers.{s}.com", f"jobs.{s}.com", f"{s}.eightfold.ai",
                  f"apply.careers.{s}.com", f"www.{s}careers.com"]
    return list(dict.fromkeys(hosts))[:MAX_HOSTS_PER_EMPLOYER]


def registrable(host: str) -> str:
    """The politeness key: the last two labels (every *.eightfold.ai tenant shares one gate)."""
    return ".".join(host.lower().split(".")[-2:])


# ----------------------------------------------------------------------------------------- #
# classification (pure: fixtures in tests/test_discover_careersites.py)
# ----------------------------------------------------------------------------------------- #

_AUTOMATION = re.compile(r"automated|automatic|crawl|scrap|robot|spider|harvest|bots?\b", re.I)
_FORBIDDEN = re.compile(r"prohibit|forbid|not (be )?permitted|not allowed|written permission|"
                        r"express permission|without permission", re.I)


def robots_forbids_collection(text: str) -> bool:
    """robots.txt whose comments forbid automated collection (Meta's file does). Its rules may
    allow the path, but the owner has said no in words; we listen to the words."""
    comments = [line.split("#", 1)[1] for line in text.splitlines() if "#" in line]
    joined = " ".join(c.strip() for c in comments)
    return any(_AUTOMATION.search(c) and _FORBIDDEN.search(c) for c in [*comments, joined])


def looks_like_robots(text: str) -> bool:
    head = text.lstrip()[:400].lower()
    return not (head.startswith("<") or "<html" in head or "<!doctype" in head)


def classify(host: str, robots_text: str = "", urls=()) -> str:
    """The platform behind a careers host, from the host name, robots.txt and sitemap URLs."""
    h = host.lower()
    blob = (robots_text + "\n" + "\n".join(urls[:200] if isinstance(urls, list) else list(urls)[:200])).lower()
    if h.endswith("myworkdayjobs.com") or "myworkdayjobs.com" in blob:
        return "workday"
    if h.endswith("eightfold.ai") or "eightfold" in blob or "/api/pcsx" in blob or re.search(r"/careers/job/\d+[^\n]*[?&]domain=", blob):
        return "eightfold"
    if "phenom" in blob or "px-widgets" in blob or re.search(r"/[a-z]{2,6}/[a-z]{2}/job/[a-z0-9_-]*\d", blob):
        return "phenom"
    if h.endswith("icims.com") or "icims" in blob:
        return "icims"
    if "successfactors" in blob or "jobs2web" in blob or "/rmk" in blob or "career_ns" in blob:
        return "successfactors"
    if "oraclecloud" in blob or "candidateexperience" in blob:
        return "oracle"
    if "avature" in blob:
        return "avature"
    if ("jobs_sitemap" in blob or "radancy" in blob or "/search-jobs" in blob
            or re.search(r"/job/[^/\s]+/[^/\s]+/\d+/\d+", blob)):
        return "radancy"
    return "other"


_JOB_SEG = re.compile(r"/(?:job|jobs|position|positions|requisition|requisitions|vacancy|opening|"
                      r"openings|posting|postings)/", re.I)


def is_job_url(url: str, host: str) -> bool:
    """A job-detail page URL on `host`: a /job/ (or similar) segment followed by an id."""
    p = urlsplit(url)
    if p.scheme != "https" or p.netloc.lower() != host:
        return False
    m = _JOB_SEG.search(p.path)
    return bool(m and re.search(r"\d{3,}", p.path[m.end():]))


_LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)


def job_prefix(url: str) -> str:
    """The path up to and including the job segment: /global/en/job/, /careers/job/, /job/.
    A title slug BEFORE the job segment (/de/athlete-iii/job/R-1, Attrax-style sites) varies per
    job, so it becomes `*`; a locale or a fixed word stays literal."""
    p = urlsplit(url).path
    m = _JOB_SEG.search(p)
    if not m:
        return ""
    segs = p[:m.end()].split("/")
    return "/".join("*" if (s and "-" in s and not _LOCALE.match(s)) else s for s in segs)


def job_url_pattern(host: str, platform: str, sample_url: str) -> str:
    prefix = job_prefix(sample_url)
    pat = "^https://" + re.escape(host) + "[^/]+".join(re.escape(part) for part in prefix.split("*"))
    if platform == "eightfold" and prefix.endswith("/careers/job/"):
        pat += r"\d+"
    return pat


def default_sitemaps(host: str, platform: str, robots: Robots) -> list[str]:
    """robots.txt's Sitemap lines on this host, else where the platform usually keeps it."""
    own = [u for u in robots.sitemaps if urlsplit(u).netloc.lower() == host and urlsplit(u).scheme == "https"]
    if own:
        return own
    if platform == "eightfold":
        dom = ".".join(host.split(".")[-2:])
        return [f"https://{host}/careers/sitemap.xml?domain={dom}"]
    if platform == "phenom":
        return [f"https://{host}/global/en/sitemap_index.xml", f"https://{host}/us/en/sitemap_index.xml"]
    return [f"https://{host}/sitemap.xml"]


# ----------------------------------------------------------------------------------------- #
# polite fetching
# ----------------------------------------------------------------------------------------- #

class Blocked(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url: str, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    """(status, body, Location) without following redirects; network errors raise OSError."""
    url = quote(url, safe="/:?=&%#+@!$,;'()*[]~")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml,text/xml,*/*"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES)
            status = resp.status
            loc = ""
    except urllib.error.HTTPError as exc:
        return exc.code, "", exc.headers.get("Location", "") if exc.headers else ""
    except http.client.HTTPException as exc:          # a cut-off chunked body (IncompleteRead):
        raise OSError(f"broken response: {exc!r:.80}") from exc   # a network error, not a crash
    if body[:2] == b"\x1f\x8b":
        import gzip
        body = gzip.decompress(body)
    return status, body.decode("utf-8", "replace"), loc


def prefer_ipv4() -> None:
    """Resolve hosts to IPv4 only. For a network whose IPv6 route is broken (every connect to a
    v6 address times out before Python falls back, ~10 s per address). Not an identity change:
    same UA, same host, same request."""
    import socket
    orig = socket.getaddrinfo

    def v4(host, port, family=0, *a, **k):
        return orig(host, port, socket.AF_INET, *a, **k)
    socket.getaddrinfo = v4


class Gate:
    """One request at a time per registrable domain, at least `gap` seconds apart."""

    def __init__(self, gap: float = 1.0, sleep=time.sleep, clock=time.monotonic):
        self.gap, self._sleep, self._clock = gap, sleep, clock
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()
        self.requests = 0

    def run(self, host: str, fn, gap: float | None = None):
        key = registrable(host)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            last = self._last.get(key)
            if last is not None:
                wait = max(self.gap, gap or 0) - (self._clock() - last)
                if wait > 0:
                    self._sleep(wait)
            try:
                return fn()
            finally:
                self._last[key] = self._clock()
                with self._guard:
                    self.requests += 1


class Prober:
    """Verifies one careers host. `fetch(url) -> (status, body, location)` is injectable."""

    def __init__(self, gate: Gate, fetch=http_get, log=None):
        self.gate, self.fetch, self.log = gate, fetch, log or (lambda *_: None)

    def _get(self, host: str, url: str, delay: float = 0.0) -> tuple[int, str, str]:
        status, body, loc = self.gate.run(host, lambda: self.fetch(url), gap=delay)
        if status in (401, 403, 429):
            raise Blocked(f"answered {status} for {url}")
        if status == 200 and looks_challenged(body):
            raise Blocked(f"bot challenge at {url}")
        return status, body, loc

    def probe(self, host: str) -> dict:
        res = {"host": host, "status": "absent", "reason": "", "platform": "", "redirect": ""}
        try:
            return self._probe(host, res)
        except Blocked as exc:
            return {**res, "status": "blocked", "reason": str(exc)}
        except (OSError, ValueError) as exc:          # DNS, refused, timeout, TLS: no such site
            return {**res, "status": "absent", "reason": f"{type(exc).__name__}: {exc}"[:160]}

    def _probe(self, host: str, res: dict) -> dict:
        url = f"https://{host}/robots.txt"
        status, body, loc = self._get(host, url)
        if 300 <= status < 400:
            target = (urlsplit(urljoin(url, loc)).hostname or "").lower()
            if target and target != host:
                return {**res, "status": "redirect", "redirect": target,
                        "reason": f"robots.txt redirects to {target}"}
            status, body, loc = self._get(host, urljoin(url, loc))
        if status == 404 or status == 410:
            text = ""
        elif status == 200:
            if not looks_like_robots(body):
                return {**res, "status": "rejected", "reason": "robots.txt answers an HTML page"}
            text = body
        else:
            return {**res, "status": "rejected", "reason": f"robots.txt answered {status}"}
        if robots_forbids_collection(text):
            return {**res, "status": "rejected", "reason": "robots.txt text forbids automated collection"}
        robots = Robots(text)
        platform = classify(host, text)
        res["platform"] = platform
        if platform == "workday":
            return {**res, "status": "workday", "reason": "Workday tenant: belongs on the watchlist"}
        sitemaps = default_sitemaps(host, platform, robots)
        allowed = [s for s in sitemaps if robots.can_fetch(s)]
        if not allowed:
            return {**res, "status": "rejected", "reason": "robots.txt disallows the sitemap"}
        delay = robots.delay
        # sitemap -> job URLs, grouped by top-level sitemap
        per_top: dict[str, dict[str, str]] = {}
        seen_urls: list[str] = []
        for top in allowed:
            found: dict[str, str] = {}
            queue, children = [top], 0
            while queue:
                sm = queue.pop(0)
                if not robots.can_fetch(sm) or urlsplit(sm).netloc.lower() != host:
                    continue
                st, xml, _ = self._get(host, sm, delay)
                if st != 200:
                    continue
                kind, entries = parse_sitemap(xml)
                if kind == "index":
                    kids = [loc for loc, _ in entries]
                    kids.sort(key=lambda u: (0 if re.search(r"job|career|position|requisition", u, re.I) else 1))
                    room = MAX_CHILDREN - children
                    queue.extend(kids[:max(0, room)])
                    children += min(len(kids), max(0, room))
                    continue
                seen_urls.extend(loc for loc, _ in entries[:50])
                for loc, lastmod in entries:
                    if is_job_url(loc, host):
                        found.setdefault(loc, lastmod)
            if found:
                per_top[top] = found
        if not per_top:
            return {**res, "status": "rejected", "reason": "sitemap lists no job pages"}
        # the job-URL prefix most of the listed pages share (one locale of a Phenom site, etc.)
        by_prefix: Counter = Counter()
        for found in per_top.values():
            by_prefix.update(job_prefix(u) for u in found)
        top_count = by_prefix.most_common(1)[0][1]
        # one locale of a multi-locale site: English when it lists a fair share of the jobs
        prefix = max(by_prefix, key=lambda p: (bool(re.search(r"/en(-[a-z]{2})?/", p, re.I))
                                               and by_prefix[p] * 2 >= top_count, by_prefix[p]))
        platform = classify(host, text, seen_urls) if platform == "other" else platform
        res["platform"] = platform
        if platform == "workday":
            return {**res, "status": "workday", "reason": "Workday tenant: belongs on the watchlist"}
        if platform in ("oracle", "avature"):
            return {**res, "status": "rejected", "reason": f"{platform}: client-rendered, no usable job pages"}
        tops = [t for t, f in per_top.items() if any(job_prefix(u) == prefix for u in f)]
        jobs = {u: m for t in tops for u, m in per_top[t].items() if job_prefix(u) == prefix}
        res["jobs_seen"] = len(jobs)
        if len(jobs) < MIN_JOBS:
            return {**res, "status": "rejected", "reason": f"only {len(jobs)} job URLs in the sitemap"}
        sample = [u for u, _ in sorted(jobs.items(), key=lambda kv: kv[1], reverse=True) if robots.can_fetch(u)][:2]
        if not sample:
            return {**res, "status": "rejected", "reason": "robots.txt disallows the job pages"}
        title = ""
        for u in sample:
            st, page, _ = self._get(host, u, delay)
            posting = extract_jobposting(page) if st == 200 else None
            title = str((posting or {}).get("title") or "").strip()
            if title:
                break
        if not title:
            return {**res, "status": "rejected", "reason": "job page carries no JobPosting JSON-LD with a title",
                    "sample": sample[0]}
        return {**res, "status": "verified", "sitemaps": tops, "job_url": job_url_pattern(host, platform, sample[0]),
                "sample": sample[0], "sample_title": title[:120]}


# ----------------------------------------------------------------------------------------- #
# the run
# ----------------------------------------------------------------------------------------- #

class Checkpoint:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self.employers: dict[str, dict] = data.get("employers", {})
        self.hosts: dict[str, dict] = data.get("hosts", {})

    def set_host(self, host: str, res: dict) -> None:
        with self._lock:
            self.hosts[host] = res

    def set_employer(self, key: str, rec: dict) -> None:
        with self._lock:
            self.employers[key] = rec

    def save(self) -> None:
        with self._lock:                          # writers hold the same lock: a consistent snapshot
            text = json.dumps({"employers": self.employers, "hosts": self.hosts}, indent=1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(text)
            tmp.replace(self.path)


def company_name(display_name: str) -> str:
    toks = display_name.split()
    while toks and toks[-1].lower().strip(".,") in _TRAILING:
        toks.pop()
    return " ".join(toks) or display_name


def covered_slugs(watch_rows, sites: dict) -> set[str]:
    out = set(SKIP_BRANDS)
    for r in watch_rows:
        toks = brand_tokens(r.get("company", ""))
        if toks:
            out.add("".join(toks))
    for cfg in sites.values():
        toks = brand_tokens(cfg.get("company", ""))
        if toks:
            out.add("".join(toks))
    return out


def pick_employers(rows, watch_rows, sites: dict, limit: int) -> list[dict]:
    """The top `limit` sponsors not already covered, with the hosts to try for each."""
    from sourcing.growth import rank_sponsors, watched_norms
    from sourcing.quality import employer_is_clearance_heavy
    covered = covered_slugs(watch_rows, sites)
    out: list[dict] = []
    for r in rank_sponsors(rows, skip_norms=watched_norms(watch_rows)):
        name = r["display_name"]
        slugs = brand_slugs(name)
        if not slugs or "".join(brand_tokens(name)) in covered or any(s in SKIP_BRANDS for s in slugs):
            continue
        if employer_is_clearance_heavy(name):
            continue
        out.append({"key": r["norm_name"], "name": name, "approvals": r["h1b_approvals"],
                    "hosts": candidate_hosts(slugs)})
        if len(out) >= limit:
            break
    return out


def discover(employers, cp: Checkpoint, prober: Prober, *, deadline: float | None = None,
             workers: int = 8, log=print, clock=time.monotonic) -> dict:
    """Probe each employer's hosts until one verifies; skip hosts already in the checkpoint."""
    host_locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()
    stats = Counter()

    def probe_host(host: str) -> dict:
        with guard:
            lock = host_locks.setdefault(host, threading.Lock())
        with lock:
            if host in cp.hosts:
                return cp.hosts[host]
            res = prober.probe(host)
            res["checked"] = date.today().isoformat()
            cp.set_host(host, res)
            return res

    def one(emp: dict) -> None:
        if emp["key"] in cp.employers:
            return
        if deadline is not None and clock() >= deadline:
            return
        queue, tried, found = list(emp["hosts"]), [], ""
        while queue:
            if deadline is not None and clock() >= deadline:
                return                                   # unfinished: the next run continues
            host = queue.pop(0)
            if host in tried:
                continue
            tried.append(host)
            res = probe_host(host)
            if res["status"] == "redirect" and res.get("redirect") and len(tried) < MAX_HOSTS_PER_EMPLOYER:
                tgt = res["redirect"]
                slugs = brand_slugs(emp["name"])
                if any(s in tgt for s in slugs) or tgt.endswith((".eightfold.ai", ".myworkdayjobs.com")):
                    queue.insert(0, tgt)                 # a careers host that moved: follow once
            if res["status"] == "verified":
                found = host
                break
        cp.set_employer(emp["key"], {"name": emp["name"], "approvals": emp["approvals"],
                                     "tried": tried, "verified": found})
        cp.save()
        with guard:
            stats["employers"] += 1
            if found:
                stats["verified"] += 1
                r = cp.hosts[found]
                log(f"  VERIFIED {emp['name']} -> {found} [{r['platform']}] {r.get('jobs_seen')} jobs, "
                    f"e.g. {r.get('sample_title')!r}")
            elif stats["employers"] % 25 == 0:
                log(f"  ... {stats['employers']} employers probed, {stats['verified']} verified")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, employers))
    cp.save()
    return dict(stats)


def table_entries(cp: Checkpoint, existing: dict) -> dict[str, dict]:
    """Verified, addable hosts from the checkpoint as site-table entries (new hosts only)."""
    best: dict[str, dict] = {}
    for key, emp in cp.employers.items():
        host = emp.get("verified")
        if not host or host in existing:
            continue
        r = cp.hosts.get(host) or {}
        if r.get("status") != "verified" or r.get("platform") not in PLATFORMS_ADDED:
            continue
        entry = {"company": company_name(emp["name"]), "platform": r["platform"],
                 "sitemaps": r["sitemaps"], "job_url": r["job_url"],
                 "verified": r.get("checked") or date.today().isoformat(),
                 "jobs_seen": r.get("jobs_seen", 0), "sponsor": emp["name"],
                 "h1b_approvals": emp.get("approvals", 0)}
        if site_entry_ok(host, entry) and (host not in best or entry["h1b_approvals"] > best[host]["h1b_approvals"]):
            best[host] = entry
    return best


def write_table(entries: dict[str, dict], path: Path = SITES_FILE) -> int:
    doc = json.loads(Path(path).read_text())
    sites = doc.setdefault("sites", {})
    added = 0
    for host, entry in sorted(entries.items()):
        if host not in sites:
            sites[host] = entry
            added += 1
    Path(path).write_text(json.dumps(doc, indent=2) + "\n")
    return added


def report(cp: Checkpoint, log=print) -> None:
    st = Counter(r["status"] for r in cp.hosts.values())
    log(f"employers probed: {len(cp.employers)}; hosts probed: {len(cp.hosts)} {dict(st)}")
    for host, r in sorted(cp.hosts.items()):
        if r["status"] in ("verified", "rejected", "blocked", "workday"):
            log(f"  {r['status']:9} {host:45} {r.get('platform', ''):14} "
                f"{r.get('jobs_seen', '')!s:>6}  {r.get('reason', '') or r.get('sample_title', '')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=600, help="top sponsors to consider (default 600)")
    ap.add_argument("--start", type=int, default=0,
                    help="skip this many uncovered sponsors first (a fresh checkpoint continues past ranks "
                         "an earlier run already probed, e.g. --start 600 after the 2026-10-12 run)")
    ap.add_argument("--minutes", type=float, default=60.0, help="stop after this long (0 = no cap)")
    ap.add_argument("--workers", type=int, default=8, help="employers probed at once (default 8)")
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between requests to one domain (min 1)")
    ap.add_argument("--checkpoint", default=str(CHECKPOINT))
    ap.add_argument("--write", action="store_true", help="add verified sites to config/careersites.json")
    ap.add_argument("--report", action="store_true", help="print what the checkpoint holds and exit")
    ap.add_argument("--ipv4", action="store_true", help="resolve IPv4 only (networks with broken IPv6)")
    a = ap.parse_args(argv)
    if a.ipv4:
        prefer_ipv4()
    cp = Checkpoint(Path(a.checkpoint))
    if a.report:
        report(cp)
        return 0
    from sourcing.growth import load_sponsor_seed, read_watchlist_seed
    sites = load_sites()
    employers = pick_employers(load_sponsor_seed(), read_watchlist_seed(), sites, a.start + a.limit)[a.start:]
    todo = [e for e in employers if e["key"] not in cp.employers]
    print(f"{len(employers)} uncovered top sponsors; {len(todo)} still to probe "
          f"(checkpoint {a.checkpoint})")
    gate = Gate(gap=max(1.0, a.gap))
    deadline = time.monotonic() + a.minutes * 60 if a.minutes else None
    t0 = time.time()
    stats = discover(todo, cp, Prober(gate), deadline=deadline, workers=a.workers)
    print(f"done in {time.time() - t0:.0f}s: {stats}, {gate.requests} requests")
    entries = table_entries(cp, sites)
    print(f"{len(entries)} verified sites not yet in the table: "
          + ", ".join(f"{h} ({e['platform']}, {e['jobs_seen']})" for h, e in sorted(entries.items())))
    if a.write and entries:
        print(f"added {write_table(entries)} sites to {SITES_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
