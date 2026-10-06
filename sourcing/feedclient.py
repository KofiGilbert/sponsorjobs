"""The desktop app's reader for the static job feed (see sourcing/feedfile.py for the format).

No server is involved: `jobs.json.gz` is downloaded into the app's data dir AT MOST ONCE AN HOUR,
at a per-install random minute (persisted, so a fleet of installs spreads its downloads across the
hour instead of stampeding on the hour), and conditionally (ETag / If-None-Match, so an unchanged
file costs one 304). Filtering + pagination then run locally over the cached list. Job
descriptions are fetched per shard when a role is opened and cached for a day.

Every network failure degrades, never raises, as long as a cached copy exists; with no cache at
all `FeedUnavailable` tells the caller to fall back to the local crawl. A data dir with no cache
yet downloads the list at once (the app shows "fetching jobs" meanwhile). A failed download is
remembered and not retried for a short, growing back-off (5 min doubling up to the hourly slot), so
an unreachable host costs one short timeout per window instead of one per request. Flask serves
requests from several threads: refresh/load run under a per-instance lock, every file lands via a
uniquely named temp file + os.replace, and a corrupt cached list is deleted and re-downloaded
rather than failing until the next hourly slot.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .feedfile import JD_DIR, JOBS_FILE, loads_maybe_gz, shard_for

CHECK_EVERY = 3600          # a new list is looked for at most once an hour
RETRY_AFTER = 300           # a failed download is not retried for 5 min, doubling per failure...
RETRY_MAX = CHECK_EVERY     # ...up to the hourly slot
JD_TTL = 86400              # a cached JD shard is trusted for a day (matches its Cache-Control)
DEFAULT_TIMEOUT = 10        # a background refresh must never hold a request longer than this
STATE_FILE = "feed_state.json"
_UA = "resume-agent/1.0 (static feed client)"


class FeedUnavailable(RuntimeError):
    """No cached feed and the download failed: the caller should use the local crawl."""


def is_placeholder(url: str) -> bool:
    """True for the unbought-domain placeholder (`https://tailor.example/feed`): `.example` is an
    RFC 2606 reserved TLD that can never resolve, so treating it as "no feed configured" keeps a
    fresh install on the local crawl instead of waiting on a lookup that cannot succeed."""
    host = (urlsplit(url or "").hostname or "").lower()
    return not host or host == "example" or host.endswith(".example")


def _http_fetch(url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, dict, bytes]:
    """(status, response headers, body). A 304 is returned as a status, not raised."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = getattr(resp, "headers", None)
            return int(getattr(resp, "status", 200) or 200), dict(hdrs.items()) if hdrs else {}, body
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, {}, b""
        raise


class StaticFeed:
    """One install's view of the static feed, cached under `cache_dir`.

    `fetch(url, headers) -> (status, headers, body)` and `clock() -> epoch seconds` are injectable
    so the hourly schedule, ETag handling and failure modes are unit-testable offline. With no
    `base_url` (feed not configured) nothing is ever downloaded: the reader only serves whatever
    cache it already holds."""

    def __init__(self, base_url: str, cache_dir, fetch=None, clock=time.time,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.cache_dir = Path(cache_dir)
        self._fetch = fetch or (lambda u, h: _http_fetch(u, h, timeout))
        self._clock = clock
        # Flask is threaded: /api/jobs and /api/jobs/detail can both find the list due at the same
        # moment. One downloads; the other waits, sees it is no longer due, and serves the cache.
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._jobs: list[dict] | None = None
        self._header: dict = {}
        self._loaded_mtime: float | None = None
        self._by_id: tuple[float | None, dict] | None = None
        self._bench: tuple[float | None, dict] | None = None

    # -- persisted state ---------------------------------------------------------------- #

    @property
    def jobs_path(self) -> Path:
        return self.cache_dir / JOBS_FILE

    def _load_state(self) -> dict:
        try:
            return json.loads((self.cache_dir / STATE_FILE).read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001 - missing/corrupt = fresh
            return {}

    def _save_state(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.cache_dir / STATE_FILE,
                      json.dumps(self._state, indent=1).encode("utf-8"))

    def offset_minute(self) -> int:
        """This install's minute-of-the-hour for feed checks; drawn once and kept forever."""
        m = self._state.get("offset_minute")
        if not isinstance(m, int) or not (0 <= m < 60):
            m = random.randrange(60)
            self._state["offset_minute"] = m
            self._save_state()
        return m

    def retry_after(self) -> float:
        """Seconds a download is held off after the last failure: 5 min, doubling per consecutive
        failure, capped at the hourly slot. 0 when the last attempt succeeded."""
        fails = int(self._state.get("fail_count") or 0)
        return min(RETRY_AFTER * 2 ** (fails - 1), RETRY_MAX) if fails > 0 else 0.0

    def failing_for(self, now: float | None = None) -> float:
        """Seconds the feed has been failing WITHOUT a successful download in between: the age of
        the current failure streak (0 when the last attempt succeeded or none was made). The app's
        first-open crawl only starts once this passes a threshold, so one failed download never
        sends a fresh install off to crawl the boards itself."""
        if not int(self._state.get("fail_count") or 0):
            return 0.0
        now = self._clock() if now is None else now
        first = float(self._state.get("first_fail") or self._state.get("last_fail") or now)
        return max(0.0, now - first)

    def due(self, now: float | None = None) -> bool:
        """True when this hour's check slot (hour boundary + offset minute) has passed since the
        last check, or when there is no cached list yet (first run downloads immediately) -- unless
        the last download failed and its back-off window hasn't elapsed (the cache, or the local
        fallback, is served instantly rather than paying the timeout on every request). Never
        with no base URL: an unconfigured feed has nothing to download."""
        if not self.base_url:
            return False
        now = self._clock() if now is None else now
        if now < float(self._state.get("last_fail") or 0) + self.retry_after():
            return False
        if not self.jobs_path.exists():
            return True
        slot = (now // CHECK_EVERY) * CHECK_EVERY + self.offset_minute() * 60
        if slot > now:
            slot -= CHECK_EVERY
        return float(self._state.get("last_check") or 0) < slot

    # -- the list -------------------------------------------------------------------------- #

    def refresh(self, force: bool = False) -> bool:
        """Download jobs.json.gz if due (or forced). Returns True when a NEW file landed. Raises on
        network/format errors; callers decide whether a cached copy covers for it. A failure is
        recorded so the next attempt waits out `retry_after()`. Serialized per instance."""
        with self._lock:
            if not force and not self.due():
                return False
            try:
                return self._download()
            except Exception:
                self._record_failure()
                raise

    def _record_failure(self) -> None:
        now = self._clock()
        self._state["last_fail"] = now
        self._state.setdefault("first_fail", now)
        self._state["fail_count"] = int(self._state.get("fail_count") or 0) + 1
        try:
            self._save_state()
        except Exception:                                 # noqa: BLE001 - never mask the real error
            traceback.print_exc()

    def _clear_failures(self) -> None:
        self._state.pop("last_fail", None)
        self._state.pop("first_fail", None)
        self._state.pop("fail_count", None)

    def _download(self) -> bool:
        headers = {"Accept": "application/json"}
        if self._state.get("etag") and self.jobs_path.exists():
            headers["If-None-Match"] = self._state["etag"]
        status, hdrs, body = self._fetch(f"{self.base_url}/{JOBS_FILE}", headers)
        now = self._clock()
        if status == 304:
            self._state["last_check"] = now
            self._clear_failures()
            self._save_state()
            return False
        if status != 200:
            raise FeedUnavailable(f"feed returned HTTP {status}")
        data = loads_maybe_gz(body)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FeedUnavailable("feed file has an unexpected shape")
        raw = body if body[:2] == b"\x1f\x8b" else gzip.compress(body, mtime=0)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not data.get("jobs") and self._cached_rows_exist():
            # A crawl that errored on every board still publishes a valid, empty list. Keep the
            # rows we have, note the check so this is not retried every request, and let the
            # caller decide (ui/app.py treats a persistently empty feed as absent).
            self._state.update({"last_check": now})
            self._save_state()
            return False
        _atomic_write(self.jobs_path, raw)
        etag = next((v for k, v in (hdrs or {}).items() if k.lower() == "etag"), None)
        self._state.update({"last_check": now, "etag": etag,
                            "generated_at": data.get("generated_at")})
        self._clear_failures()
        self._save_state()
        self._set(data, self.jobs_path.stat().st_mtime)
        return True

    def _set(self, data: dict, mtime: float) -> None:
        self._jobs = data.get("jobs") or []
        self._header = {k: v for k, v in data.items() if k != "jobs"}
        self._loaded_mtime = mtime
        self._by_id = None
        self._bench = None

    def _cached_rows_exist(self) -> bool:
        """Does the in-memory list or the on-disk cache hold at least one row?"""
        if self._jobs:
            return True
        try:
            if self.jobs_path.exists():
                d = loads_maybe_gz(self.jobs_path.read_bytes())
                return bool(isinstance(d, dict) and d.get("jobs"))
        except Exception:                             # noqa: BLE001 - unreadable = no rows
            return False
        return False

    def has_cache(self) -> bool:
        """Is there a downloaded list to serve without the network?"""
        with self._lock:
            return self.jobs_path.exists()

    def _load_cached(self) -> None:
        """Load the cached list into memory if the file changed. A corrupt file (a half-written or
        truncated download) is DELETED and treated as no cache, so the next refresh is due at once
        instead of every request failing until the hourly slot; an older copy already in memory
        keeps serving meanwhile."""
        with self._lock:
            if not self.jobs_path.exists():
                return
            mtime = self.jobs_path.stat().st_mtime
            if self._jobs is not None and self._loaded_mtime == mtime:
                return
            try:
                data = loads_maybe_gz(self.jobs_path.read_bytes())
                ok = isinstance(data, dict) and isinstance(data.get("jobs"), list)
            except Exception:                             # noqa: BLE001 - not even parseable
                ok = False
            if not ok:
                self._discard_cached()
                return
            self._set(data, mtime)

    def _discard_cached(self) -> None:
        try:
            self.jobs_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:                                 # noqa: BLE001 - best effort
            traceback.print_exc()
        self._state.pop("etag", None)                     # a conditional GET would 304 against garbage
        self._state.pop("last_check", None)
        try:
            self._save_state()
        except Exception:                                 # noqa: BLE001
            traceback.print_exc()

    def jobs(self) -> tuple[list[dict], dict]:
        """(rows, header). Refreshes when due; a failed refresh falls back to the cached copy;
        no copy at all raises FeedUnavailable."""
        with self._lock:
            self._load_cached()      # drops a corrupt file first, so the refresh below is due now
            try:
                self.refresh()
            except Exception:                             # noqa: BLE001 - serve the cache instead
                if self._jobs is None and not self.jobs_path.exists():
                    raise FeedUnavailable("feed download failed and nothing is cached")
                traceback.print_exc()
            self._load_cached()
            if self._jobs is None:
                raise FeedUnavailable("no cached feed")
            return self._jobs, self._header

    def refreshed_at(self) -> str | None:
        return (self._header or {}).get("generated_at") or self._state.get("generated_at")

    def get(self, source_id: str) -> dict | None:
        """One cached slim row by source_id, or None (also None when the feed is unavailable).
        The index is built UNDER the lock, from the list currently loaded and keyed to its mtime:
        built outside it from the list jobs() returned, a reload on another thread in between
        (which nulls the index) left an index of the superseded list in place for an hour, and
        the detail view said "no longer in the feed" for roles the board was showing."""
        with self._lock:
            try:
                self.jobs()
            except FeedUnavailable:
                return None
            if self._jobs is None:
                return None
            if self._by_id is None or self._by_id[0] != self._loaded_mtime:
                self._by_id = (self._loaded_mtime, {j.get("source_id"): j for j in self._jobs})
            return self._by_id[1].get(source_id)

    def benchmarks(self) -> dict:
        """Board-wide pay medians per role family over the cached list (for pay_insight), computed
        once per downloaded file."""
        from .filters import salary_benchmarks
        try:
            jobs, _ = self.jobs()
        except FeedUnavailable:
            return {}
        if self._bench is None or self._bench[0] != self._loaded_mtime:
            self._bench = (self._loaded_mtime, salary_benchmarks(jobs))
        return self._bench[1]

    # -- the descriptions ------------------------------------------------------------------ #

    def jd(self, source_id: str) -> str | None:
        """The JD text for one role from its shard (cached a day), or None when the shard lacks it
        or can't be fetched. Never raises."""
        shard = shard_for(source_id)
        path = self.cache_dir / JD_DIR / f"{shard}.json.gz"
        fresh = path.exists() and (self._clock() - path.stat().st_mtime) < JD_TTL
        if not fresh and self.base_url:               # no feed configured: nowhere to fetch a shard from
            try:
                status, _, body = self._fetch(f"{self.base_url}/{JD_DIR}/{shard}.json.gz", {})
                if status == 200:
                    data = loads_maybe_gz(body)
                    if isinstance(data, dict):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write(path, body if body[:2] == b"\x1f\x8b"
                                      else gzip.compress(body, mtime=0))
                        os.utime(path, None)
                        return data.get(source_id) or None
            except Exception:                             # noqa: BLE001 - fall back to a stale shard
                traceback.print_exc()
        if path.exists():
            try:
                return (loads_maybe_gz(path.read_bytes()) or {}).get(source_id) or None
            except Exception:                             # noqa: BLE001 - corrupt shard = no JD
                return None
        return None


def fetch_board_jd(job: dict, fetch=None) -> str:
    """Last resort when a shard lacks a description: ask the company's OWN public board endpoint
    for this one posting (the same GREEN-lane APIs the crawl uses, CLAUDE.md §6), or, for an
    aggregator row, freehire's full-text lookup. Returns plain text, or '' on any failure."""
    from .ats import (fetch_json, freehire_fulltext, html_to_text, smartrecruiters_detail,
                      valid_board_id)
    fetch = fetch or fetch_json
    sid = str(job.get("source_id") or "")
    parts = sid.split(":", 2)
    if len(parts) != 3:
        return ""
    source, board, jid = parts
    try:
        if source in ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters"):
            if not (valid_board_id(board) and valid_board_id(jid)):
                return ""
        if source == "greenhouse":
            d = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}") or {}
            return html_to_text(d.get("content", ""))
        if source == "lever":
            d = fetch(f"https://api.lever.co/v0/postings/{board}/{jid}") or {}
            jd = d.get("descriptionPlain") or html_to_text(d.get("description", ""))
            extra = d.get("additionalPlain") or ""
            return (jd + ("\n\n" + extra if extra else "")).strip()
        if source == "ashby":                       # no per-posting endpoint: scan the board
            d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{board}") or {}
            for j in d.get("jobs", []):
                if str(j.get("id")) == jid:
                    return j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
            return ""
        if source == "workable":
            d = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{board}/jobs/{jid}") or {}
            return html_to_text(d.get("description", "") or d.get("full_description", ""))
        if source == "recruitee":
            d = fetch(f"https://{board}.recruitee.com/api/offers/{jid}") or {}
            return html_to_text((d.get("offer") or d).get("description", ""))
        if source == "smartrecruiters":
            _, jd = smartrecruiters_detail(board, jid, fetch)
            return jd
        if source == "workday":                     # the cxs detail URL is derived from the row's url
            from .workday import jd_for_job
            return jd_for_job(job, fetch)
        # Aggregator rows: freehire carries full text for most US roles, matched by company+title.
        return html_to_text(freehire_fulltext(job.get("title", ""), job.get("company", ""),
                                              fetch=fetch))
    except Exception:                                     # noqa: BLE001 - best-effort only
        return ""


def fill_list_only_detail(job: dict, fetch=None) -> dict:
    """The description (and, when the detail carries one, the posting's real public page) of a
    list-only row (Workday, SmartRecruiters) stored WITHOUT a JD -- a row saved before the refresh
    started checking every kept row's description, or a bookmarked snapshot. The detail views
    call this on open, run the accessibility check the row has not had yet, and dismiss a barred
    role rather than show it. Returns the fields read ({"jd_text": ..., "url": ...}; "url" only
    when the detail names one); {} when the posting is gone or the row is not list-only."""
    from .ats import fetch_json, smartrecruiters_fill
    from .quality import is_list_only
    if not is_list_only(job):
        return {}
    fetch = fetch or fetch_json
    out: dict = {}
    try:
        if job.get("source") == "smartrecruiters":
            probe = {"source_id": job.get("source_id"), "url": job.get("url")}
            smartrecruiters_fill(probe, fetch)
            if probe.get("jd_text"):
                out["jd_text"] = probe["jd_text"]
            if probe.get("url") and probe["url"] != job.get("url"):
                out["url"] = probe["url"]
        else:                                 # workday: the cxs detail URL derives from the row's url
            from .workday import jd_for_job
            jd = jd_for_job(job, fetch)
            if jd:
                out["jd_text"] = jd
    except Exception:                                     # noqa: BLE001 - best-effort only
        return {}
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a UNIQUELY named temp file in the same directory, then os.replace. Two threads (or
    two processes) writing the same path each get their own temp file, so one can never publish
    the other's half-written bytes; readers see the old file or the new one, never a mix."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
