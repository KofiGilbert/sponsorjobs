"""The freehire SEARCH-BY-SPONSOR pass of the feed build (GREEN lane, CLAUDE.md §6).

freehire.me (MIT project strelov1/freehire) aggregates over a million postings behind a keyless,
documented API (https://freehire.me/docs/api). The feed build already pulls its newest rows whose
posting STATES sponsorship (service.refresh_watchlist, step 3b). Most postings say nothing either
way, though, and any one freehire query only reaches its newest ~10,000 rows (offset + limit may
not pass 10,000). Research on 2026-10-18 found about 9% of fresh US freehire rows sit at employers
with real H-1B approvals; those rows only reach the board if we ask for them by EMPLOYER.

This pass does that, politely and in small steps:

1. Ranks the H-1B employers from the committed sponsor seed by approvals (filers still active in
   the newest years only) and drops the ones our own crawl already covers (a watched board or a
   career site), because those rows come straight from the employer.
2. Walks that list from a cursor saved in the data dir, so each run picks up where the last one
   stopped and the whole list is reached over many runs.
3. For each employer, finds its freehire company slugs with ONE company search (GET /companies,
   the cache keeps the answer for SLUG_CACHE_DAYS), keeping only names that really are that
   employer (names_match below; a wrong employer would put a false sponsor badge on a job).
4. Asks the documented agent search for the US postings of up to SLUGS_PER_REQUEST slugs at a
   time (company_slug is repeatable and ORed), newest first, within the recency window.
5. Keeps a row only when it is US, accessible to a visa holder (no citizenship / clearance bar),
   its ad does not say no, and it carries an employer sponsor badge (quality.is_sponsor_relevant
   after SponsorDB tagging); then drops rows we already hold through another feed (dedup.py).

Every request counts against one budget per run (env FREEHIRE_SPONSOR_PASS_REQUESTS, default 200).
The API publishes its limits in X-RateLimit-* headers: the pass slows down when the remaining
budget runs low, honours Retry-After on a 429, and gives up for the run on a second 429 or a run
of failures. It sends the same identifying User-Agent as the rest of the crawl, and short timeouts.
Rows keep source "freehire", so cards credit freehire.me exactly as the sponsor slice's rows do.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from .ats import _UA, _freehire_row
from .dedup import DedupIndex
from .quality import is_intl_student_accessible, is_sponsor_relevant
from .sponsors import _ALIASES, _BRAND_PREFIXES, looks_us, normalize_employer

API_BASE = "https://freehire.me/api/v1"
CURSOR_FILE = "freehire_sponsor_cursor.json"     # under the feed's --data dir
SLUG_CACHE_FILE = "freehire_sponsor_slugs.json"
DEFAULT_REQUESTS = 200          # env FREEHIRE_SPONSOR_PASS_REQUESTS (0 switches the pass off)
DEFAULT_EMPLOYERS = 4000        # env FREEHIRE_SPONSOR_PASS_EMPLOYERS: how deep the ranked list goes
ACTIVE_YEARS = 2                # employers whose last H-1B filing is older than newest-2 are skipped
SLUGS_PER_REQUEST = 10          # company_slug values ORed into one search
PAGE = 100                      # the API's maximum page size
MAX_DEPTH = 10_000              # offset + limit may not pass this (a 400 otherwise)
SLUG_CACHE_DAYS = 30            # re-resolve an employer's slugs after this long
COMPANY_SEARCH_LIMIT = 100
TIMEOUT = 15                    # seconds per request
LOW_REMAINING = 10              # X-RateLimit-Remaining at or below this -> wait for the reset
MAX_WAIT = 60.0                 # never sleep longer than this on one header
MAX_FAILURES = 3                # consecutive network / 5xx failures before the run gives up

# Spellings H-1B filings abbreviate that the employer's own name spells out.
_ABBREV = {"svcs": "services", "svc": "service", "intl": "international", "natl": "national",
           "mgmt": "management", "assoc": "associates", "tech": "technologies"}
# First words too common to search on alone ("American ..." would list a hundred companies).
_GENERIC_FIRST = {"american", "general", "national", "united", "first", "global", "international",
                  "new", "north", "south", "east", "west", "advanced", "applied", "data", "digital",
                  "health", "bank", "city", "state", "university", "us", "the", "integrated",
                  "information", "systems", "software", "tech", "technology", "technologies"}


# -- company-name matching ------------------------------------------------------------------ #

def name_tokens(name: str) -> list[str]:
    """An employer name as comparable words: sponsors.normalize_employer (lowercase, punctuation
    gone, initials joined, Inc / LLC / Corp ... stripped), then 'and' dropped (filings spell
    'Ernst & Young' as 'Ernst Young'), common filing abbreviations spelled out, and the curated
    brand aliases applied ('Meta' -> 'meta platforms')."""
    norm = normalize_employer(name)
    norm = _ALIASES.get(norm, norm)
    return [_ABBREV.get(t, t) for t in norm.split() if t != "and"]


def names_match(target: str, candidate: str) -> bool:
    """Is `candidate` (a freehire company name) the same employer as `target` (a sponsor record)?

    Deliberately conservative, mirroring SponsorDB's own matching: a wrong match would carry one
    employer's sponsor history onto another's jobs. True when the two are the same words, the
    same letters ('JPMorgan Chase' / 'JP Morgan Chase'), one is the other plus more whole words
    and the shorter has at least two words ('Wells Fargo' / 'Wells Fargo Bank'), or both start
    with a curated well-known brand ('Amazon' / 'Amazon Com Services'). A lone generic word never
    stretches to a longer name: 'Nowhere' does not match 'Nowhere Partners'."""
    a, b = name_tokens(target), name_tokens(candidate)
    if not a or not b:
        return False
    if a == b or "".join(a) == "".join(b):
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 2 and long_[:len(short)] == short:
        return True
    return a[0] == b[0] and a[0] in _BRAND_PREFIXES


def search_term(name: str) -> str:
    """What to send freehire's company search (a case-insensitive substring match) so the real
    employer is among the answers: its brand word for a curated brand, its first word when that
    word is distinctive, else its first two words. Matching the answers is names_match's job."""
    toks = name_tokens(name)
    if not toks:
        return ""
    if toks[0] in _BRAND_PREFIXES or len(toks) == 1:
        return toks[0]
    if len(toks[0]) >= 5 and toks[0] not in _GENERIC_FIRST:
        return toks[0]
    return " ".join(toks[:2])


# -- which employers ---------------------------------------------------------------------- #

def ranked_employers(seed_rows, covered_names=(), limit: int = DEFAULT_EMPLOYERS,
                     active_years: int = ACTIVE_YEARS) -> list[dict]:
    """The H-1B employers worth asking freehire about, most approvals first: one entry per
    normalized name (filing entities summed), still filing within `active_years` of the newest
    year in the data, and NOT covered by our own crawl (a name in `covered_names` that
    names_match accepts). Deterministic: ties by name."""
    agg: dict[str, dict] = {}
    for r in seed_rows or ():
        norm = r.get("norm_name") or normalize_employer(r.get("display_name", ""))
        if not norm:
            continue
        e = agg.setdefault(norm, {"norm": norm, "name": r.get("display_name") or norm,
                                  "approvals": 0, "last_fy": 0})
        e["approvals"] += int(r.get("h1b_approvals") or 0)
        e["last_fy"] = max(e["last_fy"], int(r.get("h1b_last_fy") or 0))
    newest = max((e["last_fy"] for e in agg.values()), default=0)
    covered = [c for c in {str(c or "").strip() for c in covered_names} if c]
    by_first: dict[str, list[str]] = {}
    for c in covered:
        toks = name_tokens(c)
        if toks:
            by_first.setdefault(toks[0], []).append(c)
    out = []
    for e in sorted(agg.values(), key=lambda e: (-e["approvals"], e["norm"])):
        if e["approvals"] <= 0 or (newest and e["last_fy"] < newest - active_years):
            continue
        toks = name_tokens(e["name"])
        if not toks or any(names_match(e["name"], c) for c in by_first.get(toks[0], ())):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def covered_company_names(store) -> list[str]:
    """Companies our own crawl already reads straight from the employer: every watched board
    (active or not: an inactive board is one we chose to stop, not a gap) and every career site."""
    names = [w.get("company", "") for w in store.companies(active_only=False)]
    try:
        from .careersite import SITES
        names += [cfg.get("company", "") for cfg in SITES.values()]
    except Exception:                                    # noqa: BLE001 - boards alone still help
        pass
    return names


# -- the cursor and the slug cache ------------------------------------------------------------ #

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def rotate(employers: list[dict], cursor_path: Path) -> tuple[list[dict], int]:
    """The employers starting at the saved cursor (remembered by NAME, so a seed update that
    shifts positions does not skip anyone; a name that left the list resumes at the next one
    after where it sorted, by rank)."""
    if not employers:
        return [], 0
    cur = _read_json(cursor_path, {})
    want = cur.get("next") if isinstance(cur, dict) else None
    start = 0
    if want:
        norms = [e["norm"] for e in employers]
        if want in norms:
            start = norms.index(want)
        else:
            rank = cur.get("approvals")
            if isinstance(rank, int):
                start = next((i for i, e in enumerate(employers) if e["approvals"] < rank), 0)
    return employers[start:] + employers[:start], start


def save_cursor(cursor_path: Path, nxt: dict | None) -> None:
    cursor_path.write_text(json.dumps(
        {"next": nxt["norm"], "approvals": nxt["approvals"]} if nxt else {}) + "\n")


# -- HTTP ----------------------------------------------------------------------------------- #

class RateLimited(Exception):
    """freehire refused twice in a row (429): stop for this run."""


def http_get(url: str, timeout: int = TIMEOUT):
    """GET a freehire JSON endpoint -> (status, headers, body). An HTTP error status comes back
    as a status, not an exception, so the caller can read Retry-After and the rate headers."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, dict((exc.headers or {}).items()), None


def _header(headers: dict, name: str) -> float | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


class Client:
    """Counts every request against the run's budget and paces by the API's own headers."""

    def __init__(self, budget: int, *, http=http_get, sleep=time.sleep, base: str = API_BASE,
                 log=print):
        self.budget, self.used, self.http, self.sleep = budget, 0, http, sleep
        self.base, self.log = base.rstrip("/"), log
        self.rate_limited = 0
        self.failures = 0
        self.stopped = ""               # why the run stopped early, '' while it may go on

    def left(self) -> int:
        return self.budget - self.used if not self.stopped else 0

    def get(self, path: str, params: list[tuple[str, str]]):
        """One request (two on a 429 when the budget allows), or None. Never raises."""
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        for attempt in (1, 2):
            if self.left() <= 0:
                return None
            self.used += 1
            try:
                status, headers, body = self.http(url)
            except Exception as exc:                     # noqa: BLE001 - timeouts, resets
                status, headers, body = 0, {}, None
                self.log(f"[freehire-sponsors] {path}: {exc}")
            if status == 429:
                self.rate_limited += 1
                wait = min(MAX_WAIT, _header(headers, "Retry-After") or 5.0)
                if attempt == 2:
                    self.stopped = "rate limited twice"
                    return None
                self.log(f"[freehire-sponsors] 429: waiting {wait:.0f}s as asked")
                self.sleep(wait)
                continue
            if status != 200 or not isinstance(body, dict):
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self.stopped = f"{self.failures} failures in a row (last status {status})"
                return None
            self.failures = 0
            remaining = _header(headers, "X-RateLimit-Remaining")
            if remaining is not None and remaining <= LOW_REMAINING:
                self.sleep(min(MAX_WAIT, max(1.0, _header(headers, "X-RateLimit-Reset") or 1.0)))
            ignored = (body.get("meta") or {}).get("ignored_params")
            if ignored:
                # An unknown parameter is DROPPED, not refused: a renamed company_slug would turn
                # our narrow query into the whole catalogue. Stop rather than ingest that.
                self.stopped = f"the API ignored {ignored}"
                return None
            return body
        return None


# -- the pass ------------------------------------------------------------------------------- #

def resolve_slugs(client: Client, emp: dict) -> list[str] | None:
    """freehire company slugs that are this employer, or None when the request did not happen."""
    term = search_term(emp["name"])
    if not term:
        return []
    body = client.get("/companies", [("q", term), ("limit", str(COMPANY_SEARCH_LIMIT))])
    if body is None:
        return None
    return sorted({c["slug"] for c in body.get("data") or []
                   if c.get("slug") and int(c.get("job_count") or 0) > 0
                   and names_match(emp["name"], c.get("name", ""))})


def search_slugs(client: Client, slugs: list[str], days: str) -> tuple[list[dict], bool]:
    """Every fresh US posting of these company slugs, newest first -> (raw rows, finished)."""
    out: list[dict] = []
    offset = 0
    while offset + PAGE <= MAX_DEPTH:
        params = [("company_slug", s) for s in slugs] + [
            ("countries", "US"), ("posted_within_days", days), ("limit", str(PAGE)),
            ("offset", str(offset)), ("description_format", "text")]
        body = client.get("/agent/jobs/search", params)
        if body is None:
            return out, False
        rows = body.get("data") or []
        out.extend(rows)
        if len(rows) < PAGE:
            return out, True
        offset += PAGE
    return out, True


def keep_row(job: dict, tag) -> bool:
    """The feed's own rules, applied before a row is stored: US, accessible to a visa holder,
    an ad that does not say no, and a sponsor badge (or a posting that states sponsorship)."""
    from .adstance import UNKNOWN, ad_stance
    if not looks_us(job.get("location", "")) or not is_intl_student_accessible(job):
        return False
    st = ad_stance(job.get("jd_text") or "")
    if st["stance"] != UNKNOWN:
        job["ad_stance"] = st["stance"]
    tag([job])
    return bool(job.get("us")) and is_sponsor_relevant(job)


def run_pass(store, data_dir, tag, *, seed_rows=None, http=http_get, sleep=time.sleep,
             budget: int | None = None, base: str = API_BASE, log=print, today=None) -> dict:
    """Run one budgeted slice of the pass into `store` (a Watchlist). `tag(jobs)` is
    SponsorDB.tag_jobs. Returns a summary; never raises for a network problem."""
    if budget is None:
        try:
            budget = max(0, int(os.environ.get("FREEHIRE_SPONSOR_PASS_REQUESTS") or DEFAULT_REQUESTS))
        except ValueError:
            budget = DEFAULT_REQUESTS
    summary = {"requests": 0, "employers": 0, "employers_with_slugs": 0, "fetched": 0,
               "kept": 0, "new": 0, "already_held": 0, "duplicates": 0, "not_sponsor": 0,
               "rate_limited": 0, "stopped": "", "list_size": 0, "start": 0}
    if budget <= 0:
        summary["stopped"] = "off"
        return summary
    if seed_rows is None:
        from .growth import load_sponsor_seed
        seed_rows = load_sponsor_seed()
    try:
        limit = max(1, int(os.environ.get("FREEHIRE_SPONSOR_PASS_EMPLOYERS") or DEFAULT_EMPLOYERS))
    except ValueError:
        limit = DEFAULT_EMPLOYERS
    employers = ranked_employers(seed_rows, covered_company_names(store), limit=limit)
    data_dir = Path(data_dir)
    cursor_path, cache_path = data_dir / CURSOR_FILE, data_dir / SLUG_CACHE_FILE
    order, start = rotate(employers, cursor_path)
    summary.update(list_size=len(order), start=start)
    if not order:
        return summary
    cache = _read_json(cache_path, {})
    cache = cache if isinstance(cache, dict) else {}
    today = today or date.today()
    fresh_after = (today - timedelta(days=SLUG_CACHE_DAYS)).isoformat()
    days = os.environ.get("FREEHIRE_MAX_DAYS") or "14"
    client = Client(budget, http=http, sleep=sleep, base=base, log=log)
    jobs = store.list_jobs(include_dismissed=True)
    held = {j["source_id"] for j in jobs}
    index = DedupIndex(jobs)
    del jobs

    def ingest(raw: list[dict]) -> None:
        seen: set[str] = set()
        rows = []
        for j in raw:
            key = j.get("public_slug") or j.get("external_id")
            if key and key not in seen:
                seen.add(key)
                rows.append(_freehire_row(j))
        summary["fetched"] += len(rows)
        keep = []
        for r in rows:
            if keep_row(r, tag):
                keep.append(r)
            else:
                summary["not_sponsor"] += 1
        summary["already_held"] += sum(1 for r in keep if r["source_id"] in held)
        keep, dup = index.filter(keep)
        summary["duplicates"] += dup
        summary["kept"] += len(keep)
        if keep:
            n, _ = store.upsert_jobs(keep)
            summary["new"] += n
            held.update(r["source_id"] for r in keep)

    done = 0                       # employers fully searched, in `order`
    batch: list[tuple[int, list[str]]] = []      # (position in order, slugs) waiting for a search

    def flush() -> bool:
        nonlocal done
        slugs = sorted({s for _, ss in batch for s in ss})
        raw, finished = search_slugs(client, slugs, days)
        ingest(raw)
        if finished:
            done = batch[-1][0] + 1
            batch.clear()
        return finished

    for pos, emp in enumerate(order):
        if client.left() <= 0:
            break
        hit = cache.get(emp["norm"])
        if isinstance(hit, dict) and str(hit.get("at", "")) >= fresh_after:
            slugs = list(hit.get("slugs") or [])
        else:
            slugs = resolve_slugs(client, emp)
            if slugs is None:
                break
            cache[emp["norm"]] = {"slugs": slugs, "at": today.isoformat()}
        summary["employers"] += 1
        if not slugs:
            if not batch:
                done = pos + 1
            else:
                batch.append((pos, []))
            continue
        summary["employers_with_slugs"] += 1
        batch.append((pos, slugs))
        if sum(len(s) for _, s in batch) >= SLUGS_PER_REQUEST and not flush():
            break
    if batch and client.left() > 0:
        flush()                    # the last, partly filled batch
    # Resume at the first employer whose search did not finish (or the one after the last done).
    nxt = order[done % len(order)] if done < len(order) else order[0]
    save_cursor(cursor_path, nxt)
    try:
        cache_path.write_text(json.dumps(cache, sort_keys=True) + "\n")
    except OSError as exc:
        log(f"[freehire-sponsors] could not save the slug cache: {exc}")
    summary.update(requests=client.used, rate_limited=client.rate_limited, stopped=client.stopped,
                   next=nxt["name"])
    return summary
