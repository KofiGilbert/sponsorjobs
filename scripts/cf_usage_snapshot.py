#!/usr/bin/env python3
"""A daily snapshot of how much of Cloudflare's FREE plan the account used, for the admin dashboard
("Cloudflare Free Plan" on the Health page, and its "Time to switch to Workers Paid" banner:
worker/src/admin_cf.ts).

Run by the search loader (scripts/load_search_index.py) at the end of every load, with the same
Cloudflare token: it asks the GraphQL Analytics API for the previous UTC day's totals for the
WHOLE account and writes ONE row a day (`cf_usage`, worker/migrations-search/0002_cf_usage.sql)
into the search database. Serving a request never writes it, so tracking costs nothing per request.

    Workers requests     workersInvocationsAdaptive, every script     limit 100,000 a day
    D1 rows read         d1AnalyticsAdaptiveGroups, every database     limit 5 million a day
    D1 rows written      d1AnalyticsAdaptiveGroups, every database     limit 100,000 a day
    D1 storage           d1StorageAdaptiveGroups, per database         limit 5 GB in all, 500 MB each

The token needs "Account Analytics Read" for this (and "D1 Edit" for the loader itself). The names
of the databases come from the D1 list endpoint (the D1 permission covers it). Nothing here can
fail the load: a query that is refused (a missing permission, an outage) is logged as a warning,
the row is still written with what was read plus the load's progress, and a later run that day
fills in the rest (a NULL never replaces a number already stored).

Docs: https://developers.cloudflare.com/analytics/graphql-api/ ,
https://developers.cloudflare.com/d1/observability/metrics-analytics/ ,
https://developers.cloudflare.com/analytics/graphql-api/tutorials/querying-workers-metrics/
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
D1_LIST = "https://api.cloudflare.com/client/v4/accounts/{account}/d1/database?per_page=100"
_ACCOUNT = re.compile(r"^[0-9a-fA-F]{32}$")

COLUMNS = ("day", "taken_at", "worker_requests", "d1_rows_read", "d1_rows_written", "d1_storage_bytes",
           "d1_largest_bytes", "d1_largest_name", "d1_databases", "load_rows", "load_total",
           "load_per_day", "load_ready", "warning")


def _queries(account: str, day: str) -> dict[str, str]:
    """One GraphQL query per dataset, so one that is refused does not lose the others. The values
    are put in the text (an account id we checked, and dates we made), so no variable types."""
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    acct = f'accounts(filter: {{accountTag: "{account}"}})'
    return {
        "workers": (f'{{ viewer {{ {acct} {{ workersInvocationsAdaptive(limit: 10000, filter: '
                    f'{{datetime_geq: "{day}T00:00:00Z", datetime_lt: "{nxt}T00:00:00Z"}}) '
                    f'{{ sum {{ requests }} dimensions {{ scriptName }} }} }} }} }}'),
        "d1": (f'{{ viewer {{ {acct} {{ d1AnalyticsAdaptiveGroups(limit: 10000, filter: '
               f'{{date_geq: "{day}", date_leq: "{day}"}}) '
               f'{{ sum {{ rowsRead rowsWritten }} dimensions {{ databaseId }} }} }} }} }}'),
        "storage": (f'{{ viewer {{ {acct} {{ d1StorageAdaptiveGroups(limit: 10000, filter: '
                    f'{{date_geq: "{day}", date_leq: "{day}"}}) '
                    f'{{ max {{ databaseSizeBytes }} dimensions {{ databaseId }} }} }} }} }}'),
    }


class Cloudflare:
    """The two calls the snapshot makes, with the loader's token. `opener` is swapped in tests."""

    def __init__(self, account: str, token: str, *, opener=None,
                 graphql_url: str = GRAPHQL, list_url: str = D1_LIST):
        self.account, self.token = account, token
        self._open = opener or urllib.request.urlopen
        self.graphql_url, self.list_url = graphql_url, list_url

    def _call(self, req: urllib.request.Request) -> dict:
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read()[:200].decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - only for the message
                pass
            raise RuntimeError(f"HTTP {e.code} {body}".strip()) from None

    def graphql(self, query: str) -> dict:
        data = self._call(urllib.request.Request(
            self.graphql_url, data=json.dumps({"query": query}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"}))
        if data.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in data["errors"] if isinstance(e, dict)) or str(data["errors"])
            raise RuntimeError(msg[:200])
        accounts = ((data.get("data") or {}).get("viewer") or {}).get("accounts") or []
        if not accounts:
            raise RuntimeError("the account was not found (check the account id and the token)")
        return accounts[0]

    def databases(self) -> dict[str, dict]:
        data = self._call(urllib.request.Request(self.list_url.format(account=self.account), method="GET"))
        if not data.get("success", True):
            raise RuntimeError(json.dumps(data.get("errors"))[:200])
        return {str(d.get("uuid")): d for d in data.get("result") or [] if d.get("uuid")}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def read_usage(cf: Cloudflare, day: str) -> tuple[dict, list[str]]:
    """({column: value} for the day's account totals, [warnings]). A dataset that cannot be read
    leaves its columns out (None) and adds a plain-words warning."""
    out: dict = {}
    warnings: list[str] = []
    q = _queries(cf.account, day)
    per_db: dict[str, dict] = {}
    try:
        rows = cf.graphql(q["workers"]).get("workersInvocationsAdaptive") or []
        out["worker_requests"] = sum(_int((r.get("sum") or {}).get("requests")) for r in rows)
    except Exception as e:  # noqa: BLE001 - never fails the load
        warnings.append(f"Workers requests: {e}")
    try:
        rows = cf.graphql(q["d1"]).get("d1AnalyticsAdaptiveGroups") or []
        for r in rows:
            db = str((r.get("dimensions") or {}).get("databaseId") or "")
            s = r.get("sum") or {}
            d = per_db.setdefault(db, {"id": db})
            d["rows_read"] = d.get("rows_read", 0) + _int(s.get("rowsRead"))
            d["rows_written"] = d.get("rows_written", 0) + _int(s.get("rowsWritten"))
        out["d1_rows_read"] = sum(d.get("rows_read", 0) for d in per_db.values())
        out["d1_rows_written"] = sum(d.get("rows_written", 0) for d in per_db.values())
    except Exception as e:  # noqa: BLE001
        warnings.append(f"D1 rows: {e}")
    names: dict[str, dict] = {}
    try:
        names = cf.databases()
    except Exception as e:  # noqa: BLE001 - names are a nicety; storage below may still be read
        warnings.append(f"D1 database names: {e}")
    sizes: dict[str, int] = {}
    try:
        for r in cf.graphql(q["storage"]).get("d1StorageAdaptiveGroups") or []:
            db = str((r.get("dimensions") or {}).get("databaseId") or "")
            sizes[db] = max(sizes.get(db, 0), _int((r.get("max") or {}).get("databaseSizeBytes")))
    except Exception as e:  # noqa: BLE001
        # The list endpoint reports each database's current size: use it when analytics is refused.
        listed = {k: _int(v.get("file_size")) for k, v in names.items() if v.get("file_size") is not None}
        if listed:
            sizes = listed
        else:
            warnings.append(f"D1 storage: {e}")
    for db, b in sizes.items():
        per_db.setdefault(db, {"id": db})["bytes"] = b
    for db, d in per_db.items():
        if db in names and names[db].get("name"):
            d["name"] = str(names[db]["name"])
    if sizes:
        out["d1_storage_bytes"] = sum(sizes.values())
        big = max(sizes, key=lambda k: sizes[k])
        out["d1_largest_bytes"] = sizes[big]
        out["d1_largest_name"] = per_db.get(big, {}).get("name") or big
    if per_db:
        out["d1_databases"] = json.dumps(sorted(per_db.values(), key=lambda d: d["id"]), separators=(",", ":"))
    return out, warnings


def progress(report: dict | None, daily_writes: int) -> dict:
    """The search index's first-load progress, from the loader's report: jobs in the index, jobs
    in the feed, about how many jobs a day's write budget loads, and whether it is done."""
    if not report or "indexed" not in report:
        return {}
    per_row = float(report.get("per_row") or 0) or 3.0
    return {"load_rows": int(report["indexed"]), "load_total": int(report.get("rows") or 0),
            "load_per_day": int(daily_writes / per_row) if daily_writes > 0 else None,
            "load_ready": 1 if report.get("ready") else 0}


def upsert_statement(values: dict, lit) -> str:
    """One row a day: later runs that day refresh it, and a NULL never replaces a stored number."""
    cols = [c for c in COLUMNS if c in values]
    keep = {"day"}
    sets = ", ".join(f"{c} = COALESCE(excluded.{c}, cf_usage.{c})" if c not in ("taken_at", "warning")
                     else f"{c} = excluded.{c}" for c in cols if c not in keep)
    return (f"INSERT INTO cf_usage ({', '.join(cols)}) VALUES ({', '.join(lit(values[c]) for c in cols)}) "
            f"ON CONFLICT(day) DO UPDATE SET {sets}")


def snapshot(client, *, account: str, token: str, report: dict | None, daily_writes: int,
             now: float | None = None, cf: Cloudflare | None = None, lit=None, out=sys.stdout) -> dict | None:
    """Read yesterday's account totals and write the day's row. Returns the row written, or None
    when even the write failed. Never raises: a snapshot must never fail the search load."""
    if lit is None:
        from scripts.load_search_index import lit  # noqa: PLC0415 - the loader's own SQL quoting
    t = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    day = (t - timedelta(days=1)).strftime("%Y-%m-%d")
    values: dict = {"day": day, "taken_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "warning": None}
    warnings: list[str] = []
    if not _ACCOUNT.match(account or ""):
        warnings.append("Cloudflare usage: no valid account id, so it was not read")
    elif not token:
        warnings.append("Cloudflare usage: no token, so it was not read")
    else:
        try:
            got, warnings = read_usage(cf or Cloudflare(account, token), day)
            values.update(got)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Cloudflare usage: {e}")
    values.update(progress(report, daily_writes))
    if warnings:
        values["warning"] = "; ".join(warnings)[:600]
        # "::warning::" puts it on the workflow run's summary page in GitHub Actions.
        print("::warning title=Cloudflare usage::[cf-usage] could not read all of the Cloudflare "
              f"usage for {day}: " + "; ".join(warnings) + " (the search load is not affected; the "
              "token needs D1 Edit and Account Analytics Read)", file=out)
    try:
        client.query(upsert_statement(values, lit))
    except Exception as e:  # noqa: BLE001
        print(f"::warning title=Cloudflare usage::[cf-usage] the usage snapshot was not saved: {e}", file=out)
        return None
    shown = {k: values.get(k) for k in ("worker_requests", "d1_rows_read", "d1_rows_written",
                                        "d1_storage_bytes", "load_rows", "load_total")}
    print(f"[cf-usage] {day}: " + " ".join(f"{k}={v}" for k, v in shown.items()), file=out)
    return values

