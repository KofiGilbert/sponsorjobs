"""Per-employer depth, offline: year-by-year H-1B approvals and denials with PERM by year and
the three flags, the streaming LCA wage aggregate and its bundle, the store's history() and
wages() with their fallbacks, the Flask endpoints, the lottery odds and the offer check, and
the extension index's compact facts. Small synthetic fixtures only; no real files.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from sourcing import lca
from sourcing.lottery import DEFAULT_MIX, lottery_odds, likely_level, offer_check
from sourcing.sponsors import H1B_YEAR_COLS, SponsorDB


# ---------------------------------------------------------------- fixtures

def _h1b_rows():
    return [
        {"Fiscal Year": "2022", "Employer": "Acme Corp", "Initial Approval": "10", "Initial Denial": "2",
         "Continuing Approval": "5", "Continuing Denial": "1", "NAICS": "541511", "State": "CA"},
        {"Fiscal Year": "2022", "Employer": "Acme Corp", "Initial Approval": "1", "Initial Denial": "0",
         "Continuing Approval": "1", "Continuing Denial": "0", "NAICS": "541511", "State": "TX"},
        {"Fiscal Year": "2023", "Employer": "ACME CORP", "Initial Approval": "8", "Initial Denial": "0",
         "Continuing Approval": "7", "Continuing Denial": "3", "NAICS": "541511", "State": "CA"},
        {"Fiscal Year": "2023", "Employer": "Other Inc", "Initial Approval": "3", "Initial Denial": "9",
         "Continuing Approval": "0", "Continuing Denial": "0", "NAICS": "", "State": "NY"},
    ]


def _lca_row(**kw):
    base = {"status": "Certified", "visa": "H-1B", "title": "Senior Software Engineer II",
            "soc": "15-1252.00", "employer": "Acme Corp", "city": "Austin", "state": "TX",
            "wage_from": 120000, "wage_to": 150000, "wage_unit": "Year", "pw": 110000,
            "pw_unit": "Year", "pw_level": "II", "dependent": "No", "violator": "No",
            "decided": "2025-03-01"}
    base.update(kw)
    return base


def _acme_db(with_lca=True):
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows(_h1b_rows())
    db.ingest_perm_rows([
        {"EMPLOYER_NAME": "ACME CORP", "CASE_STATUS": "Certified", "DECISION_DATE": "2023-11-02"},
        {"EMPLOYER_NAME": "ACME CORP", "CASE_STATUS": "Certified", "DECISION_DATE": "2024-02-02"},
        {"EMPLOYER_NAME": "ACME CORP", "CASE_STATUS": "Denied", "DECISION_DATE": "2024-02-02"},
        {"EMPLOYER_NAME": "ACME CORP", "CASE_STATUS": "Certified", "DECISION_DATE": "2022-12-01"},
    ])
    if with_lca:
        agg = lca.LCAAggregate()
        for w in (120000, 130000, 125000):
            agg.add(_lca_row(wage_from=w))
        for w, lvl, pw in ((160000, "III", 150000), (165000, "III", 150000)):
            agg.add(_lca_row(title="Staff Software Engineer", wage_from=w, pw_level=lvl, pw=pw))
        agg.add(_lca_row(title="Software Engineer", city="Dallas", wage_from=100000, pw_level="I", pw=90000))
        agg.add(_lca_row(title="Software Engineer", city="Dallas", wage_from=104000, pw_level="I", pw=90000))
        agg.add(_lca_row(title="Accountant", city="Dallas", wage_from=70000, pw_level="I", pw=65000,
                         dependent="Yes"))
        agg.add(_lca_row(title="Accountant", city="Dallas", wage_from=72000, pw_level="I", pw=65000))
        db.ingest_lca_wage_rows(agg.cells())
        db.ingest_lca_flag_rows(agg.flags())
    return db


# ---------------------------------------------------------------- A. history

def test_schema_is_additive_and_an_old_db_gets_the_new_tables(tmp_path):
    import sqlite3
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE sponsor_employer (norm_name TEXT PRIMARY KEY, display_name TEXT, "
                "h1b_approvals INTEGER DEFAULT 0, h1b_last_fy INTEGER DEFAULT 0, "
                "h1b_first_fy INTEGER DEFAULT 0, naics TEXT, state TEXT, cap_exempt INTEGER DEFAULT 0, "
                "e_verify INTEGER DEFAULT 0)")
    con.execute("INSERT INTO sponsor_employer(norm_name, h1b_approvals) VALUES('acme', 5)")
    con.commit(); con.close()
    db = SponsorDB(old)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(sponsor_employer)")}
    assert {"perm_certs", "h1b_dependent", "willful_violator"} <= cols
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sponsor_h1b_year", "sponsor_perm_year", "sponsor_lca_wage"} <= tables
    assert db.lookup("Acme").h1b_approvals == 5        # the old row still serves the badge
    assert db.history("Acme")["years"] == []            # no detail yet, no crash
    db.close()


def test_ingest_keeps_per_year_approvals_and_denials_and_sets_not_adds():
    db = _acme_db(with_lca=False)
    h = db.history("Acme Corp")
    by = {y["fy"]: y for y in h["years"]}
    assert by[2022]["approvals"] == 17 and by[2022]["denials"] == 3        # both CA + TX rows
    assert by[2023]["initial_approvals"] == 8 and by[2023]["continuing_denials"] == 3
    assert by[2024]["perm"] == 2 and by[2023]["perm"] == 1 and by[2022]["perm"] == 0   # FY starts in October
    assert h["perm_total"] == 3 and h["h1b_total"] == 32
    assert h["approvals_5y"] == 32 and h["denials_5y"] == 6 and h["denial_rate_5y"] == pytest.approx(0.158, abs=0.001)
    # Re-ingesting the same year doubles the total (additive, as before) but NOT the year row.
    db.ingest_h1b_rows([r for r in _h1b_rows() if r["Fiscal Year"] == "2023"])
    by = {y["fy"]: y for y in db.history("Acme Corp")["years"]}
    assert by[2023]["approvals"] == 15
    assert db.lookup("Acme Corp").h1b_approvals == 47
    db.close()


def test_history_aggregates_the_same_entities_as_lookup_and_caps_the_years():
    db = SponsorDB(":memory:")
    rows = [{"Fiscal Year": str(fy), "Employer": "Amazon Web Services", "Initial Approval": "1",
             "Initial Denial": "0", "Continuing Approval": "0", "Continuing Denial": "0"}
            for fy in range(2010, 2024)]
    rows += [{"Fiscal Year": "2023", "Employer": "Amazon Data Services", "Initial Approval": "4",
              "Initial Denial": "1", "Continuing Approval": "0", "Continuing Denial": "0"}]
    db.ingest_h1b_rows(rows)
    h = db.history("Amazon")
    assert len(h["years"]) == 10 and h["years"][0]["fy"] == 2014
    assert h["years"][-1]["approvals"] == 5 and h["years"][-1]["denials"] == 1   # both entities
    assert db.history("Nowhere Corp") is None
    db.close()


def test_flags_come_from_lca_attestations_never_lowered_and_cap_exempt_from_the_heuristic():
    db = _acme_db()
    f = db.history("Acme Corp")["flags"]
    assert f["h1b_dependent"] is True and f["willful_violator"] is False and f["cap_exempt"] is False
    db.ingest_lca_flag_rows([{"norm_name": "acme", "h1b_dependent": 0, "willful_violator": 1}])
    f = db.history("Acme Corp")["flags"]
    assert f["h1b_dependent"] is True and f["willful_violator"] is True
    db.ingest_h1b_rows([{"Fiscal Year": "2023", "Employer": "State University", "Initial Approval": "2",
                         "Initial Denial": "0", "Continuing Approval": "0", "Continuing Denial": "0"}])
    assert db.history("State University")["flags"]["cap_exempt"] is True
    db.close()


def test_history_bundle_rows_round_trip_and_perm_year_counts_take_max(tmp_path):
    db = _acme_db(with_lca=False)
    rows = list(db.h1b_year_rows())
    assert [r["fy"] for r in rows if r["norm_name"] == "acme"] == [2022, 2023]
    db2 = SponsorDB(":memory:")
    assert db2.ingest_h1b_year_rows(rows) == 3
    assert db2.h1b_year_fiscal_years() == [2022, 2023]
    db2.ingest_perm_year_counts([{"EMPLOYER_NAME": "ACME CORP", "FY": "2024", "PERM_CERTS": "5"},
                                 {"EMPLOYER_NAME": "ACME CORP", "FY": "2024", "PERM_CERTS": "2"}])
    db2.ingest_perm_year_counts([{"EMPLOYER_NAME": "ACME CORP", "FY": "2024", "PERM_CERTS": "3"}])
    assert db2.history("Acme Corp")["years"][-1] == {
        "fy": 2024, "initial_approvals": 0, "initial_denials": 0, "continuing_approvals": 0,
        "continuing_denials": 0, "approvals": 0, "denials": 0, "perm": 7}
    db.close(); db2.close()


# ---------------------------------------------------------------- B. LCA wages

def test_lca_aggregate_annualizes_filters_and_counts_levels():
    agg = lca.LCAAggregate()
    agg.add(_lca_row(wage_from=60, wage_unit="Hour"))                 # 124,800
    agg.add(_lca_row(wage_from=10000, wage_unit="Month"))             # 120,000
    agg.add(_lca_row(status="Withdrawn", wage_from=1))                # ignored
    agg.add(_lca_row(status="Denied"))                                # ignored
    agg.add(_lca_row(visa="E-3 Australian"))                          # ignored
    agg.add(_lca_row(visa="H-1B1 Chile"))                             # ignored
    agg.add(_lca_row(wage_from=5000, wage_unit="Year"))               # below the sanity floor
    agg.add(_lca_row(wage_from=130000, pw_level="III", pw=125000))
    cells = agg.cells(min_count=2)
    assert len(cells) == 1
    c = cells[0]
    assert c["norm_name"] == "acme" and c["fy"] == 2025 and c["city"] == "AUSTIN" and c["state"] == "TX"
    assert c["title"] == "senior software engineer ii" and c["family"] == "software engineer"
    assert c["n"] == 3 and c["wage_min"] == 120000 and c["wage_median"] == 124800 and c["wage_max"] == 130000
    assert (c["lvl2"], c["lvl3"]) == (2, 1) and c["pw2"] == 110000 and c["pw3"] == 125000
    assert c["soc_code"] == "15-1252.00"
    assert agg.cells(min_count=4) == []
    assert agg.rows_seen == 8 and agg.rows_kept == 3


def test_lca_reader_accepts_both_dependent_spellings_and_skips_blank_rows(tmp_path):
    import openpyxl
    for dep_header in ("H-1B_DEPENDENT", "H_1B_DEPENDENT"):
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["CASE_NUMBER", "CASE_STATUS", "DECISION_DATE", "VISA_CLASS", "JOB_TITLE", "SOC_CODE",
                   "EMPLOYER_NAME", "WORKSITE_CITY", "WORKSITE_STATE", "WAGE_RATE_OF_PAY_FROM",
                   "WAGE_RATE_OF_PAY_TO", "WAGE_UNIT_OF_PAY", "PREVAILING_WAGE", "PW_UNIT_OF_PAY",
                   "PW_WAGE_LEVEL", dep_header, "WILLFUL_VIOLATOR"])
        ws.append(["I-200-1", "Certified", "2025-07-01", "H-1B", "Data Analyst", "15-2041.00", "Acme Corp",
                   "Austin", "TX", 90000, 95000, "Year", 85000, "Year", "I", "Yes", "No"])
        ws.append([None] * 17)
        p = tmp_path / f"lca_{dep_header}.xlsx"; wb.save(p)
        rows = list(lca.iter_lca_rows(p))
        assert len(rows) == 1 and rows[0]["dependent"] == "Yes" and rows[0]["employer"] == "Acme Corp"
    with pytest.raises(ValueError, match="missing columns"):
        lca.header_index(["CASE_NUMBER", "JOB_TITLE"])


def test_title_normalization_and_family():
    assert lca.normalize_title("Software Engineer II (Backend)") == "software engineer ii"
    assert lca.title_family("Sr. Software Engineer II") == "software engineer"
    assert lca.title_family("Staff Data Scientist, Level 3") == "data scientist"
    assert lca.title_family("Engineer") == "engineer"
    assert lca.fiscal_year_of("2025-10-01") == 2026 and lca.fiscal_year_of("09/30/2025") == 2025
    assert lca.fiscal_year_of("") == 0


def test_wages_narrow_from_exact_title_and_city_to_the_employer():
    db = _acme_db()
    w = db.wages("Acme Corp", "Senior Software Engineer II", "Austin", "TX")
    assert w["scope"] == "title_city" and w["n"] == 3 and w["wage_median"] == 125000
    assert w["top_level"] == 2 and w["pw2"] == 110000 and w["title_label"] == "Senior Software Engineer II"
    w = db.wages("Acme Corp", "Senior Software Engineer II", "Houston", "TX")
    assert w["scope"] == "title_state" and w["n"] == 3
    w = db.wages("Acme Corp", "Principal Software Engineer", "Austin", "TX")
    assert w["scope"] == "family_city" and w["n"] == 5 and w["wage_min"] == 120000 and w["wage_max"] == 165000
    assert w["levels"] == {1: 0, 2: 3, 3: 2, 4: 0} and w["pw3"] == 150000
    w = db.wages("Acme Corp", "Principal Software Engineer", "Dallas", "TX")
    assert w["scope"] == "family_city" and w["n"] == 2 and w["top_level"] == 1
    w = db.wages("Acme Corp", "Marketing Manager", "Austin", "TX")
    assert w["scope"] == "employer" and w["n"] == 9 and w["title_label"] == "all titles"
    assert db.wages("Nowhere Corp", "Engineer", "Austin", "TX") is None
    assert db.wages("Acme Corp") ["scope"] == "employer"
    db.close()


def test_wage_bundle_writes_loads_once_per_version_and_syncs_from_a_host(tmp_path):
    db = _acme_db()
    agg = lca.LCAAggregate()
    for w in (120000, 130000, 125000):
        agg.add(_lca_row(wage_from=w))
    cells, flags = agg.cells(), agg.flags()
    m = lca.write_lca_bundle(tmp_path / "lca", cells, flags, "2026-10", [2025], sources=["x.xlsx"])
    assert m["cells"] == 1 and m["employers"] == 1 and (tmp_path / "lca" / "wages.csv.gz").exists()
    assert json.loads((tmp_path / "lca" / "latest.json").read_text())["version"] == "2026-10"
    fresh = SponsorDB(":memory:")
    r = lca.load_lca_bundle(fresh, tmp_path / "lca")
    assert r["status"] == "loaded" and r["cells"] == 1
    assert fresh.wages("Acme Corp", "Senior Software Engineer II", "Austin", "TX")["n"] == 3
    assert lca.load_lca_bundle(fresh, tmp_path / "lca")["status"] == "up_to_date"
    assert lca.load_lca_bundle(fresh, tmp_path / "nope")["status"] == "absent"
    # Hosted: the same three files behind a base URL, applied once per version.
    files = {f"https://host/data/{lca.MANIFEST_FILE}": (tmp_path / "lca" / "latest.json").read_bytes(),
             f"https://host/data/{lca.WAGES_FILE}": (tmp_path / "lca" / "wages.csv.gz").read_bytes(),
             f"https://host/data/{lca.FLAGS_FILE}": (tmp_path / "lca" / "flags.csv.gz").read_bytes()}
    hosted = SponsorDB(":memory:")
    r = lca.sync_lca_bundle(hosted, "https://host/data", fetch=lambda u: files[u])
    assert r["status"] == "merged" and r["cells"] == 1 and hosted.lca_stats()["cells"] == 1
    assert lca.sync_lca_bundle(hosted, "https://host/data", fetch=lambda u: files[u])["status"] == "up_to_date"
    r = lca.sync_lca_bundle(SponsorDB(":memory:"), "https://host/data",
                            fetch=lambda u: (_ for _ in ()).throw(OSError("offline")))
    assert r["status"] == "unreachable"
    db.close(); fresh.close(); hosted.close()


def test_import_script_streams_a_workbook_into_the_store_and_the_bundle(tmp_path):
    import openpyxl
    from scripts.import_lca_disclosure import import_lca
    wb = openpyxl.Workbook(); ws = wb.active
    hdr = ["CASE_STATUS", "DECISION_DATE", "VISA_CLASS", "JOB_TITLE", "SOC_CODE", "EMPLOYER_NAME",
           "WORKSITE_CITY", "WORKSITE_STATE", "WAGE_RATE_OF_PAY_FROM", "WAGE_UNIT_OF_PAY",
           "PREVAILING_WAGE", "PW_UNIT_OF_PAY", "PW_WAGE_LEVEL", "H_1B_DEPENDENT", "WILLFUL_VIOLATOR"]
    ws.append(hdr)
    for w in (100000, 110000):
        ws.append(["Certified", "2025-02-01", "H-1B", "Analyst", "13-2051.00", "Beta LLC", "Boston", "MA",
                   w, "Year", 95000, "Year", "II", "No", "No"])
    ws.append(["Certified", "2024-02-01", "H-1B", "Analyst", "13-2051.00", "Beta LLC", "Boston", "MA",
               90000, "Year", 85000, "Year", "I", "Yes", "No"])
    p = tmp_path / "LCA_Disclosure_Data_FY2025_Q2.xlsx"; wb.save(p)
    logs = []
    r = import_lca([p], db_path=tmp_path / "s.db", out_dir=tmp_path / "out", version="t1",
                   keep_fys=1, log=logs.append)
    assert r["cells"] == 1 and r["stats"]["cells"] == 1            # the FY2024 single filing is dropped
    m = json.loads((tmp_path / "out" / "latest.json").read_text())
    assert m["fiscal_years"] == [2025] and m["cells"] == 1 and m["sources"] == [p.name]
    db = SponsorDB(tmp_path / "s.db")
    assert db.history("Beta")["flags"]["h1b_dependent"] is True
    assert db.wages("Beta LLC", "Analyst", "Boston", "MA")["wage_median"] == 105000
    db.close()
    assert any("1 cells" in l for l in logs)


# ---------------------------------------------------------------- C. lottery + offer

def test_lottery_odds_follow_the_weights_and_stay_below_one():
    o = lottery_odds(2)
    assert o["entries"] == 2 and 0 < o["by_level"][1] < o["by_level"][2] < o["by_level"][3] < o["by_level"][4] <= 1
    assert o["registrations"] == 211_600 and o["selections"] == 120_141 and o["mix"][2] == 0.55
    # With everyone a Level I registrant the weighting changes nothing: odds = S / R.
    flat = lottery_odds(1, registrations=100_000, selections=25_000, mix={1: 1.0})
    assert flat["probability"] == pytest.approx(0.25, abs=1e-3)
    # More selections than registrations: certain selection at every level.
    assert lottery_odds(3, registrations=10, selections=50)["by_level"] == {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}
    # The expected selections across levels equal S (the solver's invariant).
    R, S = 300_000, 100_000
    o = lottery_odds(1, R, S)
    assert sum(R * DEFAULT_MIX[l] * o["by_level"][l] for l in (1, 2, 3, 4)) == pytest.approx(S, rel=0.01)
    # Weighting pushes Level I's share of selections well under its 28% share of registrations
    # (USCIS has not published the real FY2027 per-level split; see the module docstring).
    o = lottery_odds(1)
    share1 = o["registrations"] * o["mix"][1] * o["by_level"][1] / o["selections"]
    assert 0.12 < share1 < 0.24
    assert lottery_odds(9)["level"] == 4 and lottery_odds(0)["level"] == 1
    assert "not legal advice" in o["note"]


def test_likely_level_uses_the_employers_own_thresholds():
    pw = {1: 90000, 2: 110000, 3: 150000, 4: 0}
    assert likely_level(95000, pw) == 1 and likely_level(149999, pw) == 2 and likely_level(200000, pw) == 3
    assert likely_level(50000, pw) == 1                   # below Level I's wage: still Level I
    assert likely_level(50000, {3: 150000}) == 0          # only Level III known: unknown
    assert likely_level(50000, {}) == 0


def test_offer_check_states_position_level_odds_and_three_points():
    db = _acme_db()
    w = db.wages("Acme Corp", "Principal Software Engineer", "Austin", "TX")   # family: pw2 110k, pw3 150k
    r = offer_check(140000, w, "Principal Software Engineer", "Austin", "TX")
    assert r["has_data"] and r["position"] == "above_median" and r["likely_level"] == 2
    assert r["odds"]["level"] == 2 and r["odds"]["entries"] == 2
    assert len(r["talking_points"]) == 3
    assert "$150,000" in r["talking_points"][1] and "3 lottery entries" in r["talking_points"][1]
    assert "Level II" in r["level_basis"] and "not legal advice" in r["note"]
    r = offer_check(200000, w, "Principal Software Engineer", "Austin", "TX")
    assert r["position"] == "above_range" and r["likely_level"] == 3
    assert "highest prevailing-wage level" in r["talking_points"][1]
    r = offer_check(100000, w, "Principal Software Engineer", "Austin", "TX")
    assert r["position"] == "below_range" and r["likely_level"] == 0 and r["odds"] is None
    r = offer_check(100000, None, "Engineer")
    assert not r["has_data"] and len(r["talking_points"]) == 3
    assert offer_check(0, w)["error"]
    db.close()


# ---------------------------------------------------------------- endpoints

@pytest.fixture
def client(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setenv("JOBS_SYNC_SEED", "1")
    monkeypatch.setattr(app, "_SPONSORS_DB", str(tmp_path / "sp.db"))
    monkeypatch.setattr(app, "BUNDLED_H1B_SEED", str(tmp_path / "no-seed.csv.gz"))
    monkeypatch.setattr(app, "BUNDLED_PERM", tmp_path / "no-perm.csv")
    monkeypatch.setattr(app, "BUNDLED_EVERIFY_GZ", tmp_path / "no-ev.csv.gz")
    monkeypatch.setattr(app, "BUNDLED_EVERIFY", tmp_path / "no-ev.csv")
    monkeypatch.setattr(app, "BUNDLED_H1B_HISTORY", str(tmp_path / "no-hist.csv.gz"))
    monkeypatch.setattr(app, "BUNDLED_PERM_YEARS", str(tmp_path / "no-py.csv.gz"))
    monkeypatch.setattr(app, "BUNDLED_LCA_DIR", str(tmp_path / "no-lca"))
    app._SPONSORS.clear()
    db = app._sponsors()
    db.ingest_h1b_rows(_h1b_rows())
    agg = lca.LCAAggregate()
    for w in (120000, 130000, 125000):
        agg.add(_lca_row(wage_from=w))
    db.ingest_lca_wage_rows(agg.cells()); db.ingest_lca_flag_rows(agg.flags())
    yield app.app.test_client()
    app._SPONSORS.clear()


def test_history_endpoint(client):
    d = client.get("/api/sponsors/Acme%20Corp/history").get_json()
    assert d["matched"] and [y["fy"] for y in d["years"]] == [2022, 2023]
    assert d["years"][0]["denials"] == 3 and d["flags"] == {"h1b_dependent": False, "willful_violator": False,
                                                            "cap_exempt": False, "e_verify": False}
    assert d["denial_rate_5y"] == pytest.approx(0.158, abs=0.001)
    d = client.get("/api/sponsors/Nowhere%20Corp/history").get_json()
    assert d["matched"] is False and d["years"] == []


def test_wages_endpoint_reads_city_and_state_from_a_location(client):
    d = client.get("/api/sponsors/Acme%20Corp/wages?title=Senior%20Software%20Engineer%20II&location=Austin,%20TX").get_json()
    assert d["found"] and d["city"] == "Austin" and d["state"] == "TX"
    assert d["wages"]["scope"] == "title_city" and d["wages"]["wage_median"] == 125000
    d = client.get("/api/sponsors/Acme%20Corp/wages?title=Senior%20Software%20Engineer%20II&city=Austin&state=tx").get_json()
    assert d["found"] and d["wages"]["n"] == 3
    d = client.get("/api/sponsors/Nowhere/wages?title=X").get_json()
    assert d["found"] is False and d["wages"] is None


def test_offer_check_endpoint_accepts_k_shorthand_and_rejects_junk(client):
    d = client.post("/api/sponsors/offer-check", json={"company": "Acme Corp", "title": "Senior Software Engineer II",
                                                        "location": "Austin, TX", "offer": "118k"}).get_json()
    assert d["offer"] == 118000 and d["position"] == "below_range" and d["likely_level"] == 2
    assert d["odds"]["percent"] > 0 and len(d["talking_points"]) == 3 and "not legal advice" in d["note"]
    assert d["wages"]["n"] == 3
    r = client.post("/api/sponsors/offer-check", json={"company": "Acme Corp", "offer": "lots"})
    assert r.status_code == 400 and "annual salary" in r.get_json()["error"]


def test_status_reports_the_depth_and_the_bundles_load_on_startup(tmp_path, monkeypatch):
    import ui.app as app
    hist = tmp_path / "hist.csv.gz"
    with gzip.open(hist, "wt", newline="") as f:
        w = csv.writer(f); w.writerow(H1B_YEAR_COLS)
        w.writerow(["acme", 2021, 4, 1, 2, 0])
    py = tmp_path / "perm_years.csv.gz"
    with gzip.open(py, "wt", newline="") as f:
        w = csv.writer(f); w.writerow(["EMPLOYER_NAME", "FY", "PERM_CERTS"]); w.writerow(["ACME CORP", 2022, 6])
    agg = lca.LCAAggregate()
    for wage in (120000, 130000):
        agg.add(_lca_row(wage_from=wage))
    lca.write_lca_bundle(tmp_path / "lca", agg.cells(), agg.flags(), "v1", [2025])
    monkeypatch.setenv("JOBS_SYNC_SEED", "1")
    monkeypatch.setattr(app, "_SPONSORS_DB", str(tmp_path / "sp.db"))
    monkeypatch.setattr(app, "BUNDLED_H1B_SEED", str(tmp_path / "no-seed.csv.gz"))
    monkeypatch.setattr(app, "BUNDLED_PERM", tmp_path / "no-perm.csv")
    monkeypatch.setattr(app, "BUNDLED_EVERIFY_GZ", tmp_path / "no-ev.csv.gz")
    monkeypatch.setattr(app, "BUNDLED_EVERIFY", tmp_path / "no-ev.csv")
    monkeypatch.setattr(app, "BUNDLED_H1B_HISTORY", str(hist))
    monkeypatch.setattr(app, "BUNDLED_PERM_YEARS", str(py))
    monkeypatch.setattr(app, "BUNDLED_LCA_DIR", str(tmp_path / "lca"))
    app._SPONSORS.clear()
    try:
        c = app.app.test_client()
        s = c.get("/api/sponsors/status").get_json()
        assert s["history_fiscal_years"] == [2021] and s["lca"]["cells"] == 1
        assert s["lca_bundle_version"] == "v1"
        d = c.get("/api/sponsors/Acme%20Corp/history").get_json()
        assert [(y["fy"], y["approvals"], y["denials"], y["perm"]) for y in d["years"]] == [(2021, 6, 1, 0), (2022, 0, 0, 6)]
        # A refresh re-runs the loaders without doubling anything.
        monkeypatch.setattr(app, "download_h1b_rows", lambda fy: (_ for _ in ()).throw(app.H1BYearUnavailable(fy)))
        r = c.post("/api/sponsors/refresh", json={"fiscal_years": [2023]}).get_json()
        assert r["lca"]["status"] == "disabled"
        d = c.get("/api/sponsors/Acme%20Corp/history").get_json()
        assert d["years"][-1]["perm"] == 6 and c.get("/api/sponsors/status").get_json()["lca"]["cells"] == 1
    finally:
        app._SPONSORS.clear()


# ---------------------------------------------------------------- extension index + bundler

def _bsi():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_sponsor_index.py"
    spec = importlib.util.spec_from_file_location("build_sponsor_index_depth", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_index_records_carry_the_compact_facts_only_when_present(tmp_path):
    bsi = _bsi()
    db = _acme_db()
    shards = bsi.collect(db)
    rec = shards["a"]["acme"]
    # [display, approvals, last_fy, first_fy, perm, e_verify, cap, naics2, state, 5y, denial%, flags]
    assert rec[1] == 32 and rec[9] == 32 and rec[10] == 16 and rec[11] == 1
    other = shards["o"]["other"]
    assert other[9] == 3 and other[10] == 75 and len(other) == 11      # no flags: trailing zero dropped
    row = db._conn.execute("SELECT * FROM sponsor_employer WHERE norm_name='acme'").fetchone()
    plain = bsi.encode_record("acme", row)                 # no depth facts: 5y and denial% stay 0
    assert len(plain) == 12 and plain[9:] == [0, 0, 1]
    assert bsi.encode_record("acme", row, {"h1b_5y": 32, "denial_pct": 16}) == rec
    m = bsi.write_index(db, tmp_path / "idx")
    assert m["employers"] == 2
    db.close()


def test_the_extension_decoder_reads_the_compact_facts():
    import subprocess
    core = Path(__file__).resolve().parents[1] / "extension" / "sponsor_core.js"
    js = (f"const SponsorCore = require({json.dumps(str(core))});"
          "const r = SponsorCore._decode ? SponsorCore._decode('acme', [0, 32, 2023, 2022, 3, 0, 0, '54', 'CA', 32, 16, 3]) : null;"
          "console.log(JSON.stringify(r));")
    src = core.read_text(encoding="utf-8")
    assert "h1b_5y: num(9)" in src and "willful_violator: (num(11) & 2) === 2" in src
    try:
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        pytest.skip("node not installed")
    if out.returncode == 0 and out.stdout.strip() not in ("", "null"):
        d = json.loads(out.stdout)
        assert d["h1b_5y"] == 32 and d["denial_pct"] == 16 and d["h1b_dependent"] and d["willful_violator"]


def test_the_installer_bundler_accepts_the_depth_tables(tmp_path):
    path = Path(__file__).resolve().parents[1] / "packaging" / "bundle_sponsor_db.py"
    spec = importlib.util.spec_from_file_location("bundle_sponsor_db_depth", path)
    b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
    db = _acme_db()
    db.close()
    p = tmp_path / "with_depth.db"
    d2 = SponsorDB(p); d2.ingest_h1b_rows(_h1b_rows()); d2.close()
    assert b.vet(p, min_employers=1) == ""


def test_the_installer_bundler_layers_the_depth_bundles_into_the_staged_db(tmp_path):
    """The staged sponsors.db must carry the three depth tables (an installed app has no
    scripts to run), from the shipped history, PERM-by-year and LCA bundle files, and layering
    them twice changes nothing."""
    from sourcing.sponsors import SEED_COLS
    path = Path(__file__).resolve().parents[1] / "packaging" / "bundle_sponsor_db.py"
    spec = importlib.util.spec_from_file_location("bundle_sponsor_db_layer", path)
    b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
    seed = tmp_path / "seed.csv.gz"
    with gzip.open(seed, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(SEED_COLS)
        w.writerow(["acme", "Acme Corp", 30, 2023, 2022, "541511", "CA", 0, 0, 3])   # key is normalized
    history = tmp_path / "h1b_history.csv.gz"
    with gzip.open(history, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(H1B_YEAR_COLS)
        w.writerow(["acme", 2022, 11, 2, 6, 1]); w.writerow(["acme", 2023, 8, 0, 7, 3])
    perm_years = tmp_path / "perm_by_year.csv.gz"
    with gzip.open(perm_years, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["EMPLOYER_NAME", "FY", "PERM_CERTS"])
        w.writerow(["ACME CORP", 2024, 2]); w.writerow(["ACME CORP", 2023, 1])
    agg = lca.LCAAggregate()
    for wage in (120000, 130000):
        agg.add(_lca_row(wage_from=wage, dependent="Yes"))
    lca.write_lca_bundle(tmp_path / "lca", agg.cells(), agg.flags(), "v-test", [2025])
    logs = []
    r = b.build(seed=seed, source=tmp_path / "nope.db", dest=tmp_path / "pk" / "sponsors.db",
                min_employers=1, everify=tmp_path / "no-everify.csv.gz", log=logs.append,
                history=history, perm_years=perm_years, lca_dir=tmp_path / "lca")
    assert r["used"] == "seed" and r["h1b"] == 1
    assert any("2 employer-years" in l and "2 PERM-by-year rows" in l and "1 LCA wage cells" in l
               for l in logs), logs
    db = SponsorDB(tmp_path / "pk" / "sponsors.db")
    try:
        h = db.history("Acme Corp")
        assert [y["fy"] for y in h["years"]] == [2022, 2023, 2024]
        assert h["years"][0]["approvals"] == 17 and h["years"][2]["perm"] == 2
        assert h["flags"]["h1b_dependent"] is True
        assert db.wages("Acme Corp", "Senior Software Engineer", "Austin", "TX")["n"] == 2
        assert db.get_meta("lca_bundle_version") == "v-test"
        # Idempotent: a second layering (the local-DB path does one) leaves the counts alone.
        b._layer_depth(db, history=history, perm_years=perm_years, lca_dir=tmp_path / "lca")
        assert db.history("Acme Corp")["years"][2]["perm"] == 2 and db.lca_stats()["cells"] == 1
        assert b.vet(tmp_path / "pk" / "sponsors.db", min_employers=1) == ""
    finally:
        db.close()
    # Pointed at nothing, the bundler still stages a plain database (CI without the bundles).
    r2 = b.build(seed=seed, source=tmp_path / "nope.db", dest=tmp_path / "pk2" / "sponsors.db",
                 min_employers=1, everify=tmp_path / "no-everify.csv.gz", log=logs.append,
                 history=tmp_path / "none.gz", perm_years=tmp_path / "none2.gz",
                 lca_dir=tmp_path / "no-lca")
    assert r2["used"] == "seed"
    db = SponsorDB(tmp_path / "pk2" / "sponsors.db")
    try:
        assert db.history("Acme Corp")["years"] == [] and db.lca_stats()["cells"] == 0
    finally:
        db.close()


def test_a_history_only_employer_has_a_history_but_no_sponsor_record_and_no_index_entry():
    """The year-by-year bundle covers employers the totals seed lacks; the ingest adds a bare
    row so history() can find them. That bare row is NOT a sponsor record: lookup() ignores it
    (no badge, no "matched"), and the extension index drops it, so the app and the JS port
    answer alike."""
    db = SponsorDB(":memory:")
    db.ingest_h1b_year_rows([{"norm_name": "ghost labs", "fy": 2023, "initial_approvals": 4,
                              "initial_denials": 1, "continuing_approvals": 0, "continuing_denials": 0}])
    assert db.lookup("Ghost Labs") is None
    assert db.tag_jobs([{"company": "Ghost Labs", "location": "Austin, TX"}])[0]["visa"] == []
    h = db.history("Ghost Labs")
    assert h and h["years"][0]["approvals"] == 4 and h["denial_rate_5y"] == 0.2
    assert all(not rec for rec in _bsi().collect(db).values())
    # A flag from the LCA attestations is a signal (the index keeps those rows), so it matches.
    db.ingest_lca_flag_rows([{"norm_name": "ghost labs", "display_name": "Ghost Labs",
                              "h1b_dependent": 1, "willful_violator": 0}])
    assert db.lookup("Ghost Labs") is not None and db.lookup("Ghost Labs").h1b_approvals == 0
    assert "ghost labs" in _bsi().collect(db)["g"]


def test_release_urls_follow_dols_two_layouts():
    assert lca.lca_release_url(2025, 4).endswith("/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx")
    assert lca.lca_release_url(2026, 3) == "https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx"
