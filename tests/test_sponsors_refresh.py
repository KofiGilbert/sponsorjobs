"""POST /api/sponsors/refresh must be IDEMPOTENT: H-1B ingest sums approvals across
fiscal years, so re-ingesting an already-counted year doubles the counts. Each click of
'Update visa data' previously corrupted sponsors.db — this pins the fix.
"""

from __future__ import annotations

import ui.app as app

FY2023 = [{"Fiscal Year": "2023", "Employer": "GOOGLE LLC", "Initial Approval": "50",
           "Continuing Approval": "400", "NAICS": "54", "State": "CA"}]


def _fresh_app(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_DATA", tmp_path)
    app._SPONSORS.clear()                       # force a fresh SponsorDB under tmp
    monkeypatch.setattr(app, "download_h1b_rows", lambda fy: FY2023 if int(fy) == 2023 else [])
    monkeypatch.setattr(app, "_load_bundled_perm", lambda db: None)
    monkeypatch.setattr(app, "_load_bundled_everify", lambda db: None)
    monkeypatch.setattr(app, "_load_bundled_h1b", lambda db: 0)   # the real seed is ~105k rows
    monkeypatch.delenv("TAILOR_DATA_URL", raising=False)
    return app


def test_refresh_does_not_double_count_on_repeat(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    client = a.app.test_client()
    r1 = client.post("/api/sponsors/refresh", json={"fiscal_years": [2023]}).get_json()
    assert a._sponsors().lookup("Google").h1b_approvals == 450   # 50 + 400
    assert r1["skipped_fys"] == []

    # Second click of "Update visa data" must NOT re-ingest FY2023.
    r2 = client.post("/api/sponsors/refresh", json={"fiscal_years": [2023]}).get_json()
    assert r2["skipped_fys"] == [2023]
    assert a._sponsors().lookup("Google").h1b_approvals == 450   # still 450, not 900

    # A NEW fiscal year still ingests; force re-ingests when explicitly asked.
    assert 2023 in r2["fiscal_years"]


def test_refresh_force_reingests(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    client = a.app.test_client()
    client.post("/api/sponsors/refresh", json={"fiscal_years": [2023]})
    client.post("/api/sponsors/refresh", json={"fiscal_years": [2023], "force": True})
    # force re-ingests the same year -> additive (this is the manual-reset escape hatch).
    assert a._sponsors().lookup("Google").h1b_approvals == 900


def test_default_refresh_pulls_new_years_and_skips_shipped(tmp_path, monkeypatch):
    """The shipped DB already has FY2020-2023, so the default refresh must SKIP those and
    try the newer years (the old default of [2023..2020] made the button a no-op)."""
    from sourcing.sponsors import H1BYearUnavailable
    a = _fresh_app(tmp_path, monkeypatch)
    a._sponsors().set_h1b_fys_ingested([2020, 2021, 2022, 2023])
    fy2024 = [{"Fiscal Year": "2024", "Employer": "STRIPE INC", "Initial Approval": "10",
               "Continuing Approval": "5", "NAICS": "52", "State": "CA"}]

    def fake_download(fy):
        if int(fy) == 2024:
            return fy2024
        if int(fy) > 2024:
            raise H1BYearUnavailable(fy)       # USCIS publishes no file for these
        return [{"Fiscal Year": str(fy), "Employer": "OLD CO", "Initial Approval": "1",
                 "Continuing Approval": "0", "NAICS": "52", "State": "CA"}]
    monkeypatch.setattr(a, "download_h1b_rows", fake_download)
    r = a.app.test_client().post("/api/sponsors/refresh", json={}).get_json()   # default FYs
    assert r["ok"] is True and r["errors"] == []
    assert set(r["skipped_fys"]) == {2020, 2021, 2022, 2023}   # shipped years not re-ingested
    assert 2024 in r["loaded_fys"] and 2019 in r["loaded_fys"]  # probe year + oldest default
    assert 2025 in r["unavailable_fys"] and 2025 not in r["fiscal_years"]  # a 404 is not an error
    assert a._sponsors().lookup("Stripe").h1b_approvals == 15  # the NEW fiscal year ingested
    assert "FY2019 to 2024" in r["note"]


def test_refresh_with_only_unavailable_years_is_a_note_not_an_error(tmp_path, monkeypatch):
    """The Jobs page shows `note` as a calm line and only `errors` in the error style, so a
    refresh where every probed year is one USCIS never published must come back ok, with no
    errors, and a note that explains why plus the span the data does cover."""
    from sourcing.sponsors import H1BYearUnavailable
    a = _fresh_app(tmp_path, monkeypatch)
    a._sponsors().set_h1b_fys_ingested([2020, 2021, 2022, 2023])

    def gone(fy):
        raise H1BYearUnavailable(fy)
    monkeypatch.setattr(a, "download_h1b_rows", gone)
    r = a.app.test_client().post("/api/sponsors/refresh", json={"fiscal_years": [2024, 2025]}).get_json()
    assert r["ok"] is True and r["errors"] == [] and r["loaded_fys"] == []
    assert r["unavailable_fys"] == [2024, 2025]
    assert "FY2023" in r["note"] and "FY2020 to 2023" in r["note"]

    # A real failure (offline, blocked) IS an error, and still never a crash.
    def blocked(fy):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(a, "download_h1b_rows", blocked)
    r = a.app.test_client().post("/api/sponsors/refresh", json={"fiscal_years": [2024]}).get_json()
    assert r["ok"] is False and r["errors"] == [{"fy": 2024, "error": "HTTP 403"}]


def test_status_reports_the_h1b_fiscal_year_span(tmp_path, monkeypatch):
    a = _fresh_app(tmp_path, monkeypatch)
    client = a.app.test_client()
    client.post("/api/sponsors/refresh", json={"fiscal_years": [2023]})
    s = client.get("/api/sponsors/status").get_json()
    assert s["h1b_fiscal_years"] == [2023]
    assert s["h1b_fy_span"] == "FY2023"
