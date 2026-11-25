"""The search-index diff loader (scripts/load_search_index.py) and the parity fixture the Worker is
tested against (scripts/gen_search_parity.py). The SQL is run on a real SQLite with the Worker's
migration, so the literals, the upsert and the FTS triggers are exercised, not just counted."""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import gen_search_parity as parity
from scripts import load_search_index as L
from sourcing.feedfile import recency_key

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "worker" / "migrations-search" / "0001_job_search.sql"


def _row(i, **kw):
    r = {"source_id": f"gh:x:{i}", "source": "greenhouse", "company": "Acme", "title": f"Engineer {i}",
         "location": "Austin, TX", "remote": "", "url": f"https://x/{i}", "posted_at": "2026-10-01",
         "first_seen": "2026-10-01 10:00:00", "salary": "$120k/yr", "us": True, "entry_level": False}
    r.update(kw)
    return r


def _feed(path: Path, rows, generated_at="2026-10-24T00:00:00+00:00"):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"feed_version": 1, "generated_at": generated_at, "count": len(rows), "jobs": rows}, fh)
    return path


def _db():
    db = sqlite3.connect(":memory:")
    db.executescript(MIGRATION.read_text(encoding="utf-8"))
    return db


class FakeD1:
    """Runs the loader's statements on a local SQLite, as the D1 HTTP API would."""

    def __init__(self, fail_after=None):
        self.db = _db()
        self.requests = 0
        self.fail_after = fail_after

    def query(self, sql, params=None):
        self.requests += 1
        if self.fail_after is not None and self.requests > self.fail_after:
            raise RuntimeError("D1 query failed: HTTP 500")
        cur = self.db.execute(sql, params or [])
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def remote_version(self):
        rows = self.query("SELECT v FROM search_meta WHERE k = 'index_version'")
        return rows[0]["v"] if rows else None

    def remote_state(self, page=5000):
        return {r["source_id"]: r["h"] for r in self.query("SELECT source_id, h FROM jobs")}

    def ids(self):
        return sorted(r[0] for r in self.db.execute("SELECT source_id FROM jobs"))


# ---------------------------------------------------------------- records
def test_record_precomputes_the_facets_the_python_way():
    r = L.record(_row(1, salary="$45-$60/hr", posted_at="2026-10-01T08:30:00Z", remote="Remote",
                      title="Ingénieur LOGICIEL", ad_stance=None, sponsorship_stated=True,
                      visa=[{"code": "H-1B"}], nationality_visas=[{"code": "E-3"}, {"code": "H-1B"}]))
    assert r["pay"] == 124800
    assert r["posted_ts"] == int(datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc).timestamp())
    assert r["remote_l"] == "remote" and r["title_l"] == "ingénieur logiciel"
    assert r["sponsored"] == 1 and r["visa_any"] == 1 and r["visa_codes"] == ",H-1B,E-3,"
    assert L.record(_row(2, sponsorship_stated=1))["sponsored"] == 0          # `is True`, as apply_facets
    assert L.record(_row(3, ad_stance="not_offered", sponsorship_stated=True))["sponsored"] == 0
    assert L.record(_row(4, posted_at="yesterday"))["posted_ts"] is None       # kept by the date facet
    assert L.record(_row(5, nationality_visas=[{"code": "TN"}]))["visa_any"] == 0


def test_rkey_sorts_exactly_like_recency_key():
    rows = [_row(i, posted_at=p, first_seen=f) for i, (p, f) in enumerate([
        ("2026-10-01", "2026-10-02"), ("2026-10-01", "2026-10-02"), ("2026-10-01T09:00:00", ""),
        ("", "2026-10-09"), ("", ""), ("2026-09-30", "2026-10-09"), ("2026-10-01", "2026-10-02 10:00"),
        ("2026-10-01", "")])]
    rows.append(_row(10, posted_at="2026-10-01", first_seen="2026-10-02"))
    assert sorted(rows, key=L.rkey, reverse=True) == sorted(rows, key=recency_key, reverse=True)


def test_employer_fields_are_stored_once_and_joined_back():
    rows = [_row(i, company="Acme", visa=[{"code": "H-1B", "basis": "long text"}],
                 sponsor={"matched_name": "Acme"}) for i in range(5)]
    up, removed, _ = L.diff(rows, {})
    db = _db()
    for s in L.statements(up, removed):
        db.execute(s)
    assert db.execute("SELECT count(*) FROM job_extras").fetchone()[0] == 1
    got = db.execute("SELECT j.row, x.v FROM jobs j JOIN job_extras x ON x.h = j.xh "
                     "WHERE j.source_id = 'gh:x:3'").fetchone()
    assert {**json.loads(got[0]), **json.loads(got[1])} == rows[3]


# ---------------------------------------------------------------- diff + SQL
def test_diff_sends_only_added_changed_and_removed():
    old = [_row(i) for i in range(10)]
    _, _, state = L.diff(old, {})
    new = old[:8] + [_row(8, salary="$200k/yr")] + [_row(20), _row(21)]   # 9 removed, 8 changed
    up, removed, new_state = L.diff(new, state)
    assert sorted(r["source_id"] for r in up) == ["gh:x:20", "gh:x:21", "gh:x:8"]
    assert removed == ["gh:x:9"]
    assert len(new_state) == 11


def test_statements_stay_under_the_d1_limit_and_quote_safely():
    nasty = ["O'Reilly'); DROP TABLE jobs; --", 'say "hi"', "Zürich \\ back", "tab\there", "nul\x00byte",
             "\ud800 lone surrogate"]
    rows = [_row(i, title=t * 40, company=t) for i, t in enumerate(nasty * 30)]
    rows = [dict(r, source_id=f"gh:n:{i}") for i, r in enumerate(rows)]
    up, removed, _ = L.diff(rows, {})
    sqls = L.statements(up, removed, max_bytes=20_000)
    assert len(sqls) > 3
    assert all(len(s.encode("utf-8")) <= 20_000 for s in sqls[:-1])
    db = _db()
    for s in sqls:
        db.execute(s)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == len(rows)
    assert db.execute("SELECT company_l FROM jobs WHERE source_id = 'gh:n:0'").fetchone()[0] \
        == "o'reilly'); drop table jobs; --"
    # The FTS triggers indexed every row; a trigram phrase finds the substring.
    hits = db.execute("SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH ?",
                      ['company_l : "reilly\'); drop"']).fetchone()[0]
    assert hits == 30


def test_update_and_delete_keep_the_fts_index_in_step():
    db = _db()
    up, rm, state = L.diff([_row(1, title="Nurse"), _row(2, title="Nurse")], {})
    for s in L.statements(up, rm):
        db.execute(s)
    up, rm, _ = L.diff([_row(1, title="Pilot")], state)
    for s in L.statements(up, rm):
        db.execute(s)
    m = lambda q: db.execute("SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH ?", [q]).fetchone()[0]  # noqa: E731
    assert m('title_l : "nurse"') == 0 and m('title_l : "pilot"') == 1
    db.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('integrity-check')")


# ---------------------------------------------------------------- run()
def test_dry_run_prints_counts_and_touches_nothing(tmp_path):
    feed = _feed(tmp_path / "jobs.json.gz", [_row(i) for i in range(25)])
    state = tmp_path / "state.json.gz"
    out = io.StringIO()
    rep = L.run(feed, state, dry_run=True, out=out)
    assert rep["added"] == 25 and rep["removed"] == 0 and rep["statements"] >= 2
    assert "DRY RUN" in out.getvalue() and "added=25" in out.getvalue()
    assert not state.exists()
    assert L.main(["--feed", str(feed), "--state", str(state), "--dry-run"]) == 0


def test_main_without_credentials_says_not_configured(tmp_path, monkeypatch):
    for k in ("SEARCH_D1_ACCOUNT_ID", "SEARCH_D1_DATABASE_ID", "SEARCH_D1_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    feed = _feed(tmp_path / "jobs.json.gz", [_row(1)])
    assert L.main(["--feed", str(feed), "--state", str(tmp_path / "s.gz")]) == 2


def test_run_loads_then_sends_only_the_diff(tmp_path):
    state = tmp_path / "state.json.gz"
    d1 = FakeD1()
    rep = L.run(_feed(tmp_path / "a.gz", [_row(i) for i in range(30)]), state, dry_run=False,
                client=d1, out=io.StringIO())
    assert rep["added"] == 30 and state.exists()
    v1 = d1.remote_version()
    assert v1 == rep["index_version"]
    before = d1.requests
    rows = [_row(i) for i in range(1, 30)] + [_row(5, salary="$1k/yr"), _row(99)]
    rows = [r for r in rows if not (r["source_id"] == "gh:x:5" and r["salary"] == "$120k/yr")]
    rep = L.run(_feed(tmp_path / "b.gz", rows, "2026-10-24T03:00:00+00:00"), state, dry_run=False,
                client=d1, out=io.StringIO())
    assert (rep["added"], rep["changed"], rep["removed"]) == (1, 1, 1)
    assert d1.requests - before <= 6          # version check + delete + extras-free upsert + sweep + meta
    assert d1.remote_version() != v1
    assert "gh:x:0" not in d1.ids() and "gh:x:99" in d1.ids()
    # Nothing changed: no writes and the version (and every cursor and cached page) stays.
    v2 = d1.remote_version()
    rep = L.run(_feed(tmp_path / "c.gz", rows), state, dry_run=False, client=d1, out=io.StringIO())
    assert rep["statements"] == 0 and d1.remote_version() == v2


def test_a_failed_load_drops_the_state_and_the_next_run_finishes_it(tmp_path):
    state = tmp_path / "state.json.gz"
    d1 = FakeD1()
    L.run(_feed(tmp_path / "a.gz", [_row(i) for i in range(10)]), state, dry_run=False, client=d1,
          out=io.StringIO())
    d1.fail_after = d1.requests + 2           # version read, then one statement, then the error
    rows = [_row(i) for i in range(5, 400)]
    with pytest.raises(RuntimeError):
        L.run(_feed(tmp_path / "b.gz", rows), state, dry_run=False, client=d1, out=io.StringIO())
    assert not state.exists()
    d1.fail_after = None
    L.run(_feed(tmp_path / "b.gz", rows), state, dry_run=False, client=d1, out=io.StringIO())
    assert d1.ids() == sorted(r["source_id"] for r in rows)


def test_a_state_from_another_index_version_is_rebuilt_from_d1(tmp_path):
    state = tmp_path / "state.json.gz"
    d1 = FakeD1()
    L.run(_feed(tmp_path / "a.gz", [_row(i) for i in range(10)]), state, dry_run=False, client=d1,
          out=io.StringIO())
    stale = tmp_path / "stale.json.gz"
    L.save_state(stale, {"gh:x:0": "old"}, "some-other-version")      # e.g. a lost cache save
    rep = L.run(_feed(tmp_path / "b.gz", [_row(i) for i in range(3, 10)]), stale, dry_run=False,
                client=d1, out=io.StringIO())
    assert rep["removed"] == 3 and rep["added"] == 0
    assert d1.ids() == sorted(f"gh:x:{i}" for i in range(3, 10))


def test_d1_client_retries_then_reports(monkeypatch):
    import urllib.error
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, io.BytesIO(b""))
        body = json.dumps({"success": True, "result": [{"results": [{"v": "x"}]}]}).encode()
        return io.BytesIO(body)

    c = L.D1("acct", "db", "tok", spacing=0, opener=opener, sleep=lambda s: None)
    assert c.query("SELECT 1") == [{"v": "x"}]
    assert len(calls) == 3
    assert calls[0].full_url.endswith("/accounts/acct/d1/database/db/query")
    assert calls[0].headers["Authorization"] == "Bearer tok"


# ---------------------------------------------------------------- the Worker's parity fixture
def test_parity_fixture_is_up_to_date():
    """worker/test/search-parity.test.ts checks the Worker against this file; regenerate it with
    `python scripts/gen_search_parity.py` after changing apply_facets, the picker or the loader."""
    assert parity.OUT.read_text(encoding="utf-8") == parity.render()
    d = json.loads(parity.render())
    assert len(d["cases"]) >= 50
    assert any(c["ids"] for c in d["cases"]) and any(not c["ids"] for c in d["cases"])
