"""Compliance + safety hardening for sourcing/ (CLAUDE.md §6 GREEN/RED lanes). Pins the
invariants an adversarial audit found unenforced or untested:

  * the sponsor downloader identifies HONESTLY — no browser-UA masquerade, no curl TLS-
    fingerprint evasion; if a host blocks the honest client it fails to the file-import path;
  * a board_id can never escape the official ATS host (the Recruitee adapter put it in the
    HOST position → an SSRF sink);
  * the sponsor-index lookup is race-free against a concurrent ingest (the server is threaded);
  * every adapter fetches only its sanctioned official host, and one bad feed can't crash a refresh.
"""
from __future__ import annotations

import threading
from urllib.parse import urlparse

import pytest

from sourcing import ats
from sourcing.ats import normalize_jobs, valid_board_id
from sourcing.service import refresh_watchlist
from sourcing.sponsors import SponsorDB, _UA, _http_get_text
from sourcing.watchlist import Watchlist


# ============================ bug 1: honest client, no evasion ============================
def test_sponsor_ua_is_honest_not_a_browser_masquerade():
    assert "resume-agent" in _UA.lower()
    for masq in ("mozilla", "chrome", "safari", "applewebkit", "gecko"):
        assert masq not in _UA.lower(), f"{masq} in UA reads as a browser masquerade (§7)"


def test_sponsors_module_shells_out_to_nothing():
    # The curl TLS-fingerprint fallback is gone — no subprocess/shutil evasion anywhere.
    import sourcing.sponsors as s
    assert not hasattr(s, "subprocess") and not hasattr(s, "shutil")


def test_http_get_text_fails_honestly_without_evading(monkeypatch):
    import sourcing.sponsors as s

    def boom(req, timeout=None):
        raise OSError("blocked by host")

    monkeypatch.setattr(s.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        _http_get_text("https://www.uscis.gov/some/file.csv")
    # points the user at the sanctioned path instead of masquerading to get through
    assert "import" in str(ei.value).lower()


# ============================ bug 2: board_id can't escape the official host (SSRF) ============================
def test_valid_board_id_accepts_real_slugs_rejects_host_escapes():
    for good in ("dropbox", "acme", "senior-eng", "a.b_c-1", "Acme123"):
        assert valid_board_id(good), good
    for bad in ("internal-host.local/", "x@evil.com", "evil.com/x", "localhost:9200/_search#",
                "a b", "../etc", "x?y=1", "x#frag", "", "//evil", "x\\y"):
        assert not valid_board_id(bad), bad


def test_recruitee_ssrf_board_id_is_rejected_before_any_fetch():
    calls = []

    def fetch(url, **k):
        calls.append(url)
        return {}

    # The Recruitee adapter interpolates board_id into the HOST — a host-escape must never fetch.
    with pytest.raises(ValueError):
        normalize_jobs("recruitee", "internal-host.local/", "Evil", fetch=fetch)
    assert calls == []                       # no request ever left for the attacker's host


def test_recruitee_valid_board_stays_on_recruitee_host():
    seen = {}

    def fetch(url, **k):
        seen["url"] = url
        return {}

    normalize_jobs("recruitee", "acme", "Acme", fetch=fetch)
    assert urlparse(seen["url"]).hostname == "acme.recruitee.com"


def test_add_company_rejects_a_host_escaping_board_id(tmp_path):
    w = Watchlist(str(tmp_path / "w.db"))
    try:
        with pytest.raises(ValueError):
            w.add_company("Evil", "recruitee", "evil.com/")
        assert w.companies(active_only=False) == []      # never stored
    finally:
        w.close()


def test_watchlist_add_endpoint_400s_on_bad_board_id(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    r = app.app.test_client().post("/api/watchlist",
                                   json={"ats": "recruitee", "board_id": "evil.com/"})
    assert r.status_code == 400 and "board id" in r.get_json()["error"].lower()


# ============================ bug 4: sponsor-index lookup is race-free ============================
def test_lookup_is_race_free_against_concurrent_invalidation():
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows([{"Employer": "Acme Corp", "Initial Approval": "5", "Continuing Approval": "0"}])
    db.lookup("Acme")                      # warm the index
    errors: list = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                db.lookup("Acme")
            except Exception as exc:       # a raced None-index used to 500 the jobs feed
                errors.append(repr(exc))
                return

    def invalidator():
        for _ in range(3000):
            db._invalidate_index()

    t = threading.Thread(target=reader)
    t.start()
    invalidator()
    stop.set()
    t.join()
    assert not errors, errors


# ===================== sponsor lookup: on-disk (no 290MB RAM index), behaviour-preserving =========
def _seed_sponsor(db, rows):
    """rows: (norm_name, display, h1b, perm). Insert directly so the test controls exact norms."""
    for nn, disp, h1b, perm in rows:
        db._conn.execute(
            "INSERT OR REPLACE INTO sponsor_employer(norm_name, display_name, h1b_approvals, "
            "perm_certs, h1b_last_fy, h1b_first_fy) VALUES(?,?,?,?,?,?)",
            (nn, disp, h1b, perm, 2023, 2020))
    db._conn.commit()
    db._invalidate_index()


def test_lookup_reads_disk_not_a_prebuilt_ram_index():
    # The whole point of the refactor: no ~290MB in-memory index is ever built (that boot-time CPU
    # spike starved the 1-CPU broker's health check). A fresh DB answers lookups straight from SQLite.
    db = SponsorDB(":memory:")
    assert not hasattr(db, "_index") and not hasattr(db, "_by_first")
    _seed_sponsor(db, [("acme corp filings", "Acme Corp Filings", 7, 0)])
    assert db.lookup("Acme Corp Filings").h1b_approvals == 7        # works with no build step


def test_targeted_query_equals_a_full_scan_over_all_rows():
    # The targeted SQL (exact + parents + children) must return exactly what filtering EVERY row
    # through the same matcher would — proof the query didn't change badge results.
    db = SponsorDB(":memory:")
    _seed_sponsor(db, [
        ("amazon", "Amazon", 10, 1), ("amazon web services", "Amazon Web Services", 100, 5),
        ("amazon data services", "Amazon Data Services", 50, 2),
        ("acme", "Acme", 5, 0), ("acme dynamite", "Acme Dynamite", 3, 0),
        ("first", "First Co", 3, 0), ("first united bank", "First United Bank", 0, 0)])

    def full_scan(company):
        from sourcing.sponsors import (SponsorRecord, normalize_employer, _ALIASES,
                                        _BRAND_PREFIXES)
        norm = _ALIASES.get(normalize_employer(company), normalize_employer(company))
        if not norm:
            return None
        ap = (" " in norm) or (norm in _BRAND_PREFIXES)
        allrows = {r["norm_name"]: SponsorRecord(
            r["display_name"], r["h1b_approvals"], r["h1b_last_fy"], r["naics"], r["state"],
            bool(r["cap_exempt"]), bool(r["e_verify"]), r["h1b_first_fy"], r["perm_certs"])
            for r in db._conn.execute("SELECT * FROM sponsor_employer")}
        return db._match(norm, ap, allrows)

    for q in ["Amazon", "Amazon Web Services", "Acme", "Acme Dynamite", "First United Bank",
              "Nowhere", "amazon inc"]:
        a, b = db.lookup(q), full_scan(q)
        ka = None if a is None else (a.display_name, a.h1b_approvals, a.perm_certs)
        kb = None if b is None else (b.display_name, b.h1b_approvals, b.perm_certs)
        assert ka == kb, f"{q!r}: targeted {ka} != full-scan {kb}"


def test_lookup_stays_conservative_no_false_badge_from_a_generic_token():
    # A wrong sponsor badge is worse than a missing one (CLAUDE.md §8). A lone generic token
    # matches EXACTLY; it never lends its badge to an unrelated multi-token employer, and a
    # non-distinctive single-token PARENT never lends downward.
    db = SponsorDB(":memory:")
    _seed_sponsor(db, [("acme", "Acme", 5, 0), ("acme dynamite", "Acme Dynamite", 0, 0),
                       ("first", "First Co", 9, 0), ("first united bank", "First United Bank", 0, 0)])
    assert db.lookup("Acme").h1b_approvals == 5                     # exact only
    assert db.lookup("Acme Dynamite").h1b_approvals == 0           # NOT lifted by "acme"'s 5
    assert db.lookup("First United Bank").h1b_approvals == 0       # NOT lifted by "first"'s 9
    # A curated brand DOES aggregate its entities:
    _seed_sponsor(db, [("amazon", "Amazon", 1, 0), ("amazon web services", "Amazon Web Services", 200, 0)])
    assert db.lookup("Amazon").h1b_approvals == 201


def test_lookup_cache_is_cleared_when_sponsor_data_changes():
    db = SponsorDB(":memory:")
    _seed_sponsor(db, [("acme corp filings", "Acme Corp Filings", 4, 0)])
    assert db.lookup("Acme Corp Filings").h1b_approvals == 4       # caches the result
    db.ingest_h1b_rows([{"Employer": "Acme Corp Filings", "Initial Approval": "6",
                         "Continuing Approval": "0", "Fiscal Year": "2023"}])  # calls _invalidate_index
    assert db.lookup("Acme Corp Filings").h1b_approvals == 10      # 4 + 6, not a stale cached 4


# ============================ test-gap: adapters hit only sanctioned hosts ============================
def test_each_adapter_fetches_only_its_official_host(monkeypatch):
    recorded: list[str] = []

    def rec(url, **k):
        recorded.append(url)
        return {}

    board_cases = [
        (ats.greenhouse_jobs, "boards-api.greenhouse.io"),
        (ats.lever_jobs, "api.lever.co"),
        (ats.ashby_jobs, "api.ashbyhq.com"),
        (ats.workable_jobs, "apply.workable.com"),
        (ats.recruitee_jobs, "acme.recruitee.com"),
        (ats.smartrecruiters_jobs, "api.smartrecruiters.com"),
    ]
    for fn, host in board_cases:
        recorded.clear()
        fn("acme", fetch=rec)
        assert recorded, f"{fn.__name__} made no request"
        for u in recorded:
            assert urlparse(u).hostname == host, (fn.__name__, u)

    recorded.clear()
    ats.remotive_jobs("engineer", fetch=rec)
    assert recorded and urlparse(recorded[0]).hostname == "remotive.com"

    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    recorded.clear()
    ats.adzuna_jobs("engineer", fetch=rec)
    assert recorded and urlparse(recorded[0]).hostname == "api.adzuna.com"


# ============================ Option A: H-1B falls back to the file-import path ============================
def test_sponsors_import_ingests_an_h1b_csv(tmp_path, monkeypatch):
    """With auto-download dropped to honest-only, the official USCIS H-1B CSV must be
    ingestable via the same import endpoint PERM/E-Verify use, so my error message is true."""
    from io import BytesIO

    import ui.app as app
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_SPONSORS", {})      # fresh cached DB under tmp_path
    csv_text = "Employer,Initial Approval,Continuing Approval\nAcme Corp,5,2\n"
    r = app.app.test_client().post(
        "/api/sponsors/import",
        data={"file": (BytesIO(csv_text.encode()), "h1b_datahubexport-2023.csv")},
        content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200 and j.get("ok") and j.get("kind") == "H-1B", j
    assert app._sponsors().lookup("Acme").h1b_approvals == 7    # 5 + 2 summed and looked up


# ============================ test-gap: refresh isolates one bad feed ============================
def test_refresh_isolates_one_failing_feed(tmp_path):
    w = Watchlist(str(tmp_path / "w.db"))
    w.add_company("Acme", "greenhouse", "acme")
    w.add_company("Beta", "greenhouse", "beta")
    w.set_criteria({"titles": [], "locations": [], "remote": "any"})
    GH = {"jobs": [{"id": 1, "title": "Engineer", "location": {"name": "Remote"},
                    "absolute_url": "https://x/y", "content": "hi"}]}

    def fetch(url, **k):
        if "acme" in url:
            raise RuntimeError("boom")     # one feed fails hard
        return GH

    try:
        summary = refresh_watchlist(w, fetch=fetch)
        assert any(e["company"] == "Acme" for e in summary["errors"])   # bad feed surfaced, not crashed
        assert any(j["company"] == "Beta" for j in w.list_jobs())        # good feed still stored
    finally:
        w.close()
