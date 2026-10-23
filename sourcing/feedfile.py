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
                    [{"url", "sha256", "bytes", "count"}, ...]}, "stable": {...}}. jobs.json.gz is
                    still written for app versions that predate the parts.
  parts/s-{sha}.json.gz, parts/d-{sha}.json.gz
                    THE SAME LIST again in the "stable" layout (2026-10-23, see write_stable): parts
                    named by their own content and grouped by the week a row was first seen, so a
                    rebuild re-sends only the parts that changed, plus a small file of removed rows.

Public job postings only. No personal data ever touches this file (CLAUDE.md §5).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
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


# -- the compact in-memory list (2026-10-21) ------------------------------------------------ #
# The desktop app keeps the whole slim list (~150k rows) in memory for filtering. Parsed naively
# it cost ~670 MB of Python objects, with a ~1.8 GB peak while loading (the gzip bytes, the
# inflated bytes and the decoded text all alive at once), on laptops with 8 GB of RAM. Most of it
# was repetition: every row carries its employer's sponsor record and the same two nationality
# visa lists as fresh copies (~200 MB of JSON for ~3,300 distinct values), and every row had its
# own copy of each key name and of its company, location and dates. The loader below streams the
# file row by row and shares one object per distinct value, so the list costs a fraction of that
# and never needs the whole text in memory. The rows are ordinary dicts with the same keys and
# values as before; nested values are shared between rows, so they are read-only (nothing
# mutates a cached row in place: the board copies a page before marking it).

# String fields whose values repeat across rows (shared, one object per distinct value).
_SHARED_STR_KEYS = frozenset(("source", "company", "location", "remote", "posted_at", "first_seen",
                              "salary", "ad_stance", "ad_sentence"))
_CHUNK = 1 << 20


class RowCompactor:
    """Rebuilds rows so equal key names, repeated strings and equal nested values (visa lists,
    the sponsor record, tags) are ONE shared object. Keep one per load; drop it afterwards."""

    def __init__(self):
        self._strs: dict = {}
        self._nested: dict = {}

    def row(self, r: dict) -> dict:
        strs, nested, out = self._strs, self._nested, {}
        for k, v in r.items():
            k = strs.setdefault(k, k)
            if isinstance(v, str):
                if k in _SHARED_STR_KEYS:
                    v = strs.setdefault(v, v)
            elif isinstance(v, (list, dict)):
                key = (json.dumps(v, ensure_ascii=False, separators=(",", ":")) if v
                       else ("[]" if isinstance(v, list) else "{}"))
                v = nested.setdefault(key, v)
            out[k] = v
        return out

    def rows(self, rows) -> list[dict]:
        return [self.row(r) if isinstance(r, dict) else r for r in rows]


def compact_rows(rows) -> list[dict]:
    """``rows`` with repeated values shared (see RowCompactor). Same keys, same values, same order."""
    return RowCompactor().rows(rows)


def _ws(buf: str, i: int) -> int:
    n = len(buf)
    while i < n and buf[i] in " \t\r\n":
        i += 1
    return i


def load_feed_stream(fh) -> dict:
    """Parse a feed object ({"...": ..., "jobs": [rows]}) from a TEXT stream without holding the
    whole document in memory: the rows are decoded one at a time and compacted as they arrive
    (RowCompactor), so the peak is the finished list plus a ~1 MB window. Any other key is parsed
    as one value. Raises ValueError on a malformed document."""
    dec = json.JSONDecoder()
    buf, pos, eof = "", 0, False
    compact = RowCompactor()

    def more() -> bool:
        nonlocal buf, pos, eof
        if eof:
            return False
        chunk = fh.read(_CHUNK)
        if not chunk:
            eof = True
            return False
        buf = buf[pos:] + chunk
        pos = 0
        return True

    def skip() -> None:
        nonlocal pos
        while True:
            pos = _ws(buf, pos)
            if pos < len(buf) or not more():
                return

    def value():
        # A value that ends exactly at the end of the window may be cut short ("12" of "123"), so
        # it is only accepted once more text follows it or the stream has ended.
        nonlocal pos
        while True:
            try:
                v, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if more():
                    continue
                raise ValueError("feed file is truncated or malformed")
            if end >= len(buf) and more():
                continue
            pos = end
            return v

    def expect(ch: str) -> None:
        nonlocal pos
        skip()
        if pos >= len(buf) or buf[pos] != ch:
            raise ValueError(f"feed file: expected {ch!r}")
        pos += 1

    out: dict = {}
    expect("{")
    skip()
    if pos < len(buf) and buf[pos] == "}":
        return out
    while True:
        skip()
        key = value()
        if not isinstance(key, str):
            raise ValueError("feed file: object key is not a string")
        expect(":")
        skip()
        if key == "jobs" and pos < len(buf) and buf[pos] == "[":
            pos += 1
            rows: list = []
            skip()
            if pos < len(buf) and buf[pos] == "]":
                pos += 1
            else:
                while True:
                    skip()
                    r = value()
                    rows.append(compact.row(r) if isinstance(r, dict) else r)
                    skip()
                    if pos < len(buf) and buf[pos] == ",":
                        pos += 1
                        continue
                    expect("]")
                    break
            out[key] = rows
        else:
            out[key] = value()
        skip()
        if pos < len(buf) and buf[pos] == ",":
            pos += 1
            continue
        expect("}")
        return out


def load_feed_file(path) -> dict:
    """The cached jobs.json.gz (or an uncompressed copy) parsed with load_feed_stream."""
    with open(path, "rb") as raw:
        gz = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if gz else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return load_feed_stream(fh)


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


# -- stable parts (2026-10-23) --------------------------------------------------------------- #
# The parts above are named by a hash of the WHOLE list and cut by recency, so every 3-hourly
# rebuild renamed every part and an open app re-downloaded all ~15 MB each time. The "stable"
# layout fixes that, for app versions that read the manifest's "stable" block:
#
#   * Rows are laid out by the week they were first seen (oldest first, sorted by first_seen then
#     source_id inside), cut into ~1 MB parts. A row's first_seen never moves, so new roles land in
#     new parts at the end and old weeks age out together.
#   * A part is named by the hash of ITS OWN bytes (parts/s-<sha>.json.gz), so a part whose rows
#     did not change keeps its name and the client already has it.
#   * The builder starts from the previous build's parts. A row that left the list, or changed,
#     is not rewritten into its old part: the part is kept as it is and the row is listed in a
#     small "removed" file (parts/d-<sha>.json.gz, {"removed": {part url: [row indexes]}}). A
#     changed row is written again, with the new rows, into a new part. Without this, the ~1% of
#     roles that close between two builds are spread over every week and would touch every part.
#   * A kept part is rewritten once more than STABLE_MAX_DEAD of its rows are dead, or when it is
#     under STABLE_MIN_FILL of the target size (the newest, still-filling part), so the dead
#     weight and the number of parts stay bounded.
#
# The client keeps the parts it has (by content hash), fetches only the parts it lacks plus the
# removed file, joins them minus the removed rows, and sorts them by recency (recency_key), the
# order jobs.json.gz has. The "parts" block and jobs.json.gz are still written for older apps.
STABLE_FORMAT = 1
STABLE_PREFIX = "s-"
REMOVED_PREFIX = "d-"
STABLE_MAX_DEAD = 0.2          # rewrite a kept part once this share of its rows is dead
STABLE_MIN_FILL = 0.5          # rewrite a kept part smaller than this share of the target size
STABLE_STATE_DIR = "feed-prev"  # under the build's data dir: the last build's stable parts


def canon(row: dict) -> str:
    """A row as canonical JSON (sorted keys): how stable parts store it and how rows are compared."""
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def recency_key(row: dict) -> tuple:
    """The board's order, newest first with reverse=True: dated rows by posting date, then first
    seen; undated rows last (scripts/build_feed.py's order="recent"). Ties break on source_id, so
    the joined list has one order whatever order its parts came in."""
    posted = row.get("posted_at") or ""
    return (bool(posted), str(posted), str(row.get("first_seen") or ""),
            str(row.get("source_id") or ""))


def week_of(first_seen) -> str:
    """The ISO week ("2026-W43") a row was first seen in, or "" when it has no readable date."""
    s = str(first_seen or "")[:10]
    try:
        y, w, _ = datetime.strptime(s, "%Y-%m-%d").isocalendar()
    except ValueError:
        return ""
    return f"{y}-W{w:02d}"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _stable_file(out: Path, prefix: str, raw: bytes, **extra) -> dict:
    body = gzip.compress(raw, mtime=0)
    sha = _sha(body)
    name = f"{PARTS_DIR}/{prefix}{sha[:20]}.json.gz"
    path = out / name
    if not path.exists():
        path.write_bytes(body)
    return {"url": name, "sha256": sha, "raw_sha256": _sha(raw), "bytes": len(body), **extra}


def stable_block_ok(block) -> bool:
    """Is `block` a usable manifest "stable" block (as written by write_stable)?"""
    def ok(f):
        return (isinstance(f, dict) and isinstance(f.get("url"), str) and f["url"]
                and f.get("sha256"))
    return (isinstance(block, dict) and block.get("format") == STABLE_FORMAT
            and bool(block.get("version")) and isinstance(block.get("files"), list)
            and bool(block["files"]) and all(ok(f) for f in block["files"])
            and (block.get("removed") is None or ok(block["removed"])))


def read_removed(body: bytes) -> dict[str, set[int]]:
    """{part url: {row indexes}} from a removed file's bytes."""
    data = loads_maybe_gz(body)
    rem = data.get("removed") if isinstance(data, dict) else None
    if not isinstance(rem, dict):
        raise ValueError("removed file has an unexpected shape")
    return {str(k): {int(i) for i in v} for k, v in rem.items()}


def load_stable_state(state_dir) -> tuple | None:
    """The previous build's stable layout from `state_dir` (manifest.json + parts/): (block,
    {part url: [canonical rows]}, {part url: {removed indexes}}, state_dir), or None when it is missing,
    from another format, or any file fails its sha256 (the builder then lays out afresh)."""
    d = Path(state_dir)
    try:
        man = json.loads((d / MANIFEST_FILE).read_text(encoding="utf-8"))
        block = man.get("stable")
        if not stable_block_ok(block):
            return None
        parts: dict[str, list[str]] = {}
        for f in block["files"]:
            body = (d / f["url"]).read_bytes()
            if _sha(body) != f["sha256"]:
                return None
            data = loads_maybe_gz(body)
            parts[f["url"]] = [canon(r) for r in data["jobs"]]
        removed: dict[str, set[int]] = {}
        if block.get("removed"):
            body = (d / block["removed"]["url"]).read_bytes()
            if _sha(body) != block["removed"]["sha256"]:
                return None
            removed = read_removed(body)
        return block, parts, removed, d
    except Exception:                                     # noqa: BLE001 - no usable previous state
        return None


def _chunks(rows: list[tuple[str, str]], target_raw: int) -> list[list[tuple[str, str]]]:
    """Cut (week, canonical row) pairs, already in layout order, into runs of ~target_raw bytes.
    A run also ends at a week boundary once it is at least half full, so old weeks age out
    together instead of sharing a part with the next week."""
    out, cur, size = [], [], 0
    for w, c in rows:
        if cur and (size >= target_raw or (size >= target_raw // 2 and w != cur[-1][0])):
            out.append(cur)
            cur, size = [], 0
        cur.append((w, c))
        size += len(c.encode("utf-8")) + 1
    if cur:
        out.append(cur)
    return out


def write_stable(out_dir, slim: list[dict], *, prev=None, target_bytes: int = PART_TARGET_BYTES,
                 max_dead: float = STABLE_MAX_DEAD, min_fill: float = STABLE_MIN_FILL) -> dict:
    """Write the stable layout of `slim` under `out_dir/parts/` and return the manifest's "stable"
    block. `prev` is load_stable_state() of the previous build (None: lay everything out afresh).
    Kept parts are copied from the previous build's bytes, so the output dir holds every file the
    block names (GitHub Pages replaces the whole site on each deploy)."""
    from collections import Counter
    out = Path(out_dir)
    (out / PARTS_DIR).mkdir(parents=True, exist_ok=True)
    canon_rows = [canon(r) for r in slim]
    need = Counter(canon_rows)
    files: list[dict] = []
    removed: dict[str, list[int]] = {}
    if prev is not None:
        block, prev_parts, prev_removed, prev_dir = prev
        for f in block["files"]:
            rows = prev_parts.get(f["url"]) or []
            dead_before = prev_removed.get(f["url"], set())
            live, dead = [], []
            for i, c in enumerate(rows):
                if i not in dead_before and need[c] > 0:
                    need[c] -= 1
                    live.append(c)
                else:
                    dead.append(i)
            keep = (rows and len(dead) <= max_dead * len(rows)
                    and int(f.get("bytes") or 0) >= min_fill * target_bytes)
            if not keep:
                for c in live:                            # these rows are laid out again below
                    need[c] += 1
                continue
            dest = out / f["url"]
            if not dest.exists():
                shutil.copyfile(Path(prev_dir) / f["url"], dest)
            files.append({k: f[k] for k in ("url", "sha256", "raw_sha256", "bytes", "count", "bucket")
                          if k in f})
            if dead:
                removed[f["url"]] = dead
    kept_urls = {f["url"] for f in files}
    loose: list[tuple[str, str]] = []
    for r, c in zip(slim, canon_rows):
        if need[c] > 0:
            need[c] -= 1
            loose.append((week_of(r.get("first_seen")), str(r.get("first_seen") or ""),
                          str(r.get("source_id") or ""), c))
    loose.sort()
    pairs = [(w, c) for w, _, _, c in loose]
    del loose
    # Bytes of JSON per gzipped byte, measured on a sample, so a part lands near target_bytes.
    sample = "\n".join(c for _, c in pairs[:3000]).encode("utf-8")
    ratio = (len(sample) / max(1, len(gzip.compress(sample, mtime=0)))) if sample else 8.0
    for chunk in _chunks(pairs, max(1, int(target_bytes * ratio))):
        weeks = sorted({w for w, _ in chunk})
        bucket = weeks[0] if len(weeks) == 1 else f"{weeks[0]}..{weeks[-1]}"
        raw = ('{"bucket":' + json.dumps(bucket) + ',"jobs":['
               + ",".join(c for _, c in chunk) + "]}").encode("utf-8")
        files.append(_stable_file(out, STABLE_PREFIX, raw, count=len(chunk), bucket=bucket))
    # Layout order: oldest bucket first (kept parts and new parts interleave by week).
    files.sort(key=lambda f: (str(f.get("bucket") or "").split("..")[0], f["url"] not in kept_urls,
                              f["url"]))
    rem_file = None
    if removed:
        raw = json.dumps({"removed": {k: removed[k] for k in sorted(removed)}},
                         separators=(",", ":")).encode("utf-8")
        rem_file = _stable_file(out, REMOVED_PREFIX, raw,
                                count=sum(len(v) for v in removed.values()))
    names = [f["url"] for f in files] + ([rem_file["url"]] if rem_file else [])
    version = _sha("\n".join(f"{f['url']}" for f in files).encode("utf-8")
                   + b"|" + (rem_file["sha256"].encode() if rem_file else b""))[:16]
    count = sum(int(f.get("count") or 0) for f in files) - (rem_file["count"] if rem_file else 0)
    if count != len(slim):                                # cannot happen; never publish a wrong list
        raise RuntimeError(f"stable layout holds {count} rows, the list has {len(slim)}")
    block = {"format": STABLE_FORMAT, "version": version, "count": len(slim), "order": "recent",
             "total_bytes": sum(f["bytes"] for f in files) + (rem_file["bytes"] if rem_file else 0),
             "files": files}
    if rem_file:
        block["removed"] = rem_file
    keep = {Path(n).name for n in names}
    for p in (out / PARTS_DIR).glob("*.json.gz"):
        if p.name.startswith((STABLE_PREFIX, REMOVED_PREFIX)) and p.name not in keep:
            p.unlink()
    return block


def save_stable_state(out_dir, state_dir) -> None:
    """Keep this build's manifest and stable files in `state_dir` (the crawl's data dir, cached
    between workflow runs) for the next build to start from. Replaced whole, never half-written."""
    out, d = Path(out_dir), Path(state_dir)
    man = json.loads((out / MANIFEST_FILE).read_text(encoding="utf-8"))
    block = man.get("stable")
    if not stable_block_ok(block):
        return
    tmp = d.with_name(d.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / PARTS_DIR).mkdir(parents=True)
    for f in block["files"] + ([block["removed"]] if block.get("removed") else []):
        shutil.copyfile(out / f["url"], tmp / f["url"])
    (tmp / MANIFEST_FILE).write_text(json.dumps({"stable": block}) + "\n", encoding="utf-8")
    old = d.with_name(d.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if d.exists():
        d.rename(old)
    tmp.rename(d)
    shutil.rmtree(old, ignore_errors=True)


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
               generated_at: str | None = None, part_bytes: int | None = None,
               prev_dir=None) -> dict:
    """Write jobs.json.gz + jd/*.json.gz + manifest.json into `out_dir`. Returns the manifest.
    All 256 shards are written (empty ones too), so a shard fetch is never a 404. `prev_dir` holds
    the previous build's stable layout (save_stable_state); unchanged stable parts are reused."""
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
    prev = load_stable_state(prev_dir) if prev_dir else None
    stable = write_stable(out, slim, prev=prev, target_bytes=part_bytes or PART_TARGET_BYTES)
    stable["attribution"] = header["attribution"]
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
                "jd_url_template": f"{JD_DIR}/{{shard}}.json.gz", "parts": parts, "stable": stable}
    (out / MANIFEST_FILE).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest
