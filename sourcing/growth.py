"""Grow the watchlist from the ranked H-1B sponsor list (GREEN lane — CLAUDE.md §6).

The committed watchlist (seed/watchlist_seed.csv) is what day one shows: every board on it
is a proven visa sponsor with a live public ATS feed. This module is the one path that
grows it, used by both the maintainer script (scripts/grow_watchlist.py, which commits the
result) and the weekly in-app discovery (sourcing/scheduler.py -> discover_sponsors), so
the two can never drift apart on WHICH employers get probed or HOW politely.

What it adds over sourcing/discover.py (which does the per-employer identity-checked probe):
  * RANKING — employers from the sponsor seed ordered by H-1B approvals, with recent filing
    years weighted higher, so a bounded crawl spends its budget on the sponsors most likely
    to be hiring now. Employers already on the watchlist are skipped by normalized name.
  * POLITENESS — one shared fetch with per-host spacing (slower for hosts known to be
    strict), a short timeout, and a real 429 back-off: Retry-After when given, else
    exponential; the host's spacing doubles on each 429 and relaxes again after a run of
    clean responses; a host that keeps answering 429 is put in a cooldown and simply not
    asked for a while. The hosts are the ATS vendors' public APIs; being a good client of
    them is what keeps this lane GREEN.
  * RESUMABILITY — every employer's outcome is appended to a JSONL checkpoint as soon as it
    is known, so a crawl can be stopped (the time cap, Ctrl-C, a crash) and rerun without
    re-probing anything. When a host was throttled during an employer's probe, the record
    says which ATSs were NOT probed, and the next run probes only those — a throttle costs
    a retry, never a false "no board here".
  * HONESTY — a board is only added when its live feed returned at least one job, and every
    rejected live board is reported with the reason (the identity check that turned it down).
"""

from __future__ import annotations

import csv
import json
import threading
import time
import urllib.error
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlparse

from .ats import fetch_json, valid_board_id_for
from .discover import DISCOVER_ATS, discover_for_company, slug_candidates
from .quality import employer_is_clearance_heavy
from .sponsors import normalize_employer

ROOT = Path(__file__).resolve().parents[1]
SPONSOR_SEED = ROOT / "seed" / "sponsors_seed.csv.gz"
WATCHLIST_SEED = ROOT / "seed" / "watchlist_seed.csv"

# A filing year older than the newest one in the data counts for this much less per year,
# so 200 approvals last year outrank 300 approvals that stopped four years ago.
RECENCY_DECAY = 0.6

# Politeness defaults. The whole crawl (all threads together) sends at most one request per
# HOST_DELAY seconds to each ATS host; every request gives up after PROBE_TIMEOUT seconds.
HOST_DELAY = 0.25
PROBE_TIMEOUT = 8
# The public ATS hosts discovery talks to, and the ATS each one serves.
ATS_HOSTS = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "api.smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",           # every {tenant}.wd{n}.myworkdayjobs.com, one lane
}


def host_lane(host: str) -> str:
    """The pacing lane a hostname belongs to. Each Workday tenant has its own hostname, but
    they are all one vendor behind one edge, so they share a single lane (and a single
    throttle state) instead of each guessed tenant getting its own unspaced burst."""
    host = (host or "").lower()
    for lane in ATS_HOSTS:
        if host == lane or host.endswith("." + lane):
            return lane
    return host
# Hosts observed to throttle a brisk crawl get a slower base spacing from the start.
# Workable's widget endpoint answered 429 even at 1 req/s from a single thread (2026-09-30),
# so it gets one request every few seconds — and, with MAX_WAIT below, it never holds up
# the other hosts: what it can't take this run is owed to the next.
# Workday answered a 12-request burst to one tenant without a 429 (2026-09-30); discovery fans
# out across hundreds of guessed tenants, so they share ONE lane at the default spacing rather
# than each tenant hostname getting its own unspaced burst.
HOST_DELAYS = {"apply.workable.com": 4.0, "myworkdayjobs.com": HOST_DELAY}
MAX_HOST_DELAY = 16.0         # the adaptive spacing never grows past this
# If a host's next free slot is further away than this, don't queue on it: record the ATS
# as not-yet-probed for this employer and move on. A slow or pausing host then gets exactly
# the pace it tolerates while the healthy hosts keep the crawl moving.
MAX_WAIT = 20.0
RELAX_AFTER = 100             # clean responses on a host before its spacing halves again
MAX_429_RETRIES = 3           # tries after the first 429 before the host is put in cooldown
MAX_BACKOFF = 120.0           # the longest a single 429 pauses a host before it is retried
COOLDOWN = 600.0              # a host that exhausted its retries is left alone this long
# A Retry-After longer than MAX_BACKOFF is not retried at all: the host has said when it wants
# to hear from us again, so it goes straight into a cooldown of that length (bounded, so a
# nonsense header cannot park a lane for a week). Workable's edge answers a 429 with
# Retry-After ~19h once an IP has used its daily budget (seen 2026-10-01).
MAX_RETRY_AFTER = 24 * 3600.0
# When every lane a run can use is unavailable, the crawl WAITS for the earliest one rather than
# burning through the employer list -- but never longer than this without a time budget telling
# it to (with a budget, it stops as soon as the wait would outlast the budget).
MAX_LANE_PAUSE = COOLDOWN
MAX_REQUEUE = 2               # times one employer is put back after a fully-throttled probe


# -- the ranked sponsor source ------------------------------------------------- #

def load_sponsor_seed(path: str | Path = SPONSOR_SEED) -> list[dict]:
    """The employers with at least one H-1B approval from the committed sponsor seed."""
    import gzip
    opener = gzip.open if str(path).endswith(".gz") else open
    out: list[dict] = []
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                approvals = int(r.get("h1b_approvals") or 0)
            except ValueError:
                continue
            if approvals <= 0:
                continue
            out.append({"norm_name": r.get("norm_name") or normalize_employer(r.get("display_name", "")),
                        "display_name": r.get("display_name", ""),
                        "h1b_approvals": approvals,
                        "h1b_last_fy": int(r.get("h1b_last_fy") or 0)})
    return out


def recency_weight(last_fy: int, newest_fy: int, decay: float = RECENCY_DECAY) -> float:
    """1.0 for an employer still filing in the newest year, decaying per year of silence."""
    if not last_fy or not newest_fy or last_fy >= newest_fy:
        return 1.0
    return decay ** (newest_fy - last_fy)


def sponsor_score(row: dict, newest_fy: int) -> float:
    return int(row.get("h1b_approvals") or 0) * recency_weight(int(row.get("h1b_last_fy") or 0),
                                                                newest_fy)


def rank_sponsors(rows, limit: int | None = None, skip_norms=(), skip_fn=None) -> list[dict]:
    """Sponsors most worth probing first: approvals weighted toward recent filing years,
    minus anyone in `skip_norms` (normalized names already watched / already probed) and
    anyone `skip_fn(display_name)` turns down. Deterministic (ties by name)."""
    rows = [r for r in rows if int(r.get("h1b_approvals") or 0) > 0]
    newest = max((int(r.get("h1b_last_fy") or 0) for r in rows), default=0)
    skip = set(skip_norms)
    ranked: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        norm = r.get("norm_name") or normalize_employer(r.get("display_name", ""))
        if not norm or norm in skip or norm in seen:
            continue
        if skip_fn and skip_fn(r.get("display_name", "")):
            continue
        seen.add(norm)
        ranked.append({**r, "norm_name": norm, "score": sponsor_score(r, newest)})
    ranked.sort(key=lambda r: (-r["score"], r["display_name"]))
    return ranked[:limit] if limit else ranked


def watched_norms(companies) -> set[str]:
    """Normalized names of the companies already on a watchlist (rows with a `company`)."""
    return {normalize_employer(c["company"]) for c in companies if c.get("company")}


def candidates(rows, checkpoint: "Checkpoint", watched, limit: int | None,
               atss=DISCOVER_ATS) -> list[dict]:
    """The ranked employers still worth probing: not watched, not finished in the checkpoint.
    An employer whose last probe was cut short by a throttled host comes back with only the
    ATSs it still owes (`row["atss"]`), so a rerun finishes the job instead of redoing it."""
    pending = checkpoint.pending(atss)
    ranked = rank_sponsors(rows, limit=limit,
                           skip_norms=set(watched) | checkpoint.done_norms(atss))
    for r in ranked:
        if r["norm_name"] in pending:
            r["atss"] = pending[r["norm_name"]]
    return ranked


# -- the committed watchlist seed ---------------------------------------------- #

def read_watchlist_seed(path: str | Path = WATCHLIST_SEED) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{"company": r["company"], "ats": r["ats"], "board_id": r["board_id"]}
                for r in csv.DictReader(f) if r.get("ats") and r.get("board_id")]


def write_watchlist_seed(path: str | Path, rows) -> list[dict]:
    """Write the seed sorted by (company, ats, board_id) and deduped on (ats, board_id) —
    the first row for a board wins, so pass existing rows before new ones. Returns what
    was written."""
    seen: set[tuple[str, str]] = set()
    clean: list[dict] = []
    for r in rows:
        key = (r["ats"].strip().lower(), r["board_id"].strip())
        if key in seen or not valid_board_id_for(key[0], key[1]):
            continue
        seen.add(key)
        clean.append({"company": r["company"].strip(), "ats": key[0], "board_id": key[1]})
    clean.sort(key=lambda r: (r["company"], r["ats"], r["board_id"]))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["company", "ats", "board_id"])   # CRLF, as committed
        w.writeheader()
        w.writerows(clean)
    return clean


def per_ats_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["ats"]] = counts.get(r["ats"], 0) + 1
    return dict(sorted(counts.items()))


# -- resumable checkpoint ------------------------------------------------------ #

# The ATSs discovery probed before Workday joined (2026-09-30). A checkpointed miss that does
# not say what it was probed on was probed on exactly these, so it still owes Workday.
LEGACY_ATS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")

class Checkpoint:
    """Append-only JSONL of per-employer outcomes keyed by normalized name: hit, miss,
    or skipped. A miss may carry `unprobed_ats` (hosts that were throttled at the time),
    which is the one case a later run revisits — for those ATSs only. Loading replays the
    file; the last record for a name wins."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {}
        self._needs_newline = False
        if self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
            self._needs_newline = bool(raw) and not raw.endswith("\n")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue                       # a torn final line from a hard stop
                if rec.get("norm"):
                    self.records[rec["norm"]] = rec

    def __contains__(self, norm: str) -> bool:
        return norm in self.records

    def record(self, norm: str, **rec) -> dict:
        rec = {"norm": norm, "ts": int(time.time()), **rec}
        with self._lock:
            self.records[norm] = rec
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                if self._needs_newline:
                    f.write("\n")
                    self._needs_newline = False
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    @staticmethod
    def probed_ats(rec: dict) -> set[str]:
        """The ATSs a miss record HAS been probed on. Records written before `probed_ats`
        existed were probed on the original five; an ATS added since (Workday) is owed."""
        if "probed_ats" in rec:
            return set(rec["probed_ats"] or ())
        return set(LEGACY_ATS) - set(rec.get("unprobed_ats") or ())

    def pending(self, atss=DISCOVER_ATS) -> dict[str, list[str]]:
        """norm -> the ATSs a miss still owes, in crawl order: hosts that were throttled last
        time, plus any ATS in `atss` the record was never probed on (a newly added ATS, or a
        run restricted to a subset)."""
        out: dict[str, list[str]] = {}
        for norm, r in self.records.items():
            if r.get("status") != "miss":
                continue
            done = self.probed_ats(r)
            owed = [a for a in atss if a not in done]
            if owed:
                out[norm] = owed
        return out

    def done_norms(self, atss=DISCOVER_ATS) -> set[str]:
        pending = self.pending(atss)
        return {n for n in self.records if n not in pending}

    def hits(self) -> list[dict]:
        return [r for r in self.records.values() if r.get("status") == "hit"]


# -- polite fetch -------------------------------------------------------------- #

class HostCoolingDown(Exception):
    """Raised instead of a request while a host is in its 429 cooldown."""


class PoliteFetch:
    """The one fetch every probe goes through. Spaces requests per host (across all
    threads), caps each request's wait, and treats a 429 as the host asking for room:
    pause it (Retry-After when given, else exponential), double its spacing, retry a few
    times, and if it still says 429 leave it alone for a cooldown. Counts requests and
    429s, and remembers per thread which hosts were unavailable during the current
    employer's probe, so the crawl can record those ATSs as not-yet-probed rather than
    as a miss."""

    def __init__(self, fetch=fetch_json, timeout: int = PROBE_TIMEOUT,
                 host_delay: float = HOST_DELAY, host_delays=None,
                 max_retries: int = MAX_429_RETRIES, cooldown: float = COOLDOWN,
                 max_wait: float = MAX_WAIT, sleep=time.sleep, clock=time.monotonic):
        self._fetch = fetch
        self.timeout = timeout
        self._base = {**HOST_DELAYS, **(host_delays or {})}
        self._default_delay = host_delay
        self.max_retries = max_retries
        self.cooldown = cooldown
        self.max_wait = max_wait
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._next_ok: dict[str, float] = {}
        self._delay: dict[str, float] = {}
        self._clean: dict[str, int] = {}
        self._cooling: dict[str, float] = {}
        self._local = threading.local()
        self.requests = 0
        self.rate_limited = 0
        self.server_errors = 0        # 5xx answers: the host asked for room (lane backed off)
        self.cooldowns = 0
        self.deferred = 0             # requests not made because the host's queue was too long

    # -- per-employer bookkeeping (one thread probes one employer at a time) -- #
    def begin(self, atss=None) -> None:
        """Start one employer's probe on this thread. `atss` are the ATSs it may ask, which
        tells the pacer which lanes are alternatives to one another: a host is only ever
        deferred ("owed to the next run") when another lane of THIS probe could be asked
        sooner. A run restricted to one ATS has no alternative, so it waits for that lane's
        slot instead of deferring every employer before the first request (the bug that
        made a Workable-only run defer all 3,613 employers in seconds, 2026-10-01)."""
        self._local.blocked = set()
        self._local.attempted = set()
        self._local.lanes = lanes_for(atss) if atss is not None else set(ATS_HOSTS)

    @property
    def blocked_hosts(self) -> set[str]:
        return set(getattr(self._local, "blocked", set()))

    @property
    def blocked_ats(self) -> set[str]:
        return {ATS_HOSTS[h] for h in self.blocked_hosts if h in ATS_HOSTS}

    @property
    def attempted_ats(self) -> set[str]:
        """ATSs this employer's probe actually asked (or tried to ask) anything of."""
        return {ATS_HOSTS[h] for h in getattr(self._local, "attempted", set()) if h in ATS_HOSTS}

    @property
    def throttled(self) -> bool:
        return bool(self.blocked_hosts)

    def _block(self, host: str) -> None:
        if not hasattr(self._local, "blocked"):
            self._local.blocked = set()
        self._local.blocked.add(host)

    # -- pacing ------------------------------------------------------------- #
    def delay_for(self, host: str) -> float:
        return self._delay.get(host, self._base.get(host, self._default_delay))

    def _wait_for(self, host: str, now: float) -> float:
        """Seconds until `host` can be asked (inf while it is cooling down). Lock held."""
        if self._cooling.get(host, 0.0) > now:
            return float("inf")
        return max(0.0, self._next_ok.get(host, 0.0) - now)

    def _better_lane(self, host: str, now: float) -> bool:
        """Is some OTHER lane of the current employer's probe reachable within max_wait? Only
        then is deferring `host` worth anything: the thread moves on to that lane. Lock held."""
        lanes = getattr(self._local, "lanes", None) or set(ATS_HOSTS)
        return any(self._wait_for(h, now) <= self.max_wait for h in lanes if h != host)

    def next_free(self, atss=None) -> float:
        """The clock time at which the earliest of these ATSs' lanes (all lanes when None) can
        next be asked -- the end of its cooldown or 429 pause. What a crawl waits for when an
        employer's whole probe came back throttled, instead of dropping the employer."""
        lanes = lanes_for(atss) if atss is not None else set(ATS_HOSTS)
        with self._lock:
            now = self._clock()
            return min((max(now, self._cooling.get(h, 0.0), self._next_ok.get(h, 0.0))
                        for h in lanes), default=now)

    def _take_turn(self, host: str) -> bool:
        """Wait for this host's slot. False (no request made) if the host is in cooldown, or if
        its next slot is more than `max_wait` away AND another lane of this probe is nearer --
        the caller owes that ATS to a later run and asks the nearer lane now. With no nearer
        lane (a single-ATS run, or every lane equally backed up) the thread simply waits: a
        slow lane still serves requests at its spacing, it just never holds the others up."""
        with self._lock:
            now = self._clock()
            wait = self._wait_for(host, now)
            if wait == float("inf"):
                return False
            if wait > self.max_wait and self._better_lane(host, now):
                self.deferred += 1
                return False
            self._next_ok[host] = now + wait + self.delay_for(host)
        if wait > 0:
            self._sleep(wait)
            with self._lock:                         # another thread's 429 may have cooled the
                if self._cooling.get(host, 0.0) > self._clock():   # host while this one waited
                    return False
        with self._lock:
            self.requests += 1
        return True

    def _on_429(self, host: str, exc, attempt: int) -> bool:
        """Record a 429. Returns True when the host's Retry-After is longer than we retry for,
        in which case it has been put in a cooldown of that length and must not be retried."""
        with self._lock:
            self.rate_limited += 1
            self._clean[host] = 0
            self._delay[host] = min(MAX_HOST_DELAY, self.delay_for(host) * 2)
            ra = _retry_after(exc)
            if ra > MAX_BACKOFF:
                self._cooling[host] = self._clock() + min(ra, MAX_RETRY_AFTER)
                self.cooldowns += 1
                return True
            pause = _backoff_seconds(exc, attempt)
            self._next_ok[host] = max(self._next_ok.get(host, 0.0), self._clock() + pause)
            return False

    def _on_server_error(self, host: str, exc) -> None:
        """A 5xx is the host asking for room, not a clean answer: double the lane's spacing,
        pause it (Retry-After when given), and reset its clean streak so RELAX_AFTER cannot
        halve the spacing while the host is struggling. The error itself is re-raised by the
        caller unchanged (ats.fetch_with_backoff retries where that is wanted; its retry then
        waits for this pause at _take_turn instead of hitting the host again at once)."""
        with self._lock:
            self.server_errors += 1
            self._clean[host] = 0
            self._delay[host] = min(MAX_HOST_DELAY, self.delay_for(host) * 2)
            pause = _backoff_seconds(exc, 0)
            self._next_ok[host] = max(self._next_ok.get(host, 0.0), self._clock() + pause)

    def _on_clean(self, host: str) -> None:
        with self._lock:
            self._clean[host] = self._clean.get(host, 0) + 1
            if self._clean[host] >= RELAX_AFTER:
                self._clean[host] = 0
                base = self._base.get(host, self._default_delay)
                self._delay[host] = max(base, self.delay_for(host) / 2)

    def _start_cooldown(self, host: str) -> None:
        with self._lock:
            self._cooling[host] = self._clock() + self.cooldown
            self.cooldowns += 1

    def cooling(self) -> list[str]:
        now = self._clock()
        return sorted(h for h, until in self._cooling.items() if until > now)

    def __call__(self, url: str, timeout: int | None = None, **kw):
        host = host_lane(urlparse(url).hostname or url)
        if hasattr(self._local, "attempted"):
            self._local.attempted.add(host)
        for attempt in range(self.max_retries + 1):
            if not self._take_turn(host):
                self._block(host)
                raise HostCoolingDown(host)
            try:
                result = self._fetch(url, timeout=timeout or self.timeout, **kw)
            except urllib.error.HTTPError as exc:
                if 500 <= exc.code < 600:            # the host is struggling: give it room
                    self._on_server_error(host, exc)
                    raise
                if exc.code != 429:                  # a 404 is an answer about the URL, not the
                    raise                            # host's health: neither clean nor a back-off
                if self._on_429(host, exc, attempt):  # "come back in hours": no retry, cooling
                    self._block(host)
                    raise
                if attempt >= self.max_retries:
                    self._start_cooldown(host)
                    self._block(host)
                    raise
                continue
            self._on_clean(host)
            return result
        raise RuntimeError("unreachable")                 # pragma: no cover


def _retry_after(exc) -> float:
    try:
        return max(0.0, float((getattr(exc, "headers", None) or {}).get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0.0


def _backoff_seconds(exc, attempt: int) -> float:
    ra = _retry_after(exc)
    return min(MAX_BACKOFF, ra if ra > 0 else 5.0 * (2 ** attempt))


def lanes_for(atss) -> set[str]:
    """The pacing lanes (hosts) a set of ATSs is served by."""
    want = set(atss or ())
    return {h for h, a in ATS_HOSTS.items() if a in want}


# -- the crawl ----------------------------------------------------------------- #

def should_skip(display_name: str) -> str | None:
    """Why an employer is not worth a single request, or None to probe it. Uses only
    filters that already exist in the product (no new heuristics): the curated
    clearance-heavy employer list (their postings get filtered out of the feed anyway),
    and names with no distinctive brand word (discovery could never confirm a hit)."""
    if employer_is_clearance_heavy(display_name):
        return "clearance-heavy employer (feed filters its roles)"
    if not slug_candidates(display_name):
        return "no distinctive brand word to verify a board against"
    return None


def grow(ranked, checkpoint: Checkpoint, fetch, *, known_boards=(), atss=DISCOVER_ATS,
         workers: int = 8, max_seconds: float | None = None, on_progress=None,
         clock=time.monotonic, sleep=time.sleep, on_wait=None,
         discover=discover_for_company) -> dict:
    """Probe `ranked` sponsors (see `candidates`) until the list or the time budget runs
    out. Each outcome is checkpointed as soon as it is known. A row may carry its own
    `atss` (the ones it still owes from a throttled run). `known_boards` is the set of
    (ats, board_id) already watched: a hit on one of those (the same board under another
    sponsor entity) is recorded but not counted as new. Returns a summary with the new
    boards to add. Network-bound, so it fans out over `workers` threads; the checkpoint
    handles its own locking.

    An employer whose EVERY lane was unavailable (cooling down, or paused by a 429) is not
    dropped: it is put back on the queue and the crawl waits -- `sleep` -- until the fetch
    says the earliest of its lanes is free again (`on_wait(seconds, lanes)` is told). If
    that wait would outlast the time budget (or MAX_LANE_PAUSE without one), the crawl stops
    with stopped="hosts throttled" and leaves the rest for the next run, which is the honest
    outcome when a host has asked to be left alone for hours. Nothing is ever recorded as a
    miss without having been asked."""
    deadline = (clock() + max_seconds) if max_seconds else None
    known = set(known_boards)
    summary = {"probed": 0, "skipped": 0, "hits": 0, "new_boards": [], "duplicates": 0,
               "rejected": [], "throttled": 0, "partial": 0, "stopped": "exhausted",
               "per_ats": {}, "requeued": 0, "waited": 0.0}
    rejections: dict[str, list[str]] = {}
    rej_lock = threading.Lock()
    requeue: deque = deque()            # fully-throttled employers, to be asked again
    attempts: dict[str, int] = {}       # norm -> times it came back fully throttled
    pause_until = [0.0]                 # clock time before which no new probe is submitted

    def work(row):
        name, norm = row["display_name"], row["norm_name"]
        why = should_skip(name)
        if why:
            return row, "skipped", None, why, []
        local: list[str] = []

        def reject(ats, slug, reason):
            local.append(f"{ats}:{slug} — {reason}")

        row_atss = tuple(row.get("atss") or atss)
        if hasattr(fetch, "begin"):
            try:
                fetch.begin(row_atss)
            except TypeError:                           # an older fetch double without lanes
                fetch.begin()
        hit = discover(name, atss=row_atss, fetch=fetch, reject=reject)
        with rej_lock:
            if local:
                rejections[norm] = local
        blocked = [a for a in row_atss if a in getattr(fetch, "blocked_ats", set())]
        attempted = [a for a in row_atss if a in getattr(fetch, "attempted_ats", set())]
        if hit:
            return row, "hit", hit, None, blocked
        if blocked and len(blocked) == len(attempted):
            return row, "throttled", None, "every ATS host asked was throttled", blocked
        return row, "miss", None, None, blocked

    def finish(fut):
        row, status, hit, why, blocked = fut.result()
        name, norm = row["display_name"], row["norm_name"]
        rejected_here = rejections.pop(norm, [])
        for r in rejected_here:
            summary["rejected"].append({"company": name, "board": r})
        if status == "throttled":                       # nothing learned -> not checkpointed
            row_atss = tuple(row.get("atss") or atss)
            free_at = (fetch.next_free(row_atss) if hasattr(fetch, "next_free")
                       else float("inf"))
            attempts[norm] = attempts.get(norm, 0) + 1
            pause_until[0] = max(pause_until[0], free_at)
            if attempts[norm] <= MAX_REQUEUE:
                requeue.append(row)
                summary["requeued"] += 1
                status, why = "requeued", "every ATS host asked was throttled; asked again once a lane is free"
            else:
                summary["throttled"] += 1               # left for the next run
        elif status == "skipped":
            summary["skipped"] += 1
            checkpoint.record(norm, name=name, status="skipped", reason=why)
        elif status == "hit":
            summary["probed"] += 1
            if int(hit.get("jobs") or 0) < 1:           # discover never does this; stay honest
                summary["rejected"].append({"company": name,
                                            "board": f"{hit['ats']}:{hit['board_id']} — live but 0 jobs"})
                checkpoint.record(norm, name=name, status="miss", reason="0 jobs")
            else:
                summary["hits"] += 1
                key = (hit["ats"], hit["board_id"])
                rec = checkpoint.record(norm, name=name, status="hit", ats=hit["ats"],
                                        board_id=hit["board_id"], jobs=hit["jobs"])
                if key in known:
                    summary["duplicates"] += 1
                else:
                    known.add(key)
                    summary["new_boards"].append({"company": name, "ats": hit["ats"],
                                                  "board_id": hit["board_id"], "jobs": hit["jobs"]})
                    summary["per_ats"][hit["ats"]] = summary["per_ats"].get(hit["ats"], 0) + 1
                why = f"{rec['ats']}:{rec['board_id']} ({rec['jobs']} jobs)"
        else:
            summary["probed"] += 1
            prev = checkpoint.records.get(norm) or {}
            done = set(Checkpoint.probed_ats(prev)) if prev.get("status") == "miss" else set()
            done |= set(row.get("atss") or atss) - set(blocked)
            rec = {"name": name, "status": "miss",
                   "reason": "; ".join(rejected_here) or "no live board at any slug",
                   "probed_ats": [a for a in DISCOVER_ATS if a in done]}
            if blocked:
                summary["partial"] += 1
                rec["unprobed_ats"] = blocked          # owed to the next run
                status = "partial"
                why = f"still owes {', '.join(blocked)} (host throttled)"
            checkpoint.record(norm, **rec)
        if on_progress:
            on_progress(summary["probed"] + summary["skipped"], name, status, why)

    rows = iter(ranked)
    pending = set()

    def next_row():
        return requeue.popleft() if requeue else next(rows, None)

    def wait_for_lanes() -> bool:
        """Honour a pause the throttled probes asked for. False = stop the crawl: the wait
        would outlast the time budget (or MAX_LANE_PAUSE without one)."""
        now = clock()
        gap = pause_until[0] - now
        if gap <= 0:
            return True
        budget = (deadline - now) if deadline is not None else MAX_LANE_PAUSE
        if gap > budget:
            summary["stopped"] = "hosts throttled"
            return False
        if on_wait:
            on_wait(gap, fetch.cooling() if hasattr(fetch, "cooling") else [])
        summary["waited"] += gap
        sleep(gap)
        pause_until[0] = 0.0
        return True

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        while True:
            while len(pending) < max(1, workers) * 2:
                if deadline is not None and clock() >= deadline:
                    summary["stopped"] = "time cap"
                    break
                if not pending and not wait_for_lanes():  # every lane is away; wait or stop
                    break
                if pending and pause_until[0] > clock():
                    break                                # let in-flight probes land first
                row = next_row()
                if row is None:
                    break
                pending.add(ex.submit(work, row))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                finish(fut)
            if summary["stopped"] != "exhausted":
                for fut in wait(pending).done:          # let in-flight probes land, submit no more
                    finish(fut)
                pending = set()
                break
    summary["throttled"] += len(requeue)                 # never asked again: left for the next run
    summary["per_ats"] = dict(sorted(summary["per_ats"].items()))
    summary["requests"] = getattr(fetch, "requests", None)
    summary["rate_limited"] = getattr(fetch, "rate_limited", None)
    summary["cooldowns"] = getattr(fetch, "cooldowns", None)
    summary["deferred"] = getattr(fetch, "deferred", None)
    return summary


# -- the in-app weekly pass ---------------------------------------------------- #

def discover_sponsors(watchlist, checkpoint_path: str | Path, *, budget: int = 300,
                      max_minutes: float = 20, sponsor_rows=None, fetch=None,
                      workers: int = 4, atss=DISCOVER_ATS, clock=time.monotonic,
                      sleep=time.sleep, discover=discover_for_company) -> dict:
    """One bounded discovery pass for the running app (sourcing/scheduler.py calls this
    weekly): take the next `budget` un-probed sponsors from the SAME ranked source the
    maintainer script uses, probe them politely, and add confirmed boards to the live
    watchlist. The checkpoint (in the app's data dir) is what makes each pass pick up where
    the last one stopped instead of re-probing the top of the list forever — so the
    watchlist keeps growing after launch, a slice a week, without hammering anyone."""
    rows = sponsor_rows if sponsor_rows is not None else load_sponsor_seed()
    checkpoint = Checkpoint(checkpoint_path)
    companies = watchlist.companies(active_only=False)
    known = {(c["ats"], c["board_id"]) for c in companies}
    ranked = candidates(rows, checkpoint, watched_norms(companies), budget, atss)
    polite = fetch if fetch is not None else PoliteFetch(sleep=sleep, clock=clock)
    summary = grow(ranked, checkpoint, polite, known_boards=known, atss=atss,
                   workers=workers, max_seconds=max_minutes * 60 if max_minutes else None,
                   clock=clock, sleep=sleep, discover=discover)
    added = 0
    for b in summary["new_boards"]:
        try:
            watchlist.add_company(b["company"], b["ats"], b["board_id"])
            added += 1
        except ValueError:
            continue
    summary["added"] = added
    summary["candidates"] = len(ranked)
    return summary
