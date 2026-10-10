#!/usr/bin/env python3
"""Publish the visa-sponsor data as a small static index the browser extension reads on its own.

    python scripts/build_sponsor_index.py --out .site/feed            # build from the bundles
    python scripts/build_sponsor_index.py --out DIR --db sponsors.db  # or from an existing DB

Writes, under ``<out>/sponsors/``:
  {a..z,0..9,_}.json.gz   one shard per first character of the NORMALIZED employer name
  manifest.json           {index_version, version, generated_at, employers, stats, shards, total_bytes}

Why: the extension used to ask the local app for every company it saw, so without the app it
showed only "Start SponsorJobs for visa badges". With this index it looks companies up inside
the browser (extension/sponsor_core.js ports SponsorDB.lookup exactly), so no company name ever
leaves the browser and nothing needs to be installed. Shards keep the first lookup cheap: a
lookup needs only the shard(s) of its own first letter.

The database is built exactly the way a FRESH app install builds it (ui/app.py `_sponsors`):
the bundled PERM counts, the bundled E-Verify list, then the H-1B columns of the committed seed,
through the same SponsorDB ingest methods. So the extension's answer matches the app's.

Shard file: ``{"index_version": 1, "version": V, "shard": "a", "records": {norm: rec}}`` where
``rec`` is ``[display_name, h1b_approvals, h1b_last_fy, h1b_first_fy, perm_certs, e_verify,
cap_exempt, naics2, state]``. To keep the download small: ``display_name`` is ``0`` when it is
just the key title-cased (see ``title_key``), trailing zero / empty fields are omitted, and
``naics2`` is only the two-digit NAICS sector (all the profile's "industry" line uses).
Employers with no signal at all (no H-1B, PERM, E-Verify or cap-exempt flag) are left out:
they can never produce a badge.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gzip
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing.sponsors import SponsorDB, read_seed_rows  # noqa: E402

INDEX_VERSION = 1
SUBDIR = "sponsors"
MANIFEST = "manifest.json"
SEED = ROOT / "seed" / "sponsors_seed.csv.gz"
PERM = ROOT / "config" / "perm_sponsors.csv"
EVERIFY = ROOT / "config" / "everify_employers.csv.gz"
SHARDS = [chr(c) for c in range(ord("a"), ord("z") + 1)] + [str(d) for d in range(10)] + ["_"]


def shard_of(norm: str) -> str:
    """The shard a normalized name lives in: its first character, or "_" for anything else."""
    c = (norm or "_")[0]
    return c if ("a" <= c <= "z" or "0" <= c <= "9") else "_"


def title_key(norm: str) -> str:
    """Title-case a normalized key the way extension/sponsor_core.js `titleKey` does: a letter
    is upper-cased when the character before it is not a letter. (Python's str.title() does
    the same on [a-z0-9 ], but the JS port is the contract, so this is spelled out.)"""
    out, prev_alpha = [], False
    for ch in norm:
        alpha = "a" <= ch.lower() <= "z"
        out.append(ch.upper() if alpha and not prev_alpha else ch.lower() if alpha else ch)
        prev_alpha = alpha
    return "".join(out)


def build_db(db_path, seed=SEED, perm=PERM, everify=EVERIFY) -> SponsorDB:
    """A sponsor DB built the way a fresh app install builds it (ui/app.py `_sponsors`:
    `_load_bundled_perm`, `_load_bundled_everify`, then `_load_bundled_h1b`)."""
    db = SponsorDB(db_path)
    if not db.is_empty():
        return db
    if perm and Path(perm).exists():
        with open(perm, encoding="utf-8-sig") as f:
            db.ingest_perm_counts(csv.DictReader(f))
    if everify and Path(everify).exists():
        opener = gzip.open if str(everify).endswith(".gz") else open
        with opener(everify, "rt", encoding="utf-8-sig") as f:
            db.ingest_everify_rows(csv.DictReader(f))
    if seed and Path(seed).exists():
        db.merge_aggregated_rows(read_seed_rows(seed), h1b_only=True)
    return db


def encode_record(norm: str, row) -> list:
    naics = re.sub(r"\D", "", str(row["naics"] or ""))
    display = row["display_name"]
    rec = [0 if display is not None and display == title_key(norm) else display,
           int(row["h1b_approvals"] or 0), int(row["h1b_last_fy"] or 0),
           int(row["h1b_first_fy"] or 0), int(row["perm_certs"] or 0),
           1 if row["e_verify"] else 0, 1 if row["cap_exempt"] else 0,
           naics[:2] if len(naics) >= 2 else "", str(row["state"] or "")]
    while len(rec) > 1 and rec[-1] in (0, ""):
        rec.pop()
    return rec


def collect(db: SponsorDB) -> dict[str, dict]:
    """{shard: {norm: rec}} for every employer with a signal."""
    out: dict[str, dict] = {s: {} for s in SHARDS}
    rows = db._conn.execute(
        "SELECT * FROM sponsor_employer WHERE h1b_approvals>0 OR perm_certs>0 OR e_verify>0 "
        "OR cap_exempt>0 ORDER BY norm_name").fetchall()
    for r in rows:
        norm = r["norm_name"]
        if norm:
            out[shard_of(norm)][norm] = encode_record(norm, r)
    return out


def write_index(db: SponsorDB, out_dir, generated_at: str | None = None) -> dict:
    """Write the shards and the manifest under ``<out_dir>/sponsors``. Deterministic for the
    same data (sorted keys, gzip mtime 0), and ``version`` is a hash of the content, so the
    extension refetches only when the data really changed. Returns the manifest."""
    dest = Path(out_dir) / SUBDIR
    dest.mkdir(parents=True, exist_ok=True)
    shards = collect(db)
    blobs: dict[str, bytes] = {}
    h = hashlib.sha256()
    for s in SHARDS:
        body = json.dumps(shards[s], separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        h.update(s.encode() + b"\0" + body.encode("utf-8") + b"\0")
        blobs[s] = body
    version = h.hexdigest()[:16]
    meta: dict[str, dict] = {}
    total = 0
    for s in SHARDS:
        doc = ('{"index_version":%d,"version":"%s","shard":"%s","records":%s}'
               % (INDEX_VERSION, version, s, blobs[s]))
        gz = gzip.compress(doc.encode("utf-8"), compresslevel=9, mtime=0)
        (dest / f"{s}.json.gz").write_bytes(gz)
        meta[s] = {"file": f"{s}.json.gz", "bytes": len(gz), "records": len(shards[s])}
        total += len(gz)
    manifest = {
        "index_version": INDEX_VERSION,
        "version": version,
        "generated_at": generated_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "employers": sum(m["records"] for m in meta.values()),
        "stats": db.stats(),
        "shards": meta,
        "total_bytes": total,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="the feed output dir; writes <out>/sponsors/")
    ap.add_argument("--db", help="an existing sponsors.db to publish (default: build a fresh one)")
    args = ap.parse_args(argv)
    if args.db:
        db = SponsorDB(args.db)
        manifest = write_index(db, args.out)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            db = build_db(Path(tmp) / "sponsors.db")
            try:
                manifest = write_index(db, args.out)
            finally:
                db.close()
    big = max(manifest["shards"].items(), key=lambda kv: kv[1]["bytes"])
    print(f"[sponsor_index] {manifest['employers']:,} employers, version {manifest['version']}, "
          f"{manifest['total_bytes'] / 1e6:.2f} MB gzipped in {len(manifest['shards'])} shards "
          f"(largest '{big[0]}' {big[1]['bytes'] / 1e6:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
