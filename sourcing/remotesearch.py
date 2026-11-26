"""The desktop app's client for the server-side job search (docs/server-search-plan.md, phase 3).

Off by default. With JOBS_SEARCH=server the app keeps only the feed's small SLICE (roles first seen
in the last 7 days, sourcing/feedclient.py slice_only) and asks the search API for the rest:

    GET {base}/v1/jobs/search?q=&loc=&days=&remote=&visa=&level=&pay=&sort=&sponsored=&limit=&cursor=
    GET {base}/v1/jobs/candidates?<the same>&titles=A|B&limit=500&cursor=
    -> {"jobs": [slim rows], "count", "count_capped", "next_cursor", "index_version", "generated_at"}
    -> 409 when the cursor belongs to an older index: start again from page 1

What leaves the machine is ONLY the query, the facets, a cursor and, for the Auto-apply picker,
the job titles the person typed (their saved search or today's focus). Every request is built by
`build_query` from an allowlist of keys (SEARCH_KEYS / CANDIDATE_KEYS) and goes out through one
method (`_get`), which tests/test_remote_search.py guards. Never sent: the resume or profile, words
taken from them, saved or dismissed IDs, company lists, match scores. The one header is the
install's broker account token (`Authorization: Bearer`, 2026-11-25): search is locked to
SponsorJobs installs (worker/src/installauth.ts), and the server uses the account only as a
rate-limit key, never storing it next to a query. A 401 asks `on_unauthorized` (ui/app.py
`_recover_from_401`) to replace a token the broker no longer knows and retries once; a 401 that
stays, or no token at all, is handled like being offline: the board serves the slice with its note.

Engine side: an in-memory LRU of the last ~50 responses (Back and facet toggles cost nothing);
a short back-off after a failure, so an offline app pays one timeout, not one per request; and
`board_page`, which turns the server's cursors into the board's numbered pages: it applies the
device's own layers (personal ranking, dismissed) to each server page, follows next_cursor up to
3 calls to fill a short page, remembers where each page starts, and restarts from page 1 on 409.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlencode

SEARCH_PATH = "/v1/jobs/search"
CANDIDATES_PATH = "/v1/jobs/candidates"
# The only query keys that may ever leave the machine (the plan's "What goes to the server").
FACET_KEYS = ("q", "loc", "days", "remote", "visa", "level", "pay", "sort", "sponsored", "scope")
SEARCH_KEYS = frozenset(FACET_KEYS + ("limit", "cursor"))
CANDIDATE_KEYS = SEARCH_KEYS | {"titles"}
SEARCH_LIMIT = 50           # the API's caps: 50 rows a search page...
CANDIDATES_LIMIT = 500      # ...and 500 a candidates page
MAX_TITLES = 10             # typed titles sent for the picker, each at most TITLE_MAX characters
TITLE_MAX = 80
Q_MAX = 200
TIMEOUT = 6                 # seconds per request; then the board serves the slice
DOWN_FOR = 30               # after a failure, no request for this long (the slice serves)
BUSY_FOR = 600              # after a 503 (no index yet, or the day's free allowance used up)
CACHE_SIZE = 50             # responses kept in memory
PAGE_CALLS = 3              # server calls to fill one short board page
MAX_WALK = 40               # pages walked forward to reach a deep page (1,000 results / 30 = 34)
_UA = "resume-agent/1.0 (search client)"


class SearchUnavailable(RuntimeError):
    """The search API could not answer (offline, down, a bad reply): serve the slice instead."""


class StaleCursor(RuntimeError):
    """409: the cursor belongs to an older index. Start the query again from page 1."""


def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """(status, body); an HTTP error status is returned, not raised. The account token is added
    unredirected, so a redirect could never carry it to another host."""
    rest = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json",
                                               **rest})
    for k, v in headers.items():
        if k.lower() == "authorization":
            req.add_unredirected_header("Authorization", v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def build_query(facets: dict | None, *, cursor: str | None = None, limit: int = 30,
                titles=None, candidates: bool = False) -> list[tuple[str, str]]:
    """The (key, value) pairs of one request, from the board's facets (ui/app.py _facets, or a
    saved search's filters). Empty facets are left out and the order is fixed, so equal queries
    are equal URLs (the LRU and the edge cache key on them). Only allowlisted keys come out: any
    other key in `facets` is dropped here, never sent."""
    f = facets or {}
    out: list[tuple[str, str]] = []
    q = " ".join(str(f.get("q") or "").split())[:Q_MAX]
    if q:
        out.append(("q", q))
    loc = " ".join(str(f.get("loc") or "").split())[:Q_MAX]
    if loc:
        out.append(("loc", loc))
    if _int(f.get("days")):
        out.append(("days", str(_int(f.get("days")))))
    remote = str(f.get("remote") or "").strip().lower()
    if remote:
        out.append(("remote", remote))
    visa = f.get("visa") or []
    if isinstance(visa, str):
        visa = visa.split(",")
    visa = [str(v).strip() for v in visa if str(v).strip()]
    if visa:
        out.append(("visa", ",".join(visa)))
    level = str(f.get("level") or "").strip().lower()
    if level:
        out.append(("level", level))
    if _int(f.get("pay")):
        out.append(("pay", str(_int(f.get("pay")))))
    sort = str(f.get("sort") or "").strip().lower()
    if sort in ("recent", "pay"):
        out.append(("sort", sort))
    if str(f.get("sponsored", "")).strip().lower() in ("1", "true", "yes", "on"):
        out.append(("sponsored", "1"))
    # The board's scope (sourcing/filters.apply_facets): only "sponsors" narrows, so "all" and an
    # empty scope send nothing.
    if str(f.get("scope") or "").strip().lower() == "sponsors":
        out.append(("scope", "sponsors"))
    if candidates:
        typed = []
        for t in titles or []:
            t = " ".join(str(t).replace("|", " ").split())[:TITLE_MAX]
            if t and t not in typed:
                typed.append(t)
        if typed:
            out.append(("titles", "|".join(typed[:MAX_TITLES])))
    cap = CANDIDATES_LIMIT if candidates else SEARCH_LIMIT
    out.append(("limit", str(max(1, min(cap, int(limit or 30))))))
    if cursor:
        out.append(("cursor", str(cursor)))
    allowed = CANDIDATE_KEYS if candidates else SEARCH_KEYS
    bad = [k for k, _ in out if k not in allowed]
    if bad:                                         # cannot happen; never send an unlisted key
        raise ValueError(f"query key(s) outside the allowlist: {bad}")
    return out


def query_key(facets: dict | None) -> str:
    """One board query, independent of page and cursor (where its pages start is kept per key)."""
    return urlencode([kv for kv in build_query(facets) if kv[0] != "limit"])


class RemoteSearch:
    """One install's search client. `fetch(url, headers, timeout) -> (status, body)` and `clock`
    are injectable so tests run against a fake server, offline."""

    def __init__(self, base_url: str, data_dir, fetch=None, clock=time.time,
                 timeout: float = TIMEOUT, cache_size: int = CACHE_SIZE, down_for: float = DOWN_FOR,
                 token=None, on_unauthorized=None):
        self.base_url = (base_url or "").rstrip("/")
        self.data_dir = Path(data_dir)
        self._fetch = fetch or _http_get
        self._clock = clock
        self._timeout = timeout
        self._down_for = down_for
        self._cache_size = max(1, int(cache_size))
        self._lru: OrderedDict = OrderedDict()
        self._starts: dict[str, dict[int, tuple]] = {}   # query -> {page: (cursor, skip) or None}
        self._down_until = 0.0
        # `token() -> str | None`: the broker account token (ui/app.py _account_token, which
        # registers on first need). `on_unauthorized() -> bool`: replace a rejected token.
        self._token = token
        self._on_unauthorized = on_unauthorized
        self._inflight: set[str] = set()          # page-1 prefetches running now
        self._lock = threading.RLock()

    # -- the account token ----------------------------------------------------------------- #

    def _auth(self) -> dict | None:
        """The bearer header, or None when no token can be had right now (offline, the network's
        sign-up limit): the board then serves the slice, exactly as when search is down. No
        `token` callable (a search address on another host, tests): no header at all."""
        if self._token is None:
            return {}
        try:
            tok = self._token()
        except Exception:                           # noqa: BLE001 - no token = no search for now
            tok = None
        return {"Authorization": f"Bearer {tok}"} if tok else None

    # -- requests ---------------------------------------------------------------------------- #

    def down(self) -> bool:
        """Did the last request fail recently (offline / server down)? The board then shows the
        slice with its note, without waiting on another timeout."""
        return self._clock() < self._down_until

    def url(self, path: str, params: list[tuple[str, str]]) -> str:
        return f"{self.base_url}{path}?{urlencode(params)}"

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        """THE one way a request leaves the machine. A cached response is returned as is."""
        if not self.base_url:
            raise SearchUnavailable("no search address configured")
        url = self.url(path, params)
        with self._lock:
            hit = self._lru.get(url)
            if hit is not None:
                self._lru.move_to_end(url)
                return hit
            if self.down():
                raise SearchUnavailable("the search server was unreachable a moment ago")
        auth = self._auth()
        if auth is None:
            self._failed(BUSY_FOR)                  # registering again is not worth a wait per page
            raise SearchUnavailable("search needs a SponsorJobs account and none is set up yet")
        try:
            status, body = self._fetch(url, auth, self._timeout)
            if status == 401 and self._on_unauthorized is not None and self._on_unauthorized():
                auth = self._auth()                 # a fresh account: one retry
                if auth is not None:
                    status, body = self._fetch(url, auth, self._timeout)
        except Exception as exc:                    # noqa: BLE001 - offline, DNS, timeout...
            self._failed()
            raise SearchUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc
        if status == 401:                           # the account was refused: like being offline
            self._failed(BUSY_FOR)
            raise SearchUnavailable("search refused this install's account (401)")
        if status == 409:
            with self._lock:                        # the index moved on: every cursor is stale
                self._lru.clear()
                self._starts.clear()
            raise StaleCursor("the search index changed")
        if status == 503:                           # not loaded yet, or out of today's allowance
            self._failed(BUSY_FOR)
            raise SearchUnavailable("search is not available right now (503)")
        if status != 200:
            self._failed()
            raise SearchUnavailable(f"search returned HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))
            ok = isinstance(data, dict) and isinstance(data.get("jobs"), list)
        except Exception:                           # noqa: BLE001 - not JSON
            ok = False
        if not ok:
            self._failed()
            raise SearchUnavailable("search returned an unexpected reply")
        data["jobs"] = [r for r in data["jobs"] if isinstance(r, dict) and r.get("source_id")]
        with self._lock:
            self._down_until = 0.0
            self._lru[url] = data
            self._lru.move_to_end(url)
            while len(self._lru) > self._cache_size:
                self._lru.popitem(last=False)
        return data

    def _failed(self, wait: float | None = None) -> None:
        with self._lock:
            self._down_until = self._clock() + max(self._down_for, wait or 0)

    def search(self, facets: dict | None, *, cursor: str | None = None, limit: int = 30) -> dict:
        return self._get(SEARCH_PATH, build_query(facets, cursor=cursor, limit=limit))

    def candidates(self, facets: dict | None, titles=(), *, cursor: str | None = None,
                   limit: int = CANDIDATES_LIMIT) -> dict:
        """The Auto-apply picker's pool. `titles` must be titles the PERSON typed (their saved
        search or today's focus), never words taken from the resume."""
        return self._get(CANDIDATES_PATH, build_query(facets, cursor=cursor, limit=limit,
                                                      titles=titles, candidates=True))

    def cached(self, facets: dict | None, *, limit: int = 30) -> dict | None:
        """Page 1 of this query when it is already in the LRU (no request), else None."""
        with self._lock:
            return self._lru.get(self.url(SEARCH_PATH, build_query(facets, limit=limit)))

    def prefetch(self, facets: dict | None, *, limit: int = 30) -> bool:
        """Fetch page 1 of this query on a background thread (warms the LRU: its count, and the
        page behind the slice's first page). True while that fetch is running (one per query);
        False when it is cached or the server is down."""
        if self.cached(facets, limit=limit) is not None or self.down() or not self.base_url:
            return False
        url = self.url(SEARCH_PATH, build_query(facets, limit=limit))
        with self._lock:
            if url in self._inflight:
                return True
            self._inflight.add(url)

        def run():
            try:
                self.search(facets, limit=limit)
            except Exception:                       # noqa: BLE001 - noted by down()
                pass
            finally:
                with self._lock:
                    self._inflight.discard(url)
        threading.Thread(target=run, name="search-prefetch", daemon=True).start()
        return True

    # -- the board's numbered pages ---------------------------------------------------------- #

    def board_page(self, facets: dict | None, page: int, per_page: int = 30, keep=None) -> dict:
        """Board page `page` of the query, after the device's own filter `keep(row) -> bool`.
        {"jobs", "count", "count_capped", "end", "index_version", "generated_at"}: `count` is the
        server's count for the query (before the device's layers, so "about N"). Raises
        SearchUnavailable; a stale cursor (409) restarts from page 1 once."""
        for _ in range(2):
            try:
                return self._board_page(facets, max(1, int(page)), max(1, int(per_page)),
                                        keep or (lambda _r: True))
            except StaleCursor:
                continue
        raise SearchUnavailable("the search index kept changing")

    def _board_page(self, facets, page: int, per_page: int, keep) -> dict:
        key = query_key(facets)
        with self._lock:
            starts = self._starts.setdefault(key, {1: (None, 0)})
            known = max(p for p in starts if p <= page)
        if page - known > MAX_WALK:
            raise SearchUnavailable("that page is past the search depth")
        first = None
        p = known
        while True:
            start = starts.get(p)
            if start is None:                       # the results ended before this page
                return self._page_out([], first, end=True)
            rows, nxt, meta = self._collect(facets, start, per_page, keep)
            first = first or meta
            with self._lock:
                starts[p + 1] = nxt
            if p == page:
                return self._page_out(rows, meta, end=nxt is None)
            p += 1

    def _collect(self, facets, start: tuple, per_page: int, keep):
        """Rows for one board page from `start` = (cursor, rows to skip in that server page):
        follows next_cursor up to PAGE_CALLS calls. (rows, where the next page starts or None at
        the end, the first response)."""
        cursor, skip = start
        rows: list[dict] = []
        first = None
        for _ in range(PAGE_CALLS):
            resp = self.search(facets, cursor=cursor, limit=per_page)
            first = first or resp
            raw = resp["jobs"]
            nxt = resp.get("next_cursor") or None
            for i in range(skip, len(raw)):
                if keep(raw[i]):
                    rows.append(raw[i])
                    if len(rows) >= per_page:
                        if i + 1 < len(raw):
                            return rows, (cursor, i + 1), first
                        return rows, ((nxt, 0) if nxt else None), first
            if not nxt:
                return rows, None, first
            cursor, skip = nxt, 0
        return rows, (cursor, 0), first             # still short after PAGE_CALLS calls

    @staticmethod
    def _page_out(rows, meta, end: bool) -> dict:
        meta = meta or {}
        est = meta.get("count_estimate")
        return {"jobs": rows, "count": _int(meta.get("count")),
                "count_capped": bool(meta.get("count_capped")), "end": end,
                "count_estimate": _int(est) if est else None,
                "index_version": meta.get("index_version"), "generated_at": meta.get("generated_at")}


def count_label(count: int, capped: bool, estimate: int | None = None) -> str:
    """How the board reads a server count: exact ("812"), an estimate the server made from what
    it read ("about 4,900"), or a floor ("250+") when it stopped counting."""
    if not capped:
        return f"{count:,}"
    if estimate:
        return f"about {estimate:,}"
    return f"{count:,}+"
