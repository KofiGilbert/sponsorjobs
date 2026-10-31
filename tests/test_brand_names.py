"""Company display names (sourcing/brands.py): job cards show the brand ("Kraken", "1Password")
while sponsor matching keeps the legal filing name ("Payward Inc", "Agilebits Usa Inc")."""

from __future__ import annotations

import csv
from pathlib import Path

from sourcing import growth
from sourcing.brands import brand_name, display_name
from sourcing.feedfile import slim_row
from sourcing.service import seed_if_empty, sync_seed
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]


def test_legal_suffixes_are_dropped_for_display():
    assert brand_name("Datadog Inc") == "Datadog"
    assert brand_name("Red Hat, Inc.") == "Red Hat"
    assert brand_name("Anomali Incorporated") == "Anomali"
    assert brand_name("The University Of Chicago") == "The University of Chicago"
    assert brand_name("Company") == "Company"
    assert display_name("Payward Inc", "Kraken") == "Kraken"
    assert display_name("Stripe") == "Stripe"


def test_the_committed_seed_carries_brands_and_keeps_legal_names():
    rows = {(r["ats"], r["board_id"]): r for r in growth.read_watchlist_seed()}
    kraken = rows[("ashby", "kraken.com")]
    assert kraken["company"] == "Payward Inc" and kraken["display_name"] == "Kraken"
    onep = rows[("ashby", "1password")]
    assert onep["company"] == "Agilebits Usa Inc" and onep["display_name"] == "1Password"
    assert rows[("greenhouse", "circleci")]["display_name"] == "CircleCI"


def test_every_remoteintech_row_shows_a_brand_not_a_filing_name():
    ev = [r for r in csv.DictReader(open(ROOT / "seed" / "watchlist_evidence.csv", encoding="utf-8"))
          if r.get("source") == "remoteintech"]
    rows = {(r["ats"], r["board_id"]): r for r in growth.read_watchlist_seed()}
    assert len(ev) >= 40
    for e in ev:
        r = rows[(e["ats"], e["board_id"])]
        shown = display_name(r["company"], r.get("display_name", ""))
        assert not shown.lower().endswith((" inc", " llc", " incorporated", " usa")), shown


def test_seed_round_trip_keeps_the_brand_column(tmp_path):
    p = tmp_path / "wl.csv"
    growth.write_watchlist_seed(p, [{"company": "Payward Inc", "ats": "ashby", "board_id": "kraken.com",
                                     "display_name": "Kraken"},
                                    {"company": "Stripe", "ats": "greenhouse", "board_id": "stripe"}])
    assert p.read_text().splitlines()[0] == "company,ats,board_id,display_name"
    assert growth.read_watchlist_seed(p) == [
        {"company": "Payward Inc", "ats": "ashby", "board_id": "kraken.com", "display_name": "Kraken"},
        {"company": "Stripe", "ats": "greenhouse", "board_id": "stripe"}]


def _job(company, sid):
    return {"source_id": sid, "source": "ashby", "company": company, "title": "Engineer",
            "location": "Remote, US", "remote": "remote", "url": f"https://x/{sid}",
            "jd_text": "Build things.", "posted_at": "2026-10-29"}


def test_feed_rows_carry_the_brand_and_sponsor_matching_keeps_the_legal_name(tmp_path, monkeypatch):
    seed = tmp_path / "seed.csv"
    seed.write_text("company,ats,board_id,display_name\nPayward Inc,ashby,kraken.com,Kraken\n"
                    "Datadog Inc,greenhouse,datadog,\n")
    import sourcing.service as service
    monkeypatch.setattr(service, "SEED_WATCHLIST", seed)
    w = Watchlist(tmp_path / "jobs.db")
    seed_if_empty(w)
    w.upsert_jobs([_job("Payward Inc", "a1"), _job("Datadog Inc", "a2"), _job("Acme", "a3")])
    rows = {r["source_id"]: r for r in w.list_jobs()}
    assert rows["a1"]["company"] == "Payward Inc" and rows["a1"]["display_name"] == "Kraken"
    assert rows["a2"]["display_name"] == "Datadog"
    assert rows["a3"]["display_name"] == "Acme"
    assert w.get_job("a1")["display_name"] == "Kraken"
    assert slim_row(rows["a1"])["display_name"] == "Kraken"
    # A re-sync without a brand never clears the stored one.
    w.add_company("Payward Inc", "ashby", "kraken.com")
    assert w.brand_names()["Payward Inc"] == "Kraken"
    assert sync_seed(w) == 2
    w.close()


def test_job_cards_show_the_brand():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function coName(j)" in js
    assert "esc(j.company)" not in js          # every card goes through coName
