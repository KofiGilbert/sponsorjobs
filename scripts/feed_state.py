#!/usr/bin/env python3
"""The feed crawl's saved state in Cloudflare R2: what the `feed` workflow restores before a run
and saves after it (2026-10-30; before that it lived in the GitHub Actions cache, whose account
egress limit cut a run off from its state and published 158,535 jobs instead of ~233,000).

Two archives per saved state, both taken from the merge job's data dir after a good build:

  full  the whole data dir (jobs DB with every description, sponsor overlay, cursors, the last
        build's stable parts in feed-prev/). Only the merge job downloads it.
  slim  what a crawl SHARD needs (export_shard_state): the jobs DB with each stored description
        replaced by the JD_HELD marker, the cursor / slug JSON files, nothing else. A shard reads
        rows to dedupe, keep first_seen and skip descriptions already fetched; it never reads a
        stored description's text, and the merge puts the real text back (build_feed.merge_delta).

R2 layout, in a PRIVATE bucket (R2_STATE_BUCKET; the feed bucket's custom domain serves every
key in it, so the state must not live there):

  <prefix>/full/<id>.tar.zst     <prefix>/slim/<id>.tar.zst     (or .tar.gz without zstd)
  <prefix>/runs/<id>.json        that state's manifest: {format, id, created_at, rows,
                                 feed_count, full: {key, bytes, sha256}, slim: {...}}
  <prefix>/latest.json           a copy of the newest good manifest: the pointer a run starts from

push_state uploads the archives, then the run manifest, then latest.json LAST, so latest never
names an archive that is not there; then it prunes all but the newest KEEP states, never the one
latest names. boto3 is imported lazily (scripts/upload_feed.make_client); the client is
injectable so every step is tested with a fake.

    python scripts/feed_state.py pull --kind full --dest .feed-data [--pair-dir .shards]
    python scripts/feed_state.py push --data .feed-data --id 41039418699 --feed-manifest .site/feed/manifest.json
    python scripts/feed_state.py guard --manifest .site/feed/manifest.json --previous-url https://.../manifest.json
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT = 1
JOBS_DB = "resume_agent.db"            # backend.feed._paths()[0] under the data dir
# Stands in for a stored description in the slim state. Non-empty, so every "has a description"
# check a shard makes (source_ids_with_jd, list_jobs' has_jd) answers as on the full state; never
# published: merge_delta keeps the full state's text for a row that carries it.
JD_HELD = "[sponsorjobs: description held in the full crawl state]"
DEFAULT_PREFIX = "state"
KEEP = 5                               # saved states kept in R2 (the newest; latest is never pruned)
LATEST = "latest.json"
MIN_RATIO = 0.8                        # the publish guard: refuse a feed under 80% of the last one


def log_default(*a):
    print(*a, flush=True)


# -- the slim shard state ------------------------------------------------------------------ #

def count_rows(data_dir) -> int | None:
    """sourced_job rows in the jobs DB under `data_dir`, or None when there is no readable DB."""
    db = Path(data_dir) / JOBS_DB
    if not db.exists():
        return None
    try:
        c = sqlite3.connect(str(db))
        try:
            return int(c.execute("SELECT COUNT(*) FROM sourced_job").fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return None


def checkpoint(data_dir) -> None:
    """Fold the jobs DB's write-ahead log into the file, so an archive of the dir is complete."""
    db = Path(data_dir) / JOBS_DB
    if db.exists():
        c = sqlite3.connect(str(db))
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()


def export_shard_state(data_dir, out_dir, log=log_default) -> dict:
    """Write the slim state a crawl shard starts from into `out_dir` (emptied first): the jobs DB
    with every table, row and index of the full one, except that each non-empty sourced_job.jd_text
    is JD_HELD; plus every top-level *.json file of `data_dir` (the shards' cursors, the career-site
    cursor, the freehire cursor and slug cache). Left out: the sponsor overlay (shard 0 rebuilds it
    from the committed seed, which is all the cached copy ever held) and feed-prev/ (only the
    merge writes a feed). Returns {rows, bytes, files}."""
    src_dir, out = Path(data_dir), Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files: list[str] = []
    rows = 0
    src = src_dir / JOBS_DB
    if src.exists():
        dst = out / JOBS_DB
        conn = sqlite3.connect(str(dst))
        try:
            conn.execute("ATTACH DATABASE ? AS f", (str(src),))
            objs = conn.execute("SELECT type, name, sql FROM f.sqlite_master WHERE sql IS NOT NULL "
                                "AND name NOT LIKE 'sqlite_%'").fetchall()
            for typ, name, sql in objs:
                if typ != "table":
                    continue
                conn.execute(sql)
                cols = [r[1] for r in conn.execute(f'PRAGMA f.table_info("{name}")')]
                if name == "sourced_job" and "jd_text" in cols:
                    sel = ", ".join("CASE WHEN jd_text IS NULL OR jd_text = '' THEN jd_text ELSE ? END"
                                    if c == "jd_text" else f'"{c}"' for c in cols)
                    conn.execute(f'INSERT INTO main."{name}" SELECT {sel} FROM f."{name}"', (JD_HELD,))
                else:
                    conn.execute(f'INSERT INTO main."{name}" SELECT * FROM f."{name}"')
            for typ, name, sql in objs:                    # indexes, views, triggers after the rows
                if typ != "table":
                    conn.execute(sql)
            conn.commit()
            rows = conn.execute("SELECT COUNT(*) FROM main.sourced_job").fetchone()[0] \
                if any(n == "sourced_job" for t, n, _ in objs if t == "table") else 0
            conn.execute("DETACH DATABASE f")
        finally:
            conn.close()
        files.append(JOBS_DB)
    for f in sorted(src_dir.glob("*.json")):
        if f.is_file():
            shutil.copy2(f, out / f.name)
            files.append(f.name)
    size = sum((out / n).stat().st_size for n in files)
    log(f"[feed_state] slim shard state: {rows} rows, {len(files)} files, {size / 1e6:.1f} MB "
        f"uncompressed")
    return {"rows": rows, "bytes": size, "files": files}


# -- archives ------------------------------------------------------------------------------ #

def archive_ext() -> str:
    """.tar.zst when the zstd tool is installed (GitHub's runners have it), else .tar.gz."""
    return ".tar.zst" if shutil.which("zstd") else ".tar.gz"


def pack(src_dir, archive) -> Path:
    """Archive the contents of `src_dir` (paths relative to it) into `archive`; the suffix picks
    the compression (.tar.zst through the zstd tool, .tar.gz in-process)."""
    src_dir, archive = Path(src_dir), Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)
    entries = sorted(p.name for p in src_dir.iterdir())
    if archive.name.endswith(".tar.zst"):
        with open(archive, "wb") as fout:
            proc = subprocess.Popen(["zstd", "-q", "-T0", "-c"], stdin=subprocess.PIPE, stdout=fout)
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                for name in entries:
                    tar.add(src_dir / name, arcname=name)
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f"zstd failed packing {archive}")
    else:
        with tarfile.open(archive, "w:gz", compresslevel=6) as tar:
            for name in entries:
                tar.add(src_dir / name, arcname=name)
    return archive


def _extract(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(dest, filter="data")              # Python 3.12: no absolute / .. paths
    except TypeError:                                    # pragma: no cover - older Python
        tar.extractall(dest)


def unpack(archive, dest) -> None:
    """Extract `archive` into `dest` (emptied first)."""
    archive, dest = Path(archive), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if archive.name.endswith(".tar.zst"):
        proc = subprocess.Popen(["zstd", "-q", "-d", "-c", str(archive)], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            _extract(tar, dest)
        if proc.wait() != 0:
            raise RuntimeError(f"zstd failed unpacking {archive}")
    else:
        with tarfile.open(archive, "r:gz") as tar:
            _extract(tar, dest)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# -- R2 ------------------------------------------------------------------------------------ #

def _base(prefix: str) -> str:
    prefix = (prefix or "").strip("/")
    return f"{prefix}/" if prefix else ""


def _get_json(client, bucket: str, key: str) -> dict | None:
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:                                    # noqa: BLE001 - absent (NoSuchKey) or unreadable
        return None
    try:
        d = json.loads(body)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _put_json(client, bucket: str, key: str, data: dict) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=json.dumps(data, indent=1).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")


def read_latest(client, bucket: str, prefix: str = DEFAULT_PREFIX) -> dict | None:
    return _get_json(client, bucket, f"{_base(prefix)}{LATEST}")


def read_manifest(client, bucket: str, prefix: str, state_id: str) -> dict | None:
    return _get_json(client, bucket, f"{_base(prefix)}runs/{state_id}.json")


def list_manifests(client, bucket: str, prefix: str = DEFAULT_PREFIX) -> list[dict]:
    """Every saved state's manifest, oldest first (by created_at, then id)."""
    base = _base(prefix)
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": f"{base}runs/"}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents") or [] if o["Key"].endswith(".json")]
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    out = [m for m in (_get_json(client, bucket, k) for k in keys) if m and m.get("id")]
    return sorted(out, key=lambda m: (str(m.get("created_at") or ""), str(m["id"])))


def prune_states(client, bucket: str, prefix: str = DEFAULT_PREFIX, keep: int = KEEP,
                 log=log_default) -> list[str]:
    """Delete the archives and manifests of all but the newest `keep` saved states. The state
    latest.json names is never deleted, whatever its age. Returns the deleted keys."""
    latest = read_latest(client, bucket, prefix) or {}
    protect = {str(latest.get("id") or "")}
    states = list_manifests(client, bucket, prefix)
    old = states[:-keep] if keep > 0 else states
    gone: list[str] = []
    for m in old:
        if str(m["id"]) in protect:
            continue
        keys = [str((m.get(k) or {}).get("key") or "") for k in ("full", "slim")]
        keys = [k for k in keys if k] + [f"{_base(prefix)}runs/{m['id']}.json"]
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
        gone += keys
    if gone:
        log(f"[feed_state] pruned {len(gone)} objects of older saved states (keeping {keep})")
    return gone


def push_state(client, bucket: str, data_dir, state_id: str, *, prefix: str = DEFAULT_PREFIX,
               feed_count: int | None = None, keep: int = KEEP, workdir=None, log=log_default,
               now: str | None = None) -> dict:
    """Save `data_dir` as state `state_id`: pack the full dir and its slim shard state, upload both,
    then the run manifest, then latest.json (last, so it never names a missing archive), then
    prune. Returns the manifest."""
    state_id = str(state_id).strip()
    if not state_id or "/" in state_id:
        raise ValueError(f"bad state id {state_id!r}")
    data_dir = Path(data_dir)
    rows = count_rows(data_dir)
    if rows is None:
        raise ValueError(f"no jobs DB under {data_dir}: refusing to save an empty state")
    checkpoint(data_dir)
    base = _base(prefix)
    ext = archive_ext()
    manifest: dict = {"format": FORMAT, "id": state_id,
                      "created_at": now or datetime.datetime.now(datetime.timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "rows": rows, "feed_count": feed_count}
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        tmp = Path(tmp)
        export_shard_state(data_dir, tmp / "slim", log=log)
        for kind, src in (("full", data_dir), ("slim", tmp / "slim")):
            arc = pack(src, tmp / f"{kind}{ext}")
            key = f"{base}{kind}/{state_id}{ext}"
            client.upload_file(str(arc), bucket, key)
            manifest[kind] = {"key": key, "bytes": arc.stat().st_size, "sha256": sha256_file(arc)}
            log(f"[feed_state] uploaded {kind} state {key}: {arc.stat().st_size / 1e6:.1f} MB")
    _put_json(client, bucket, f"{base}runs/{state_id}.json", manifest)
    _put_json(client, bucket, f"{base}{LATEST}", manifest)
    log(f"[feed_state] saved state {state_id}: {rows} rows" +
        (f", feed {feed_count}" if feed_count is not None else ""))
    try:
        prune_states(client, bucket, prefix, keep=keep, log=log)
    except Exception as exc:                             # noqa: BLE001 - tidy-up only
        log(f"[feed_state] could not prune older states: {exc}")
    return manifest


def fetch_state(client, bucket: str, dest, *, kind: str = "full", prefix: str = DEFAULT_PREFIX,
                state_id: str | None = None, workdir=None, log=log_default) -> dict | None:
    """Download and unpack one saved state's `kind` archive into `dest` (replacing it). With
    `state_id`, that state; if it has no manifest, latest is used (with a warning). Returns the
    manifest, or None when R2 holds no saved state at all. Raises when the archive fails its
    checksum, so a damaged state never becomes a run's starting point."""
    if kind not in ("full", "slim"):
        raise ValueError(kind)
    m = None
    if state_id:
        m = read_manifest(client, bucket, prefix, state_id)
        if m is None:
            log(f"::warning::[feed_state] saved state {state_id} not found; using the latest")
    if m is None:
        m = read_latest(client, bucket, prefix)
    if m is None:
        return None
    entry = m.get(kind) or {}
    key = str(entry.get("key") or "")
    if not key:
        raise ValueError(f"saved state {m.get('id')} has no {kind} archive")
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        arc = Path(tmp) / Path(key).name
        client.download_file(bucket, key, str(arc))
        digest = sha256_file(arc)
        if entry.get("sha256") and digest != entry["sha256"]:
            raise ValueError(f"{key}: sha256 {digest} does not match the manifest's {entry['sha256']}")
        unpack(arc, dest)
    rows = count_rows(dest)
    log(f"[feed_state] restored {kind} state {m.get('id')} ({m.get('created_at')}): "
        f"{rows} rows (saved with {m.get('rows')})")
    if rows is not None and m.get("rows") is not None and rows != int(m["rows"]):
        raise ValueError(f"{kind} state {m.get('id')} holds {rows} rows, its manifest says {m['rows']}")
    return m


def paired_state_id(pair_dir) -> str | None:
    """The saved state every crawl shard started from (the base-*.txt files they hand the merge),
    so the merge folds their changes into that same state. None when they disagree or left none."""
    if not pair_dir or not Path(pair_dir).exists():
        return None
    ids = {p.read_text().strip() for p in Path(pair_dir).rglob("base-*.txt")} - {""}
    return ids.pop() if len(ids) == 1 else None


# -- the publish guard --------------------------------------------------------------------- #

def previous_count(url: str = "", state_manifest=None, fetch=None) -> int | None:
    """The job count of the last published feed: its live manifest.json at `url`, else the
    feed_count the last saved state recorded. None when neither is known."""
    if url:
        try:
            if fetch is None:
                import urllib.request
                req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                           "User-Agent": "sponsorjobs-feed-guard"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
            else:
                body = fetch(url)
            n = json.loads(body).get("count")
            if isinstance(n, int) and n >= 0:
                return n
        except Exception:                                # noqa: BLE001 - fall through to the state's
            pass
    if state_manifest:
        try:
            n = json.loads(Path(state_manifest).read_text()).get("feed_count")
            if isinstance(n, int) and n >= 0:
                return n
        except (OSError, ValueError):
            pass
    return None


def check_drop(count: int, previous: int | None, min_ratio: float = MIN_RATIO) -> tuple[bool, str]:
    """(ok, message): a feed of `count` jobs may replace one of `previous` when it holds at least
    min_ratio of it. With no previous count there is nothing to compare, so it passes."""
    if previous is None:
        return True, f"{count} jobs; no previous count to compare with"
    if previous <= 0 or count >= min_ratio * previous:
        return True, f"{count} jobs vs {previous} last time"
    return False, (f"{count} jobs is under {min_ratio:.0%} of the {previous} published last time: "
                   "refusing to publish (re-run with allow_drop to publish anyway)")


# -- CLI ----------------------------------------------------------------------------------- #

def _client_and_bucket(env=None):
    env = os.environ if env is None else env
    bucket = (env.get("R2_STATE_BUCKET") or "").strip()
    if not bucket:
        raise SystemExit("feed_state: set R2_STATE_BUCKET, a PRIVATE R2 bucket for the crawl state "
                         "(the feed bucket's custom domain serves every key; docs/feed.md)")
    if bucket == (env.get("R2_BUCKET") or "").strip():
        raise SystemExit("feed_state: R2_STATE_BUCKET must not be the public feed bucket")
    # The archives go up as multipart uploads; R2 documents these two settings for boto3 >= 1.36,
    # whose default extra checksums it does not accept on every call (older boto3 ignores them).
    # Integrity is checked end to end anyway: every archive's sha256 is in its manifest.
    os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "WHEN_REQUIRED")
    os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "WHEN_REQUIRED")
    from scripts.upload_feed import make_client
    return make_client(env), bucket


def main(argv=None, client=None, bucket=None, log=log_default) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    prefix_default = os.environ.get("R2_STATE_PREFIX") or DEFAULT_PREFIX

    p = sub.add_parser("pull", help="download a saved state into a data dir")
    p.add_argument("--kind", choices=("full", "slim"), required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--prefix", default=prefix_default)
    p.add_argument("--id", default="", help="this saved state instead of the latest")
    p.add_argument("--pair-dir", default="", help="start from the state the shards' base-*.txt name")
    p.add_argument("--id-file", default="", help="write the id of the state restored here")
    p.add_argument("--save-manifest", default="", help="write the restored state's manifest here")
    p.add_argument("--allow-empty", action="store_true",
                   help="start from an empty dir when R2 holds no saved state (first run only)")

    p = sub.add_parser("push", help="save a data dir as a new state (full + slim) and prune")
    p.add_argument("--data", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--prefix", default=prefix_default)
    p.add_argument("--feed-manifest", default="", help="the built manifest.json, for its count")
    p.add_argument("--keep", type=int, default=KEEP)

    p = sub.add_parser("shard-state", help="write the slim shard state of a data dir (local)")
    p.add_argument("--data", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("guard", help="fail when the built feed shrank under --min-ratio of the last")
    p.add_argument("--manifest", required=True, help="the built manifest.json")
    p.add_argument("--previous-url", default="")
    p.add_argument("--state-manifest", default="", help="fallback: the restored state's manifest")
    p.add_argument("--min-ratio", type=float, default=MIN_RATIO)
    p.add_argument("--allow-drop", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "shard-state":
        export_shard_state(a.data, a.out, log=log)
        return 0
    if a.cmd == "guard":
        count = int(json.loads(Path(a.manifest).read_text())["count"])
        ok, msg = check_drop(count, previous_count(a.previous_url, a.state_manifest or None), a.min_ratio)
        if ok:
            log(f"[feed_state] guard: {msg}")
            return 0
        if a.allow_drop:
            log(f"::warning::[feed_state] guard overridden by allow_drop: {msg}")
            return 0
        log(f"::error::[feed_state] guard: {msg}")
        return 3
    if client is None:
        client, bucket = _client_and_bucket()
    if a.cmd == "push":
        feed_count = None
        if a.feed_manifest and Path(a.feed_manifest).exists():
            feed_count = int(json.loads(Path(a.feed_manifest).read_text()).get("count") or 0)
        push_state(client, bucket, a.data, a.id, prefix=a.prefix, feed_count=feed_count, keep=a.keep, log=log)
        return 0
    # pull
    state_id = a.id or paired_state_id(a.pair_dir) or None
    if a.pair_dir and not a.id:
        log(f"[feed_state] shards started from state {state_id}" if state_id
            else "[feed_state] the shards named no single base state; using the latest")
    m = fetch_state(client, bucket, a.dest, kind=a.kind, prefix=a.prefix, state_id=state_id, log=log)
    if m is None:
        if not a.allow_empty:
            log("::error::[feed_state] R2 holds no saved crawl state. Starting from an empty database "
                "would publish a fraction of the board and save it as the next base, so the run stops. "
                "Seed it (workflow input seed_from_cache) or, for a first-ever run, allow_empty_state.")
            return 4
        log("::warning::[feed_state] no saved state: starting EMPTY (allow_empty_state)")
        Path(a.dest).mkdir(parents=True, exist_ok=True)
        m = {"id": ""}
    if a.id_file:
        Path(a.id_file).parent.mkdir(parents=True, exist_ok=True)
        Path(a.id_file).write_text(str(m.get("id") or "") + "\n")
    if a.save_manifest:
        Path(a.save_manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(a.save_manifest).write_text(json.dumps(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
