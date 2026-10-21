"""The static job feed's FILE FORMAT, shared by the builder (scripts/build_feed.py) and the
desktop app's reader (sourcing/feedclient.py), so the two can never drift apart.

Three kinds of object, all produced by one crawl and uploaded to an S3-compatible bucket
(Cloudflare R2) behind the product domain. No server: every install downloads the slim list,
filters it locally with sourcing/filters.py, and fetches one small JD shard when a role is opened.

  jobs.json.gz      {"feed_version", "generated_at", "count", "attribution", "jobs": [slim rows]}
                    A slim row is a board row WITHOUT jd_text (~500 bytes instead of ~9KB), keeping
                    every field the board's facets need (sourcing/filters.py) plus the sponsor badge.
  jd/{xx}.json.gz   {source_id: jd_text} for every job whose sha1(source_id) starts with `xx`, so the
                    descriptions are 256 fixed objects (one crawl = ~257 uploads, not 50,000).
  parts/jobs-{version}-{nnn}.json.gz
                    THE SAME LIST split into ~1 MB gzipped parts (2026-10-14), so a client on a
                    flaky link that drops every 1-3 MB resumes part by part instead of restarting a
                    ~10 MB download from zero (the CDN ignores Range). `version` is a hash of the
                    rows, so the names are content-addressed: a part never changes once published
                    and may be cached forever. Each part: {"version", "part", "parts", "jobs": [...]}.
  manifest.json     tiny, uncompressed, never cached: {"generated_at", "count", "jobs_url", ...,
                    "parts": {"version", "count", "total_bytes", "attribution", "files":
                    [{"url", "sha256", "bytes", "count"}, ...]}}. jobs.json.gz is still written for
                    app versions that predate the parts.

Public job postings only. No personal data ever touches this file (CLAUDE.md §5).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .ats import ADAPTERS
from .dedup import DedupIndex

FEED_VERSION = 1
JOBS_FILE = "jobs.json.gz"
MANIFEST_FILE = "manifest.json"
JD_DIR = "jd"
JD_SHARDS = 256
PARTS_DIR = "parts"
PART_TARGET_BYTES = 1_000_000      # each list part is about this many gzipped bytes

# Everything the board + its facets read from a row (see sourcing/filters.apply_facets and the
# card renderer in ui/static/app.js). jd_text is deliberately absent: it lives in the shards.
SLIM_KEYS = ("source_id", "source", "company", "title", "location", "remote", "url", "posted_at",
             "first_seen", "salary", "sponsorship_stated", "us", "entry_level", "visa",
             "nationality_visas", "sponsor", "ad_stance", "ad_sentence", "tags")

# Rows from a company's OWN board (Greenhouse, Lever, ...). When the same opening also arrives
# through an aggregator, the board row wins: canonical URL, full JD, the employer's own wording.
BOARD_SOURCES = frozenset(ADAPTERS)

# Credits the public file must carry for the feeds that let us share their jobs further. The
# conditions are each feed's own published API notice (quoted in docs/feed.md).
ATTRIBUTION = {
    "remotive": {"name": "Remotive", "url": "https://remotive.com",
                 "note": "Link back to the job's Remotive URL and name Remotive as the source."},
    "remoteok": {"name": "Remote OK", "url": "https://remoteok.com",
                 "note": "Link back (follow) to the Remote OK URL and name Remote OK as the source."},
    "jsearch": {"name": "JSearch by OpenWeb Ninja", "url": "https://www.openwebninja.com",
                "note": "Licensed API data, redistributed as part of this product."},
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shard_for(source_id: str) -> str:
    """The two-hex-char shard a job's description lives in: sha1(source_id)[:2]."""
    return hashlib.sha1(str(source_id or "").encode("utf-8")).hexdigest()[:2]


def all_shards() -> list[str]:
    return [f"{i:02x}" for i in range(JD_SHARDS)]


def slim_row(job: dict) -> dict:
    """A board row minus its JD body and its per-install flags (is_new / dismissed / saved /
    last_seen are personal or crawl-internal and never belong in a public file)."""
    return {k: job[k] for k in SLIM_KEYS if k in job}


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """One row per opening across feeds, preferring the company's own board over an aggregator
    copy (sourcing/dedup.py keeps the FIRST sighting, so board rows are indexed first). The
    survivors come back in the input order, so the feed's recency sort is preserved."""
    index = DedupIndex()
    board = [r for r in rows if r.get("source") in BOARD_SOURCES]
    rest = [r for r in rows if r.get("source") not in BOARD_SOURCES]
    kept_board, _ = index.filter(board)
    kept_rest, _ = index.filter(rest)
    keep_ids = {id(r) for r in kept_board} | {id(r) for r in kept_rest}
    return [r for r in rows if id(r) in keep_ids]


def dumps_gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                         mtime=0)


def loads_maybe_gz(body: bytes):
    """Parse a feed object whether it arrived gzipped (the stored bytes) or already decoded (a CDN
    or client that honoured Content-Encoding and inflated it for us)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def rows_version(rows: list[dict]) -> str:
    """A content hash of the slim rows: the list's version, unchanged while the rows are."""
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def part_name(version: str, index: int) -> str:
    return f"{PARTS_DIR}/jobs-{version}-{index:03d}.json.gz"


def write_parts(out_dir, slim: list[dict], whole_gz_bytes: int, *,
                target_bytes: int = PART_TARGET_BYTES) -> dict:
    """Split the slim list into ~`target_bytes` gzipped parts under `out_dir/parts/`. Returns the
    manifest's "parts" block. Rows are split into contiguous runs of near-equal count (rows are
    similar in size, so the parts are near-equal in bytes too); joining the parts in order gives
    the list back exactly. Old versions' parts are removed from `out_dir` (the CDN host keeps or
    prunes its own copies; see scripts/upload_feed.py)."""
    out = Path(out_dir)
    pdir = out / PARTS_DIR
    pdir.mkdir(parents=True, exist_ok=True)
    version = rows_version(slim)
    n = max(1, -(-int(whole_gz_bytes) // max(1, int(target_bytes))))
    n = min(n, max(1, len(slim)))
    size, extra = divmod(len(slim), n)
    files, start = [], 0
    for i in range(n):
        end = start + size + (1 if i < extra else 0)
        chunk = slim[start:end]
        start = end
        raw = json.dumps({"version": version, "part": i, "parts": n, "jobs": chunk},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        body = gzip.compress(raw, mtime=0)
        name = part_name(version, i)
        (out / name).write_bytes(body)
        # sha256 of the stored bytes; raw_sha256 of the JSON inside, so a part a CDN re-encodes
        # (or inflates) on the way still verifies.
        files.append({"url": name, "sha256": hashlib.sha256(body).hexdigest(),
                      "raw_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(body),
                      "count": len(chunk)})
    keep = {Path(f["url"]).name for f in files}
    for p in pdir.glob("jobs-*.json.gz"):
        if p.name not in keep:
            p.unlink()
    return {"version": version, "count": len(slim), "total_bytes": sum(f["bytes"] for f in files),
            "files": files}


# Reading every ad (its sponsorship stance and function tags) is the slow part of a feed build:
# ~150k descriptions took ~17 minutes in one process (2026-10-21). Rows are independent, so a
# large build reads them in worker processes forked from this one (they inherit the rows, no
# copying); the answers come back in row order, so the file is the same as a one-process build.
# Linux only (the CI runner): fork is the safe default there. FEED_READ_WORKERS=1 turns it off.
PARALLEL_MIN_ROWS = 2000
_ADS: tuple = ((), {})      # (rows, jd_by_id) for the forked workers to read


def _read_one(r: dict, jd_by_id: dict):
    from sourcing.adstance import UNKNOWN, ad_stance
    from sourcing.jobtags import job_tags
    jd = (jd_by_id or {}).get(r.get("source_id")) or r.get("jd_text") or ""
    st = ad_stance(jd) if jd else {"stance": UNKNOWN, "sentence": ""}
    return st, job_tags(r.get("title") or "", jd)


def _read_span(span: tuple[int, int]) -> list:
    rows, jd_by_id = _ADS
    return [_read_one(r, jd_by_id) for r in rows[span[0]:span[1]]]


def read_workers(n_rows: int, env=None, threads: int | None = None) -> int:
    """How many processes read the ads: FEED_READ_WORKERS when set; else 1 for a small build,
    off Linux, or when another thread is running (forking a threaded process can deadlock the
    child on a lock that thread held); else the CPU count, capped at 8. The CI merge job runs
    no other thread by the time it writes."""
    import os
    import sys
    import threading
    env = os.environ if env is None else env
    raw = (env.get("FEED_READ_WORKERS") or "").strip()
    if raw:
        return max(1, int(raw))
    threads = threading.active_count() if threads is None else threads
    if n_rows < PARALLEL_MIN_ROWS or not sys.platform.startswith("linux") or threads > 1:
        return 1
    return max(1, min(8, os.cpu_count() or 1))


def _read_ads(rows: list[dict], jd_by_id: dict) -> list:
    """[(stance dict, tags)] for each row, in row order."""
    global _ADS
    workers = read_workers(len(rows))
    if workers <= 1:
        return [_read_one(r, jd_by_id) for r in rows]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    step = max(1, -(-len(rows) // (workers * 8)))    # small spans keep the workers evenly busy
    spans = [(i, min(i + step, len(rows))) for i in range(0, len(rows), step)]
    _ADS = (rows, jd_by_id)
    try:
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("fork")) as pool:
            return [x for part in pool.map(_read_span, spans) for x in part]
    finally:
        _ADS = ((), {})


def write_feed(out_dir, rows: list[dict], jd_by_id: dict, *, jobs_url: str = "",
               generated_at: str | None = None, part_bytes: int | None = None) -> dict:
    """Write jobs.json.gz + jd/*.json.gz + manifest.json into `out_dir`. Returns the manifest.
    All 256 shards are written (empty ones too), so a shard fetch is never a 404."""
    out = Path(out_dir)
    (out / JD_DIR).mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or utc_now_iso()
    # What each ad SAYS about sponsorship, read from its own text (sourcing/adstance.py): the
    # aggregator's yes/no flag was wrong for most flagged ads, and a third of ads that mention
    # sponsorship say they will NOT sponsor, which the student needs to see (2026-10-08).
    from sourcing.adstance import UNKNOWN
    slim = []
    for r, (st, tags) in zip(rows, _read_ads(rows, jd_by_id)):
        row = slim_row(r)
        if st["stance"] != UNKNOWN:
            row["ad_stance"], row["ad_sentence"] = st["stance"], st["sentence"]
        # Function tags from title + description (sourcing/jobtags.py), so the card can show
        # several specific areas the way Migrate Mate does, instead of one title-only bucket.
        row["tags"] = tags
        slim.append(row)
    sources = {r.get("source") for r in slim}
    header = {"feed_version": FEED_VERSION, "generated_at": generated_at, "count": len(slim),
              "attribution": {k: v for k, v in ATTRIBUTION.items() if k in sources},
              "jobs": slim}
    whole = dumps_gz(header)
    (out / JOBS_FILE).write_bytes(whole)
    parts = write_parts(out, slim, len(whole), target_bytes=part_bytes or PART_TARGET_BYTES)
    parts["attribution"] = header["attribution"]
    del whole
    shards: dict[str, dict] = {s: {} for s in all_shards()}
    for r in rows:
        sid = r.get("source_id")
        jd = (jd_by_id.get(sid) or "").strip() if sid else ""
        if jd:
            shards[shard_for(sid)][sid] = jd
    for s, m in shards.items():
        (out / JD_DIR / f"{s}.json.gz").write_bytes(dumps_gz(m))
    manifest = {"feed_version": FEED_VERSION, "generated_at": generated_at, "count": len(slim),
                "jobs_url": jobs_url or JOBS_FILE, "jd_shard_count": JD_SHARDS,
                "jd_url_template": f"{JD_DIR}/{{shard}}.json.gz", "parts": parts}
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest
