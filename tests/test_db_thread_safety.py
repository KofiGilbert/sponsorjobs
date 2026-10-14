"""The local engine is a THREADED Flask server: every request runs on its own thread, and the
bundled H-1B seed merges on a background thread. The visa-sponsor store (`ui.app._sponsors()`)
is cached once per process and shared by all of them.

It used to hold ONE sqlite3 connection opened with check_same_thread=False and no lock around
most calls, so concurrent requests drove the same connection (and its statement cache) at once.
That surfaced as intermittent `sqlite3.InterfaceError` / `ProgrammingError` on /api/jobs and
/api/sponsors/status, and a long background write on that connection made every other request
queue behind it. These tests hammer the store and the two endpoints from many threads at once
while a writer runs, and fail on any error."""
from __future__ import annotations

import threading

import pytest

from sourcing.sponsors import SponsorDB
from sourcing.watchlist import Watchlist

N_COMPANIES = 400


def _seed_rows(n=N_COMPANIES, bump=0):
    return [{"norm_name": f"acme {i} systems", "display_name": f"Acme {i} Systems",
             "h1b_approvals": 10 + i + bump, "h1b_last_fy": 2024, "h1b_first_fy": 2019}
            for i in range(n)]


def _run_threads(n, target):
    errors: list[BaseException] = []
    lock = threading.Lock()

    def wrap(i):
        try:
            target(i)
        except BaseException as e:            # noqa: BLE001 - every failure is the finding
            with lock:
                errors.append(e)

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert not any(t.is_alive() for t in ts), "a thread stalled on the sponsor DB"
    return errors


def test_sponsor_db_survives_concurrent_readers_and_a_writer(tmp_path):
    db = SponsorDB(tmp_path / "sponsors.db")
    db.merge_aggregated_rows(_seed_rows())
    stop = threading.Event()
    writer_errors: list[BaseException] = []

    def writer():                               # stands in for the background H-1B seed merge
        k = 0
        try:
            while not stop.is_set():
                k += 1
                db.merge_aggregated_rows(_seed_rows(bump=k))
                db.set_meta("jobs_refreshed_at", str(k))
                db._invalidate_index()
        except BaseException as e:              # noqa: BLE001
            writer_errors.append(e)

    w = threading.Thread(target=writer)
    w.start()

    def reader(i):
        for r in range(40):
            jobs = [{"company": f"Acme {(i * 7 + r + j) % N_COMPANIES} Systems",
                     "location": "Austin, TX"} for j in range(10)]
            db._invalidate_index()              # force real SQL, not the lookup cache
            db.tag_jobs(jobs)
            db.stats()
            db.count()
            db.get_meta("jobs_refreshed_at")
            db.h1b_year_fiscal_years()
            db.lca_stats()

    try:
        errors = _run_threads(24, reader)
    finally:
        stop.set()
        w.join(60)
    db.close()
    assert not errors, f"{len(errors)} concurrent read errors, first: {errors[0]!r}"
    assert not writer_errors, f"writer failed: {writer_errors[0]!r}"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import ui.app as app
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(app, "_DATA", data)
    monkeypatch.setattr(app, "DB_PATH", str(data / "resume_agent.db"))
    monkeypatch.setattr(app, "_SPONSORS_DB", data / "sponsors.db")
    monkeypatch.setattr(app, "_feed_base_url", lambda: "")
    monkeypatch.setattr(app, "_SALARY_BACKFILLED", True)
    app._SPONSORS.clear()
    seed = SponsorDB(data / "sponsors.db")
    seed.merge_aggregated_rows(_seed_rows())
    seed.set_meta("jobs_refreshed_at", "2026-10-01T00:00:00Z")
    seed.close()
    w = Watchlist(app.DB_PATH)
    w.upsert_jobs([{"source_id": f"gh:{i}", "source": "greenhouse",
                    "company": f"Acme {i % N_COMPANIES} Systems", "title": "Software Engineer",
                    "location": "Austin, TX", "remote": "", "url": f"https://x/{i}",
                    "jd_text": "We sponsor H-1B visas. Python.", "posted_at": "2026-10-01"}
                   for i in range(300)])
    w.close()
    yield app
    for db in list(app._SPONSORS.values()):
        db.close()
    app._SPONSORS.clear()


def test_jobs_and_sponsor_status_endpoints_under_parallel_load(engine):
    app = engine
    app._sponsors()                              # opened once, like the running engine
    stop = threading.Event()

    def writer():                                # the background seed merging during requests
        db = app._sponsors()
        k = 0
        while not stop.is_set():
            k += 1
            db.merge_aggregated_rows(_seed_rows(bump=k))
            db._invalidate_index()

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    statuses: list[int] = []
    hdr = {"X-Tailor-Extension": "1"}

    def hit(i):
        c = app.app.test_client()
        for r in range(4):
            path = "/api/jobs?per_page=50" if (i + r) % 2 else "/api/sponsors/status"
            resp = c.get(path, headers=hdr)
            statuses.append(resp.status_code)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.data[:300]!r}"

    try:
        errors = _run_threads(50, hit)
    finally:
        stop.set()
        wt.join(60)
    assert not errors, f"{len(errors)} failed requests, first: {errors[0]!r}"
    assert statuses.count(200) == 200
