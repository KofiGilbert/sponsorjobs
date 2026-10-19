"""The feed crawl split into parallel shards (2026-10-13): each shard crawls its own slice of the
board list into a copy of the saved DB and exports what it changed; one merge folds the changes
back and writes the feed once. Pins the slicing, the per-shard cursors, which shard owns the
unsplit lanes, and that merging gives the same feed a single build would. Offline."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from sourcing.feedfile import loads_maybe_gz

_TODAY = datetime.date.today().isoformat()
_BOARDS = [f"co{i:02d}" for i in range(12)]


def _boards(n=200):
    return [{"company": f"Co{i:04d}", "ats": ("greenhouse", "lever", "workday")[i % 3],
             "board_id": f"b{i}"} for i in range(n)]


# -- slicing ------------------------------------------------------------------------------ #

def test_shard_config_defaults_to_one_shard_and_rejects_nonsense():
    from scripts import build_feed
    assert build_feed.shard_config({}) == (0, 1)
    assert build_feed.shard_config({"FEED_SHARD_INDEX": "1", "FEED_SHARD_COUNT": "2"}) == (1, 2)
    for bad in ({"FEED_SHARD_INDEX": "2", "FEED_SHARD_COUNT": "2"},
                {"FEED_SHARD_INDEX": "-1", "FEED_SHARD_COUNT": "2"},
                {"FEED_SHARD_COUNT": "0"}):
        with pytest.raises(ValueError):
            build_feed.shard_config(bad)


def test_shards_split_every_board_exactly_once_and_roughly_evenly():
    from scripts import build_feed
    boards = _boards(1400)
    for count in (1, 2, 3):
        parts = [build_feed.shard_boards(boards, i, count) for i in range(count)]
        keys = [(b["ats"], b["board_id"]) for p in parts for b in p]
        assert sorted(keys) == sorted((b["ats"], b["board_id"]) for b in boards)   # no gap, no repeat
        assert min(map(len, parts)) > 0.85 * len(boards) / count                    # balanced
        for p in parts:                                   # each slice keeps the watchlist order
            assert p == [b for b in boards if b in p]
    assert build_feed.shard_boards(boards, 0, 1) == boards


def test_a_board_stays_in_its_shard_when_the_list_grows_or_is_renamed():
    from scripts import build_feed
    boards = _boards(300)
    before = {b["board_id"]: build_feed.board_shard(b, 2) for b in boards}
    grown = boards + _boards(600)[300:]
    renamed = [{**b, "company": b["company"] + " Inc"} for b in grown]
    assert all(build_feed.board_shard(b, 2) == before[b["board_id"]] for b in renamed[:300])
    # not Python's salted hash(): pinned values, the same in every process
    assert build_feed.board_shard({"ats": "greenhouse", "board_id": "stripe"}, 2) == \
        build_feed.board_shard({"ats": "Greenhouse", "board_id": "stripe"}, 2)


def test_each_shard_keeps_its_own_cursor_and_starts_from_the_old_one(tmp_path):
    from scripts import build_feed
    assert build_feed.cursor_file_for(0, 1) == build_feed.CURSOR_FILE   # single build: unchanged
    f0, f1 = build_feed.cursor_file_for(0, 2), build_feed.cursor_file_for(1, 2)
    assert len({f0, f1, build_feed.CURSOR_FILE}) == 3
    boards = sorted(_boards(40), key=lambda b: b["company"])
    # the single-job crawl stopped before Co0020
    order, start = build_feed.rotate_boards(boards, tmp_path)
    build_feed.save_cursor(tmp_path, start, crawled=20, total=40, order=order)
    for i, f in ((0, f0), (1, f1)):
        mine = build_feed.shard_boards(boards, i, 2)
        order, start = build_feed.rotate_boards(mine, tmp_path, f, fallback_file=build_feed.CURSOR_FILE)
        assert order[0]["company"] >= "Co0020"            # resumes, does not restart at "A"
        build_feed.save_cursor(tmp_path, start, crawled=3, total=len(mine), order=order, cursor_file=f)
    a = (tmp_path / f0).read_text()
    b = (tmp_path / f1).read_text()
    assert a != b                                          # two cursors, neither overwrote the other
    order1, _ = build_feed.rotate_boards(build_feed.shard_boards(boards, 1, 2), tmp_path, f1,
                                         fallback_file=build_feed.CURSOR_FILE)
    assert build_feed.board_shard(order1[0], 2) == 1


def test_only_shard_zero_runs_the_unsplit_lanes():
    from scripts import build_feed
    assert build_feed.owns_lanes(0) and not build_feed.owns_lanes(1)


# -- crawl shards + merge, end to end ----------------------------------------------------- #

@pytest.fixture
def fake_world(tmp_path, monkeypatch):
    import backend.feed as feed
    import sourcing.service as service
    from sourcing.sponsors import SponsorDB
    seed = tmp_path / "seed.csv"
    seed.write_text("company,ats,board_id\n" + "".join(f"Co {b},greenhouse,{b}\n" for b in _BOARDS))
    monkeypatch.setattr(service, "SEED_WATCHLIST", seed)
    monkeypatch.setattr(feed, "_sponsors", lambda path: SponsorDB(str(tmp_path / "s.db")))
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    monkeypatch.setenv("CAREERSITE_LANE", "0")
    calls: list[str] = []

    def fetch(url, timeout=20):
        calls.append(url)
        if "greenhouse.io" in url:
            board = url.split("/boards/")[1].split("/")[0]
            return {"jobs": [{"id": 1, "title": "Software Engineer", "location": {"name": "New York, NY"},
                              "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/1",
                              "content": f"<p>Build {board}.</p>", "first_published": _TODAY}]}
        if "remotive.com" in url:
            return {"jobs": [{"id": 77, "company_name": "Remote Co", "title": "Software Engineer",
                              "candidate_required_location": "USA", "url": "https://remotive.com/j/77",
                              "description": "Remote role.", "publication_date": _TODAY}]}
        return {}
    return tmp_path, fetch, calls


def _ids(out):
    return sorted(j["source_id"] for j in loads_maybe_gz((out / "jobs.json.gz").read_bytes())["jobs"])


def _copy(src, dst):
    import shutil
    shutil.copytree(src, dst)


def test_two_shards_and_a_merge_publish_what_one_build_would(fake_world):
    from scripts import build_feed
    tmp, fetch, calls = fake_world
    quiet = dict(log=lambda *a: None)
    # The saved state the run starts from (first build seeds the DB, crawls nothing new later).
    build_feed.build(tmp / "single_out", tmp / "single", fetch=fetch, **quiet)
    single = _ids(tmp / "single_out")
    assert len([i for i in single if i.startswith("greenhouse:")]) == len(_BOARDS)

    base = tmp / "base"
    build_feed.build(tmp / "o", base, crawl=False, **quiet)            # empty DB with the seed
    deltas = []
    for i in (0, 1):
        _copy(base, tmp / f"shard{i}")                                  # each job restores the state
        calls.clear()
        r = build_feed.build(tmp / f"never{i}", tmp / f"shard{i}", fetch=fetch, shard=(i, 2),
                             delta_out=tmp / "shards" / f"shard-{i}.db", **quiet)
        assert not (tmp / f"never{i}").exists()                         # a shard writes no feed
        assert r["rows"] > 0
        hit_aggregator = any("remotive.com" in u for u in calls)
        assert hit_aggregator == (i == 0)                               # only shard 0 calls APIs
        gh = {u.split("/boards/")[1].split("/")[0] for u in calls if "greenhouse.io" in u}
        assert gh == {b for b in _BOARDS
                      if build_feed.board_shard({"ats": "greenhouse", "board_id": b}, 2) == i}
        deltas.append(tmp / "shards" / f"shard-{i}.db")

    assert build_feed.main(["--data", str(base), "--out", str(tmp / "merged_out"),
                            "--merge-dir", str(tmp / "shards")]) == 0
    assert _ids(tmp / "merged_out") == single                           # same feed as one build
    for i in (0, 1):                                                     # cursors carried over
        assert (base / build_feed.cursor_file_for(i, 2)).exists()

    # Merging again changes nothing (a retried merge is safe).
    build_feed.build(tmp / "again", base, crawl=False, merge=deltas, **quiet)
    assert _ids(tmp / "again") == single


def test_a_failed_shard_keeps_its_boards_from_the_saved_state(fake_world):
    from scripts import build_feed
    tmp, fetch, _ = fake_world
    quiet = dict(log=lambda *a: None)
    base = tmp / "base"
    build_feed.build(tmp / "first", base, fetch=fetch, **quiet)          # last run: every board
    before = _ids(tmp / "first")
    _copy(base, tmp / "shard0")
    build_feed.build(tmp / "x", tmp / "shard0", fetch=fetch, shard=(0, 2),
                     delta_out=tmp / "shards" / "shard-0.db", **quiet)
    # shard 1 died: no delta from it
    build_feed.build(tmp / "merged", base, crawl=False, merge=[tmp / "shards" / "shard-0.db"], **quiet)
    assert _ids(tmp / "merged") == before                                # nothing dropped
    assert not (base / build_feed.cursor_file_for(1, 2)).exists()        # shard 1 retries next run


def test_merge_keeps_the_newer_row_and_the_earlier_first_seen(fake_world):
    from scripts import build_feed
    tmp, fetch, _ = fake_world
    quiet = dict(log=lambda *a: None)
    base = tmp / "base"
    build_feed.build(tmp / "o", base, fetch=fetch, **quiet)
    db = base / "resume_agent.db"
    c = sqlite3.connect(db)
    sid = c.execute("SELECT source_id FROM sourced_job WHERE source='greenhouse' LIMIT 1").fetchone()[0]
    c.execute("UPDATE sourced_job SET first_seen='2026-01-01 00:00:00', title='Old' WHERE source_id=?", (sid,))
    c.commit()
    c.close()
    delta = tmp / "d.db"
    _copy(base, tmp / "shard")
    s = sqlite3.connect(tmp / "shard" / "resume_agent.db")
    s.execute("UPDATE sourced_job SET title='New', first_seen='2026-02-01 00:00:00', "
              "last_seen=datetime('now','+1 minute') WHERE source_id=?", (sid,))
    s.execute("UPDATE sourced_job SET title='Stale', last_seen='2000-01-01 00:00:00' WHERE source_id<>?", (sid,))
    s.commit()
    s.close()
    build_feed.export_delta(tmp / "shard" / "resume_agent.db", tmp / "shard", "2000-01-01 00:00:00",
                            delta)
    build_feed.merge_delta(db, base, delta, log=lambda *a: None)
    c = sqlite3.connect(db)
    rows = dict(c.execute("SELECT source_id, title FROM sourced_job").fetchall())
    first = c.execute("SELECT first_seen FROM sourced_job WHERE source_id=?", (sid,)).fetchone()[0]
    c.close()
    assert rows[sid] == "New" and first == "2026-01-01 00:00:00"
    assert "Stale" not in rows.values()                  # an older copy never replaces a newer one


def test_merge_refuses_to_publish_when_no_shard_produced_output(fake_world):
    from scripts import build_feed
    tmp, _, _ = fake_world
    (tmp / "empty").mkdir()
    assert build_feed.main(["--data", str(tmp / "d"), "--out", str(tmp / "out"),
                            "--merge-dir", str(tmp / "empty")]) == 2
    assert not (tmp / "out").exists()


def test_the_workflow_matrix_matches_its_shard_count():
    # A matrix entry without a matching FEED_SHARD_COUNT would crawl some boards twice and others
    # never (2 shards 2026-10-13, 3 since 2026-10-18).
    import re
    from pathlib import Path
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "feed.yml").read_text()
    shards = [int(x) for x in re.search(r"shard:\s*\[([\d,\s]+)\]", wf).group(1).split(",")]
    count = int(re.search(r"FEED_SHARD_COUNT:\s*(\d+)", wf).group(1))
    assert shards == list(range(count)) and count >= 3
