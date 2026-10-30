"""The feed crawl's saved state in R2 (scripts/feed_state.py, 2026-10-30): the slim state a crawl
shard starts from, the merge putting the held descriptions back, the archive round trip, the R2
save / restore / prune steps against a fake S3 client, the publish guard and the workflow wiring.
Offline: no boto3, no network."""

from __future__ import annotations

import datetime
import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from sourcing.feedfile import loads_maybe_gz

ROOT = Path(__file__).resolve().parents[1]
_TODAY = datetime.date.today().isoformat()
_BOARDS = [f"co{i:02d}" for i in range(8)]
_QUIET = dict(log=lambda *a: None)


# -- a small saved state ------------------------------------------------------------------- #

def _state(tmp_path, name="data") -> Path:
    """A data dir shaped like the workflow's: a jobs DB (made by the real Watchlist), the sponsor
    overlay, cursor files and the last build's stable parts."""
    from sourcing.watchlist import Watchlist
    d = tmp_path / name
    d.mkdir()
    w = Watchlist(str(d / "resume_agent.db"))
    w.add_company("Acme", "greenhouse", "acme")
    w.upsert_jobs([
        {"source_id": "greenhouse:acme:1", "source": "greenhouse", "company": "Acme",
         "title": "Engineer", "location": "New York, NY", "remote": "", "url": "https://x/1",
         "jd_text": "Build things. The pay range is $120,000 - $150,000 per year.",
         "posted_at": _TODAY, "salary": "$120k-$150k/yr"},
        {"source_id": "workday:big:2", "source": "workday", "company": "Big",
         "title": "Analyst", "location": "Austin, TX", "remote": "", "url": "https://x/2",
         "jd_text": "", "posted_at": _TODAY},
    ])
    w.record_crawl("greenhouse", "acme", True, rows=1)
    w.close()
    (d / "sponsors.db").write_bytes(b"x" * 1000)
    (d / "crawl_cursor.s0of4.json").write_text('{"next": 3}\n')
    (d / "freehire_sponsor_slugs.json").write_text('{"acme": "acme"}\n')
    (d / "feed-prev").mkdir()
    (d / "feed-prev" / "part.json").write_text("{}")
    return d


def _rows(db) -> dict:
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    try:
        return {r["source_id"]: dict(r) for r in c.execute("SELECT * FROM sourced_job")}
    finally:
        c.close()


def test_slim_state_keeps_every_row_and_table_but_holds_no_description(tmp_path):
    from scripts import feed_state
    full = _state(tmp_path)
    r = feed_state.export_shard_state(full, tmp_path / "slim", **_QUIET)
    slim = tmp_path / "slim"
    assert r["rows"] == 2
    assert sorted(p.name for p in slim.iterdir()) == sorted(
        ["resume_agent.db", "crawl_cursor.s0of4.json", "freehire_sponsor_slugs.json"])
    a, b = _rows(full / "resume_agent.db"), _rows(slim / "resume_agent.db")
    assert a.keys() == b.keys()
    for sid in a:                                       # every column the same but the description
        assert {k: v for k, v in a[sid].items() if k != "jd_text"} == \
               {k: v for k, v in b[sid].items() if k != "jd_text"}
    assert b["greenhouse:acme:1"]["jd_text"] == feed_state.JD_HELD
    assert b["workday:big:2"]["jd_text"] == ""          # no description stays no description
    c = sqlite3.connect(str(slim / "resume_agent.db"))
    assert c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
    assert c.execute("SELECT rows FROM board_crawl").fetchone()[0] == 1
    c.close()
    # The crawl's own reads answer as on the full state.
    from sourcing.watchlist import Watchlist
    w = Watchlist(str(slim / "resume_agent.db"))
    assert w.source_ids_with_jd("greenhouse") == {"greenhouse:acme:1"}
    assert {j["source_id"]: j["has_jd"] for j in w.list_jobs(include_dismissed=True)} == \
        {"greenhouse:acme:1": True, "workday:big:2": False}
    w.close()


def test_merge_keeps_the_held_description_and_refills_the_salary(tmp_path):
    from scripts import build_feed, feed_state
    full = _state(tmp_path)
    feed_state.export_shard_state(full, tmp_path / "slim", **_QUIET)
    shard_db = tmp_path / "slim" / "resume_agent.db"
    s = sqlite3.connect(str(shard_db))
    # A shard saw the role again (title changed, salary reset by the list) without re-reading its
    # description, and found a brand-new held row the full state no longer has (pruned there).
    s.execute("UPDATE sourced_job SET title='Senior Engineer', salary='', "
              "last_seen=datetime('now','+1 minute') WHERE source_id='greenhouse:acme:1'")
    s.execute("INSERT INTO sourced_job (source_id, source, company, title, jd_text, last_seen) "
              "VALUES ('greenhouse:acme:9', 'greenhouse', 'Acme', 'Gone', ?, datetime('now','+1 minute'))",
              (feed_state.JD_HELD,))
    s.commit()
    s.close()
    delta = tmp_path / "d.db"
    build_feed.export_delta(shard_db, tmp_path / "slim", "2000-01-01 00:00:00", delta)
    build_feed.merge_delta(full / "resume_agent.db", full, delta, **_QUIET)
    rows = _rows(full / "resume_agent.db")
    kept = rows["greenhouse:acme:1"]
    assert kept["title"] == "Senior Engineer"
    assert kept["jd_text"].startswith("Build things.")              # the real text, not the marker
    assert kept["salary"] == "$120k-$150k/yr"                        # refilled from that text
    assert rows["greenhouse:acme:9"]["jd_text"] == ""                # never the marker
    assert not any(r["jd_text"] == feed_state.JD_HELD for r in rows.values())


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

    def fetch(url, timeout=20):
        if "greenhouse.io" in url:
            board = url.split("/boards/")[1].split("/")[0]
            return {"jobs": [{"id": 1, "title": "Software Engineer", "location": {"name": "New York, NY"},
                              "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/1",
                              "content": f"<p>Build {board}. Pay $100,000 - $130,000 a year.</p>",
                              "first_published": _TODAY}]}
        if "remotive.com" in url:
            return {"jobs": [{"id": 77, "company_name": "Remote Co", "title": "Software Engineer",
                              "candidate_required_location": "USA", "url": "https://remotive.com/j/77",
                              "description": "Remote role.", "publication_date": _TODAY}]}
        return {}
    return tmp_path, fetch


def _feed(out):
    return sorted((j["source_id"], j.get("salary")) for j in
                  loads_maybe_gz((out / "jobs.json.gz").read_bytes())["jobs"])


def _content(db):
    return {sid: (r["title"], r["jd_text"], r["salary"], r["first_seen"]) for sid, r in _rows(db).items()}


def test_shards_on_the_slim_state_publish_what_shards_on_the_full_state_would(fake_world):
    from scripts import build_feed, feed_state
    tmp, fetch = fake_world
    base = tmp / "base"
    build_feed.build(tmp / "o0", base, fetch=fetch, **_QUIET)          # the saved state: every board
    results = {}
    for mode in ("full", "slim"):
        state = tmp / f"state-{mode}"
        shutil.copytree(base, state)
        for i in (0, 1):
            src = tmp / f"{mode}-shard{i}"
            if mode == "full":
                shutil.copytree(state, src)
            else:
                feed_state.export_shard_state(state, src, **_QUIET)
            build_feed.build(tmp / f"never-{mode}{i}", src, fetch=fetch, shard=(i, 2),
                             delta_out=tmp / f"deltas-{mode}" / f"shard-{i}.db", **_QUIET)
        assert build_feed.main(["--data", str(state), "--out", str(tmp / f"out-{mode}"),
                                "--merge-dir", str(tmp / f"deltas-{mode}")]) == 0
        results[mode] = (_feed(tmp / f"out-{mode}"), _content(state / "resume_agent.db"))
    assert results["slim"] == results["full"]
    assert not any(feed_state.JD_HELD in (c[1] or "") for c in results["slim"][1].values())


# -- archives ------------------------------------------------------------------------------ #

@pytest.mark.parametrize("ext", [".tar.gz", ".tar.zst"])
def test_pack_and_unpack_round_trip(tmp_path, ext):
    from scripts import feed_state
    if ext == ".tar.zst" and not shutil.which("zstd"):
        pytest.skip("zstd not installed")
    src = _state(tmp_path)
    arc = feed_state.pack(src, tmp_path / f"a{ext}")
    feed_state.unpack(arc, tmp_path / "back")
    got = sorted(str(p.relative_to(tmp_path / "back")) for p in (tmp_path / "back").rglob("*"))
    assert got == sorted(str(p.relative_to(src)) for p in src.rglob("*"))
    assert feed_state.count_rows(tmp_path / "back") == 2


# -- R2 ------------------------------------------------------------------------------------ #

class _S3:
    """The few S3 calls feed_state makes, over a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = bytes(Body)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, Filename, Bucket, Key):
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        if Key not in self.objects:
            raise KeyError(Key)
        Path(Filename).write_bytes(self.objects[Key])

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page, more = keys[start:start + 2], start + 2 < len(keys)       # small pages: paginate
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + 2)} if more else {})}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


def _at(i):
    return f"2026-10-{10 + i:02d}T00:00:00Z"


def test_push_then_pull_full_and_slim(tmp_path):
    from scripts import feed_state
    s3, data = _S3(), _state(tmp_path)
    m = feed_state.push_state(s3, "b", data, "101", feed_count=1234, workdir=tmp_path, now=_at(0), **_QUIET)
    assert m["rows"] == 2 and m["feed_count"] == 1234
    assert set(s3.objects) == {m["full"]["key"], m["slim"]["key"], "state/runs/101.json", "state/latest.json"}
    assert json.loads(s3.objects["state/latest.json"])["id"] == "101"
    got = feed_state.fetch_state(s3, "b", tmp_path / "full", kind="full", workdir=tmp_path, **_QUIET)
    assert got["id"] == "101" and (tmp_path / "full" / "sponsors.db").exists()
    assert (tmp_path / "full" / "feed-prev" / "part.json").exists()
    feed_state.fetch_state(s3, "b", tmp_path / "slim", kind="slim", workdir=tmp_path, **_QUIET)
    assert not (tmp_path / "slim" / "sponsors.db").exists()
    assert _rows(tmp_path / "slim" / "resume_agent.db")["greenhouse:acme:1"]["jd_text"] == feed_state.JD_HELD


def test_pull_finds_nothing_when_r2_holds_no_state_and_rejects_a_damaged_archive(tmp_path):
    from scripts import feed_state
    s3 = _S3()
    assert feed_state.fetch_state(s3, "b", tmp_path / "x", kind="full", **_QUIET) is None
    m = feed_state.push_state(s3, "b", _state(tmp_path), "7", workdir=tmp_path, now=_at(0), **_QUIET)
    s3.objects[m["full"]["key"]] += b"garbage"
    with pytest.raises(ValueError, match="sha256"):
        feed_state.fetch_state(s3, "b", tmp_path / "x", kind="full", workdir=tmp_path, **_QUIET)


def test_a_named_state_is_restored_and_an_unknown_one_falls_back_to_the_latest(tmp_path):
    from scripts import feed_state
    s3, data = _S3(), _state(tmp_path)
    feed_state.push_state(s3, "b", data, "1", workdir=tmp_path, now=_at(0), **_QUIET)
    feed_state.push_state(s3, "b", data, "2", workdir=tmp_path, now=_at(1), **_QUIET)
    assert feed_state.fetch_state(s3, "b", tmp_path / "a", state_id="1", workdir=tmp_path, **_QUIET)["id"] == "1"
    assert feed_state.fetch_state(s3, "b", tmp_path / "a", state_id="9", workdir=tmp_path, **_QUIET)["id"] == "2"


def test_prune_keeps_the_newest_five_and_never_the_latest(tmp_path):
    from scripts import feed_state
    s3, data = _S3(), _state(tmp_path)
    for i in range(7):
        feed_state.push_state(s3, "b", data, str(100 + i), workdir=tmp_path, now=_at(i), **_QUIET)
    ids = [m["id"] for m in feed_state.list_manifests(s3, "b")]
    assert ids == ["102", "103", "104", "105", "106"]
    assert not any("/100." in k or "/101." in k for k in s3.objects)       # archives went too
    # latest.json naming an OLD state (a rollback push stamped earlier) keeps it, whatever its age.
    feed_state.push_state(s3, "b", data, "old", workdir=tmp_path, now="2026-01-01T00:00:00Z", **_QUIET)
    ids = [m["id"] for m in feed_state.list_manifests(s3, "b")]
    assert "old" in ids and json.loads(s3.objects["state/latest.json"])["id"] == "old"
    assert feed_state.prune_states(s3, "b", keep=0, **_QUIET)              # everything else may go
    assert [m["id"] for m in feed_state.list_manifests(s3, "b")] == ["old"]


def test_push_refuses_an_empty_dir_or_a_bad_id(tmp_path):
    from scripts import feed_state
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError):
        feed_state.push_state(_S3(), "b", tmp_path / "empty", "1", **_QUIET)
    with pytest.raises(ValueError):
        feed_state.push_state(_S3(), "b", _state(tmp_path), "a/b", **_QUIET)


def test_the_merge_restores_the_state_every_shard_started_from(tmp_path):
    from scripts import feed_state
    d = tmp_path / "shards"
    for i, base in enumerate(("41", "41\n", "41")):
        (d / f"feed-shard-{i}").mkdir(parents=True)
        (d / f"feed-shard-{i}" / f"base-{i}.txt").write_text(base)
    assert feed_state.paired_state_id(d) == "41"
    (d / "feed-shard-2" / "base-2.txt").write_text("40")
    assert feed_state.paired_state_id(d) is None                            # disagree: use latest
    assert feed_state.paired_state_id(tmp_path / "none") is None


def test_cli_pull_stops_without_a_state_unless_a_first_run_allows_empty(tmp_path):
    from scripts import feed_state
    s3 = _S3()
    args = ["pull", "--kind", "slim", "--dest", str(tmp_path / "d"), "--id-file", str(tmp_path / "id.txt")]
    assert feed_state.main(args, client=s3, bucket="b", **_QUIET) == 4
    assert feed_state.main(args + ["--allow-empty"], client=s3, bucket="b", **_QUIET) == 0
    assert (tmp_path / "d").is_dir() and (tmp_path / "id.txt").read_text().strip() == ""
    feed_state.push_state(s3, "b", _state(tmp_path), "55", workdir=tmp_path, now=_at(0), **_QUIET)
    assert feed_state.main(args, client=s3, bucket="b", **_QUIET) == 0
    assert (tmp_path / "id.txt").read_text().strip() == "55"
    assert feed_state.count_rows(tmp_path / "d") == 2


def test_the_state_bucket_must_be_set_and_private():
    from scripts import feed_state
    with pytest.raises(SystemExit, match="R2_STATE_BUCKET"):
        feed_state._client_and_bucket({})
    with pytest.raises(SystemExit, match="public feed bucket"):
        feed_state._client_and_bucket({"R2_STATE_BUCKET": "feed", "R2_BUCKET": "feed"})


# -- the publish guard --------------------------------------------------------------------- #

def test_check_drop_rule():
    from scripts import feed_state
    assert feed_state.check_drop(190_000, 233_000)[0]                      # 81.5%: fine
    ok, msg = feed_state.check_drop(158_535, 233_496)
    assert not ok and "refusing" in msg                                     # the 2026-10-30 run
    assert feed_state.check_drop(5, None)[0]                                # nothing to compare
    assert feed_state.check_drop(5, 0)[0]


def test_previous_count_reads_the_live_manifest_then_the_saved_state(tmp_path):
    from scripts import feed_state
    sm = tmp_path / "restored.json"
    sm.write_text(json.dumps({"id": "1", "feed_count": 200}))
    assert feed_state.previous_count("u", sm, fetch=lambda u: b'{"count": 300}') == 300

    def down(u):
        raise OSError("offline")
    assert feed_state.previous_count("u", sm, fetch=down) == 200
    assert feed_state.previous_count("", None) is None


def test_cli_guard_fails_on_a_shrunk_feed_unless_allowed(tmp_path, monkeypatch):
    from scripts import feed_state
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"count": 100}))
    monkeypatch.setattr(feed_state, "previous_count", lambda url, sm=None, fetch=None: 1000)
    assert feed_state.main(["guard", "--manifest", str(man)], **_QUIET) == 3
    assert feed_state.main(["guard", "--manifest", str(man), "--allow-drop"], **_QUIET) == 0
    man.write_text(json.dumps({"count": 900}))
    assert feed_state.main(["guard", "--manifest", str(man)], **_QUIET) == 0


# -- the workflow -------------------------------------------------------------------------- #

def test_the_workflow_keeps_the_state_in_r2_and_off_the_actions_cache():
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "feed.yml").read_text())
    jobs = wf["jobs"]

    def runs(job):
        return "\n".join(s.get("run", "") for s in jobs[job]["steps"])

    def uses(job):
        return [s.get("uses", "") for s in jobs[job]["steps"]]

    for job in ("crawl", "build-and-upload"):
        assert not any(u.startswith("actions/cache") for u in uses(job))   # no cache egress
        assert all("cache" not in (s.get("with") or {}) for s in jobs[job]["steps"])   # no pip cache
    assert "--kind slim" in runs("crawl") and "--kind full" not in runs("crawl")
    assert "--kind full" in runs("build-and-upload") and "--pair-dir .shards" in runs("build-and-upload")
    names = [s.get("name") or s.get("uses") for s in jobs["build-and-upload"]["steps"]]
    order = [names.index(n) for n in ("Restore the crawl state from R2", "Merge the shards and build the feed",
                                      "Check the feed did not shrink", "Save the crawl state to R2",
                                      "Upload to R2")]
    assert order == sorted(order)                       # guard before saving, saving before publishing
    # Only the bootstrap job touches the Actions cache, and only for a key named by hand.
    assert any(u.startswith("actions/cache/restore") for u in uses("seed-state"))
    assert "seed_from_cache" in jobs["seed-state"]["if"]
    assert wf["env"]["R2_STATE_BUCKET"] == "${{ vars.R2_STATE_BUCKET }}"
