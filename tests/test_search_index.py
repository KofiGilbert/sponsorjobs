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
from sourcing.filters import salary_value

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
    """Runs the loader's statements on a local SQLite, as the D1 HTTP API would, and reports rows
    written the way the loader reads them (SQLite's change count, triggers included)."""

    def __init__(self, fail_after=None):
        self.db = _db()
        self.requests = 0
        self.fail_after = fail_after
        self.read = 0
        self.written = 0
        self.last = {"rows_read": 0, "rows_written": 0}

    def query(self, sql, params=None):
        self.requests += 1
        if self.fail_after is not None and self.requests > self.fail_after:
            raise RuntimeError("D1 query failed: HTTP 500")
        before = self.db.total_changes
        cur = self.db.execute(sql, params or [])
        cols = [c[0] for c in cur.description or []]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        w = 0 if sql.lstrip().upper().startswith("SELECT") else self.db.total_changes - before
        if "VALUES('merge'" in sql:
            w = 1                             # D1 counts an FTS5 merge as one row (measured)
        self.last = {"rows_read": len(out), "rows_written": w}
        self.written += w
        return out

    def meta(self):
        return dict(self.db.execute("SELECT k, v FROM search_meta").fetchall())

    def ids(self):
        return sorted(r[0] for r in self.db.execute("SELECT json_extract(r, '$[0]') FROM jobs"))

    def order(self):
        return [r[0] for r in self.db.execute("SELECT json_extract(r, '$[0]') FROM jobs ORDER BY id DESC")]


def _load(d1, tmp_path, rows, name="feed.gz", **kw):
    kw.setdefault("daily_writes", 0)
    return L.run(_feed(tmp_path / name, rows), tmp_path / "state.json.gz", dry_run=False, client=d1,
                 out=io.StringIO(), **kw)


def _whole_row(d1, sid):
    """A row as the Worker hands it back (search.ts toRow): the stored fields, tag names, text
    columns and employer fields."""
    m = d1.meta()
    fields, tags = json.loads(m["fields"]), json.loads(m["tags"])
    r, title, company, location, xv = d1.db.execute(
        "SELECT j.r, j.title, j.company, j.location, x.v FROM jobs j LEFT JOIN extras x ON x.x = j.x "
        "AND j.x != 0 WHERE json_extract(j.r, '$[0]') = ?", [sid]).fetchone()
    out = {}
    for k, v in zip(fields, json.loads(r)):
        if v is None:
            continue
        out[k] = [tags[t] if isinstance(t, int) else t for t in v] if k == "tags" else v
    out.update(title=title, company=company, location=location)
    if xv:
        out.update(json.loads(xv))
    return out


# ---------------------------------------------------------------- records
def test_record_precomputes_the_facets_the_python_way():
    d = L.Dicts()
    row = _row(1, salary="$45-$60/hr", posted_at="2026-10-01T08:30:00Z", remote="Remote",
               title="INGÉNIEUR Logiciel", ad_stance=None, sponsorship_stated=True, entry_level=True,
               visa=[{"code": "H-1B"}], nationality_visas=[{"code": "E-3"}, {"code": "H-1B"}],
               sponsor_relevant=False, tags=["Engineering"])
    d.learn([row])
    r = L.record(row, d)
    assert r["pay"] == 124800
    assert r["ts"] == int(datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc).timestamp())
    assert r["f"] == L.F_ENTRY | L.F_OFFERED | L.F_VISA_ANY | (1 << L.F_REMOTE_SHIFT)   # not a sponsor row
    assert r["vm"] == (1 << d.visa.index("H-1B")) | (1 << d.visa.index("E-3"))
    assert r["tl"] == "ingénieur logiciel" and r["cl"] is None      # ASCII-only text stores nothing
    assert L.record(_row(7, title="Ingénieur"), d)["tl"] is None    # SQLite's lower() agrees: nothing
    assert json.loads(r["r"])[0] == "gh:x:1"
    assert L.record(_row(2, sponsorship_stated=1), d)["f"] & L.F_OFFERED == 0   # `is True`, as apply_facets
    assert L.record(_row(3, ad_stance="not_offered", sponsorship_stated=True), d)["f"] & L.F_OFFERED == 0
    assert L.record(_row(4, posted_at="yesterday"), d)["ts"] is None             # kept by the date facet
    assert L.record(_row(5, nationality_visas=[{"code": "TN"}]), d)["f"] & L.F_VISA_ANY == 0
    assert L.record(_row(6), d)["f"] & L.F_SPONSOR                                # no flag: a sponsor row


def test_ids_sort_exactly_like_recency_key():
    rows = [_row(i, posted_at=p, first_seen=f) for i, (p, f) in enumerate([
        ("2026-10-01", "2026-10-02"), ("2026-10-01", "2026-10-02"), ("2026-10-01T09:00:00", ""),
        ("", "2026-10-09"), ("", ""), ("2026-09-30", "2026-10-09"), ("2026-10-01", "2026-10-02 10:00"),
        ("2026-10-01", ""), ("yesterday", ""), ("2026-10-01 09:00:00", "x"), ("2026-10-01T09:00:00Z", "")])]
    rows.append(_row(20, posted_at="2026-10-01", first_seen="2026-10-02"))
    ids = L.assign_ids(rows, {})
    assert sorted(rows, key=lambda r: ids[r["source_id"]], reverse=True) \
        == sorted(rows, key=recency_key, reverse=True)
    assert max(ids.values()) < 2 ** 53                                    # exact in JavaScript


def test_ids_stay_put_when_rows_arrive_and_make_room_in_the_middle():
    old = [_row(i, posted_at="2026-10-01", first_seen=f"2026-10-01 10:00:{i:02d}") for i in range(10)]
    ids = L.assign_ids(old, {})
    later = old + [_row(50, posted_at="2026-10-01", first_seen="2026-10-01 11:00:00"),     # after all
                   _row(51, posted_at="2026-10-01", first_seen="2026-10-01 10:00:04.5")]   # in the middle
    ids2 = L.assign_ids(later, ids)
    assert all(ids2[r["source_id"]] == ids[r["source_id"]] for r in old)
    assert sorted(later, key=lambda r: ids2[r["source_id"]]) == sorted(later, key=recency_key)
    # 100 rows squeezed between two neighbours: only that stretch is renumbered, still in order.
    crowd = old + [_row(100 + k, posted_at="2026-10-01", first_seen=f"2026-10-01 10:00:04.{k:03d}")
                   for k in range(100)]
    ids3 = L.assign_ids(crowd, ids)
    assert sorted(crowd, key=lambda r: ids3[r["source_id"]]) == sorted(crowd, key=recency_key)
    assert sum(ids3[r["source_id"]] != ids[r["source_id"]] for r in old) < len(old)


def test_employer_fields_are_stored_once_and_rows_come_back_whole(tmp_path):
    rows = [_row(i, company="Acme", visa=[{"code": "H-1B", "basis": "long text"}], tags=["A", "B"],
                 sponsor={"matched_name": "Acme"}, ad_stance="offered", ad_sentence="We sponsor.")
            for i in range(5)]
    d1 = FakeD1()
    _load(d1, tmp_path, rows)
    assert d1.db.execute("SELECT count(*) FROM extras").fetchone()[0] == 1
    assert _whole_row(d1, "gh:x:3") == rows[3]


# ---------------------------------------------------------------- the plan + SQL
def test_plan_sends_only_added_changed_and_removed():
    old = [_row(i) for i in range(10)]
    p0 = L.Plan(old, {}, {})
    new = old[:8] + [_row(8, salary="$200k/yr")] + [_row(20), _row(21)]   # 9 removed, 8 changed
    p = L.Plan(new, p0.target, p0.extras_ids)
    assert sorted(t[0] for t in p.new) == ["gh:x:20", "gh:x:21"]
    assert [t[0] for t in p.moved] == ["gh:x:8"]
    assert p.deletes == [p0.target["gh:x:9"][1]]
    assert len(p.target) == 11


def test_statements_stay_under_the_d1_limit_and_quote_safely():
    nasty = ["O'Reilly'); DROP TABLE jobs; --", 'say "hi"', "Zürich \\ back", "tab\there", "nul\x00byte",
             "\ud800 lone surrogate"]
    rows = [_row(i, title=t * 40, company=t) for i, t in enumerate(nasty * 30)]
    rows = [dict(r, source_id=f"gh:n:{i}") for i, r in enumerate(rows)]
    d = L.Dicts()
    d.learn(rows)
    plan = L.Plan(rows, {}, {})
    sqls = [s for s, _ in L.upsert_statements(sorted(plan.new, key=lambda t: t[2]), d, max_bytes=20_000)]
    assert len(sqls) > 3
    assert all(len(s.encode("utf-8")) <= 20_000 for s in sqls[:-1])
    db = _db()
    for s in sqls:
        db.execute(s)
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == len(rows)
    assert db.execute("SELECT company FROM jobs WHERE json_extract(r, '$[0]') = 'gh:n:0'").fetchone()[0] \
        == "O'Reilly'); DROP TABLE jobs; --"
    # The FTS triggers indexed every row; the trigram query finds the substring.
    m = " AND ".join(f'"{g}"' for g in ("rei", "eil", "ill", "lly"))
    assert db.execute("SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH ?", [m]).fetchone()[0] >= 30


def test_update_and_delete_keep_the_fts_index_in_step(tmp_path):
    d1 = FakeD1()
    _load(d1, tmp_path, [_row(1, title="Nurse"), _row(2, title="Nurse")], "a.gz")
    _load(d1, tmp_path, [_row(1, title="Pilot")], "b.gz")
    m = lambda q: d1.db.execute("SELECT count(*) FROM jobs_fts WHERE jobs_fts MATCH ?", [q]).fetchone()[0]  # noqa: E731
    assert m('"urs"') == 0 and m('"ilo"') == 1
    d1.db.execute("INSERT INTO jobs_fts(jobs_fts) VALUES ('integrity-check')")


def test_histograms_count_days_and_pay():
    rows = [_row(i, salary=f"${100 + i}k/yr" if i % 2 else "") for i in range(20)]
    ids = L.assign_ids(rows, {})
    h = L.histograms(ids, {r["source_id"]: r for r in rows}, payq_step=3)
    assert sum(n for _, n in json.loads(h["hist"])) == 20
    pays = sorted((salary_value(r["salary"]) for r in rows if r["salary"]), reverse=True)
    assert json.loads(h["payq"]) == pays[::3] and h["payq_step"] == "3"


# ---------------------------------------------------------------- run()
def test_dry_run_prints_counts_and_touches_nothing(tmp_path):
    feed = _feed(tmp_path / "jobs.json.gz", [_row(i) for i in range(25)])
    state = tmp_path / "state.json.gz"
    out = io.StringIO()
    rep = L.run(feed, state, dry_run=True, out=out)
    assert rep["added"] == 25 and rep["removed"] == 0
    assert "DRY RUN" in out.getvalue() and "added=25" in out.getvalue()
    assert not state.exists()
    assert L.main(["--feed", str(feed), "--state", str(state), "--dry-run"]) == 0


def test_main_without_credentials_says_not_configured(tmp_path, monkeypatch):
    for k in ("SEARCH_D1_ACCOUNT_ID", "SEARCH_D1_DATABASE_ID", "SEARCH_D1_TOKEN", "SEARCH_D1_API_URL"):
        monkeypatch.delenv(k, raising=False)
    feed = _feed(tmp_path / "jobs.json.gz", [_row(1)])
    assert L.main(["--feed", str(feed), "--state", str(tmp_path / "s.gz")]) == 2


def test_run_loads_then_sends_only_the_diff(tmp_path):
    d1 = FakeD1()
    rows = [_row(i, posted_at=f"2026-10-{1 + i % 9:02d}") for i in range(100)]
    rep = _load(d1, tmp_path, rows, "a.gz")
    assert rep["added"] == 100 and rep["complete"] and (tmp_path / "state.json.gz").exists()
    meta = d1.meta()
    assert meta["ready"] == "1" and meta["rows"] == "100"
    assert d1.order() == [r["source_id"] for r in sorted(rows, key=recency_key, reverse=True)]
    v1 = meta["index_version"]
    rows2 = [r for r in rows if r["source_id"] != "gh:x:0"]
    rows2 = [dict(r, salary="$1k/yr") if r["source_id"] == "gh:x:5" else r for r in rows2] + [_row(999)]
    rep = _load(d1, tmp_path, rows2, "b.gz")
    assert (rep["added"], rep["changed"], rep["removed"]) == (1, 1, 1)
    # The changed rows (about 2 each, the keyword index included) and the index's bookkeeping
    # (about 15: the version, sizes and histograms), not the list (about 250).
    assert rep["rows_written"] < 40
    assert d1.meta()["index_version"] != v1
    assert "gh:x:0" not in d1.ids() and "gh:x:999" in d1.ids()
    # Nothing changed: no writes, and the version (so every cursor and cached page) stays.
    v2 = d1.meta()["index_version"]
    rep = _load(d1, tmp_path, rows2, "c.gz")
    assert rep["rows_written"] == 0 and d1.meta()["index_version"] == v2


def test_the_write_budget_stops_a_run_and_the_next_one_carries_on(tmp_path):
    d1 = FakeD1()
    rows = [_row(i, posted_at=f"2026-10-{1 + i % 20:02d}") for i in range(600)]
    day1 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp()
    rep = _load(d1, tmp_path, rows, daily_writes=500, now=day1)
    assert not rep["complete"] and 0 < rep["applied_upserts"] < 600 and rep["left"] > 0
    assert rep["written_today"] <= 500
    assert d1.meta().get("ready") != "1"                  # the API answers 503 until it is all in
    rep = _load(d1, tmp_path, rows, daily_writes=500, now=day1 + 3600)    # same day: nothing left
    assert rep["applied_upserts"] == 0 and not rep["complete"]
    for day in range(1, 10):                              # the next days finish it
        rep = _load(d1, tmp_path, rows, daily_writes=500, now=day1 + day * 86400)
        if rep["complete"]:
            break
    assert rep["complete"] and d1.meta()["ready"] == "1"
    assert d1.order() == [r["source_id"] for r in sorted(rows, key=recency_key, reverse=True)]
    used = d1.db.execute("SELECT max(rows_written) FROM search_usage").fetchone()[0]
    assert used <= 500


def test_after_the_first_load_new_rows_go_first_newest_first(tmp_path):
    d1 = FakeD1()
    rows = [_row(i, posted_at="2026-10-01") for i in range(50)]
    _load(d1, tmp_path, rows, "a.gz")
    fresh = [_row(100 + i, posted_at=f"2026-10-{2 + i:02d}") for i in range(20)]
    changed = [dict(r, salary="$1k/yr") for r in rows]
    rep = _load(d1, tmp_path, changed + fresh, "b.gz", max_writes=L.RESERVE + 12)
    assert not rep["complete"]
    got = set(d1.ids()) - {r["source_id"] for r in rows}
    assert got and got == {f"gh:x:{100 + i}" for i in range(19, 19 - len(got), -1)}   # the newest


def test_a_failed_load_drops_the_state_and_the_next_run_finishes_it(tmp_path):
    d1 = FakeD1()
    _load(d1, tmp_path, [_row(i) for i in range(10)], "a.gz")
    d1.fail_after = d1.requests + 4           # meta, usage, dirty mark, then one statement
    rows = [_row(i) for i in range(5, 400)]
    with pytest.raises(RuntimeError):
        _load(d1, tmp_path, rows, "b.gz")
    assert not (tmp_path / "state.json.gz").exists()
    assert d1.meta()["load_token"].startswith("dirty:")
    d1.fail_after = None
    rep = _load(d1, tmp_path, rows, "b.gz")
    assert rep["complete"] and d1.ids() == sorted(r["source_id"] for r in rows)


def test_a_state_from_another_load_is_rebuilt_from_d1(tmp_path):
    d1 = FakeD1()
    _load(d1, tmp_path, [_row(i) for i in range(10)], "a.gz")
    L.save_state(tmp_path / "state.json.gz", "some-other-token", {"gh:x:0": ["old", 1, 0]}, {})
    rep = _load(d1, tmp_path, [_row(i) for i in range(3, 10)], "b.gz")
    assert rep["removed"] == 3 and rep["added"] == 0
    assert d1.ids() == sorted(f"gh:x:{i}" for i in range(3, 10))


def test_d1_client_retries_then_reports_rows_written():
    import urllib.error
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, io.BytesIO(b""))
        body = json.dumps({"success": True, "result": [{"results": [{"v": "x"}],
                                                        "meta": {"rows_read": 2, "rows_written": 7}}]}).encode()
        return io.BytesIO(body)

    c = L.D1("acct", "db", "tok", spacing=0, opener=opener, sleep=lambda s: None)
    assert c.query("SELECT 1") == [{"v": "x"}]
    assert len(calls) == 3 and c.written == 7 and c.last["rows_written"] == 7
    assert calls[0].full_url.endswith("/accounts/acct/d1/database/db/query")
    assert calls[0].headers["Authorization"] == "Bearer tok"


# ---------------------------------------------------------------- the Worker's parity fixture
def test_parity_fixture_is_up_to_date():
    """worker/test/search-parity.test.ts checks the Worker against this file; regenerate it with
    `python scripts/gen_search_parity.py` after changing apply_facets, the picker or the loader."""
    assert parity.OUT.read_text(encoding="utf-8") == parity.render()
    d = json.loads(parity.render())
    assert len(d["cases"]) >= 60
    assert any(c["ids"] for c in d["cases"]) and any(not c["ids"] for c in d["cases"])
    assert any(c["params"].get("scope") == "sponsors" for c in d["cases"])
