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
  manifest.json     tiny, uncompressed, never cached: {"generated_at", "count", "jobs_url", ...}.

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
    "freehire": {"name": "freehire.me", "url": "https://freehire.me",
                 "note": "Free, open-source job API."},
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


def write_feed(out_dir, rows: list[dict], jd_by_id: dict, *, jobs_url: str = "",
               generated_at: str | None = None) -> dict:
    """Write jobs.json.gz + jd/*.json.gz + manifest.json into `out_dir`. Returns the manifest.
    All 256 shards are written (empty ones too), so a shard fetch is never a 404."""
    out = Path(out_dir)
    (out / JD_DIR).mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or utc_now_iso()
    # What each ad SAYS about sponsorship, read from its own text (sourcing/adstance.py): the
    # aggregator's yes/no flag was wrong for most flagged ads, and a third of ads that mention
    # sponsorship say they will NOT sponsor, which the student needs to see (2026-10-08).
    from sourcing.adstance import UNKNOWN, ad_stance
    from sourcing.jobtags import job_tags
    slim = []
    for r in rows:
        row = slim_row(r)
        jd = (jd_by_id or {}).get(r.get("source_id")) or r.get("jd_text") or ""
        st = ad_stance(jd) if jd else {"stance": UNKNOWN, "sentence": ""}
        if st["stance"] != UNKNOWN:
            row["ad_stance"], row["ad_sentence"] = st["stance"], st["sentence"]
        # Function tags from title + description (sourcing/jobtags.py), so the card can show
        # several specific areas the way Migrate Mate does, instead of one title-only bucket.
        row["tags"] = job_tags(r.get("title") or "", jd)
        slim.append(row)
    sources = {r.get("source") for r in slim}
    header = {"feed_version": FEED_VERSION, "generated_at": generated_at, "count": len(slim),
              "attribution": {k: v for k, v in ATTRIBUTION.items() if k in sources},
              "jobs": slim}
    (out / JOBS_FILE).write_bytes(dumps_gz(header))
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
                "jd_url_template": f"{JD_DIR}/{{shard}}.json.gz"}
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest
