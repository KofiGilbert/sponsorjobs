"""The job list in PARTS (2026-10-14). The CDN ignores Range, and a flaky link dropped the ~10 MB
list every 1 to 3 MB, so every try restarted from zero and the list never landed. The builder now
also writes the list as ~1 MB content-addressed parts listed in the manifest; the app downloads
them two at a time, checks each against its sha256, keeps what arrived, and on a drop fetches only
the parts still missing. jobs.json.gz is still written for older app versions.

A real local HTTP server cuts connections mid-part, corrupts a part, or rebuilds the feed under a
running download -- all fast enough for a unit test. Offline."""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sourcing import feedclient
from sourcing.feedclient import FeedLoading, StaticFeed
from sourcing.feedfile import (JOBS_FILE, MANIFEST_FILE, PARTS_DIR, loads_maybe_gz, rows_version,
                               write_feed)

_TODAY = datetime.date.today().isoformat()


def _rows(n, company="Acme"):
    # Random URLs keep the gzip honest (identical rows would compress to nothing).
    return [{"source_id": f"greenhouse:acme:{i}", "source": "greenhouse", "company": company,
             "title": f"Software Engineer {i}", "location": "New York, NY", "remote": "hybrid",
             "url": f"https://acme.example/jobs/{i}/{os.urandom(16).hex()}", "posted_at": _TODAY,
             "first_seen": "2026-10-14 10:00:00", "salary": "", "sponsorship_stated": True,
             "us": True, "entry_level": False, "visa": [], "nationality_visas": [], "sponsor": {}}
            for i in range(n)]


def _build(out, rows, part_bytes=3000, generated="2026-10-14T12:00:00+00:00"):
    return write_feed(out, rows, {}, jobs_url="", generated_at=generated, part_bytes=part_bytes)


# -- the builder ----------------------------------------------------------------------------- #

def test_the_builder_splits_the_list_into_verified_parts(tmp_path):
    rows = _rows(600)
    m = _build(tmp_path, rows)
    p = m["parts"]
    whole = loads_maybe_gz((tmp_path / JOBS_FILE).read_bytes())
    assert len(p["files"]) == -(-(tmp_path / JOBS_FILE).stat().st_size // 3000) > 3
    joined = []
    for i, f in enumerate(p["files"]):
        body = (tmp_path / f["url"]).read_bytes()
        assert f["url"] == f"{PARTS_DIR}/jobs-{p['version']}-{i:03d}.json.gz"   # content-addressed
        assert hashlib.sha256(body).hexdigest() == f["sha256"] and len(body) == f["bytes"]
        part = loads_maybe_gz(body)
        assert part["version"] == p["version"] and part["part"] == i and part["parts"] == len(p["files"])
        assert len(part["jobs"]) == f["count"]
        joined += part["jobs"]
    assert joined == whole["jobs"]                                   # the parts ARE the list
    assert p["count"] == m["count"] == 600 and p["total_bytes"] == sum(f["bytes"] for f in p["files"])
    sizes = [f["bytes"] for f in p["files"]]
    assert max(sizes) < 1.5 * min(sizes)                             # near-equal parts
    assert json.loads((tmp_path / MANIFEST_FILE).read_text()) == m
    # The single file stays for app versions that predate the parts.
    assert whole["feed_version"] == 1 and whole["count"] == 600


def test_the_version_follows_the_rows_not_the_build_time(tmp_path):
    rows = _rows(50)
    a = _build(tmp_path / "a", rows, generated="2026-10-14T12:00:00+00:00")["parts"]
    b = _build(tmp_path / "b", rows, generated="2026-10-14T15:00:00+00:00")["parts"]
    assert a["version"] == b["version"] == rows_version(
        loads_maybe_gz((tmp_path / "a" / JOBS_FILE).read_bytes())["jobs"])
    assert [f["sha256"] for f in a["files"]] == [f["sha256"] for f in b["files"]]
    # New rows: a new version, and the old version's parts leave the output dir.
    c = _build(tmp_path / "a", _rows(51))["parts"]
    assert c["version"] != a["version"]
    names = sorted(p.name for p in (tmp_path / "a" / PARTS_DIR).iterdir())
    assert names == sorted(Path(f["url"]).name for f in c["files"])


def test_a_tiny_list_is_one_part(tmp_path):
    m = _build(tmp_path, _rows(2), part_bytes=1_000_000)
    assert len(m["parts"]["files"]) == 1 and m["parts"]["files"][0]["count"] == 2
    m = _build(tmp_path / "e", [], part_bytes=1_000_000)
    assert len(m["parts"]["files"]) == 1 and m["parts"]["files"][0]["count"] == 0


class _S3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts, self.deleted = [], []

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 3]                                  # small pages: paginate
        more = start + 3 < len(keys)
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + 3)} if more else {})}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.deleted.append(o["Key"])
            self.objects.pop(o["Key"], None)


def test_the_uploader_sends_parts_immutable_before_the_list_and_prunes_old_versions(tmp_path):
    from scripts import upload_feed
    s3 = _S3()
    v1 = _build(tmp_path / "v1", _rows(300))["parts"]
    keys = upload_feed.upload(tmp_path / "v1", s3, "b", prefix="feed", log=lambda *a: None)
    part_keys = [f"feed/{f['url']}" for f in v1["files"]]
    assert keys[-2:] == ["feed/jobs.json.gz", "feed/manifest.json"]
    assert keys[-2 - len(part_keys):-2] == part_keys                 # parts land before the list
    hdr = {p["Key"]: p for p in s3.puts}[part_keys[0]]
    assert hdr["CacheControl"] == "public, max-age=31536000, immutable"
    assert hdr["ContentEncoding"] == "gzip" and hdr["ContentType"] == "application/json"
    v2 = _build(tmp_path / "v2", _rows(301))["parts"]
    upload_feed.upload(tmp_path / "v2", s3, "b", prefix="feed", log=lambda *a: None)
    assert not s3.deleted                                            # v1 kept one more version
    v3 = _build(tmp_path / "v3", _rows(302))["parts"]
    upload_feed.upload(tmp_path / "v3", s3, "b", prefix="feed", log=lambda *a: None)
    assert sorted(s3.deleted) == sorted(part_keys)                   # v1 gone, v2 + v3 stay
    left = {k for k in s3.objects if k.startswith("feed/parts/")}
    assert left == {f"feed/{f['url']}" for f in v2["files"] + v3["files"]}


# -- the client ------------------------------------------------------------------------------ #

class Origin:
    """Serves a built feed dir. Per path it can cut the connection mid-body `cuts[path]` times,
    send corrupt bytes `corrupt[path]` times, or trickle. Every GET is logged."""

    def __init__(self, root: Path, pause=0.0):
        self.root, self.pause = Path(root), pause
        self.cuts: dict[str, int] = {}
        self.corrupt: dict[str, int] = {}
        self.log: list[str] = []
        self.lock = threading.Lock()
        origin = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                rel = self.path.split("/feed/", 1)[-1]
                with origin.lock:
                    origin.log.append(rel)
                    cut = origin.cuts.get(rel, 0) > 0
                    if cut:
                        origin.cuts[rel] -= 1
                    bad = origin.corrupt.get(rel, 0) > 0
                    if bad:
                        origin.corrupt[rel] -= 1
                path = origin.root / rel
                if not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                body = path.read_bytes()
                if bad:
                    body = body[:-8] + b"\x00" * 8
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    send = body[:len(body) // 2] if cut else body
                    step = 1024 if origin.pause else len(send) or 1
                    for i in range(0, len(send), step):
                        self.wfile.write(send[i:i + step])
                        self.wfile.flush()
                        if origin.pause:
                            time.sleep(origin.pause)
                    if cut:                                   # the drop: close mid-part
                        self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/feed"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def parts_fetched(self):
        return [r for r in self.log if r.startswith(PARTS_DIR + "/")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_PAUSE", 0)
    made = []

    def make(rows, **kw):
        site = tmp_path / f"site{len(made)}"
        man = _build(site, rows, **{k: v for k, v in kw.items() if k in ("part_bytes", "generated")})
        o = Origin(site, pause=kw.get("pause", 0.0))
        o.manifest = man
        made.append(o)
        return o
    yield make
    for o in made:
        o.close()


def _feed(o, cache, **kw):
    return StaticFeed(o.url, cache, retry_pause=0, **kw)


def test_parts_download_assemble_and_never_touch_the_single_file(tmp_path, origin):
    rows = _rows(800)
    o = origin(rows)
    sf = _feed(o, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    got, header = sf.jobs()
    assert [r["source_id"] for r in got] == [r["source_id"] for r in rows]
    assert header["generated_at"] == "2026-10-14T12:00:00+00:00" and header["count"] == 800
    assert JOBS_FILE not in o.log                                    # the parts, not the 10 MB file
    assert sorted(o.parts_fetched()) == sorted(f["url"] for f in o.manifest["parts"]["files"])
    assert not (tmp_path / "cache" / PARTS_DIR).exists()             # joined, then tidied away
    st = sf.status()
    assert st["parts_done"] == st["parts_total"] == len(o.manifest["parts"]["files"])
    assert st["bytes"] == st["total"] == o.manifest["parts"]["total_bytes"]


def test_a_dropped_part_is_the_only_one_fetched_again(tmp_path, origin, monkeypatch):
    o = origin(_rows(800))
    files = [f["url"] for f in o.manifest["parts"]["files"]]
    assert len(files) >= 5
    # Every part is cut once mid-body: each needs exactly one more try, and none restarts the rest.
    for f in files:
        o.cuts[f] = 1
    sf = _feed(o, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    fetched = o.parts_fetched()
    assert len(fetched) == 2 * len(files) and all(fetched.count(f) == 2 for f in files)
    assert len(sf.jobs()[0]) == 800


def test_a_download_that_gives_up_resumes_from_the_parts_on_disk(tmp_path, origin, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_TRIES", 1)
    o = origin(_rows(800))
    files = [f["url"] for f in o.manifest["parts"]["files"]]
    bad = files[2]
    o.cuts[bad] = 10                                                 # this part keeps dropping
    sf = _feed(o, tmp_path / "cache", tries=2)
    with pytest.raises(OSError):
        sf.refresh(force=True)
    assert sf.status()["failing"] and not sf.jobs_path.exists()
    vdir = tmp_path / "cache" / PARTS_DIR / o.manifest["parts"]["version"]
    on_disk = sorted(p.name for p in vdir.iterdir())
    assert on_disk == sorted(Path(f).name for f in files if f != bad)   # every other part kept
    assert all(o.parts_fetched().count(f) == 1 for f in files if f != bad)
    # The link recovers (the app restarts, even): only the missing part is fetched.
    o.cuts.clear()
    o.log.clear()
    sf2 = _feed(o, tmp_path / "cache")
    assert sf2.refresh(force=True) is True
    assert o.parts_fetched() == [bad] and len(sf2.jobs()[0]) == 800


def test_a_part_that_fails_its_sha256_is_fetched_again(tmp_path, origin):
    o = origin(_rows(400))
    f0 = o.manifest["parts"]["files"][0]["url"]
    o.corrupt[f0] = 1
    sf = _feed(o, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    assert o.parts_fetched().count(f0) == 2 and len(sf.jobs()[0]) == 400


def test_a_part_left_corrupt_on_disk_is_not_trusted(tmp_path, origin, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_TRIES", 1)
    o = origin(_rows(400))
    files = [f["url"] for f in o.manifest["parts"]["files"]]
    o.cuts[files[-1]] = 10
    sf = _feed(o, tmp_path / "cache", tries=1)
    with pytest.raises(OSError):
        sf.refresh(force=True)
    vdir = tmp_path / "cache" / PARTS_DIR / o.manifest["parts"]["version"]
    (vdir / Path(files[0]).name).write_bytes(b"garbage")               # disk damage
    o.cuts.clear()
    o.log.clear()
    assert _feed(o, tmp_path / "cache").refresh(force=True) is True
    assert sorted(o.parts_fetched()) == sorted([files[0], files[-1]])


def test_a_new_version_discards_the_old_parts(tmp_path, origin, monkeypatch):
    monkeypatch.setattr(feedclient, "PART_TRIES", 1)
    old = origin(_rows(400, company="Old Co"))
    o_files = [f["url"] for f in old.manifest["parts"]["files"]]
    old.cuts[o_files[1]] = 10
    cache = tmp_path / "cache"
    with pytest.raises(OSError):
        _feed(old, cache, tries=1).refresh(force=True)
    old_dir = cache / PARTS_DIR / old.manifest["parts"]["version"]
    assert old_dir.exists() and any(old_dir.iterdir())
    new = origin(_rows(400, company="New Co"), generated="2026-10-14T15:00:00+00:00")
    sf = _feed(new, cache)
    assert sf.refresh(force=True) is True
    assert not old_dir.exists()
    assert {r["company"] for r in sf.jobs()[0]} == {"New Co"}
    assert len(new.parts_fetched()) == len(new.manifest["parts"]["files"])


def test_the_same_version_downloads_nothing_but_moves_the_build_time(tmp_path, origin):
    rows = _rows(300)
    o = origin(rows)
    sf = _feed(o, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    # A rebuild with the same rows: the same parts, a newer generated_at.
    _build(o.root, rows, generated="2026-10-14T18:00:00+00:00")
    o.log.clear()
    assert sf.refresh(force=True) is False
    assert o.log == [MANIFEST_FILE]
    assert sf.jobs()[1]["generated_at"] == "2026-10-14T18:00:00+00:00"
    assert sf.refreshed_at() == "2026-10-14T18:00:00+00:00"


def test_a_feed_rebuilt_mid_download_is_followed_to_the_new_version(tmp_path, origin):
    o = origin(_rows(400, company="Old Co"))
    stale = json.dumps(o.manifest).encode()                           # what the client read first
    _build(o.root, _rows(400, company="New Co"))                      # ...then the site was rebuilt
    sf = _feed(o, tmp_path / "cache", tries=3)
    real, calls = sf._manifest_fetch, []

    def manifest_fetch(url, headers):
        calls.append(url)
        return (200, {}, stale) if len(calls) == 1 else real(url, headers)
    sf._manifest_fetch = manifest_fetch
    assert sf.refresh(force=True) is True
    assert len(calls) == 2                                            # the old parts 404: re-read
    assert {r["company"] for r in sf.jobs()[0]} == {"New Co"}


def test_no_parts_in_the_manifest_falls_back_to_the_single_file(tmp_path, origin):
    o = origin(_rows(200))
    man = json.loads((o.root / MANIFEST_FILE).read_text())
    man.pop("parts")
    (o.root / MANIFEST_FILE).write_text(json.dumps(man))
    sf = _feed(o, tmp_path / "cache")
    assert sf.refresh(force=True) is True
    assert o.log == [MANIFEST_FILE, JOBS_FILE] and len(sf.jobs()[0]) == 200
    # No manifest at all (404) is the same.
    (o.root / MANIFEST_FILE).unlink()
    o.log.clear()
    assert sf.refresh(force=True) in (True, False)
    assert o.log == [MANIFEST_FILE, JOBS_FILE]


def test_the_loading_state_counts_parts_and_bytes(tmp_path, origin):
    o = origin(_rows(800), pause=0.01)                              # ~1 KB per 10 ms per part
    total = o.manifest["parts"]["total_bytes"]
    n = len(o.manifest["parts"]["files"])
    sf = StaticFeed(o.url, tmp_path / "cache", wait=0.05, retry_pause=0)
    with pytest.raises(FeedLoading):
        sf.jobs()
    seen = []
    deadline = time.monotonic() + 15
    while sf.downloading() and time.monotonic() < deadline:
        st = sf.status()
        seen.append((st["parts_done"], st["bytes"]))
        assert st["parts_total"] == n and st["total"] == total and st["bytes"] <= total
        time.sleep(0.02)
    assert sf.wait_download(10)
    done_counts = [d for d, _ in seen]
    assert seen and done_counts == sorted(done_counts) and 0 < max(done_counts) < n + 1
    assert any(0 < b < total for _, b in seen)                      # bytes moved while it ran
    assert len(sf.jobs()[0]) == 800
