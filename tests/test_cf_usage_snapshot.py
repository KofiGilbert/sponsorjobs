"""The daily Cloudflare usage snapshot (scripts/cf_usage_snapshot.py) the search loader writes for the
admin dashboard's "Cloudflare Free Plan" card. Cloudflare's GraphQL Analytics API and the D1 list
endpoint are stubbed; the row is written to a real SQLite with the search migrations. No network."""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from scripts import cf_usage_snapshot as S
from scripts import load_search_index as L

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "worker" / "migrations-search").glob("*.sql"))
ACCOUNT = "0123456789abcdef0123456789abcdef"
SEARCH_DB, BROKER_DB = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 11, 26, 3, 23, tzinfo=timezone.utc).timestamp()     # the 25th is "yesterday"


class FakeD1:
    """The loader's D1 client on a local SQLite with every search migration."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        for m in MIGRATIONS:
            self.db.executescript(m.read_text(encoding="utf-8"))
        self.requests = self.read = self.written = 0
        self.last = {"rows_read": 0, "rows_written": 0}

    def query(self, sql, params=None):
        self.requests += 1
        before = self.db.total_changes
        cur = self.db.execute(sql, params or [])
        cols = [c[0] for c in cur.description or []]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        w = 0 if sql.lstrip().upper().startswith("SELECT") else self.db.total_changes - before
        self.last = {"rows_read": len(out), "rows_written": w}
        self.written += w
        return out

    def usage(self):
        cur = self.db.execute("SELECT * FROM cf_usage")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def cloudflare(*, refuse=(), seen=None):
    """A stubbed urlopen for GraphQL (by dataset) and the D1 list. `refuse` names what answers
    with an error: "graphql" (every query, as a token without Account Analytics Read), a dataset
    name, or "list"."""

    def opener(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append((url, req.data.decode() if req.data else "", req.get_header("Authorization")))
        if "/d1/database" in url:
            if "list" in refuse:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"errors":[{"message":"no"}]}'))
            return _Resp(json.dumps({"success": True, "result": [
                {"uuid": SEARCH_DB, "name": "sponsorjobs-search", "file_size": 300_000_000},
                {"uuid": BROKER_DB, "name": "sponsorjobs-broker", "file_size": 2_000_000}]}).encode())
        q = json.loads(req.data)["query"]
        assert ACCOUNT in q and "2026-11-25" in q
        if "graphql" in refuse or any(name in q for name in refuse):
            return _Resp(json.dumps({"data": None, "errors": [
                {"message": "not authorized for that account"}]}).encode())
        if "workersInvocationsAdaptive" in q:
            assert 'datetime_geq: "2026-11-25T00:00:00Z", datetime_lt: "2026-11-26T00:00:00Z"' in q
            data = {"workersInvocationsAdaptive": [
                {"sum": {"requests": 41_000}, "dimensions": {"scriptName": "sponsorjobs-broker"}},
                {"sum": {"requests": 1_500}, "dimensions": {"scriptName": "site-worker"}}]}
        elif "d1AnalyticsAdaptiveGroups" in q:
            data = {"d1AnalyticsAdaptiveGroups": [
                {"sum": {"rowsRead": 2_000_000, "rowsWritten": 79_000}, "dimensions": {"databaseId": SEARCH_DB}},
                {"sum": {"rowsRead": 50_000, "rowsWritten": 3_000}, "dimensions": {"databaseId": BROKER_DB}}]}
        else:
            data = {"d1StorageAdaptiveGroups": [
                {"max": {"databaseSizeBytes": 310_000_000}, "dimensions": {"databaseId": SEARCH_DB}},
                {"max": {"databaseSizeBytes": 2_100_000}, "dimensions": {"databaseId": BROKER_DB}}]}
        return _Resp(json.dumps({"data": {"viewer": {"accounts": [data]}}, "errors": None}).encode())

    return opener


REPORT = {"rows": 500_000, "indexed": 96_000, "ready": False, "per_row": 2.5}


def _snap(d1, opener, report=REPORT, now=NOW):
    out = io.StringIO()
    row = S.snapshot(d1, account=ACCOUNT, token="tok", report=report, daily_writes=80_000, now=now,
                     cf=S.Cloudflare(ACCOUNT, "tok", opener=opener), lit=L.lit, out=out)
    return row, out.getvalue()


def test_analytics_success_writes_one_row_for_yesterday_with_the_account_totals():
    d1, seen = FakeD1(), []
    row, log = _snap(d1, cloudflare(seen=seen))
    assert row is not None and "WARNING" not in log and "::warning" not in log
    [u] = d1.usage()
    assert u["day"] == "2026-11-25" and u["taken_at"] == "2026-11-26T03:23:00Z"
    assert u["worker_requests"] == 42_500                       # every script on the account
    assert u["d1_rows_read"] == 2_050_000 and u["d1_rows_written"] == 82_000   # every database
    assert u["d1_storage_bytes"] == 312_100_000
    assert u["d1_largest_bytes"] == 310_000_000 and u["d1_largest_name"] == "sponsorjobs-search"
    dbs = {d["name"]: d for d in json.loads(u["d1_databases"])}
    assert dbs["sponsorjobs-broker"] == {"id": BROKER_DB, "name": "sponsorjobs-broker", "rows_read": 50_000,
                                         "rows_written": 3_000, "bytes": 2_100_000}
    # First-load progress: 96,000 of 500,000 jobs, 80,000 writes a day at 2.5 a job = 32,000 a day.
    assert (u["load_rows"], u["load_total"], u["load_per_day"], u["load_ready"]) == (96_000, 500_000, 32_000, 0)
    assert u["warning"] is None
    assert all(auth == "Bearer tok" for _, _, auth in seen)
    assert sum("graphql" in url for url, _, _ in seen) == 3     # one query a dataset
    # Every run that day refreshes the SAME row: one row a day, never one per run.
    _snap(d1, cloudflare(), report=dict(REPORT, indexed=128_000), now=NOW + 3 * 3600)
    [u] = d1.usage()
    assert u["load_rows"] == 128_000 and u["taken_at"] == "2026-11-26T06:23:00Z"


def test_analytics_refused_still_writes_the_progress_and_warns_without_raising():
    d1 = FakeD1()
    row, log = _snap(d1, cloudflare(refuse=("graphql", "list")))
    assert row is not None
    assert "::warning" in log and "Account Analytics Read" in log
    [u] = d1.usage()
    assert u["worker_requests"] is None and u["d1_rows_read"] is None and u["d1_storage_bytes"] is None
    assert "not authorized" in u["warning"] and "Workers requests" in u["warning"]
    assert u["load_rows"] == 96_000 and u["load_total"] == 500_000          # progress still recorded


def test_storage_falls_back_to_the_d1_list_and_a_null_never_replaces_a_number():
    d1 = FakeD1()
    _snap(d1, cloudflare())
    _snap(d1, cloudflare(refuse=("graphql",)), now=NOW + 3600)               # a later run, refused
    [u] = d1.usage()
    assert u["worker_requests"] == 42_500 and u["d1_rows_written"] == 82_000   # kept
    assert u["d1_storage_bytes"] == 302_000_000                              # the list's file_size
    assert "Workers requests" in u["warning"] and "D1 storage" not in u["warning"]
    _snap(d1, cloudflare(), now=NOW + 7200)                                  # works again: warning gone
    assert d1.usage()[0]["warning"] is None


def test_a_failing_save_or_a_broken_opener_never_raises():
    class Broken(FakeD1):
        def query(self, sql, params=None):
            raise RuntimeError("D1 query failed: HTTP 500")

    def boom(req, timeout=None):
        raise OSError("network down")

    row, log = _snap(Broken(), boom)
    assert row is None and "not saved" in log
    row, log = _snap(FakeD1(), boom)
    assert row is not None and "network down" in row["warning"]


def _feed(path: Path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"feed_version": 1, "generated_at": "2026-11-25T00:00:00+00:00", "count": len(rows), "jobs": rows}, fh)
    return path


def _row(i):
    return {"source_id": f"gh:x:{i}", "source": "greenhouse", "company": "Acme", "title": f"Engineer {i}",
            "location": "Austin, TX", "remote": "", "url": f"https://x/{i}", "posted_at": "2026-10-01",
            "first_seen": "2026-10-01 10:00:00", "salary": "$120k/yr", "us": True}


def test_the_loader_loads_and_snapshots_and_a_refused_analytics_query_does_not_fail_it(tmp_path, monkeypatch):
    d1 = FakeD1()
    monkeypatch.setattr(L, "D1", lambda *a, **k: d1)
    for k, v in {"SEARCH_D1_ACCOUNT_ID": ACCOUNT, "SEARCH_D1_DATABASE_ID": SEARCH_DB, "SEARCH_D1_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SEARCH_D1_API_URL", raising=False)
    monkeypatch.delenv("SEARCH_CF_USAGE", raising=False)
    monkeypatch.setattr(S.urllib.request, "urlopen", cloudflare(refuse=("graphql", "list")))
    feed = _feed(tmp_path / "jobs.json.gz", [_row(i) for i in range(30)])
    assert L.main(["--feed", str(feed), "--state", str(tmp_path / "s.gz"), "--daily-writes", "0"]) == 0
    assert d1.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 30     # the load went through
    [u] = d1.usage()
    assert u["worker_requests"] is None and "not authorized" in u["warning"]
    assert (u["load_rows"], u["load_total"], u["load_ready"]) == (30, 30, 1)
    # SEARCH_CF_USAGE=0 switches the snapshot off.
    d1.db.execute("DELETE FROM cf_usage")
    monkeypatch.setenv("SEARCH_CF_USAGE", "0")
    assert L.main(["--feed", str(feed), "--state", str(tmp_path / "s.gz"), "--daily-writes", "0"]) == 0
    assert d1.usage() == []
