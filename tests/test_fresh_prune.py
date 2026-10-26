"""The fast prune (2026-10-24): a job the employer took down used to linger in the feed for up to
21 days (the stale prune). Now every board read records its outcome (board_crawl), and a row its
board no longer lists goes after 7 days unseen, but ONLY when the board was read in full since the
row was last seen. A board that failed to load, was not reached or stopped at a cap says nothing
about its rows, so they keep the 21-day rule, as do aggregator and career-site rows and the
person's own saved / dismissed / tracked rows. Offline: fake fetches, no network."""

from __future__ import annotations

import sqlite3

import pytest

from sourcing.watchlist import Watchlist


def _job(sid, source="greenhouse", company="Acme", title="Engineer"):
    return {"source_id": sid, "source": source, "company": company, "title": title,
            "location": "Austin, TX", "remote": "", "url": f"https://x/{sid}", "jd_text": "Build.",
            "posted_at": "2026-10-01"}


def _age(w, sid, days):
    w._conn.execute("UPDATE sourced_job SET last_seen=datetime('now', ?) WHERE source_id=?",
                    (f"-{days} days", sid))
    w._conn.commit()


def _crawl(w, ats, board):
    return {(r["ats"], r["board_id"]): r for r in w.board_crawls()}[(ats, board)]


# -- the board_crawl table ------------------------------------------------------------------ #

def test_record_crawl_keeps_success_partial_and_error_apart(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.record_crawl("greenhouse", "acme", True)
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_ok"] and not r["last_partial"] and not r["last_error"]
    assert r["consecutive_errors"] == 0 and r["error"] == ""

    w.record_crawl("greenhouse", "acme", False, error="HTTP 503")
    w.record_crawl("greenhouse", "acme", False, error="timed out")
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_ok"] and r["last_error"]                 # the old success is kept beside the error
    assert r["consecutive_errors"] == 2 and r["error"] == "timed out"

    w.record_crawl("greenhouse", "acme", True, complete=False)
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_partial"] and r["consecutive_errors"] == 0 and r["error"] == ""

    w.record_crawl("workday", "big.wd5/Jobs", True)          # another board, its own row
    assert len(w.board_crawls()) == 2
    w.close()


def test_record_crawl_runs_after_the_upsert_so_last_ok_is_never_older_than_last_seen(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1")])
    w.record_crawl("greenhouse", "acme", True)
    seen = w._conn.execute("SELECT last_seen FROM sourced_job").fetchone()[0]
    assert _crawl(w, "greenhouse", "acme")["last_ok"] >= seen
    w.close()


# -- the fast rule --------------------------------------------------------------------------- #

def test_unlisted_row_goes_after_7_days_only_when_its_board_was_read_in_full_since(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:gone"), _job("greenhouse:acme:fresh"),
                   _job("greenhouse:acme:recent")])
    _age(w, "greenhouse:acme:gone", 10)
    _age(w, "greenhouse:acme:recent", 3)                     # unseen, but not for 7 days yet
    assert w.prune_unlisted(7) == 0                          # no read recorded: nothing goes
    w.record_crawl("greenhouse", "acme", True)               # the board answered, in full
    assert w.prune_unlisted(7) == 1
    assert {j["source_id"] for j in w.list_jobs()} == {"greenhouse:acme:fresh", "greenhouse:acme:recent"}
    w.close()


def test_a_full_read_older_than_the_row_s_last_seen_does_not_count(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1")])
    w.record_crawl("greenhouse", "acme", True)
    w._conn.execute("UPDATE board_crawl SET last_ok=datetime('now','-30 days')")
    _age(w, "greenhouse:acme:1", 10)                         # seen 10 days ago, read in full 30 days ago
    w._conn.commit()
    assert w.prune_unlisted(7) == 0                          # the board has not been read since
    w.close()


def test_a_board_that_keeps_failing_keeps_its_rows_until_the_stale_window(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1"), _job("greenhouse:acme:2")])
    _age(w, "greenhouse:acme:1", 10)
    _age(w, "greenhouse:acme:2", 25)
    for _ in range(5):
        w.record_crawl("greenhouse", "acme", False, error="HTTP 500")
    assert w.prune_unlisted(7) == 0                          # a failed read proves nothing
    assert w.prune_stale(21) == 1                            # the 21-day safety net still applies
    assert {j["source_id"] for j in w.list_jobs()} == {"greenhouse:acme:1"}
    w.close()


def test_a_partial_read_does_not_prune(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("amazonjobs:USA:old", source="amazonjobs")])
    _age(w, "amazonjobs:USA:old", 10)
    w.record_crawl("amazonjobs", "USA", True, complete=False)   # newest N only: the row may be live
    assert w.prune_unlisted(7) == 0
    w.record_crawl("amazonjobs", "USA", True)                   # a later full read
    assert w.prune_unlisted(7) == 1
    w.close()


def test_saved_dismissed_and_tracked_rows_are_never_pruned(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job(f"greenhouse:acme:{i}") for i in range(4)])
    w.set_saved("greenhouse:acme:0", True)
    w.dismiss("greenhouse:acme:1")
    w.track_job("greenhouse:acme:2", "applied")
    for i in range(4):
        _age(w, f"greenhouse:acme:{i}", 10)
    w.record_crawl("greenhouse", "acme", True)
    assert w.prune_unlisted(7) == 1                          # only the plain feed row
    left = {r[0] for r in w._conn.execute("SELECT source_id FROM sourced_job")}
    assert left == {"greenhouse:acme:0", "greenhouse:acme:1", "greenhouse:acme:2"}
    w.close()


def test_aggregator_and_career_site_rows_keep_the_stale_rule(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("freehire:abc123", source="freehire", company="Remote Co"),
                   _job("careersite:walmart.com:9", source="careersite", company="Walmart"),
                   _job("remotive:77", source="remotive", company="Remote Co")])
    for sid in ("freehire:abc123", "careersite:walmart.com:9", "remotive:77"):
        _age(w, sid, 10)
    w.record_crawl("greenhouse", "acme", True)               # some board read in full: unrelated
    assert w.prune_unlisted(7) == 0
    assert w._conn.execute("SELECT COUNT(*) FROM sourced_job").fetchone()[0] == 3
    for sid in ("freehire:abc123", "careersite:walmart.com:9", "remotive:77"):
        _age(w, sid, 22)
    assert w.prune_stale(21) == 3                            # unchanged: 21 days unseen
    w.close()


def test_one_board_s_full_read_never_reaches_another_board_s_rows(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:1"), _job("greenhouse:acme-labs:1", company="Acme Labs"),
                   _job("greenhouse:acme_x:1", company="Acme X"), _job("lever:acme:1", source="lever"),
                   _job("workday:acme.wd5/Jobs:1", source="workday")])
    for sid in ("greenhouse:acme:1", "greenhouse:acme-labs:1", "greenhouse:acme_x:1", "lever:acme:1",
                "workday:acme.wd5/Jobs:1"):
        _age(w, sid, 10)
    w.record_crawl("greenhouse", "acme", True)
    assert w.prune_unlisted(7) == 1                          # greenhouse:acme:1 only
    left = {r[0] for r in w._conn.execute("SELECT source_id FROM sourced_job")}
    assert left == {"greenhouse:acme-labs:1", "greenhouse:acme_x:1", "lever:acme:1", "workday:acme.wd5/Jobs:1"}
    w.record_crawl("workday", "acme.wd5/Jobs", True)         # a two-part Workday id works too
    assert w.prune_unlisted(7) == 1
    w.close()


# -- refresh_watchlist records every board it attempts -------------------------------------- #

def _gh(board, ids):
    # Distinct titles: two rows with one company, title and location are one posting to the
    # cross-feed dedup (sourcing/dedup.py), and the second would never be stored.
    return {"jobs": [{"id": i, "title": f"Software Engineer {i}", "location": {"name": "New York, NY"},
                      "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/{i}",
                      "content": f"<p>Build {board}.</p>", "first_published": "2026-10-20"} for i in ids]}


def test_refresh_watchlist_records_success_error_and_a_capped_read(tmp_path, monkeypatch):
    import sourcing.service as service
    monkeypatch.setattr(service, "MAX_PER_COMPANY", 2)
    w = Watchlist(tmp_path / "wl.db")
    for name in ("good", "down", "huge"):
        w.add_company(name.title(), "greenhouse", name)
    w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})

    def fetch(url, timeout=20):
        if "/boards/good/" in url:
            return _gh("good", [1, 2])
        if "/boards/huge/" in url:
            return _gh("huge", [1, 2, 3])                   # more rows than the cap keeps
        raise RuntimeError("HTTP 503")

    s = service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    assert [e["company"] for e in s["errors"]] == ["Down"]
    good, down, huge = (_crawl(w, "greenhouse", b) for b in ("good", "down", "huge"))
    assert good["last_ok"] and not good["last_partial"]
    assert down["last_error"] and down["consecutive_errors"] == 1 and "503" in down["error"]
    assert not down["last_ok"]
    assert huge["last_partial"] and not huge["last_ok"]      # capped: not a full read
    # An old row of the board that was down is untouched by the fast rule; an old row of the
    # board that answered in full (good:1, aged behind the read) goes.
    w.upsert_jobs([_job("greenhouse:down:9")])
    _age(w, "greenhouse:down:9", 10)
    _age(w, "greenhouse:good:1", 10)
    assert w.prune_unlisted(7) == 1
    left = {r[0] for r in w._conn.execute("SELECT source_id FROM sourced_job")}
    assert "greenhouse:down:9" in left and "greenhouse:good:1" not in left
    w.close()


def test_refresh_then_prune_end_to_end(tmp_path):
    """The real sequence: a row seen last week, this week's read lists the board without it."""
    import sourcing.service as service
    w = Watchlist(tmp_path / "wl.db")
    w.add_company("Acme", "greenhouse", "acme")
    w.add_company("Beta", "greenhouse", "beta")
    w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})
    boards = {"acme": [1, 2], "beta": [1]}

    def fetch(url, timeout=20):
        board = url.split("/boards/")[1].split("/")[0]
        if board not in boards:
            raise RuntimeError("HTTP 500")
        return _gh(board, boards[board])

    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)   # last week's crawl
    # One instant for the rows and the boards (see _watch): beta's read fails this week, so
    # beta:1 stays only while last_ok is not after its last_seen.
    stamp = w._conn.execute("SELECT datetime('now','-8 days')").fetchone()[0]
    w._conn.execute("UPDATE sourced_job SET last_seen=?", (stamp,))
    w._conn.execute("UPDATE board_crawl SET last_ok=?, updated_at=?", (stamp, stamp))
    w._conn.commit()
    boards["acme"] = [1]                                     # the employer took acme:2 down
    del boards["beta"]                                       # and beta's board is down this week
    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)   # this week's crawl
    assert w.prune_unlisted(7) == 1
    left = {r[0] for r in w._conn.execute("SELECT source_id FROM sourced_job")}
    assert left == {"greenhouse:acme:1", "greenhouse:beta:1"}             # beta:1 waits for 21 days
    assert w.prune_stale(21) == 0
    w.close()


def test_listing_complete_knows_the_capped_adapters(monkeypatch):
    from sourcing.service import listing_complete
    assert listing_complete("greenhouse", [{}] * 5000)      # one response is the whole board
    monkeypatch.setenv("AMAZON_MAX_JOBS", "3")
    assert listing_complete("amazonjobs", [{}] * 2)
    assert not listing_complete("amazonjobs", [{}] * 3)      # reached the cap: newest N only
    monkeypatch.setenv("WORKDAY_MAX_JOBS", "25000")
    assert listing_complete("workday", [{}] * 1999)
    assert not listing_complete("workday", [{}] * 2000)      # Workday's ceiling: a split board
    monkeypatch.setenv("WORKDAY_MAX_JOBS", "100")
    assert not listing_complete("workday", [{}] * 100)


# -- the shard deltas and the merge ----------------------------------------------------------- #

def _copy_db(src, dst):
    import shutil
    shutil.copy(src, dst)


def test_board_crawl_rides_the_delta_and_the_merge_keeps_the_newer_outcome(tmp_path):
    from scripts import build_feed
    base = tmp_path / "base"
    base.mkdir()
    db = base / "resume_agent.db"
    w = Watchlist(db)
    w.record_crawl("greenhouse", "acme", False, error="old failure")
    w.record_crawl("greenhouse", "beta", True)
    w._conn.execute("UPDATE board_crawl SET last_ok=datetime('now','-1 day'), last_error=datetime('now','-1 day'), "
                    "updated_at=datetime('now','-1 day')")
    w._conn.commit()
    w.close()
    since = sqlite3.connect(db).execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]

    # Shard A read acme fine this run; shard B saw beta fail.
    for name, board, ok in (("a", "acme", True), ("b", "beta", False)):
        d = tmp_path / name
        d.mkdir()
        _copy_db(db, d / "resume_agent.db")
        s = Watchlist(d / "resume_agent.db")
        s.record_crawl("greenhouse", board, ok, error="" if ok else "HTTP 429")
        s.close()
        build_feed.export_delta(d / "resume_agent.db", d, since, tmp_path / f"{name}.db")
    dconn = sqlite3.connect(tmp_path / "a.db")
    assert dconn.execute("SELECT board_id FROM board_crawl").fetchall() == [("acme",)]   # touched rows only
    dconn.close()

    for order in (("a", "b"), ("b", "a")):                   # either order, and twice, ends the same
        for name in order:
            build_feed.merge_delta(db, base, tmp_path / f"{name}.db", log=lambda *a: None)
        w = Watchlist(db)
        acme, beta = _crawl(w, "greenhouse", "acme"), _crawl(w, "greenhouse", "beta")
        assert acme["last_ok"] >= since and acme["last_error"] < since   # the new success, the old error kept
        assert acme["consecutive_errors"] == 0 and acme["error"] == ""
        assert beta["last_error"] >= since and beta["last_ok"] < since
        assert beta["consecutive_errors"] == 1 and beta["error"] == "HTTP 429"
        w.close()


def test_merge_accepts_a_delta_from_before_the_table_existed(tmp_path):
    from scripts import build_feed
    base = tmp_path / "base"
    base.mkdir()
    db = base / "resume_agent.db"
    Watchlist(db).close()
    delta = tmp_path / "old.db"
    c = sqlite3.connect(delta)
    c.execute("CREATE TABLE sourced_job (source_id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, "
              "last_seen TEXT, first_seen TEXT)")
    c.execute("INSERT INTO sourced_job VALUES ('greenhouse:acme:1','greenhouse','Acme','E',"
              "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    c.commit()
    c.close()
    assert build_feed.merge_delta(db, base, delta, log=lambda *a: None)["rows"] == 1


# -- the build calls both rules ---------------------------------------------------------------- #

def test_prune_rows_runs_the_fast_rule_then_the_safety_net_and_logs_both(tmp_path, monkeypatch):
    from scripts import build_feed
    import sourcing.scheduler as scheduler
    monkeypatch.setattr(scheduler, "FAST_PRUNE_DAYS", 7)
    monkeypatch.setattr(scheduler, "PRUNE_DAYS", 21)
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job("greenhouse:acme:unlisted"), _job("greenhouse:down:ancient"),
                   _job("greenhouse:down:recent"), _job("greenhouse:acme:live")])
    _age(w, "greenhouse:acme:unlisted", 10)
    _age(w, "greenhouse:down:ancient", 30)
    _age(w, "greenhouse:down:recent", 10)
    w.record_crawl("greenhouse", "acme", True)
    w.record_crawl("greenhouse", "down", False, error="HTTP 503")
    lines = []
    assert build_feed.prune_rows(w, lines.append, "merge") == (1, 1)
    left = {r[0] for r in w._conn.execute("SELECT source_id FROM sourced_job")}
    assert left == {"greenhouse:acme:live", "greenhouse:down:recent"}
    assert lines == ["[build_feed] merge: pruned unlisted=1 (unseen 7 days on a board read in full since) "
                     "stale=1 (unseen 21 days)"]
    w.close()


def test_the_days_come_from_the_environment(monkeypatch):
    import importlib
    import sourcing.scheduler as scheduler
    monkeypatch.setenv("JOBS_FAST_PRUNE_DAYS", "5")
    monkeypatch.setenv("JOBS_PRUNE_DAYS", "30")
    try:
        mod = importlib.reload(scheduler)
        assert (mod.FAST_PRUNE_DAYS, mod.PRUNE_DAYS) == (5, 30)
    finally:
        monkeypatch.delenv("JOBS_FAST_PRUNE_DAYS")
        monkeypatch.delenv("JOBS_PRUNE_DAYS")
        importlib.reload(scheduler)
        assert (scheduler.FAST_PRUNE_DAYS, scheduler.PRUNE_DAYS) == (7, 21)


def test_the_workflow_passes_the_prune_days_to_the_merge():
    from pathlib import Path
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "feed.yml").read_text()
    assert "JOBS_FAST_PRUNE_DAYS" in wf and "JOBS_PRUNE_DAYS" in wf


@pytest.mark.parametrize("sid", ["greenhouse:acme:1"])
def test_a_dismissed_row_survives_the_fast_prune_so_it_does_not_come_back(tmp_path, sid):
    """Pruning a dismissal would bring a hidden role back if the board lists it again."""
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job(sid)])
    w.dismiss(sid)
    _age(w, sid, 10)
    w.record_crawl("greenhouse", "acme", True)
    assert w.prune_unlisted(7) == 0 and sid in w.dismissed_ids()
    w.close()


# -- a collapsed listing is not a full read ---------------------------------------------------- #

def _watch(tmp_path, monkeypatch, board="acme"):
    import sourcing.service as service
    monkeypatch.setattr(service, "MAX_PER_COMPANY", 25000)  # the feed's cap, not the desktop's 60
    w = Watchlist(tmp_path / "wl.db")
    w.add_company(board.title(), "greenhouse", board)
    w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})
    boards = {board: list(range(1, 481))}                    # 480 postings last time

    def fetch(url, timeout=20):
        b = url.split("/boards/")[1].split("/")[0]
        return _gh(b, boards[b])

    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    assert _crawl(w, "greenhouse", board)["rows"] == 480     # the full count is remembered
    # The rows' last_seen and the board's last_ok must be the SAME instant: prune_unlisted drops a
    # row only when last_ok is after last_seen. Stamping each with its own datetime('now') let the
    # clock tick a second between the two under a loaded suite, and last_ok then sat 1 s after
    # last_seen, so "a genuinely emptied board" lost all 480 rows (flaked 2026-10-26).
    stamp = w._conn.execute("SELECT datetime('now','-10 days')").fetchone()[0]
    w._conn.execute("UPDATE sourced_job SET last_seen=? WHERE source_id >= ? AND source_id < ?",
                    (stamp, f"greenhouse:{board}:", f"greenhouse:{board};"))
    w._conn.execute("UPDATE board_crawl SET last_ok=?, updated_at=?", (stamp, stamp))
    w._conn.commit()
    return w, boards, fetch, service


def test_listing_collapsed_rule():
    from sourcing.service import listing_collapsed
    assert listing_collapsed(0, 480) and listing_collapsed(0, 1)
    assert listing_collapsed(95, 480) and not listing_collapsed(96, 480)   # under a fifth
    assert not listing_collapsed(410, 480)                   # a normal shrink
    assert not listing_collapsed(2, 5)                       # small boards: only zero rows count
    assert not listing_collapsed(0, 0) and not listing_collapsed(5, 0)     # nothing to compare


def test_an_empty_200_does_not_stamp_last_ok_and_the_rows_keep_the_21_day_rule(tmp_path, monkeypatch):
    w, boards, fetch, service = _watch(tmp_path, monkeypatch)
    boards["acme"] = []                                      # 200, zero rows
    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_partial"] and r["last_ok"] < r["last_partial"]
    assert r["error"] == "listing collapsed: 0 of 480" and r["rows"] == 480
    assert w.prune_unlisted(7) == 0                          # protected
    for i in range(1, 481):
        _age(w, f"greenhouse:acme:{i}", 22)
    assert w.prune_stale(21) == 480                          # the 21-day rule still applies
    w.close()


def test_a_collapse_below_a_fifth_does_not_stamp_last_ok(tmp_path, monkeypatch):
    w, boards, fetch, service = _watch(tmp_path, monkeypatch)
    boards["acme"] = list(range(1, 51))                      # 50 of 480
    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_partial"] and r["error"] == "listing collapsed: 50 of 480" and r["rows"] == 480
    assert w.prune_unlisted(7) == 0
    w.close()


def test_a_normal_shrink_is_a_full_read_and_prunes_the_missing_rows(tmp_path, monkeypatch):
    w, boards, fetch, service = _watch(tmp_path, monkeypatch)
    boards["acme"] = list(range(1, 411))                     # 410 of 480
    service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    r = _crawl(w, "greenhouse", "acme")
    assert r["last_ok"] == r["updated_at"] and not r["last_partial"] and r["rows"] == 410
    assert w.prune_unlisted(7) == 70                         # the 70 taken down go
    w.close()


def test_a_genuinely_emptied_board_waits_the_full_21_days(tmp_path, monkeypatch):
    """The trade-off: an employer that removed every posting looks the same as a broken API."""
    w, boards, fetch, service = _watch(tmp_path, monkeypatch)
    boards["acme"] = []
    for _ in range(3):                                       # run after run, still empty
        service.refresh_watchlist(w, fetch=fetch, include_aggregators=False)
    assert w.prune_unlisted(7) == 0
    assert w._conn.execute("SELECT COUNT(*) FROM sourced_job").fetchone()[0] == 480
    w.close()


def test_before_any_read_the_stored_rows_are_the_comparison(tmp_path):
    w = Watchlist(tmp_path / "wl.db")
    w.upsert_jobs([_job(f"greenhouse:acme:{i}") for i in range(30)])
    assert w.last_full_count("greenhouse", "acme") == 30
    assert w.last_full_count("greenhouse", "other") == 0
    w.record_crawl("greenhouse", "acme", True, rows=25)
    assert w.last_full_count("greenhouse", "acme") == 25     # the recorded count takes over
    w.record_crawl("greenhouse", "acme", True, complete=False, error="stopped at a cap")
    assert w.last_full_count("greenhouse", "acme") == 25     # a partial read does not change it
    w.close()


def test_the_merge_carries_rows_with_the_later_full_read(tmp_path):
    from scripts import build_feed
    base = tmp_path / "base"
    base.mkdir()
    db = base / "resume_agent.db"
    w = Watchlist(db)
    w.record_crawl("greenhouse", "acme", True, rows=480)
    w._conn.execute("UPDATE board_crawl SET last_ok=datetime('now','-1 day'), updated_at=datetime('now','-1 day')")
    w._conn.commit()
    w.close()
    since = sqlite3.connect(db).execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    d = tmp_path / "a"
    d.mkdir()
    _copy_db(db, d / "resume_agent.db")
    s = Watchlist(d / "resume_agent.db")
    s.record_crawl("greenhouse", "acme", True, rows=410)
    s.close()
    build_feed.export_delta(d / "resume_agent.db", d, since, tmp_path / "a.db")
    for _ in range(2):
        build_feed.merge_delta(db, base, tmp_path / "a.db", log=lambda *a: None)
        w = Watchlist(db)
        assert _crawl(w, "greenhouse", "acme")["rows"] == 410
        w.close()
