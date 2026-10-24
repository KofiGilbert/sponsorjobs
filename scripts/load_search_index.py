#!/usr/bin/env python3
"""Load the built feed into the server-side search index (D1), writing only what changed.

    SEARCH_D1_ACCOUNT_ID=... SEARCH_D1_DATABASE_ID=... SEARCH_D1_TOKEN=... \\
        python scripts/load_search_index.py --feed .site/feed/jobs.json.gz \\
            --state .feed-data/search-index-state.json.gz
    python scripts/load_search_index.py --feed ... --state ... --dry-run   # counts only, no network

docs/server-search-plan.md (phase 2). The feed rebuilds every 3 hours and most rows do not change,
so rewriting the whole index each time would cost ~120M D1 row writes a month at 500k rows (plus
the FTS writes). Instead the loader keeps the last loaded state (source_id -> row hash) next to the
crawl state, compares the new feed with it, and sends only:

  DELETE  the rows that left the feed
  UPSERT  the rows that are new or changed (INSERT ... ON CONFLICT DO UPDATE, so the FTS triggers
          in worker/migrations/0006_job_search.sql keep the index in step)

then sets index_version, which retires old cursors (409) and old edge-cache entries.

Every derived column is computed here with the app's own functions (sourcing/filters.salary_value
and _parse_posted, feedfile.recency_key, Python's lower()), so the Worker compares precomputed
values and filters exactly as apply_facets does.

Safety: the state file is written only after every statement succeeded. On any failure it is
deleted, and the next run rebuilds the state from D1 itself (SELECT source_id, h: cheap reads, no
writes), so a half-applied diff is finished next time and never leaves orphan rows. The same
rebuild runs when the saved state's index_version is not the one D1 holds (a lost cache save).

The D1 HTTP API takes at most 100 bound parameters per query, so rows are sent as SQL literals
(strings quoted the SQLite way: ' doubled; nothing else is special), many rows per statement, each
statement under D1's 100 KB limit. Requests are spaced (Cloudflare's API allows 1,200 per 5 minutes
per token) and retried on 429 / 5xx.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing.feedfile import canon, load_feed_file  # noqa: E402
from sourcing.filters import _parse_posted, salary_value  # noqa: E402

COLUMNS = ("source_id", "h", "rkey", "pay", "posted_ts", "remote_l", "entry", "sponsored",
           "visa_any", "visa_codes", "title_l", "company_l", "location_l", "xh", "row")
EXTRA_KEYS = ("sponsor", "visa", "nationality_visas")   # per-employer, stored once in job_extras
MAX_STATEMENT_BYTES = 90_000        # D1 caps one SQL statement at 100 KB
DELETE_PER_STATEMENT = 500
API = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db}/query"
SEP = "\x01"                        # sorts below every character the recency fields hold


# ---------------------------------------------------------------- one row -> one index record
def _text(v) -> str:
    """A column value as text SQLite will store: NUL cannot sit inside a literal."""
    return str(v or "").replace("\x00", "")


def rkey(row: dict) -> str:
    """feedfile.recency_key as one string: ORDER BY rkey DESC is the board's order."""
    posted = str(row.get("posted_at") or "")
    return ("1" if posted else "0") + posted + SEP + str(row.get("first_seen") or "") + SEP \
        + str(row.get("source_id") or "")


def _visa_codes(row: dict) -> str:
    codes = [v.get("code") for v in (row.get("visa") or []) + (row.get("nationality_visas") or [])
             if isinstance(v, dict) and isinstance(v.get("code"), str) and v.get("code")]
    # A facet code never holds a comma (the query string is split on commas), so "," delimits.
    return "," + ",".join(dict.fromkeys(codes)) + "," if codes else ""


def _sponsored(row: dict) -> bool:
    """apply_facets' sponsored test, verbatim."""
    st = row.get("ad_stance")
    return st == "offered" or (st is None and row.get("sponsorship_stated") is True)


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def record(row: dict) -> dict:
    """The index columns for one slim feed row, plus "_extra": (hash, JSON) of its employer
    fields when it has any (written to job_extras)."""
    body = canon(row)
    extra = {k: row[k] for k in EXTRA_KEYS if k in row}
    extra_json = canon(extra) if extra else ""
    slim = canon({k: v for k, v in row.items() if k not in EXTRA_KEYS}) if extra else body
    dt = _parse_posted(row.get("posted_at") or "")
    return {
        "source_id": _text(row.get("source_id")),
        "h": _hash(body),
        "rkey": _text(rkey(row)),
        "pay": salary_value(row.get("salary")),
        "posted_ts": int(dt.timestamp()) if dt is not None else None,
        "remote_l": _text((row.get("remote") or "").lower()),
        "entry": 1 if row.get("entry_level") else 0,
        "sponsored": 1 if _sponsored(row) else 0,
        "visa_any": 1 if (row.get("visa") or []) else 0,
        "visa_codes": _text(_visa_codes(row)),
        "title_l": _text((row.get("title") or "").lower()),
        "company_l": _text((row.get("company") or "").lower()),
        "location_l": _text((row.get("location") or "").lower()),
        "xh": _hash(extra_json) if extra_json else "",
        "row": slim.replace("\x00", ""),
        "_extra": (_hash(extra_json), extra_json.replace("\x00", "")) if extra_json else None,
    }


# ---------------------------------------------------------------- the diff
def diff(rows: list[dict], state: dict[str, str]) -> tuple[list[dict], list[str], dict[str, str]]:
    """(upserts, removed source_ids, new state) for the feed `rows` against the loaded `state`."""
    upserts, new_state = [], {}
    for r in rows:
        sid = r.get("source_id")
        if not sid or sid in new_state:
            continue
        rec = record(r)
        new_state[rec["source_id"]] = rec["h"]
        if state.get(rec["source_id"]) != rec["h"]:
            upserts.append(rec)
    removed = sorted(sid for sid in state if sid not in new_state)
    return upserts, removed, new_state


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


_UPSERT_HEAD = ("INSERT INTO jobs (" + ", ".join(COLUMNS) + ") VALUES ")
_UPSERT_TAIL = (" ON CONFLICT(source_id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "source_id"))


def statements(upserts: list[dict], removed: list[str], *,
               max_bytes: int = MAX_STATEMENT_BYTES) -> list[str]:
    """Deletes first (so a re-added id is a clean insert), then the upserts, many rows a statement."""
    out = []
    for i in range(0, len(removed), DELETE_PER_STATEMENT):
        chunk = removed[i:i + DELETE_PER_STATEMENT]
        out.append("DELETE FROM jobs WHERE source_id IN (" + ", ".join(lit(s) for s in chunk) + ")")
    # The employer fields the upserted rows point at, each once. A value D1 already holds is a
    # no-op (INSERT OR IGNORE writes nothing); orphans are swept after the upserts.
    extras = {}
    for rec in upserts:
        if rec.get("_extra"):
            extras.setdefault(rec["_extra"][0], rec["_extra"][1])
    head = "INSERT OR IGNORE INTO job_extras (h, v) VALUES "
    values: list[str] = []
    size = 0
    for h, v in extras.items():
        item = "(" + lit(h) + ", " + lit(v) + ")"
        n = len(item.encode("utf-8")) + 2
        if values and size + n > max_bytes - len(head):
            out.append(head + ", ".join(values))
            values, size = [], 0
        values.append(item)
        size += n
    if values:
        out.append(head + ", ".join(values))
    budget = max_bytes - len(_UPSERT_HEAD) - len(_UPSERT_TAIL.encode())
    values, size = [], 0
    for rec in upserts:
        v = "(" + ", ".join(lit(rec[c]) for c in COLUMNS) + ")"
        n = len(v.encode("utf-8")) + 2
        if values and size + n > budget:
            out.append(_UPSERT_HEAD + ", ".join(values) + _UPSERT_TAIL)
            values, size = [], 0
        values.append(v)
        size += n
    if values:
        out.append(_UPSERT_HEAD + ", ".join(values) + _UPSERT_TAIL)
    if out:
        out.append(SWEEP_EXTRAS)
    return out


# Employer values no row points at any more (reads every row once: cheap; writes only orphans).
SWEEP_EXTRAS = "DELETE FROM job_extras WHERE h NOT IN (SELECT xh FROM jobs)"


def meta_statement(index_version: str, generated_at: str, rows: int = 0) -> str:
    return ("INSERT INTO search_meta (k, v) VALUES ('index_version', " + lit(index_version)
            + "), ('generated_at', " + lit(generated_at or "") + "), ('rows', " + lit(str(rows)) + ") "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v")


# ---------------------------------------------------------------- state file
def load_state(path: Path) -> tuple[dict[str, str], str | None]:
    """({source_id: hash}, index_version) of the last successful load, or ({}, None)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        return dict(d.get("rows") or {}), d.get("index_version")
    except (OSError, ValueError, EOFError):
        return {}, None


def save_state(path: Path, rows: dict[str, str], index_version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({"index_version": index_version, "rows": rows}, fh, separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------- D1 HTTP API
class D1:
    """The D1 query endpoint, one statement a request, spaced and retried."""

    def __init__(self, account: str, database: str, token: str, *, spacing: float = 0.3,
                 opener=urllib.request.urlopen, sleep=time.sleep):
        self.url = API.format(account=account, db=database)
        self.token = token
        self.spacing = spacing
        self._open, self._sleep = opener, sleep
        self._last = 0.0
        self.requests = 0

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
            res = data.get("result") or [{}]
            return res[0].get("results") or []
        raise RuntimeError("D1 query failed after retries")

    def remote_version(self) -> str | None:
        rows = self.query("SELECT v FROM search_meta WHERE k = 'index_version'")
        return rows[0]["v"] if rows else None

    def remote_state(self, page: int = 5000) -> dict[str, str]:
        """{source_id: h} as D1 holds it, read in keyset pages (reads only)."""
        out: dict[str, str] = {}
        after = ""
        while True:
            rows = self.query("SELECT source_id, h FROM jobs WHERE source_id > ? "
                              "ORDER BY source_id LIMIT ?", [after, page])
            for r in rows:
                out[r["source_id"]] = r["h"]
            if len(rows) < page:
                return out
            after = rows[-1]["source_id"]


# ---------------------------------------------------------------- main
def run(feed_path: Path, state_path: Path, *, dry_run: bool, client: D1 | None = None,
        emit_sql: Path | None = None, out=sys.stdout) -> dict:
    feed = load_feed_file(feed_path)
    rows = feed.get("jobs") or []
    generated_at = str(feed.get("generated_at") or "")
    state, version = load_state(state_path)
    note = ""
    if not dry_run:
        remote_v = client.remote_version()
        if not state or version != remote_v:
            note = "state rebuilt from D1"
            state = client.remote_state()
    elif not state:
        note = "no saved state: everything counts as added"
    upserts, removed, new_state = diff(rows, state)
    sqls = statements(upserts, removed)
    added = sum(1 for r in upserts if r["source_id"] not in state)
    report = {"rows": len(new_state), "added": added, "changed": len(upserts) - added,
              "removed": len(removed), "unchanged": len(new_state) - len(upserts),
              "statements": len(sqls), "sql_bytes": sum(len(s.encode("utf-8")) for s in sqls)}
    new_version = hashlib.sha1(f"{generated_at}|{version}|{len(upserts)}|{len(removed)}"
                               .encode()).hexdigest()[:12] if sqls else (version or "")
    if emit_sql:
        with open(emit_sql, "w", encoding="utf-8") as fh:
            for s in sqls + [meta_statement(new_version or "v0", generated_at, len(new_state))]:
                fh.write(s + ";\n")
    print("[search-index] " + " ".join(f"{k}={v}" for k, v in report.items())
          + (f" ({note})" if note else "") + (" DRY RUN" if dry_run else ""), file=out)
    if dry_run:
        return report
    try:
        for s in sqls:
            client.query(s)
        if sqls or not version:
            client.query(meta_statement(new_version or "v0", generated_at, len(new_state)))
        if sqls:
            client.query("PRAGMA optimize")      # refresh the planner's statistics (cheap)
    except Exception:
        state_path.unlink(missing_ok=True)       # next run rebuilds from D1 and finishes the job
        raise
    save_state(state_path, new_state, new_version or "v0")
    report["index_version"] = new_version or "v0"
    report["requests"] = client.requests
    print(f"[search-index] loaded: index_version={report['index_version']} "
          f"requests={client.requests}", file=out)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--feed", required=True, type=Path, help="the built jobs.json.gz")
    ap.add_argument("--state", required=True, type=Path,
                    help="last loaded state (written after a successful load)")
    ap.add_argument("--dry-run", action="store_true", help="print the diff counts; no network")
    ap.add_argument("--emit-sql", type=Path, help="also write the statements to this file")
    a = ap.parse_args(argv)
    client = None
    if not a.dry_run:
        env = {k: os.environ.get(k, "") for k in
               ("SEARCH_D1_ACCOUNT_ID", "SEARCH_D1_DATABASE_ID", "SEARCH_D1_TOKEN")}
        missing = [k for k, v in env.items() if not v]
        if missing:
            print("[search-index] not configured, missing: " + ", ".join(missing), file=sys.stderr)
            return 2
        client = D1(env["SEARCH_D1_ACCOUNT_ID"], env["SEARCH_D1_DATABASE_ID"], env["SEARCH_D1_TOKEN"])
    run(a.feed, a.state, dry_run=a.dry_run, client=client, emit_sql=a.emit_sql)
    return 0


if __name__ == "__main__":
    sys.exit(main())
