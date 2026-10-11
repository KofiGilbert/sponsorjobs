"""More useful jobs on the board without noise (Kofi, 2026-10-11).

A feed sample showed two filters hiding most of 26,279 matched rows: the 45-day posting-date rule
(9,488 rows, many still live on their boards) and a US detector that did not know spelled-out
states or most cities (US rows like "McLean, Virginia" and "Clearwater" were dropped as abroad)."""
import sqlite3

from sourcing.sponsors import looks_us
from sourcing.watchlist import Watchlist

US = ["Washington", "Clearwater", "Pasadena", "McLean, Virginia", "Kalamazoo, Michigan",
      "Reston, Virginia", "Columbus, Ohio", "Cambridge, MA", "St. Louis, Missouri", "Austin, TX",
      "Atlanta, Georgia", "Remote - US"]
ABROAD = ["Bangalore, India", "Krakow, Poland", "Toronto, Ontario, Canada", "London",
          "Cambridge, UK (ZUK140)", "Tbilisi, Georgia", "Birmingham", "Richmond", "Vancouver",
          "Costa Rica", "Indore; Hyderabad", "Spain - Barcelona", "Hiroshima - Fab 15, Japan",
          "Milan, Italy; Rome, Italy – Remote", "AMER - Canada - Ontario - Toronto", "Remote", ""]


def test_us_detector_on_the_locations_the_feed_actually_dropped():
    assert [x for x in US if not looks_us(x)] == []
    assert [x for x in ABROAD if looks_us(x)] == []


def _db(tmp_path):
    w = Watchlist(tmp_path / "jobs.db")
    con = w._conn
    cols = [r[1] for r in con.execute("PRAGMA table_info(sourced_job)")]
    base = {c: None for c in cols}
    def row(sid, posted, seen):
        r = dict(base, source_id=sid, title=sid, company="Acme", url=f"https://x/{sid}", source="greenhouse",
                 posted_at=posted, last_seen=seen, first_seen=seen, dismissed=0, saved=0, is_new=0)
        r = {k: v for k, v in r.items() if k in cols}
        con.execute(f"INSERT INTO sourced_job ({','.join(r)}) VALUES ({','.join('?'*len(r))})", list(r.values()))
    row("fresh", "2099-01-01", "2099-01-01 00:00:00")
    row("old_but_live", "2020-01-01", "2099-01-01 00:00:00")      # posted long ago, re-seen just now
    row("old_and_gone", "2020-01-01", "2020-01-02 00:00:00")      # posted long ago, not seen since
    row("undated", None, "2020-01-02 00:00:00")
    con.commit()
    return w


def test_a_job_still_listed_on_its_board_stays_whatever_its_posting_date(tmp_path):
    w = _db(tmp_path)
    ids = {j["source_id"] for j in w.list_jobs(since_days=45, seen_days=7)}
    assert ids == {"fresh", "old_but_live", "undated"}
    # Without the seen window the old-but-live role is hidden (the old behaviour).
    assert {j["source_id"] for j in w.list_jobs(since_days=45)} == {"fresh", "undated"}
