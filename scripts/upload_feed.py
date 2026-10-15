#!/usr/bin/env python3
"""Upload a built static feed (scripts/build_feed.py output) to Cloudflare R2 over its
S3-compatible API.

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=... \\
        python scripts/upload_feed.py --dir .feed-out [--prefix feed]

Order matters: JD shards first, then the list parts, then jobs.json.gz, then manifest.json last,
so a client that reads the manifest never points at data that isn't there yet. Headers per object:
  jobs.json.gz   Content-Type application/json, Content-Encoding gzip, Cache-Control max-age=600
  jd/*.json.gz   same, Cache-Control max-age=86400
  parts/jobs-<version>-<nnn>.json.gz
                 same, Cache-Control max-age=31536000, immutable: the name carries the content hash,
                 so a part never changes. After the manifest is up, parts named by neither the new
                 manifest nor the one it replaced are deleted (the previous version is kept so a
                 client mid-download when the manifest flips can still finish).
  manifest.json  Content-Type application/json, Cache-Control no-cache
  sponsors/*.json.gz      the extension's sponsor index (scripts/build_sponsor_index.py), when
                          built: like the JD shards; sponsors/manifest.json after them, no-cache
boto3 is imported lazily (it is a CI-only dependency; the desktop app never needs it), and the
client is injectable so the upload plan is unit-tested with a fake.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from sourcing.feedfile import JD_DIR, JOBS_FILE, MANIFEST_FILE, PARTS_DIR  # noqa: E402

SPONSOR_DIR = "sponsors"            # scripts/build_sponsor_index.py writes <out>/sponsors/

CACHE_LIST = "public, max-age=600"
CACHE_SHARD = "public, max-age=86400"
CACHE_MANIFEST = "no-cache"
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"


def object_headers(rel_path: str) -> dict:
    """The S3 metadata for one published path (keys as put_object expects them)."""
    rel = rel_path.replace(os.sep, "/")
    if rel == MANIFEST_FILE:
        return {"ContentType": "application/json", "CacheControl": CACHE_MANIFEST}
    if rel == JOBS_FILE:
        return {"ContentType": "application/json", "ContentEncoding": "gzip", "CacheControl": CACHE_LIST}
    if rel.startswith(PARTS_DIR + "/") and rel.endswith(".json.gz"):
        return {"ContentType": "application/json", "ContentEncoding": "gzip",
                "CacheControl": CACHE_IMMUTABLE}
    if rel.startswith(JD_DIR + "/") and rel.endswith(".json.gz"):
        return {"ContentType": "application/json", "ContentEncoding": "gzip", "CacheControl": CACHE_SHARD}
    if rel == f"{SPONSOR_DIR}/manifest.json":
        return {"ContentType": "application/json", "CacheControl": CACHE_MANIFEST}
    if rel.startswith(SPONSOR_DIR + "/") and rel.endswith(".json.gz"):
        return {"ContentType": "application/json", "ContentEncoding": "gzip", "CacheControl": CACHE_SHARD}
    raise ValueError(f"not a feed file: {rel_path}")


def plan(out_dir) -> list[tuple[Path, str]]:
    """(local_path, relative key) in upload order: shards, the sponsor index (its shards, then its
    manifest) when it was built, the list parts, list, manifest."""
    out = Path(out_dir)
    for name in (JOBS_FILE, MANIFEST_FILE):
        if not (out / name).exists():
            raise FileNotFoundError(f"{out / name} missing: run scripts/build_feed.py first")
    shards = sorted((out / JD_DIR).glob("*.json.gz"))
    sponsors = []
    if (out / SPONSOR_DIR / "manifest.json").exists():
        sponsors = ([(p, f"{SPONSOR_DIR}/{p.name}") for p in sorted((out / SPONSOR_DIR).glob("*.json.gz"))]
                    + [(out / SPONSOR_DIR / "manifest.json", f"{SPONSOR_DIR}/manifest.json")])
    parts = [(out / rel, rel) for rel in _manifest_parts((out / MANIFEST_FILE).read_bytes())]
    for p, rel in parts:
        if not p.exists():
            raise FileNotFoundError(f"{p} missing: the manifest lists it")
    return ([(p, f"{JD_DIR}/{p.name}") for p in shards] + sponsors + parts
            + [(out / JOBS_FILE, JOBS_FILE), (out / MANIFEST_FILE, MANIFEST_FILE)])


def _manifest_parts(body: bytes) -> list[str]:
    """The list-part paths a manifest names ([] for a manifest from before the parts)."""
    try:
        man = json.loads(body.decode("utf-8"))
        files = (man.get("parts") or {}).get("files") or []
        return [str(f["url"]) for f in files if str(f.get("url", "")).startswith(PARTS_DIR + "/")]
    except Exception:                                       # noqa: BLE001 - unreadable = none
        return []


def _previous_parts(client, bucket: str, key: str) -> list[str]:
    """The parts the manifest being replaced names, so they outlive this upload by one version."""
    try:
        return _manifest_parts(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:                                       # noqa: BLE001 - none yet, or no access
        return []


def prune_parts(client, bucket: str, prefix: str, keep: set[str], log=print) -> list[str]:
    """Delete list parts under `prefix/parts/` that `keep` (relative paths) does not name."""
    base = f"{prefix}/" if prefix else ""
    stale, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": f"{base}{PARTS_DIR}/"}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for o in page.get("Contents") or []:
            if o["Key"][len(base):] not in keep:
                stale.append(o["Key"])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    for i in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]],
                                                     "Quiet": True})
    if stale:
        log(f"[upload_feed] removed {len(stale)} list parts of older versions")
    return stale


def upload(out_dir, client, bucket: str, prefix: str = "", log=print) -> list[str]:
    """Put every feed object with its headers. Returns the object keys in upload order."""
    prefix = prefix.strip("/")
    keys = []
    steps = plan(out_dir)
    man_key = f"{prefix}/{MANIFEST_FILE}" if prefix else MANIFEST_FILE
    keep = set(_previous_parts(client, bucket, man_key))
    for path, rel in steps:
        key = f"{prefix}/{rel}" if prefix else rel
        client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), **object_headers(rel))
        keys.append(key)
        if rel.startswith(PARTS_DIR + "/"):
            keep.add(rel)
    log(f"[upload_feed] uploaded {len(keys)} objects to {bucket}/{prefix or ''}".rstrip("/"))
    try:
        prune_parts(client, bucket, prefix, keep, log=log)
    except Exception as exc:                                # noqa: BLE001 - tidy-up only
        log(f"[upload_feed] could not prune old list parts: {exc}")
    return keys


def make_client(env=None):
    """An S3 client pointed at R2. boto3 is imported here, lazily, with a clear message if absent."""
    env = os.environ if env is None else env
    missing = [k for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"upload_feed: missing env {', '.join(missing)} (see docs/feed.md)")
    try:
        import boto3
    except ImportError as exc:                              # pragma: no cover - env-dependent
        raise SystemExit("upload_feed: boto3 is not installed; `pip install boto3` "
                         "(CI-only dependency, the app itself never needs it)") from exc
    return boto3.client(
        "s3", endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"], aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.environ.get("FEED_OUT_DIR", ".feed-out"))
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET", ""))
    ap.add_argument("--prefix", default=os.environ.get("R2_PREFIX", ""),
                    help="key prefix inside the bucket (e.g. 'feed' when the domain serves /feed/)")
    a = ap.parse_args(argv)
    if not a.bucket:
        raise SystemExit("upload_feed: set R2_BUCKET or pass --bucket")
    upload(a.dir, make_client(), a.bucket, a.prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
