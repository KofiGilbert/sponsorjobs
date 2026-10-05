#!/usr/bin/env python3
"""Stage the job-list SNAPSHOT that ships inside the installer.

Run before pyinstaller:   python scripts/bundle_feed_snapshot.py
Writes:                   packaging/seed/feed/jobs.json.gz + manifest.json   (git-ignored)

WHY IT SHIPS. The desktop app downloads the static feed (docs/feed.md) hourly, and falls back
to crawling the committed watchlist from the person's own laptop when the feed is unreachable or
not configured. A fresh install with nothing cached therefore used to crawl all 1,105 boards at
full speed on first open -- one brisk run got an IP barred by Workable for 19 hours, and during a
feed outage every new install would have done the same at once. Shipping a snapshot of the list
means a fresh install has a board the moment it opens: sourcing/feedclient.StaticFeed adopts the
snapshot as its initial cache (honestly aged by its `generated_at`), and the hourly download
replaces it. Only the slim list is bundled, no JD shards: a description is fetched from the
company's own public board endpoint when a role is opened (feedclient.fetch_board_jd).

TWO SOURCES. (a) The PUBLISHED feed, when `--from-url` or the TAILOR_FEED_URL env var names a real
base URL (the `https://tailor.example/feed` placeholder does not count): one GET of jobs.json.gz
and manifest.json. (b) Otherwise a BOUNDED, POLITE crawl: sourcing.service.first_open_refresh,
the same ranked, per-host-paced pass a fresh install runs, with a wall-clock cap (`--max-minutes`,
default 20) and a modest description budget, shaped by scripts/build_feed.py exactly as the
published feed is. Public job postings only; no personal data is anywhere near this file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sourcing.feedclient import is_placeholder  # noqa: E402
from sourcing.feedfile import JOBS_FILE, MANIFEST_FILE, loads_maybe_gz  # noqa: E402

DEST_DIR = ROOT / "packaging" / "seed" / "feed"
DEFAULT_MAX_MINUTES = 20.0
DEFAULT_DETAIL_BUDGET = 50          # per list-only board (Workday, SmartRecruiters) for this build
_UA = "resume-agent/1.0 (feed snapshot build)"


def feed_url_from(arg: str | None = None, env=None) -> str | None:
    """The published feed's base URL to download from, or None to crawl: `arg`, else the
    TAILOR_FEED_URL env var, unless that is empty or the unbought-domain placeholder."""
    env = os.environ if env is None else env
    url = (arg or env.get("TAILOR_FEED_URL") or "").strip()
    return None if (not url or is_placeholder(url)) else url.rstrip("/")


def _http_get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _validate(body: bytes) -> dict:
    """The parsed list header (without the rows) when `body` is a feed file; raises otherwise."""
    data = loads_maybe_gz(body)
    if not (isinstance(data, dict) and isinstance(data.get("jobs"), list)):
        raise ValueError("not a feed file: expected {generated_at, jobs: [...]}")
    if not data["jobs"]:
        raise ValueError("the feed file holds no jobs; refusing to ship an empty board")
    return {k: v for k, v in data.items() if k != "jobs"} | {"count": len(data["jobs"])}


def stage(dest_dir: Path, jobs_gz: bytes, manifest: dict, log=print) -> dict:
    """Write the two files into `dest_dir` via a temp dir + rename, so a failed build never leaves
    a half-written snapshot where the pyinstaller spec will pick it up."""
    dest_dir = Path(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="feed.", dir=str(dest_dir.parent)))
    try:
        (tmp / JOBS_FILE).write_bytes(jobs_gz)
        (tmp / MANIFEST_FILE).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        os.replace(tmp, dest_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f"[bundle_feed_snapshot] staged {dest_dir / JOBS_FILE}: {manifest.get('count'):,} jobs, "
        f"generated_at={manifest.get('generated_at')}, {len(jobs_gz) / 1e6:.1f} MB "
        f"(source: {manifest.get('bundled_from')})")
    return manifest


def stage_from_url(base_url: str, dest_dir: Path = DEST_DIR, fetch=_http_get, log=print) -> dict:
    """Download the published jobs.json.gz (+ manifest.json, synthesized if absent) into `dest_dir`."""
    base_url = base_url.rstrip("/")
    body = fetch(f"{base_url}/{JOBS_FILE}")
    header = _validate(body)
    try:
        manifest = json.loads(fetch(f"{base_url}/{MANIFEST_FILE}").decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not an object")
    except Exception as exc:                              # noqa: BLE001 - the list is what matters
        log(f"[bundle_feed_snapshot] no usable manifest.json ({exc}); writing one from the list header")
        manifest = {"feed_version": header.get("feed_version"), "generated_at": header.get("generated_at"),
                    "count": header["count"], "jobs_url": f"{base_url}/{JOBS_FILE}"}
    manifest.update({"generated_at": header.get("generated_at") or manifest.get("generated_at"),
                     "count": header["count"], "bundled_from": base_url,
                     "jd_shard_count": 0,                 # no shards in the bundle: JDs come from the boards
                     "jd_url_template": None})
    return stage(dest_dir, body, manifest, log=log)


def stage_from_crawl(dest_dir: Path = DEST_DIR, data_dir=None, *, max_minutes: float = DEFAULT_MAX_MINUTES,
                     max_boards: int | None = None, detail_budget: int = DEFAULT_DETAIL_BUDGET,
                     limit: int | None = None, fetch=None, sleep=time.sleep, clock=time.monotonic,
                     log=print) -> dict:
    """Run the bounded polite crawl (first_open_refresh) into `data_dir` (a temp dir by default),
    shape it like the published feed (build_feed.build) and stage only the slim list."""
    from scripts.build_feed import build
    from sourcing.service import first_open_refresh

    def crawler(w):
        summary = first_open_refresh(w, fetch=fetch, max_boards=max_boards, max_minutes=max_minutes,
                                     detail_budget=detail_budget, sleep=sleep, clock=clock)
        log(f"[bundle_feed_snapshot] polite crawl: boards={summary.get('boards_crawled')}/"
            f"{summary.get('boards_total')} requests={summary.get('requests')} "
            f"rate_limited={summary.get('rate_limited')} cooling={summary.get('cooling')} "
            f"stopped_early={summary.get('stopped_early')} new={summary.get('new')}")
        return summary

    scratch = tempfile.mkdtemp(prefix="feed-snapshot.") if data_dir is None else None
    data_dir = Path(scratch or data_dir)
    out = data_dir / "out"
    try:
        manifest = build(out, data_dir / "data", crawler=crawler, limit=limit, log=log)
        body = (out / JOBS_FILE).read_bytes()
        header = _validate(body)
        manifest.update({"count": header["count"], "bundled_from": "crawl",
                         "jd_shard_count": 0, "jd_url_template": None})
        return stage(dest_dir, body, manifest, log=log)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-url", default=None,
                    help="base URL of the published feed (default: env TAILOR_FEED_URL; a .example "
                         "placeholder means 'none', so the crawl runs)")
    ap.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES,
                    help=f"wall-clock cap of the fallback crawl (default {DEFAULT_MAX_MINUTES:g})")
    ap.add_argument("--max-boards", type=int, default=None,
                    help="crawl only the top N boards by H-1B volume (default: all, under the time cap; "
                         "0 crawls no boards at all)")
    ap.add_argument("--detail-budget", type=int, default=DEFAULT_DETAIL_BUDGET,
                    help=f"descriptions read per Workday / SmartRecruiters board (default {DEFAULT_DETAIL_BUDGET})")
    ap.add_argument("--limit", type=int, default=None, help="cap rows (default JOBS_FEED_LIMIT)")
    ap.add_argument("--data", default=None, help="keep the crawl's state here (default: a temp dir)")
    ap.add_argument("--dest", default=str(DEST_DIR), help=f"where to stage (default {DEST_DIR})")
    ap.add_argument("--no-crawl", action="store_true",
                    help="never crawl: fail instead when the published feed cannot be downloaded")
    a = ap.parse_args(argv)
    dest = Path(a.dest)
    url = feed_url_from(a.from_url)
    if url:
        try:
            stage_from_url(url, dest)
            return 0
        except Exception as exc:                          # noqa: BLE001 - fall back to the crawl
            print(f"[bundle_feed_snapshot] could not download the published feed from {url}: {exc}")
            if a.no_crawl:
                return 1
            print("[bundle_feed_snapshot] falling back to the bounded polite crawl")
    elif a.no_crawl:
        print("[bundle_feed_snapshot] no published feed URL (set TAILOR_FEED_URL or --from-url) and "
              "--no-crawl given; nothing staged")
        return 1
    else:
        print("[bundle_feed_snapshot] no published feed URL (TAILOR_FEED_URL unset or a placeholder): "
              f"running the bounded polite crawl (cap {a.max_minutes:g} min)")
    try:
        stage_from_crawl(dest, a.data, max_minutes=a.max_minutes, max_boards=a.max_boards,
                         detail_budget=a.detail_budget, limit=a.limit)
    except Exception as exc:                              # noqa: BLE001 - say why, exit non-zero
        print(f"[bundle_feed_snapshot] nothing staged: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
