"""The STABLE list layout (2026-10-23). The per-version parts were renamed by every 3-hourly rebuild,
so an open app re-downloaded the whole ~15 MB list each time. Stable parts are grouped by the week a
role was first seen and named by their own content; the builder starts from the previous build's
parts and lists dropped or changed rows in a small "removed" file instead of rewriting their part.
The app keeps the parts it has and fetches only new ones.

Offline: a real local HTTP server (tests/test_feed_parts.Origin) serves the built feed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sourcing import feedclient
from sourcing.feedclient import StaticFeed
from sourcing.feedfile import (JOBS_FILE, MANIFEST_FILE, PARTS_DIR, load_stable_state,
                               loads_maybe_gz, read_removed, recency_key, save_stable_state,
                               week_of, write_feed)
from test_feed_parts import Origin

WEEKS = ["2026-09-22", "2026-09-29", "2026-10-06", "2026-10-13"]   # four ISO weeks


def _row(i, day, company="Acme"):
    return {"source_id": f"greenhouse:acme:{i}", "source": "greenhouse", "company": company,
            "title": f"Software Engineer {i}", "location": "New York, NY", "remote": "hybrid",
            "url": f"https://acme.example/jobs/{i}/{os.urandom(12).hex()}",
            "posted_at": f"{day}T{i % 24:02d}:00:00", "first_seen": f"{day} 10:00:00",
            "salary": "", "sponsorship_stated": True, "us": True, "entry_level": False,
            "visa": [], "nationality_visas": [], "sponsor": {}}


def _rows(per_week=150, start=0):
    return [_row(start + w * per_week + k, day) for w, day in enumerate(WEEKS) for k in range(per_week)]


class Site:
    """A feed site rebuilt in place, with the builder's saved state between builds."""

    def __init__(self, root: Path, part_bytes=3000):
        self.dir, self.state, self.part_bytes = root / "site", root / "state", part_bytes
        self.n = 0

    def build(self, rows, prev=True):
        self.n += 1
        m = write_feed(self.dir, rows, {}, generated_at=f"2026-10-14T{self.n:02d}:00:00+00:00",
                       part_bytes=self.part_bytes, prev_dir=self.state if prev else None)
        save_stable_state(self.dir, self.state)
        return m


def _names(m):
    return [f["url"] for f in m["stable"]["files"]]


def _all_files(m):
    st = m["stable"]
    return _names(m) + ([st["removed"]["url"]] if st.get("removed") else [])


def _assembled(site_dir, m):
    """The list a client joins from the stable block: parts minus the removed rows."""
    st = m["stable"]
    removed = (read_removed((site_dir / st["removed"]["url"]).read_bytes())
               if st.get("removed") else {})
    out = []
    for f in st["files"]:
        jobs = loads_maybe_gz((site_dir / f["url"]).read_bytes())["jobs"]
        out += [r for i, r in enumerate(jobs) if i not in removed.get(f["url"], set())]
    return out


def _key(rows):
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _whole(site_dir):
    """The rows as published (write_feed adds the function tags): jobs.json.gz's list."""
    return loads_maybe_gz((site_dir / JOBS_FILE).read_bytes())["jobs"]


def _board(site_dir):
    return sorted(_whole(site_dir), key=recency_key, reverse=True)


# -- the builder ----------------------------------------------------------------------------- #

def test_a_fresh_layout_groups_rows_by_first_seen_week_into_content_named_parts(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    m = s.build(rows)
    st = m["stable"]
    assert st["count"] == len(rows) == m["count"] and "removed" not in st
    assert len(st["files"]) > len(WEEKS)                             # weeks split into ~part_bytes
    for f in st["files"]:
        body = (s.dir / f["url"]).read_bytes()
        assert f["url"] == f"{PARTS_DIR}/s-{f['sha256'][:20]}.json.gz" and len(body) == f["bytes"]
        part = loads_maybe_gz(body)
        assert part["bucket"] == f["bucket"] and len(part["jobs"]) == f["count"]
        lo, _, hi = f["bucket"].partition("..")
        weeks = {week_of(r["first_seen"]) for r in part["jobs"]}
        assert min(weeks) == lo and max(weeks) == (hi or lo)             # its weeks, labelled
    lows = [f["bucket"].split("..")[0] for f in st["files"]]
    assert lows == sorted(lows)                                      # oldest week first
    whole = _whole(s.dir)
    assert _key(_assembled(s.dir, m)) == _key(whole)
    # The older blocks are still there for older apps, with the same rows.
    assert [r["source_id"] for r in whole] == [r["source_id"] for r in rows]
    assert m["parts"]["count"] == len(rows)
    assert json.loads((s.dir / MANIFEST_FILE).read_text())["stable"]["version"] == st["version"]


def test_unchanged_weeks_keep_their_part_names_and_new_rows_get_new_parts(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    a = s.build(rows)
    b = s.build(rows)                                               # a rebuild, nothing changed
    assert _names(b) == _names(a) and b["stable"]["version"] == a["stable"]["version"]
    new = [_row(10_000 + k, "2026-10-20") for k in range(150)]       # a new week of roles
    c = s.build(new + rows)
    assert set(_names(a)) <= set(_names(c))                          # every old part kept
    fresh = [f for f in c["stable"]["files"] if f["url"] not in _names(a)]
    assert fresh and {f["bucket"] for f in fresh} == {"2026-W43"}
    assert sum(f["count"] for f in fresh) == 150 and "removed" not in c["stable"]
    assert _key(_assembled(s.dir, c)) == _key(_whole(s.dir))


def test_a_removed_or_changed_row_is_listed_as_removed_and_its_part_kept(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    a = s.build(rows)
    gone, changed = rows[3], rows[160]
    edited = {**changed, "title": "Staff Engineer"}
    rows2 = [edited if r is changed else r for r in rows if r is not gone]
    b = s.build(rows2)
    assert set(_names(a)) <= set(_names(b))                          # no old part rewritten
    rem = read_removed((s.dir / b["stable"]["removed"]["url"]).read_bytes())
    assert sum(len(v) for v in rem.values()) == 2 == b["stable"]["removed"]["count"]
    got = _assembled(s.dir, b)
    assert _key(got) == _key(_whole(s.dir))
    ids = [r["source_id"] for r in got]
    assert gone["source_id"] not in ids and ids.count(changed["source_id"]) == 1
    assert next(r for r in got if r["source_id"] == changed["source_id"])["title"] == "Staff Engineer"


def test_a_part_with_too_many_dead_rows_is_rewritten_under_a_new_name(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    a = s.build(rows)
    first = a["stable"]["files"][0]
    jobs = loads_maybe_gz((s.dir / first["url"]).read_bytes())["jobs"]
    drop = {r["source_id"] for r in jobs[: len(jobs) // 2]}          # half of one part closes
    rows2 = [r for r in rows if r["source_id"] not in drop]
    b = s.build(rows2)
    assert first["url"] not in _names(b)                             # rewritten, dead rows gone
    assert set(_names(a)) - {first["url"]} <= set(_names(b))         # every other part kept
    assert "removed" not in b["stable"]
    assert _key(_assembled(s.dir, b)) == _key(_whole(s.dir))


def test_the_part_count_stays_bounded_over_many_rebuilds(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    fresh = s.build(rows, prev=False)
    n0 = len(fresh["stable"]["files"])
    nxt = 50_000
    for build in range(12):                                          # churn: add, drop, edit
        rows = rows[5:] + [_row(nxt + k, "2026-10-20") for k in range(8)]
        rows[build * 7] = {**rows[build * 7], "salary": f"${100 + build}k"}
        nxt += 8
        m = s.build(rows)
        assert _key(_assembled(s.dir, m)) == _key(_whole(s.dir))
    assert len(m["stable"]["files"]) <= n0 + 3
    # Only the files the manifest names are left in the output dir.
    on_disk = {f"{PARTS_DIR}/{p.name}" for p in (s.dir / PARTS_DIR).glob("[sd]-*")}
    assert on_disk == set(_all_files(m))


def test_a_damaged_saved_state_falls_back_to_a_fresh_layout(tmp_path):
    s = Site(tmp_path)
    rows = _rows()
    a = s.build(rows)
    assert load_stable_state(s.state) is not None
    victim = s.state / a["stable"]["files"][0]["url"]
    victim.write_bytes(b"garbage")
    assert load_stable_state(s.state) is None
    b = s.build(rows)                                                # still a correct list
    assert _key(_assembled(s.dir, b)) == _key(_whole(s.dir)) and _names(b) == _names(a)


def test_recency_key_matches_the_board_order():
    rows = [{"posted_at": "", "first_seen": "2026-10-20 00:00:00"},
            {"posted_at": "2026-10-01", "first_seen": "2026-10-02"},
            {"posted_at": "2026-10-05", "first_seen": "2026-10-05"},
            {"posted_at": "2026-10-05", "first_seen": "2026-10-06"}]
    got = sorted(rows, key=recency_key, reverse=True)
    assert got == [rows[3], rows[2], rows[1], rows[0]]              # undated last


# -- the client ------------------------------------------------------------------------------ #

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_PAUSE", 0)
    s = Site(tmp_path)
    s.origin = Origin(s.dir)
    yield s
    s.origin.close()


def _feed(s, cache, **kw):
    return StaticFeed(s.origin.url, cache, retry_pause=0, **kw)


def test_the_client_joins_stable_parts_in_board_order_and_keeps_them(tmp_path, site):
    rows = _rows()
    m = site.build(rows)
    sf = _feed(site, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    got, header = sf.jobs()
    assert got == _board(site.dir)
    assert header["count"] == len(rows) and JOBS_FILE not in site.origin.log
    assert sorted(site.origin.parts_fetched()) == sorted(_all_files(m))
    store = tmp_path / "cache" / PARTS_DIR / "store"
    assert sorted(p.name for p in store.iterdir()) == sorted(Path(u).name for u in _all_files(m))
    st = sf.status()
    assert st["parts_done"] == st["parts_total"] == len(_all_files(m))


def test_a_rebuild_fetches_only_the_new_parts_and_a_removed_job_disappears(tmp_path, site):
    rows = _rows()
    a = site.build(rows)
    sf = _feed(site, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    gone = rows[10]
    new = [_row(20_000 + k, "2026-10-20") for k in range(40)]
    rows2 = new + [r for r in rows if r is not gone]
    b = site.build(rows2)
    site.origin.log.clear()
    assert sf.refresh(force=True) is True
    delta = [u for u in _all_files(b) if u not in _all_files(a)]
    assert sorted(site.origin.parts_fetched()) == sorted(delta)
    assert b["stable"]["removed"]["url"] in delta
    got = sf.jobs()[0]
    assert gone["source_id"] not in {r["source_id"] for r in got}
    assert got == _board(site.dir)
    store = tmp_path / "cache" / PARTS_DIR / "store"                # no stale files kept
    assert sorted(p.name for p in store.iterdir()) == sorted(Path(u).name for u in _all_files(b))


def test_the_same_version_downloads_nothing(tmp_path, site):
    rows = _rows()
    site.build(rows)
    sf = _feed(site, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    site.build(rows)
    site.origin.log.clear()
    assert sf.refresh(force=True) is False
    assert site.origin.log == [MANIFEST_FILE]
    assert sf.jobs()[1]["generated_at"] == "2026-10-14T02:00:00+00:00"


def test_a_dropped_new_part_resumes_without_refetching_the_rest(tmp_path, site, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_TRIES", 1)
    rows = _rows()
    a = site.build(rows)
    sf = _feed(site, tmp_path / "cache", tries=1)
    assert sf.refresh(force=True) is True
    rows2 = [_row(30_000 + k, "2026-10-20") for k in range(300)] + rows[1:]
    b = site.build(rows2)
    delta = [u for u in _all_files(b) if u not in _all_files(a)]
    assert len(delta) >= 3
    bad = delta[0]
    site.origin.cuts[bad] = 10
    site.origin.log.clear()
    with pytest.raises(OSError):
        sf.refresh(force=True)
    assert sorted(set(site.origin.parts_fetched())) == sorted(delta)
    assert len(sf.jobs()[0]) == len(rows)                            # the old list still serves
    site.origin.cuts.clear()
    site.origin.log.clear()
    sf2 = _feed(site, tmp_path / "cache")                            # even after a restart
    assert sf2.refresh(force=True) is True
    assert site.origin.parts_fetched() == [bad]
    assert sf2.jobs()[0] == _board(site.dir)


def test_a_stable_part_damaged_on_disk_is_fetched_again(tmp_path, site):
    rows = _rows()
    m = site.build(rows)
    sf = _feed(site, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    victim = _names(m)[1]
    (tmp_path / "cache" / PARTS_DIR / "store" / Path(victim).name).write_bytes(b"garbage")
    rows2 = rows[1:]
    site.build(rows2)
    site.origin.log.clear()
    assert sf.refresh(force=True) is True
    assert victim in site.origin.parts_fetched()
    assert len(sf.jobs()[0]) == len(rows2)


def test_older_apps_and_older_feeds_still_work(tmp_path, site):
    rows = _rows()
    site.build(rows)
    # An app from before the stable layout reads the per-version parts.
    old_app = _feed(site, tmp_path / "old", legacy_parts=True)
    assert old_app.refresh(force=True) is True
    assert old_app.jobs()[0] == _whole(site.dir)
    assert all(Path(u).name.startswith("jobs-") for u in site.origin.parts_fetched())
    # A feed from before the stable layout (no "stable" block) is read the old way.
    man = json.loads((site.dir / MANIFEST_FILE).read_text())
    man.pop("stable")
    (site.dir / MANIFEST_FILE).write_text(json.dumps(man))
    site.origin.log.clear()
    sf = _feed(site, tmp_path / "new")
    assert sf.refresh(force=True) is True and sf.jobs()[0] == _whole(site.dir)
    assert all(Path(u).name.startswith("jobs-") for u in site.origin.parts_fetched())
