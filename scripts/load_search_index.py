#!/usr/bin/env python3
"""Load the built feed into the server-side search index (D1), writing only what changed, inside a
daily write budget, and carrying on where it stopped.

    SEARCH_D1_ACCOUNT_ID=... SEARCH_D1_DATABASE_ID=... SEARCH_D1_TOKEN=... \\
        python scripts/load_search_index.py --feed .site/feed/jobs.json.gz \\
            --state .feed-data/search-index-state.json.gz
    python scripts/load_search_index.py --feed ... --state ... --dry-run   # counts only, no network

SEARCH_D1_DATABASE_ID is the search database (`sponsorjobs-search`, the Worker's SEARCH_DB binding),
never the broker's.

docs/server-search-plan.md ("Staying on Cloudflare's free plan"). The free plan allows 100,000 D1
rows written a day for the WHOLE account, so the index is built to cost few writes and the loader
never spends more than its share:

* One table row per job and no index on it for the board's order: `id` IS the order (see
  `assign_ids`), so the table itself is the newest-first index. The only other writes are the
  pay index (rows that state pay), the "ad offers sponsorship" index (under 1% of rows) and the
  keyword index (FTS5, written in pages, far less than a row per job).
* Only the diff is sent: rows that left the feed are deleted, new rows inserted, changed rows
  rewritten. Unchanged rows cost nothing.
* A budget (`--daily-writes`, default 80,000, and `--max-writes` per run): D1 reports the rows
  each statement really wrote (keyword index included), the loader adds them to today's total in
  the `search_usage` table and stops before the next statement would pass the budget. What is
  left waits for the next run. Order: removals first (a closed job must leave), then new rows
  newest first, then changed rows newest first.
* The API answers "no index yet" (503) until the first full load has finished (`ready`), so a
  half-loaded index is never searched.

State: {source_id: [row hash, id, employer-fields id]} is saved next to the crawl state after every
run, complete or not, with the token the run left in D1 (`load_token`). A run that dies part-way
leaves D1's token saying "dirty", so the next run rebuilds the state from D1 itself (reads only)
and finishes the job. Every statement is idempotent (inserts are upserts on `id`), so a retry
after an unclear failure cannot duplicate a row.

The D1 HTTP API takes at most 100 bound parameters per query, so rows are sent as SQL literals
(strings quoted the SQLite way: ' doubled; nothing else is special), many rows per statement, each
statement under D1's 100 KB limit. Requests are spaced and retried on 429 / 5xx.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing.feedfile import canon, load_feed_file, recency_key  # noqa: E402
from sourcing.filters import _parse_posted, in_sponsor_scope, salary_value  # noqa: E402

ENCODING = 1                        # bump to force every row to be rewritten (a new row layout)
TEXT_KEYS = ("title", "company", "location")            # real columns (searched, and returned)
EXTRA_KEYS = ("sponsor", "visa", "nationality_visas")   # per-employer, stored once in `extras`
COLUMNS = ("id", "title", "company", "location", "tl", "cl", "ll", "f", "vm", "pay", "ts", "x", "h",
           "r")
MAX_STATEMENT_BYTES = 90_000        # D1 caps one SQL statement at 100 KB
DELETE_PER_STATEMENT = 400
API = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"

# The flag bits in jobs.f (worker/src/search.ts reads the same numbers).
F_SPONSOR = 1          # on the sponsor board (filters.in_sponsor_scope): scope=sponsors keeps it
F_ENTRY = 2            # entry_level is truthy
F_OFFERED = 4          # the "ad offers sponsorship" facet keeps the row
F_VISA_ANY = 8         # the row's own visa list is non-empty (visa=SPONSORED)
F_REMOTE_SHIFT = 4     # bits 4-5: 0 none/other, 1 remote, 2 hybrid, 3 onsite
REMOTE_KINDS = {"remote": 1, "hybrid": 2, "onsite": 3}

# id = dated bit | posting day | label. Newest first is ORDER BY id DESC. 41 bits, so an id is an
# exact number in JavaScript and JSON, and neighbours are close together: the keyword index stores
# the gaps between ids, and small gaps keep it small.
DAY_BITS, LABEL_BITS = 16, 24
LABEL_MAX = (1 << LABEL_BITS) - 1
DAY_MAX = (1 << DAY_BITS) - 1
DATED = 1 << (DAY_BITS + LABEL_BITS)
STRIDE = 64                         # room left between neighbours for rows that arrive later
MIN_STEP = 4                        # when a crowded stretch is renumbered, leave at least this
MAX_VISA_CODES = 62                 # bits in jobs.vm

DAILY_WRITES = 80_000               # of the free plan's 100,000 a day; the rest is the broker's
EST_WRITES_PER_ROW = 3.0            # until D1 has reported real numbers in this run (measured 2.5)
RESERVE = 40                        # rows kept back for the run's bookkeeping (meta, usage, merge)


# ---------------------------------------------------------------- one row -> one index record
def _text(v) -> str:
    """A column value as text SQLite will store: NUL cannot sit inside a literal."""
    return str(v or "").replace("\x00", "")


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _lowered(s: str) -> str | None:
    """Python's lower() of a text column, but only when SQLite's own lower() (ASCII only) would
    answer something else ("Ångström"): the Worker matches against coalesce(this, lower(column)),
    so the common case stores nothing."""
    low = s.lower()
    return low if low != s.translate(_ASCII_LOWER) else None


def _offered(row: dict) -> bool:
    """apply_facets' sponsored test, verbatim."""
    st = row.get("ad_stance")
    return st == "offered" or (st is None and row.get("sponsorship_stated") is True)


def _codes(row: dict) -> list[str]:
    return [v.get("code") for v in (row.get("visa") or []) + (row.get("nationality_visas") or [])
            if isinstance(v, dict) and isinstance(v.get("code"), str) and v.get("code")]


def _hash(s: str) -> str:
    d = hashlib.sha1(s.encode("utf-8", "surrogatepass")).digest()
    return base64.urlsafe_b64encode(d[:8]).decode().rstrip("=")


def _seconds(s) -> int | None:
    """Unix seconds of a feed date string (filters._parse_posted's reading), or None."""
    dt = _parse_posted(str(s or ""))
    if dt is None:
        return None
    return max(0, min(DAY_MAX * 86400 + 86399, int(dt.timestamp())))


def bucket(row: dict) -> int:
    """The id of a row without its label: the dated bit and the day. Rows sort by this, then by
    `order_key` inside one day, and that is exactly feedfile.recency_key's order for every date
    the feed writes. A dated row's day is its posting day; an undated row's is the day it was
    first seen (undated rows come after every dated one, newest seen first). A posting date that
    cannot be read sorts as text in recency_key, so it goes above every real date when the text
    does (anything starting with a letter) and below otherwise."""
    posted = str(row.get("posted_at") or "")
    if not posted:
        return ((_seconds(row.get("first_seen")) or 0) // 86400) << LABEL_BITS
    sec = _seconds(posted)
    day = sec // 86400 if sec is not None else (DAY_MAX if posted > "9999" else 0)
    return DATED | (day << LABEL_BITS)


def order_key(row: dict) -> tuple:
    """A row's place inside its day: the posting second, then recency_key (which settles rows of
    one second, and the dates of different spellings that read as the same second)."""
    posted = str(row.get("posted_at") or "")
    sec = _seconds(posted) if posted else _seconds(row.get("first_seen"))
    return (sec or 0, recency_key(row))


def cutoff_id(seconds: int) -> int:
    """The smallest id a dated row posted on the day of `seconds`, or later, can have: the date
    facet reads no dated row below it."""
    return DATED | ((max(0, min(DAY_MAX * 86400, int(seconds))) // 86400) << LABEL_BITS)


class Dicts:
    """The index's three append-only lists, kept in search_meta so the Worker can read them back:
    `fields` (the row keys, in the order `r` stores their values), `tags` (a row stores tag
    numbers, not names) and `visa` (bit positions in jobs.vm). A name never moves once listed."""

    def __init__(self, fields=(), tags=(), visa=()):
        self.fields, self.tags, self.visa = list(fields), list(tags), list(visa)
        self._f = {k: i for i, k in enumerate(self.fields)}
        self._t = {k: i for i, k in enumerate(self.tags)}
        self._v = {k: i for i, k in enumerate(self.visa)}
        self.dirty = False
        if not self.fields:
            self._add(self.fields, self._f, "source_id")       # always first: `$[0]` in SQL

    def learn(self, rows: list[dict]) -> None:
        """List every key, tag and visa code these rows use that is not listed yet."""
        for r in rows:
            for k in r:
                if k not in self._f and k not in TEXT_KEYS and k not in EXTRA_KEYS:
                    self._add(self.fields, self._f, k)
            for t in r.get("tags") or []:
                if isinstance(t, str) and t not in self._t:
                    self._add(self.tags, self._t, t)
            for c in _codes(r):
                if c not in self._v and len(self.visa) < MAX_VISA_CODES:
                    self._add(self.visa, self._v, c)

    def _add(self, lst, idx, k):
        idx[k] = len(lst)
        lst.append(k)
        self.dirty = True

    def as_meta(self) -> dict[str, str]:
        return {"fields": json.dumps(self.fields), "tags": json.dumps(self.tags, ensure_ascii=False),
                "visa": json.dumps(self.visa)}

    @classmethod
    def from_meta(cls, meta: dict) -> "Dicts":
        def lst(k):
            try:
                v = json.loads(meta.get(k) or "[]")
                return v if isinstance(v, list) else []
            except ValueError:
                return []
        d = cls(lst("fields"), lst("tags"), lst("visa"))
        d.dirty = not lst("fields")
        return d


def row_hash(row: dict) -> str:
    """What the diff compares: the whole row, and the layout version."""
    return _hash(f"{ENCODING}|" + canon(row))


def extra_of(row: dict) -> str:
    """The row's employer fields as canonical JSON ("" when it has none)."""
    extra = {k: row[k] for k in EXTRA_KEYS if k in row and row[k] is not None}
    return canon(extra).replace("\x00", "") if extra else ""


def record(row: dict, dicts: Dicts) -> dict:
    """The index columns for one slim feed row, without `id` and `x` (the loader assigns those).
    Every derived value is computed with the app's own functions (filters.salary_value,
    _parse_posted, in_sponsor_scope; Python's lower()), so the Worker only compares numbers."""
    title, company, location = (_text(row.get(k)) for k in TEXT_KEYS)
    remote = str(row.get("remote") or "").lower()
    f = ((F_SPONSOR if in_sponsor_scope(row) else 0)
         | (F_ENTRY if row.get("entry_level") else 0)
         | (F_OFFERED if _offered(row) else 0)
         | (F_VISA_ANY if (row.get("visa") or []) else 0)
         | (REMOTE_KINDS.get(remote, 0) << F_REMOTE_SHIFT))
    vm = 0
    for c in _codes(row):
        i = dicts._v.get(c)
        if i is not None:
            vm |= 1 << i
    rest = []
    for k in dicts.fields:
        v = row.get(k)
        if k == "tags" and isinstance(v, list):
            v = [dicts._t[t] if isinstance(t, str) and t in dicts._t else t for t in v]
        rest.append(v)
    while rest and rest[-1] is None:
        rest.pop()
    return {"title": title, "company": company, "location": location,
            "tl": _lowered(title), "cl": _lowered(company), "ll": _lowered(location),
            "f": f, "vm": vm, "pay": salary_value(row.get("salary")),
            "ts": _seconds(row.get("posted_at")),
            "h": row_hash(row),
            "r": json.dumps(rest, ensure_ascii=False, separators=(",", ":")).replace("\x00", "")}


# ---------------------------------------------------------------- ids: the board's order
def assign_ids(rows: list[dict], prev: dict[str, int]) -> dict[str, int]:
    """{source_id: id} for the feed's rows, so that ORDER BY id DESC is exactly the board's order
    (feedfile.recency_key, newest first). `prev` is the id each row has in the index now: a row
    keeps it while it still fits (same day, still in order), so an unchanged row is never
    rewritten because a neighbour arrived. A new row gets a label STRIDE above the day's last one
    (the usual case: it was posted or first seen later), or half-way between its neighbours when
    it lands in the middle. Where a stretch has no room left, that stretch alone is renumbered
    (its rows are then rewritten)."""
    groups: dict[int, list[tuple]] = {}
    for r in rows:
        sid = r.get("source_id")
        if sid:
            groups.setdefault(bucket(r), []).append((order_key(r), str(sid)))
    out: dict[str, int] = {}
    for base, members in groups.items():
        members.sort()
        if len(members) > LABEL_MAX:
            raise ValueError(f"{len(members)} rows in one day: more than the index can order")
        labels: list[int | None] = []
        last = 0
        for _, sid in members:
            pid = prev.get(sid)
            lab = pid - base if pid is not None and 0 < pid - base <= LABEL_MAX else None
            if lab is not None and lab > last:
                last = lab
            else:
                lab = None
            labels.append(lab)
        _place(labels)
        for (_, sid), lab in zip(members, labels):
            out[sid] = base + lab
    return out


def _place(labels: list) -> None:
    """Give every None a label strictly between its labelled neighbours, in place. A run with no
    room takes its neighbours' labels too, wider and wider, until the stretch can be spread out
    with MIN_STEP between rows (or covers the whole day)."""
    n = len(labels)
    i = 0
    while i < n:
        if labels[i] is not None:
            i += 1
            continue
        j = i
        while j < n and labels[j] is None:
            j += 1
        need = 1
        while True:
            lo = labels[i - 1] if i else 0
            count = j - i
            if j == n:                              # nothing above: step up from the last label
                step = min(STRIDE, (LABEL_MAX - lo) // count)
            else:
                step = (labels[j] - lo) // (count + 1)
            if step >= need or (i == 0 and j == n):
                break
            need = MIN_STEP if n - (j - i) > 0 and LABEL_MAX // (n + 1) >= MIN_STEP else 1
            if j < n:                               # no room: take a neighbour on each side
                j += 1
                while j < n and labels[j] is None:
                    j += 1
            if i > 0:
                i -= 1
        step = max(1, step)
        for k in range(j - i):
            labels[i + k] = lo + step * (k + 1)
        i = j


# ---------------------------------------------------------------- the plan: what to write
class Plan:
    """What one feed needs written, against the index's state.

    state / target: {source_id: [h, id, x]}. `deletes` are ids to remove (rows that left, and the
    old id of a row that moved); `new` and `moved` are (source_id, feed row, id, x) to insert and
    to rewrite, each newest first; `new_extras` maps an employer-fields id to its JSON for values
    the index does not hold yet; `dead_extras` are ids no row will point at."""

    def __init__(self, rows: list[dict], state: dict, extras: dict[str, int]):
        feed: dict[str, dict] = {}
        for r in rows:
            sid = r.get("source_id")
            if sid and str(sid) not in feed:
                feed[str(sid)] = r
        self.feed = feed
        prev_ids = {sid: int(v[1]) for sid, v in state.items()}
        ids = assign_ids(list(feed.values()), prev_ids)
        self.extras_ids = dict(extras)              # hash of the JSON -> x, old and new
        nxt = max(self.extras_ids.values(), default=0) + 1
        self.new_extras: dict[int, str] = {}
        self.target: dict[str, list] = {}
        new, changed = [], []
        self.deletes: list[int] = [int(v[1]) for sid, v in state.items() if sid not in feed]
        self.removed = len(self.deletes)
        for sid, r in feed.items():
            ej = extra_of(r)
            x = 0
            if ej:
                eh = _hash(ej)
                x = self.extras_ids.get(eh, 0)
                if not x:
                    x = self.extras_ids[eh] = nxt
                    nxt += 1
                    self.new_extras[x] = ej
            h = row_hash(r)
            self.target[sid] = [h, ids[sid], x]
            old = state.get(sid)
            if old is None:
                new.append((sid, r, ids[sid], x))
            elif old[0] != h or int(old[1]) != ids[sid] or int(old[2]) != x:
                if int(old[1]) != ids[sid]:
                    self.deletes.append(int(old[1]))    # it moved: the old id goes first
                changed.append((sid, r, ids[sid], x))
        new.sort(key=lambda t: -t[2])               # newest first
        changed.sort(key=lambda t: -t[2])
        self.new, self.moved = new, changed
        self.added, self.changed = len(new), len(changed)
        used = {v[2] for v in self.target.values()}
        self.dead_extras = sorted(x for x in set(extras.values()) if x not in used)

    @property
    def pending(self) -> int:
        return len(self.deletes) + len(self.new) + len(self.moved)


# ---------------------------------------------------------------- SQL
def lit(v) -> str:
    """A SQL literal. Strings: single quotes doubled is SQLite's only escape."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    s = str(v).encode("utf-8", "replace").decode("utf-8")      # lone surrogates cannot be stored
    return "'" + s.replace("'", "''") + "'"


_UPSERT_HEAD = "INSERT INTO jobs (" + ", ".join(COLUMNS) + ") VALUES "
_UPSERT_TAIL = (" ON CONFLICT(id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "id"))


def delete_statements(ids: list[int]) -> list[tuple[str, list[int]]]:
    """(SQL, the ids it removes), DELETE_PER_STATEMENT ids each."""
    out = []
    for i in range(0, len(ids), DELETE_PER_STATEMENT):
        chunk = ids[i:i + DELETE_PER_STATEMENT]
        out.append(("DELETE FROM jobs WHERE id IN (" + ", ".join(str(x) for x in chunk) + ")", chunk))
    return out


def extras_statements(extras: dict[int, str], *, max_bytes: int = MAX_STATEMENT_BYTES) -> list[str]:
    head = "INSERT INTO extras (x, v) VALUES "
    tail = " ON CONFLICT(x) DO UPDATE SET v = excluded.v"
    out, values, size = [], [], 0
    for x, v in extras.items():
        item = "(" + str(x) + ", " + lit(v) + ")"
        n = len(item.encode("utf-8")) + 2
        if values and size + n > max_bytes - len(head) - len(tail):
            out.append(head + ", ".join(values) + tail)
            values, size = [], 0
        values.append(item)
        size += n
    if values:
        out.append(head + ", ".join(values) + tail)
    return out


def upsert_statements(upserts: list[tuple], dicts: Dicts, *,
                      max_bytes: int = MAX_STATEMENT_BYTES, max_rows=lambda: None):
    """Yield (SQL, [source_id, ...]) for the rows, many a statement, in the order given.
    `max_rows()` (None = no limit) is asked as each statement starts: the rows the write budget
    still has room for, so a budget is filled to the last statement instead of stopping early."""
    budget = max_bytes - len(_UPSERT_HEAD) - len(_UPSERT_TAIL.encode())
    values, sids, size = [], [], 0
    cap = max_rows()
    for sid, row, jid, x in upserts:
        rec = record(row, dicts)
        rec["id"], rec["x"] = jid, x
        v = "(" + ", ".join(lit(rec[c]) for c in COLUMNS) + ")"
        n = len(v.encode("utf-8")) + 2
        if values and (size + n > budget or (cap is not None and len(values) >= cap)):
            yield _UPSERT_HEAD + ", ".join(values) + _UPSERT_TAIL, sids
            values, sids, size = [], [], 0
            cap = max_rows()
        values.append(v)
        sids.append(sid)
        size += n
    if values:
        yield _UPSERT_HEAD + ", ".join(values) + _UPSERT_TAIL, sids


WAVE = 5000


def waves(plan: "Plan", first_load: bool, fits):
    """The rows to write, in batches, each batch in ASCENDING id order: SQLite fills a table page
    completely only when rows arrive in key order (measured: 456 bytes a row ascending, 908
    descending, and the free plan stops at 500 MB a database).

    The first load (nobody can search yet) goes oldest to newest in one pass. After that, new
    rows go before changed ones and the newest before the rest: each batch is the newest rows
    still waiting that `fits()` says the budget has room for (None = no limit), at most WAVE. An
    empty batch means the budget is spent."""
    if first_load:
        yield sorted(plan.new + plan.moved, key=lambda t: t[2])
        return
    for group in (plan.new, plan.moved):
        pos = 0
        while pos < len(group):
            n = fits()
            take = WAVE if n is None else max(0, min(WAVE, n))
            if take == 0:
                yield []
                return
            yield group[pos:pos + take][::-1]
            pos += take


PAYQ_STEP = 500


def histograms(ids: dict[str, int], feed: dict[str, dict], payq_step: int = PAYQ_STEP) -> dict[str, str]:
    """What lets the Worker bound a walk without counting rows (search.ts idBound / payBound):
    `hist`, the rows per day key (id >> LABEL_BITS), highest first; `payq`, every payq_step-th
    pay of the rows that state one, highest first. A few KB in search_meta."""
    days: dict[int, int] = {}
    for jid in ids.values():
        k = int(jid) >> LABEL_BITS
        days[k] = days.get(k, 0) + 1
    pays = sorted((p for p in (salary_value(feed[sid].get("salary")) for sid in ids if sid in feed) if p > 0),
                  reverse=True)
    return {"hist": json.dumps(sorted(([k, n] for k, n in days.items()), reverse=True), separators=(",", ":")),
            "payq": json.dumps(pays[::payq_step], separators=(",", ":")), "payq_step": str(payq_step)}


def meta_statement(values: dict[str, str]) -> str:
    return ("INSERT INTO search_meta (k, v) VALUES "
            + ", ".join("(" + lit(k) + ", " + lit(str(v)) + ")" for k, v in values.items())
            + " ON CONFLICT(k) DO UPDATE SET v = excluded.v")


def usage_statement(day: str, written: int, read: int = 0) -> str:
    return ("INSERT INTO search_usage (day, rows_read, rows_written, requests) VALUES ("
            + lit(day) + ", " + str(int(read)) + ", " + str(int(written)) + ", 0) "
            "ON CONFLICT(day) DO UPDATE SET rows_read = rows_read + excluded.rows_read, "
            "rows_written = rows_written + excluded.rows_written")


# ---------------------------------------------------------------- state file
def load_state(path: Path) -> dict:
    """The last run's {"token", "rows": {source_id: [h, id, x]}, "extras": {hash: x}}, or {}."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("format") == 2 and isinstance(d.get("rows"), dict):
            return d
    except (OSError, ValueError, EOFError):
        pass
    return {}


def save_state(path: Path, token: str, rows: dict, extras: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"format": 2, "token": token, "rows": rows, "extras": extras}, fh,
                  separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------- D1 HTTP API
class D1:
    """The D1 query endpoint, one statement a request, spaced and retried. `last` holds the
    rows_read / rows_written D1 reported for the newest statement; `read` and `written` add them
    up for the run."""

    def __init__(self, account: str, database: str, token: str, *, spacing: float = 0.3,
                 url: str | None = None, opener=urllib.request.urlopen, sleep=time.sleep):
        self.url = url or API.format(account=account, db=database)
        self.token = token
        self.spacing = spacing
        self._open, self._sleep = opener, sleep
        self._last = 0.0
        self.requests = 0
        self.read = 0
        self.written = 0
        self.last = {"rows_read": 0, "rows_written": 0}

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
        for attempt in range(6):
            wait = self._last + self.spacing - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with self._open(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    self._sleep(min(60, 2 ** attempt * 2))
                    continue
                raise RuntimeError(f"D1 query failed: HTTP {e.code} {e.read()[:300]!r}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 5:
                    self._sleep(min(60, 2 ** attempt * 2))
                    continue
                raise
            if not data.get("success"):
                raise RuntimeError(f"D1 query failed: {json.dumps(data.get('errors'))[:300]}")
            res = (data.get("result") or [{}])[0]
            meta = res.get("meta") or {}
            self.last = {"rows_read": int(meta.get("rows_read") or 0),
                         "rows_written": int(meta.get("rows_written") or 0)}
            self.read += self.last["rows_read"]
            self.written += self.last["rows_written"]
            return res.get("results") or []
        raise RuntimeError("D1 query failed after retries")


def remote_meta(client) -> dict[str, str]:
    return {r["k"]: r["v"] for r in client.query("SELECT k, v FROM search_meta")}


def remote_state(client, page: int = 5000) -> tuple[dict, dict]:
    """({source_id: [h, id, x]}, {hash of the employer JSON: x}) as D1 holds them, read in keyset
    pages (reads only). Ids travel as text: they are past what JSON numbers hold exactly. Two
    rows for one source_id (a run that died between writing a moved row and removing its old id)
    keep the newer id; the older one is returned under a key no feed row has, so the next plan
    deletes it."""
    rows: dict[str, list] = {}
    before = None
    while True:
        got = client.query(
            "SELECT CAST(id AS TEXT) AS id, h, x, json_extract(r, '$[0]') AS sid FROM jobs "
            + ("WHERE id < CAST(? AS INTEGER) " if before else "") + "ORDER BY id DESC LIMIT ?",
            ([before] if before else []) + [page])
        for r in got:
            sid = str(r["sid"])
            if sid in rows:
                sid = f"\x00dup:{r['id']}"
            rows[sid] = [r["h"], int(r["id"]), int(r["x"])]
        if len(got) < page:
            break
        before = got[-1]["id"]
    extras = {_hash(r["v"]): int(r["x"]) for r in client.query("SELECT x, v FROM extras")}
    return rows, extras


def _utc(now: float | None) -> datetime:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)


# ---------------------------------------------------------------- main
def run(feed_path: Path, state_path: Path, *, dry_run: bool, client=None,
        daily_writes: int = DAILY_WRITES, max_writes: int = 0, now: float | None = None,
        out=sys.stdout) -> dict:
    """One load. `daily_writes` is the most rows the index may have written today in all (0 = no
    limit); `max_writes` caps this run alone (0 = no cap)."""
    feed = load_feed_file(feed_path)
    rows = feed.get("jobs") or []
    generated_at = str(feed.get("generated_at") or "")
    saved = load_state(state_path)
    state, extras = saved.get("rows") or {}, saved.get("extras") or {}
    note = ""
    meta: dict[str, str] = {}
    spent_today = 0
    day = _utc(now).strftime("%Y-%m-%d")
    if not dry_run:
        meta = remote_meta(client)
        if not saved or saved.get("token") != meta.get("load_token"):
            note = "state rebuilt from D1"
            state, extras = remote_state(client)
        used = client.query("SELECT rows_written FROM search_usage WHERE day = ?", [day])
        spent_today = int(used[0]["rows_written"]) if used else 0
    elif not saved:
        note = "no saved state: everything counts as added"
    dicts = Dicts.from_meta(meta)
    dicts.learn(rows)
    plan = Plan(rows, state, extras)
    report = {"rows": len(plan.feed), "added": plan.added, "changed": plan.changed,
              "removed": plan.removed, "unchanged": len(plan.feed) - plan.added - plan.changed,
              "deletes": len(plan.deletes), "new_extras": len(plan.new_extras)}
    print("[search-index] " + " ".join(f"{k}={v}" for k, v in report.items())
          + (f" ({note})" if note else "") + (" DRY RUN" if dry_run else ""), file=out)
    if dry_run:
        return report

    budget = None                                   # rows this run may still write; None = no limit
    if daily_writes > 0:
        budget = max(0, daily_writes - spent_today - RESERVE)
    if max_writes > 0:
        budget = max(0, max_writes - RESERVE) if budget is None else min(budget, max(0, max_writes - RESERVE))
    start_written = client.written
    cost = {"per_row": EST_WRITES_PER_ROW, "rows": 0, "written": 0}

    def room(n_rows: int) -> bool:
        return budget is None or (client.written - start_written) + n_rows * cost["per_row"] <= budget

    def rows_left():
        if budget is None:
            return None
        return max(1, int((budget - (client.written - start_written)) / cost["per_row"]))

    def learn_cost(n_rows: int) -> None:
        cost["rows"] += n_rows
        cost["written"] += client.last["rows_written"]
        if cost["rows"] >= 200:
            cost["per_row"] = max(0.5, cost["written"] / cost["rows"])

    first_load = meta.get("ready") != "1"
    work = bool(plan.pending or plan.new_extras or plan.dead_extras or dicts.dirty)
    token = meta.get("load_token") or ""
    done_deletes = done_upserts = 0
    stopped = ""
    live = {sid: list(v) for sid, v in state.items()}      # what D1 holds as the run goes on
    by_id = {int(v[1]): sid for sid, v in live.items()}
    live_extras = dict(extras)
    try:
        if work and not room(1):
            stopped = "write budget"
        elif work:
            # D1 says "dirty" while rows are being written: a run that dies here is noticed.
            token = "dirty:" + secrets.token_hex(6)
            client.query(meta_statement({"load_token": token,
                                         **(dicts.as_meta() if dicts.dirty else {})}))
            for sql in extras_statements(plan.new_extras):
                client.query(sql)
            live_extras = dict(plan.extras_ids)
            for sql, ids in delete_statements(plan.deletes):
                if not room(len(ids)):
                    stopped = "write budget"
                    break
                client.query(sql)
                learn_cost(len(ids))
                done_deletes += len(ids)
                for i in ids:
                    sid = by_id.pop(i, None)
                    if sid is not None and int(live[sid][1]) == i:
                        del live[sid]
            for group in (() if stopped else waves(plan, first_load, lambda: (
                    None if budget is None
                    else int((budget - (client.written - start_written)) / cost["per_row"])))):
                if not group:
                    stopped = "write budget"
                    break
                for sql, sids in upsert_statements(group, dicts, max_rows=rows_left):
                    if not room(len(sids)):
                        stopped = "write budget"
                        break
                    client.query(sql)
                    learn_cost(len(sids))
                    done_upserts += len(sids)
                    for sid in sids:
                        live[sid] = list(plan.target[sid])
                if stopped:
                    break
            if not stopped and plan.dead_extras:
                client.query("DELETE FROM extras WHERE x IN ("
                             + ", ".join(map(str, plan.dead_extras)) + ")")
                dead = set(plan.dead_extras)
                live_extras = {h: x for h, x in live_extras.items() if x not in dead}
        complete = not stopped
        wrote = bool(done_deletes or done_upserts)
        if token.startswith("dirty:") or not meta.get("index_version"):
            new_meta: dict[str, str] = {}
            if wrote or not meta.get("index_version"):
                # A new version retires old cursors (409) and old edge-cache entries.
                new_meta["index_version"] = hashlib.sha1(
                    f"{generated_at}|{meta.get('index_version')}|{token}".encode()).hexdigest()[:12]
            token = secrets.token_hex(8)
            new_meta.update({
                "load_token": token, "generated_at": generated_at, "rows": str(len(live)),
                "sponsor_rows": str(sum(1 for sid in live
                                        if sid in plan.feed and in_sponsor_scope(plan.feed[sid]))),
                "pending": str(plan.pending - done_deletes - done_upserts),
                **histograms({sid: v[1] for sid, v in live.items()}, plan.feed),
                "loaded_at": _utc(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            if complete:
                new_meta["ready"] = "1"             # the first full load is in: the API may answer
            client.query(meta_statement(new_meta))
            if wrote:
                # Fold the keyword index's small new segments together, a bounded amount of work
                # (D1 counts it as one row written), so the index stays compact as it is fed.
                client.query("INSERT INTO jobs_fts(jobs_fts, rank) VALUES('merge', 2000)")
            if wrote and complete:
                client.query("PRAGMA optimize")     # refresh the planner's statistics (cheap)
        written = client.written - start_written
        if written:
            client.query(usage_statement(day, written))
            written = client.written - start_written
    except Exception:
        # D1 still says "dirty" (or holds rows this file does not know): the next run reads the
        # state back from D1 and carries on.
        state_path.unlink(missing_ok=True)
        raise
    save_state(state_path, token, live, live_extras)
    report.update({"applied_deletes": done_deletes, "applied_upserts": done_upserts,
                   "left": plan.pending - done_deletes - done_upserts,
                   # For the usage snapshot (scripts/cf_usage_snapshot.py): first-load progress.
                   "indexed": len(live), "ready": complete or not first_load,
                   "per_row": round(cost["per_row"], 3),
                   "rows_written": written, "written_today": spent_today + written,
                   "requests": client.requests, "complete": complete})
    print(f"[search-index] {'loaded' if complete else 'stopped at the ' + stopped}: "
          f"deletes={done_deletes} upserts={done_upserts} left={report['left']} "
          f"rows_written={written} today={report['written_today']}"
          + (f"/{daily_writes}" if daily_writes else "") + f" requests={client.requests}", file=out)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--feed", required=True, type=Path, help="the built jobs.json.gz")
    ap.add_argument("--state", required=True, type=Path,
                    help="last loaded state (written after every run)")
    ap.add_argument("--dry-run", action="store_true", help="print the diff counts; no network")
    ap.add_argument("--daily-writes", type=int,
                    default=int(os.environ.get("SEARCH_D1_DAILY_WRITES") or DAILY_WRITES),
                    help="most D1 rows the index may write in one UTC day (0 = no limit)")
    ap.add_argument("--max-writes", type=int,
                    default=int(os.environ.get("SEARCH_D1_MAX_WRITES") or 0),
                    help="most D1 rows this run may write (0 = no cap)")
    a = ap.parse_args(argv)
    client = None
    if not a.dry_run:
        url = os.environ.get("SEARCH_D1_API_URL") or None      # a local stand-in, for testing
        env = {k: os.environ.get(k, "") for k in
               ("SEARCH_D1_ACCOUNT_ID", "SEARCH_D1_DATABASE_ID", "SEARCH_D1_TOKEN")}
        missing = [k for k, v in env.items() if not v]
        if missing and not url:
            print("[search-index] not configured, missing: " + ", ".join(missing), file=sys.stderr)
            return 2
        client = D1(env["SEARCH_D1_ACCOUNT_ID"], env["SEARCH_D1_DATABASE_ID"], env["SEARCH_D1_TOKEN"],
                    url=url, spacing=0.0 if url else 0.3)
    report = None
    try:
        report = run(a.feed, a.state, dry_run=a.dry_run, client=client, daily_writes=a.daily_writes,
                     max_writes=a.max_writes)
    finally:
        # Yesterday's Cloudflare usage for the admin dashboard, one row a day in the same database.
        # It runs after a failed load too (with no progress) and can never fail the step itself.
        if client is not None and os.environ.get("SEARCH_CF_USAGE", "1") != "0":
            from scripts.cf_usage_snapshot import Cloudflare, snapshot  # noqa: PLC0415
            gql = os.environ.get("SEARCH_CF_GRAPHQL_URL") or None        # a local stand-in, for testing
            cf = Cloudflare(env["SEARCH_D1_ACCOUNT_ID"], env["SEARCH_D1_TOKEN"], graphql_url=gql) if gql else None
            snapshot(client, account=env["SEARCH_D1_ACCOUNT_ID"], token=env["SEARCH_D1_TOKEN"], report=report,
                     daily_writes=a.daily_writes, cf=cf, lit=lit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
