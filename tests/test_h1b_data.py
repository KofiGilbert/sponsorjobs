"""The H-1B data path end to end, offline: a packaged install must never show zero H-1B
records again, a 404 from USCIS (normal since FY2024) is not an error, the quarterly snapshot
is the authoritative total for the employers and years it declares (PERM / E-Verify never
lowered), and the maintainer import regenerates the seed.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest

from sourcing.sponsors import (SEED_COLS, H1BYearUnavailable, SponsorDB, canonical_h1b_rows,
                               download_h1b_rows, h1b_default_fiscal_years, sync_h1b_snapshot)


def _bundler():
    """packaging/ shares its name with the PyPI `packaging` library, so load the script by path."""
    path = Path(__file__).resolve().parents[1] / "packaging" / "bundle_sponsor_db.py"
    spec = importlib.util.spec_from_file_location("bundle_sponsor_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_gz(path, rows):
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SEED_COLS)
        for r in rows:
            w.writerow(r)
    return path


SEED_ROWS = [
    ["google", "Google Llc", 450, 2023, 2020, "54", "CA", 0, 1, 12],
    ["stripe", "Stripe Inc", 15, 2023, 2022, "52", "CA", 0, 0, 0],
    ["acme", "Acme Inc", 0, 0, 0, "", "", 0, 1, 0],       # E-Verify only, no H-1B
]


# ---------------------------------------------------------------- 1. installer bundler

def test_bundler_builds_the_installer_db_from_the_committed_seed(tmp_path):
    """CI on a fresh checkout has no data/ dir; the seed alone must produce the staged DB."""
    b = _bundler()
    seed = _seed_gz(tmp_path / "seed.csv.gz", SEED_ROWS)
    out = []
    r = b.build(seed=seed, source=tmp_path / "nope.db", dest=tmp_path / "pk" / "sponsors.db",
                min_employers=1, everify=tmp_path / "no-everify.csv.gz", log=out.append)
    assert r["used"] == "seed" and r["h1b"] == 2 and r["e_verify"] == 2 and r["perm"] == 1
    db = SponsorDB(tmp_path / "pk" / "sponsors.db")
    assert db.lookup("Google").h1b_approvals == 450
    assert db.h1b_fys_ingested() == {2020, 2021, 2022, 2023}   # so a refresh never re-adds them
    assert db.get_meta("h1b_updated_at") and db.get_meta("h1b_source") == "bundled seed"
    assert any("2 H-1B" in line for line in out)   # counts are printed


def test_bundler_prefers_a_local_db_only_when_it_has_more_h1b(tmp_path):
    b = _bundler()
    seed = _seed_gz(tmp_path / "seed.csv.gz", SEED_ROWS)
    local = SponsorDB(tmp_path / "data" / "sponsors.db")
    local.ingest_h1b_rows([{"Fiscal Year": "2026", "Employer": f"NEWCO {i}", "Initial Approval": "1",
                            "Continuing Approval": "0", "NAICS": "54", "State": "TX"}
                           for i in range(5)])
    local.close()
    r = b.build(seed=seed, source=tmp_path / "data" / "sponsors.db", dest=tmp_path / "pk.db",
                min_employers=1, everify=tmp_path / "none.gz", log=lambda *_: None)
    assert r["used"] == "local" and r["h1b"] == 5

    # Fewer H-1B employers locally (the dev DB that caused the bug had ZERO): the seed wins.
    local = SponsorDB(tmp_path / "data2" / "sponsors.db")
    local.ingest_perm_rows([{"EMPLOYER_NAME": "X CORP", "CASE_STATUS": "Certified"}])
    local.close()
    r = b.build(seed=seed, source=tmp_path / "data2" / "sponsors.db", dest=tmp_path / "pk2.db",
                min_employers=1, everify=tmp_path / "none.gz", log=lambda *_: None)
    assert r["used"] == "seed" and r["h1b"] == 2


def test_bundler_still_vets_tables_and_minimum_rows(tmp_path):
    b = _bundler()
    seed = _seed_gz(tmp_path / "seed.csv.gz", SEED_ROWS)
    # A local DB with an extra table is never shipped, whatever it holds.
    bad = tmp_path / "bad.db"
    SponsorDB(bad).close()
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE profile(secret TEXT)")
    con.execute("INSERT INTO sponsor_employer(norm_name, h1b_approvals) VALUES('z', 99)")
    con.commit(); con.close()
    logs = []
    r = b.build(seed=seed, source=bad, dest=tmp_path / "pk.db", min_employers=1,
                everify=tmp_path / "none.gz", log=logs.append)
    assert r["used"] == "seed" and any("unexpected tables" in l for l in logs)
    # The minimum-rows rule applies to the seed-built DB too.
    with pytest.raises(RuntimeError, match="REFUSING"):
        b.build(seed=seed, source=tmp_path / "none.db", dest=tmp_path / "pk3.db",
                min_employers=50_000, everify=tmp_path / "none.gz", log=lambda *_: None)
    assert not (tmp_path / "pk3.db").exists()           # no half-built file left behind


# ---------------------------------------------------------------- 2. honest refresh

def test_a_404_means_not_published_not_an_error():
    def fetch(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    with pytest.raises(H1BYearUnavailable) as e:
        download_h1b_rows(2024, fetch=fetch)
    assert e.value.fy == 2024


def test_default_years_probe_newer_then_the_published_files():
    import datetime
    assert h1b_default_fiscal_years(datetime.date(2026, 9, 30)) == \
        [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019]
    assert h1b_default_fiscal_years(datetime.date(2026, 10, 1))[0] == 2027   # FY starts Oct 1


def _fresh_app(tmp_path, monkeypatch, seed_rows=SEED_ROWS):
    import ui.app as app
    monkeypatch.setattr(app, "_DATA", tmp_path)
    app._SPONSORS.clear()
    monkeypatch.setattr(app, "BUNDLED_H1B_SEED", _seed_gz(tmp_path / "seed.csv.gz", seed_rows))
    monkeypatch.setattr(app, "_load_bundled_perm", lambda db: None)
    monkeypatch.setattr(app, "_load_bundled_everify", lambda db: None)
    monkeypatch.delenv("TAILOR_DATA_URL", raising=False)
    return app


def test_refresh_reports_unavailable_years_and_never_leaves_zero_h1b(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)

    def fake_download(fy):           # the world since FY2024: every year 404s
        raise H1BYearUnavailable(fy)
    monkeypatch.setattr(a, "download_h1b_rows", fake_download)
    r = a.app.test_client().post("/api/sponsors/refresh", json={}).get_json()
    assert r["ok"] is True and r["errors"] == []
    assert r["loaded_fys"] == []
    assert {2024, 2025} <= set(r["unavailable_fys"])
    # The seed's years were counted in (and so skipped), the DB is not empty, and the note
    # tells the person where newer data comes from instead of reading as a failure.
    assert set(r["skipped_fys"]) >= {2020, 2021, 2022, 2023}
    assert r["h1b"] == 2 and r["fiscal_years"] == [2020, 2021, 2022, 2023]
    assert "quarterly" in r["note"] and "FY2020 to 2023" in r["note"]
    assert a._sponsors().lookup("Google").h1b_approvals == 450   # not doubled by the refresh
    s = a.app.test_client().get("/api/sponsors/status").get_json()
    assert s["h1b_source"] == "bundled seed" and s["h1b_fy_span"] == "FY2020 to 2023"


def test_startup_loads_h1b_from_the_bundled_seed_when_the_db_has_none(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    db = a._sponsors()                      # first open of an empty DB
    assert db.stats()["h1b"] == 2 and db.get_meta("h1b_source") == "bundled seed"
    # A DB that already has H-1B rows is left alone (mirrors _load_bundled_perm).
    a._SPONSORS.clear()
    other = SponsorDB(tmp_path / "sponsors.db")
    other.ingest_h1b_rows([{"Fiscal Year": "2024", "Employer": "ZED LLC", "Initial Approval": "3",
                            "Continuing Approval": "0", "NAICS": "54", "State": "WA"}])
    other.close()
    assert a._load_bundled_h1b(a._sponsors()) == 0


def test_the_bundled_seed_loads_in_the_background_and_the_db_serves_meanwhile(tmp_path, monkeypatch):
    """_sponsors() used to merge the 340k-row seed on whichever request thread opened the DB
    first (tens of seconds, every other request queued behind it). It now returns at once and
    the merge runs on one background thread per process; JOBS_SYNC_SEED=1 (the suite's default)
    keeps the synchronous path for tests and the CLI."""
    import threading
    a = _fresh_app(tmp_path, monkeypatch)
    monkeypatch.delenv("JOBS_SYNC_SEED", raising=False)
    gate = threading.Event()
    real = a._load_bundled_h1b

    def slow_load(db):
        gate.wait(5)                             # the seed is "still reading"
        return real(db)
    monkeypatch.setattr(a, "_load_bundled_h1b", slow_load)
    monkeypatch.setattr(a, "_H1B_SEED", {"thread": None, "done": 0.0})
    db = a._sponsors()                           # returns immediately...
    assert db.stats()["h1b"] == 0 and a._h1b_seed_running()
    assert a._sponsors() is db                   # ...and a second open starts no second load
    assert a.app.test_client().get("/api/sponsors/status").get_json()["h1b_seeding"] is True
    # Tagging degrades (no badge) rather than blocking while the rows are on their way.
    assert db.tag_jobs([{"company": "Google", "location": "New York, NY"}])[0]["visa"] == []
    gate.set()
    a._join_h1b_seed(5)
    assert not a._h1b_seed_running()
    assert db.stats()["h1b"] == 2 and db.get_meta("h1b_source") == "bundled seed"
    assert db.lookup("Google").h1b_approvals == 450
    assert db.tag_jobs([{"company": "Google", "location": "New York, NY"}])[0]["visa"] != []
    assert a.app.test_client().get("/api/sponsors/status").get_json()["h1b_seeding"] is False


def test_the_sync_seed_switch_keeps_the_load_on_the_calling_thread(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    monkeypatch.setenv("JOBS_SYNC_SEED", "1")
    monkeypatch.setattr(a, "_H1B_SEED", {"thread": None, "done": 0.0})
    db = a._sponsors()
    assert db.stats()["h1b"] == 2 and a._H1B_SEED["thread"] is None


def test_a_real_download_failure_is_still_an_error(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "download_h1b_rows",
                        lambda fy: (_ for _ in ()).throw(RuntimeError("blocked")))
    r = a.app.test_client().post("/api/sponsors/refresh", json={"fiscal_years": [2019]}).get_json()
    assert r["ok"] is False and r["errors"][0]["fy"] == 2019 and r["unavailable_fys"] == []


# ---------------------------------------------------------------- 3. quarterly snapshot

def _snapshot(rows, version="2026-09", fys=(2024, 2025, 2026)):
    buf = gzip.compress(("\n".join([",".join(SEED_COLS)] +
                                   [",".join(str(x) for x in r) for r in rows]) + "\n").encode())
    manifest = json.dumps({"version": version, "fiscal_years": list(fys), "rows": len(rows)})
    files = {"https://tailor.example/data/h1b/latest.json": manifest.encode(),
             "https://tailor.example/data/h1b/latest.csv.gz": buf}
    calls = []

    def fetch(url):
        calls.append(url)
        return files[url]
    return fetch, calls


def test_snapshot_is_authoritative_for_listed_employers_and_applied_once(tmp_path):
    db = SponsorDB(tmp_path / "s.db")
    db.merge_aggregated_rows([dict(zip(SEED_COLS, r)) for r in SEED_ROWS])
    db.set_h1b_fys_ingested([2020, 2021, 2022, 2023])
    fetch, calls = _snapshot([
        ["google", "Google Llc", 700, 2026, 2020, "54", "CA", 0, 0, 0],   # newer, higher
        ["stripe", "Stripe Inc", 3, 2021, 2021, "52", "CA", 0, 0, 0],     # lower: still wins
        ["newco", "Newco", 9, 2026, 2026, "51", "NY", 0, 0, 0],           # brand new
        ["acme", "Acme Inc", 0, 0, 0, "", "", 0, 1, 0],                   # no H-1B claim
    ], fys=range(2020, 2027))
    r = sync_h1b_snapshot(db, "https://tailor.example/data/", fetch=fetch)
    assert r["status"] == "merged" and r["version"] == "2026-09" and r["rows"] == 4
    g, s_, n = db.lookup("Google"), db.lookup("Stripe"), db.lookup("Newco")
    assert g.h1b_approvals == 700 and g.h1b_last_fy == 2026 and g.h1b_first_fy == 2020
    assert g.e_verify and g.perm_certs == 12          # PERM / E-Verify never lowered
    # The snapshot is built from the seed plus exports, so its number IS the total for
    # the span it declares: a lower value replaces the local one rather than losing to MAX.
    assert (s_.h1b_approvals, s_.h1b_last_fy, s_.h1b_first_fy) == (3, 2021, 2021)
    assert n.h1b_approvals == 9
    a = db.lookup("Acme")
    assert a.h1b_approvals == 0 and a.e_verify         # E-Verify-only row left as it was
    # Ingested years are exactly the snapshot's span, not a union with what was there.
    assert db.h1b_fys_ingested() == set(range(2020, 2027))
    assert r["fiscal_years"] == list(range(2020, 2027))
    # Same version again: one manifest GET, no CSV download, nothing changes.
    before = len(calls)
    assert sync_h1b_snapshot(db, "https://tailor.example/data", fetch=fetch)["status"] == "up_to_date"
    assert len(calls) == before + 1 and calls[-1].endswith("latest.json")


def test_snapshot_supersedes_a_local_per_year_download_without_hybrid_counts(tmp_path):
    """The hybrid-count bug: seed totals (FY2020-23) plus the person's own additive FY2019
    download made Google 900; the snapshot covers FY2020-26 with Google at 850. MAX kept
    900 (silently missing 2024-26) while marking 2019-2026 ingested, so nothing could ever
    add the missing years. Now the snapshot sets the total and the span it declares; FY2019
    drops out of the ingested set so the next refresh adds it back on top, for everyone."""
    db = SponsorDB(tmp_path / "s.db")
    db.merge_aggregated_rows([dict(zip(SEED_COLS, r)) for r in SEED_ROWS])
    db.set_h1b_fys_ingested([2020, 2021, 2022, 2023])
    # The person's own FY2019 download, additive: Google 450 -> 900, Stripe 15 -> 20, and
    # a LOCALCO the snapshot does not know about.
    db.ingest_h1b_rows([
        {"Fiscal Year": "2019", "Employer": "GOOGLE LLC", "Initial Approval": "450",
         "Continuing Approval": "0", "NAICS": "54", "State": "CA"},
        {"Fiscal Year": "2019", "Employer": "STRIPE INC", "Initial Approval": "5",
         "Continuing Approval": "0", "NAICS": "52", "State": "CA"},
        {"Fiscal Year": "2019", "Employer": "LOCALCO LLC", "Initial Approval": "7",
         "Continuing Approval": "0", "NAICS": "51", "State": "NY"},
    ])
    db.set_h1b_fys_ingested(db.h1b_fys_ingested() | {2019})
    assert db.lookup("Google").h1b_approvals == 900 and db.lookup("Stripe").h1b_approvals == 20
    fetch, _ = _snapshot([
        ["google", "Google Llc", 850, 2026, 2020, "54", "CA", 0, 0, 0],
        ["stripe", "Stripe Inc", 40, 2026, 2022, "52", "CA", 0, 0, 0],
    ], fys=range(2020, 2027))
    r = sync_h1b_snapshot(db, "https://tailor.example/data", fetch=fetch)
    assert r["status"] == "merged"
    g, s_, l = db.lookup("Google"), db.lookup("Stripe"), db.lookup("Localco")
    # Listed employers: the snapshot's FY2020-26 total, exactly (no 900-vs-850 hybrid).
    assert (g.h1b_approvals, g.h1b_first_fy, g.h1b_last_fy) == (850, 2020, 2026)
    assert (s_.h1b_approvals, s_.h1b_first_fy, s_.h1b_last_fy) == (40, 2022, 2026)
    assert g.e_verify and g.perm_certs == 12          # other sources untouched
    # Absent from the snapshot: untouched.
    assert (l.h1b_approvals, l.h1b_first_fy, l.h1b_last_fy) == (7, 2019, 2019)
    # FY2019 is no longer claimed as ingested, so a refresh will add it back on top of the
    # snapshot totals instead of the old state where 2019-2026 was "done" but 2024-26 missing.
    assert db.h1b_fys_ingested() == set(range(2020, 2027))
    assert 2019 not in db.h1b_fys_ingested()


def test_snapshot_unreachable_host_is_a_quiet_skip(tmp_path):
    db = SponsorDB(":memory:")

    def down(url):
        raise OSError("no route to host")
    r = sync_h1b_snapshot(db, "https://tailor.example/data", fetch=down)
    assert r["status"] == "unreachable"
    assert sync_h1b_snapshot(db, "", fetch=down) == {"status": "disabled"}


def test_refresh_uses_tailor_data_url_when_set(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "download_h1b_rows", lambda fy: (_ for _ in ()).throw(H1BYearUnavailable(fy)))
    fetch, calls = _snapshot([["google", "Google Llc", 900, 2026, 2020, "54", "CA", 0, 0, 0]])
    monkeypatch.setattr(a, "sync_h1b_snapshot", lambda db, base: sync_h1b_snapshot(db, base, fetch=fetch))
    # Unset: nothing fetched.
    r = a.app.test_client().post("/api/sponsors/refresh", json={}).get_json()
    assert r["snapshot"] == {"status": "disabled"} and calls == []
    monkeypatch.setenv("TAILOR_DATA_URL", "https://tailor.example/data")
    r = a.app.test_client().post("/api/sponsors/refresh", json={}).get_json()
    assert r["snapshot"]["status"] == "merged" and "2026-09" in r["note"]
    assert a._sponsors().lookup("Google").h1b_approvals == 900
    assert r["fiscal_years"][-1] == 2026
    assert a.app.test_client().get("/api/sponsors/status").get_json()["h1b_snapshot_version"] == "2026-09"


# ---------------------------------------------------------------- 4. maintainer import

DASHBOARD_HEADERS = ["Fiscal Year", "Employer (Petitioner) Name", "Initial Approval",
                     "Initial Denial", "Continuing Approval", "Continuing Denial", "NAICS",
                     "Tax ID", "State", "City", "ZIP"]


def test_dashboard_export_headers_map_onto_the_csv_dialect():
    rows = [{" fiscal year": "FY 2026", "EMPLOYER (PETITIONER) NAME": "ACME LLC",
             "Initial Approvals": "1,200", "Continuing Approvals": "3", "NAICS Code": "54",
             "Petitioner State": "CA"}]
    (c,) = list(canonical_h1b_rows(rows))
    assert c["Employer"] == "ACME LLC" and c["Initial Approval"] == "1,200"
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows(rows)
    rec = db.lookup("Acme")
    assert rec.h1b_approvals == 1203 and rec.h1b_last_fy == 2026


def test_import_script_ingests_regenerates_seed_and_writes_snapshot(tmp_path):
    from scripts.import_h1b_export import import_exports
    seed = _seed_gz(tmp_path / "seed.csv.gz", SEED_ROWS)
    export = tmp_path / "h1b_fy2026.csv"
    with open(export, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(DASHBOARD_HEADERS)
        w.writerow(["2026", "GOOGLE LLC", "100", "0", "200", "1", "54", "1234", "CA", "MV", "94043"])
        w.writerow(["2026", "NEWCO INC", "4", "0", "0", "0", "51", "9", "NY", "NYC", "10001"])
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(DASHBOARD_HEADERS)
    ws.append([2025, "GOOGLE LLC", 10, 0, 20, 0, "54", "1234", "CA", "MV", "94043"])
    xlsx = tmp_path / "h1b_fy2025.xlsx"
    wb.save(xlsx)
    logs = []
    r = import_exports([export, xlsx], db_path=tmp_path / "data" / "sponsors.db", seed_path=seed,
                       out_dir=tmp_path / "dist" / "h1b", version="2026-09", log=logs.append)
    assert r["fiscal_years"] == [2020, 2021, 2022, 2023, 2025, 2026]
    assert r["loaded"] == {2026: 2, 2025: 1}
    db = SponsorDB(tmp_path / "data" / "sponsors.db")
    g = db.lookup("Google")
    assert g.h1b_approvals == 450 + 300 + 30 and g.h1b_last_fy == 2026 and g.h1b_first_fy == 2020
    assert g.perm_certs == 12                      # the seed's other columns came along
    assert db.lookup("Newco").h1b_approvals == 4
    db.close()
    # The seed was regenerated from the store, and the snapshot is the same layout + manifest.
    with gzip.open(seed, "rt") as f:
        seeded = {r["norm_name"]: r for r in csv.DictReader(f)}
    assert seeded["google"]["h1b_approvals"] == "780" and "newco" in seeded and "acme" in seeded
    man = json.loads((tmp_path / "dist" / "h1b" / "latest.json").read_text())
    assert man["version"] == "2026-09" and man["fiscal_years"] == r["fiscal_years"]
    assert man["rows"] == r["snapshot_rows"] == len(seeded)
    with gzip.open(tmp_path / "dist" / "h1b" / "latest.csv.gz", "rt") as f:
        assert next(csv.reader(f)) == SEED_COLS
    # Running the same export again does NOT double count (years already in are skipped).
    r2 = import_exports([export], db_path=tmp_path / "data" / "sponsors.db", seed_path=seed,
                        out_dir=tmp_path / "dist" / "h1b", version="2026-09", log=lambda *_: None)
    assert r2["loaded"] == {} and r2["skipped"] == [2026]
    assert SponsorDB(tmp_path / "data" / "sponsors.db").lookup("Google").h1b_approvals == 780


def test_import_script_refuses_an_export_without_the_key_columns(tmp_path):
    from scripts.import_h1b_export import import_exports
    bad = tmp_path / "bad.csv"
    bad.write_text("Company,Total\nACME,5\n")
    with pytest.raises(SystemExit, match="could not find"):
        import_exports([bad], db_path=tmp_path / "s.db", seed_path=tmp_path / "none.gz",
                       out_dir=tmp_path / "out", log=lambda *_: None)


def test_import_reads_a_tab_separated_utf16_tableau_export(tmp_path):
    from scripts.import_h1b_export import read_export_rows
    text = "\t".join(DASHBOARD_HEADERS) + "\n" + "\t".join(
        ["2026", "ACME LLC", "1", "0", "2", "0", "54", "1", "CA", "LA", "90001"]) + "\n"
    p = tmp_path / "export.csv"
    p.write_bytes(text.encode("utf-16"))
    rows = read_export_rows(p)
    assert rows[0]["Employer (Petitioner) Name"] == "ACME LLC" and rows[0]["Continuing Approval"] == "2"
