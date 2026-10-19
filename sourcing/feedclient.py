"""The desktop app's reader for the static job feed (see sourcing/feedfile.py for the format).

No server is involved: `jobs.json.gz` is downloaded into the app's data dir AT MOST ONCE AN HOUR,
at a per-install random minute (persisted, so a fleet of installs spreads its downloads across the
hour instead of stampeding on the hour), and conditionally (ETag / If-None-Match, so an unchanged
file costs one 304). Filtering + pagination then run locally over the cached list. Job
descriptions are fetched per shard when a role is opened and cached for a day.

Every network failure degrades, never raises, as long as a cached copy exists; with no cache at
all `FeedUnavailable` tells the caller to fall back to the local crawl. A data dir with no cache
yet starts the download at once; `FeedLoading` (a FeedUnavailable) tells the caller the first
download is still running, with the bytes so far in `status()`, so the board says "downloading".

The list is NEVER downloaded inside a request (2026-10-14: on a ~60 KB/s link the ~10 MB list took
minutes, the request held the lock the whole time, a 10 s read timeout then killed it, and the
board fell back to an empty local list after 332 s). `jobs()` serves the cached copy and starts a
BACKGROUND download when one is due (one at a time); it waits at most `wait` seconds (under a
second) for it, so a fast link still gets the fresh list on the same request. The download streams
to a temp file in chunks, gives up after STALL_TIMEOUT seconds with no bytes or DOWNLOAD_CAP in
total, retries a couple of times, and lands by atomic rename; the old list keeps serving meanwhile.
Every request asks for `Accept-Encoding: gzip`: the CDN otherwise inflates the stored gzip on the
fly and sends ~18x the bytes (~180 MB) with no Content-Length. A failed download is
remembered and not retried for a short, growing back-off (5 min doubling up to the hourly slot), so
an unreachable host costs one short timeout per window instead of one per request. Flask serves
requests from several threads: refresh/load run under a per-instance lock, every file lands via a
uniquely named temp file + os.replace, and a corrupt cached list is deleted and re-downloaded
rather than failing until the next hourly slot.

PARTS (2026-10-14). The CDN ignores Range, and a flaky link that drops every 1-3 MB restarted the
~10 MB list from zero every time, so it never landed. When manifest.json lists `parts`, the list is
downloaded as ~1 MB parts instead (two at a time), each checked against its sha256 and kept under
`parts/<version>/` in the cache; a dropped connection costs only the part in flight, and the next
try (or the next attempt, or the next app start) fetches just the parts still missing. When every
part is present they are joined into jobs.json.gz and swapped in atomically, exactly as a single
download would land. A new version discards the old version's parts. A manifest with no parts (an
older feed) or none at all falls back to the single file.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
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

from concurrent.futures import ThreadPoolExecutor

from .feedfile import FEED_VERSION, JD_DIR, JOBS_FILE, MANIFEST_FILE, PARTS_DIR, loads_maybe_gz, shard_for

CHECK_EVERY = 3600          # a new list is looked for at most once an hour
RETRY_AFTER = 300           # a failed download is not retried for 5 min, doubling per failure...
RETRY_MAX = CHECK_EVERY     # ...up to the hourly slot
JD_TTL = 86400              # a cached JD shard is trusted for a day (matches its Cache-Control)
DEFAULT_TIMEOUT = 10        # a JD shard fetch (small, opened by a click) gives up after this
STALL_TIMEOUT = 30          # the list download: this long with no bytes at all = a stalled link
DOWNLOAD_CAP = 45 * 60      # ...and the whole download is abandoned after this long (the origin ignores
                            # Range, so a cut-off download restarts from zero: the cap must admit a
                            # ~10 MB list on a few KB/s, and the stall timeout catches a dead link)
DOWNLOAD_TRIES = 3          # attempts per background download before it counts as one failure
RETRY_PAUSE = 5             # seconds between those attempts (doubling)
REQUEST_WAIT = 0.8          # how long a request waits on a download it just started
CHUNK = 64 * 1024
PART_WORKERS = 2            # list parts downloaded at once
PART_TRIES = 4              # tries per part within one attempt (finished parts are never refetched)
PART_PAUSE = 2              # seconds between a part's tries (doubling)
STATE_FILE = "feed_state.json"
_UA = "resume-agent/1.0 (static feed client)"


class FeedUnavailable(RuntimeError):
    """No cached feed and the download failed: the caller should use the local crawl."""


class FeedLoading(FeedUnavailable):
    """No cached feed yet, and the first download is running in the background right now."""


def is_placeholder(url: str) -> bool:
    """True for the unbought-domain placeholder (`https://tailor.example/feed`): `.example` is an
    RFC 2606 reserved TLD that can never resolve, so treating it as "no feed configured" keeps a
    fresh install on the local crawl instead of waiting on a lookup that cannot succeed."""
    host = (urlsplit(url or "").hostname or "").lower()
    return not host or host == "example" or host.endswith(".example")


def _http_fetch(url: str, headers: dict, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, dict, bytes]:
    """(status, response headers, body). A 304 is returned as a status, not raised. Asks for gzip
    (the stored bytes); loads_maybe_gz reads either form."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip",
                                               **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = getattr(resp, "headers", None)
            return int(getattr(resp, "status", 200) or 200), dict(hdrs.items()) if hdrs else {}, body
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, {}, b""
        raise


def _http_download(url: str, headers: dict, dest, on_progress=None,
                   stall: float | None = None, cap: float | None = None) -> tuple[int, dict]:
    """Stream `url` into the open binary file `dest` in chunks. (status, headers); a 304 is a
    status. `stall` is the socket timeout, so a link that sends nothing for that long raises
    TimeoutError; a slow link that keeps sending never trips it. `cap` bounds the whole transfer.
    `on_progress(bytes_so_far, total_or_None)` is called after each chunk."""
    stall = STALL_TIMEOUT if stall is None else stall
    cap = DOWNLOAD_CAP if cap is None else cap
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip",
                                               **headers})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=stall) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            hdrs = getattr(resp, "headers", None)
            hdrs = dict(hdrs.items()) if hdrs else {}
            total = next((v for k, v in hdrs.items() if k.lower() == "content-length"), None)
            total = int(total) if total and str(total).isdigit() else None
            got = 0
            # read1 returns whatever has arrived (up to CHUNK), so progress moves on a slow link;
            # read(CHUNK) would sit until a full 64 KB had trickled in.
            read = getattr(resp, "read1", None) or resp.read
            while True:
                chunk = read(CHUNK)
                if not chunk:
                    break
                dest.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
                if time.monotonic() - start > cap:
                    raise TimeoutError(f"feed download passed {int(cap)} s")
            if total is not None and got < total:
                raise OSError(f"feed download ended early ({got} of {total} bytes)")
            return status, hdrs
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, {}
        raise


class StaticFeed:
    """One install's view of the static feed, cached under `cache_dir`.

    `fetch(url, headers) -> (status, headers, body)` and `clock() -> epoch seconds` are injectable
    so the hourly schedule, ETag handling and failure modes are unit-testable offline. With no
    `base_url` (feed not configured) nothing is ever downloaded: the reader only serves whatever
    cache it already holds."""

    def __init__(self, base_url: str, cache_dir, fetch=None, clock=time.time,
                 timeout: float = DEFAULT_TIMEOUT, download=None, wait: float | None = None,
                 tries: int | None = None, retry_pause: float | None = None,
                 parts: bool | None = None, manifest_fetch=None):
        self.base_url = (base_url or "").rstrip("/")
        self.cache_dir = Path(cache_dir)
        self._fetch = fetch or (lambda u, h: _http_fetch(u, h, timeout))
        # The list streams to disk. An injected `fetch` (tests) serves the list too, as one body.
        if download is not None:
            self._stream = download
        elif fetch is not None:
            self._stream = self._stream_via_fetch
        else:
            self._stream = _http_download
        # Defaults are read from the module at construction (so a test can monkeypatch them). An
        # injected one-shot `fetch` is a unit test's fake origin: one try, no pause, so its call
        # counts and back-off assertions see exactly one attempt per refresh.
        fake = fetch is not None and download is None
        # The parted download reads manifest.json first. A unit test's one-shot fake origin only
        # serves the single file, so it skips the manifest unless the test opts in.
        self._manifest_fetch = manifest_fetch or self._fetch
        self._parts = (not fake or manifest_fetch is not None) if parts is None else bool(parts)
        self._part_tries = PART_TRIES
        self._part_pause = 0 if fake else PART_PAUSE
        self._wait = REQUEST_WAIT if wait is None else wait
        self._tries = max(1, int(tries if tries is not None else (1 if fake else DOWNLOAD_TRIES)))
        self._retry_pause = (retry_pause if retry_pause is not None
                             else (0 if fake else RETRY_PAUSE))
        self._clock = clock
        # The background download: one at a time, never under self._lock while bytes flow.
        self._dl_lock = threading.Lock()
        self._dl_thread: threading.Thread | None = None
        self._dl_done = threading.Event()
        self._dl_done.set()
        self._progress: dict = {"running": False, "bytes": 0, "total": None, "started": None,
                                "attempt": 0, "error": "", "parts_done": 0, "parts_total": 0}
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
        """Download jobs.json.gz now if due (or forced), on THIS thread. Returns True when a NEW
        file landed. Raises on network/format errors; callers decide whether a cached copy covers
        for it. A failure is recorded so the next attempt waits out `retry_after()`. One download
        at a time: a second caller waits for the first, then finds the list no longer due."""
        with self._dl_lock:
            with self._lock:
                if not force and not self.due():
                    return False
            return self._run_download()

    def start_refresh(self, force: bool = False) -> bool:
        """Start a BACKGROUND download when one is due (or forced) and none is running. Returns
        True when a download is running after the call. Never blocks on the network."""
        with self._lock:
            if self._dl_thread is not None and self._dl_thread.is_alive():
                return True
            if not force and not self.due():
                return False
            self._dl_done.clear()
            self._progress.update({"running": True, "bytes": 0, "total": None,
                                   "started": time.time(), "attempt": 0, "error": "",
                                   "parts_done": 0, "parts_total": 0})

            def run():
                try:
                    with self._dl_lock:
                        with self._lock:
                            still = force or self.due()
                        if still:
                            self._run_download()
                except Exception:                     # noqa: BLE001 - recorded in state + status
                    traceback.print_exc()
                finally:
                    with self._lock:
                        self._progress["running"] = False
                    self._dl_done.set()

            t = threading.Thread(target=run, name="feed-download", daemon=True)
            self._dl_thread = t
            t.start()
            return True

    def downloading(self) -> bool:
        t = self._dl_thread
        return bool(t is not None and t.is_alive())

    def wait_download(self, timeout: float | None = None) -> bool:
        """Block until the background download (if any) finishes. True when none is running."""
        return self._dl_done.wait(timeout)

    def status(self) -> dict:
        """What the board shows while there is no list yet: is a download running, how far along,
        and the last failure. Cheap; never touches the network."""
        with self._lock:
            p = dict(self._progress)
            has = self.jobs_path.exists()
        return {"configured": bool(self.base_url), "has_cache": has,
                "downloading": self.downloading(), "bytes": p.get("bytes") or 0,
                "total": p.get("total"), "attempt": p.get("attempt") or 0,
                "parts_done": p.get("parts_done") or 0, "parts_total": p.get("parts_total") or 0,
                "error": "" if self.downloading() else (p.get("error") or ""),
                "failing": bool(int(self._state.get("fail_count") or 0)),
                "retry_in": max(0, int(float(self._state.get("last_fail") or 0)
                                       + self.retry_after() - self._clock()))
                            if int(self._state.get("fail_count") or 0) else 0}

    def _run_download(self) -> bool:
        """Up to `tries` attempts with a short growing pause; a final failure is recorded."""
        last: Exception | None = None
        for attempt in range(1, self._tries + 1):
            with self._lock:
                self._progress.update({"attempt": attempt, "bytes": 0, "total": None})
            try:
                landed = self._download()
                with self._lock:
                    self._progress["error"] = ""
                return landed
            except Exception as exc:                      # noqa: BLE001 - retried, then raised
                last = exc
                with self._lock:
                    self._progress["error"] = f"{type(exc).__name__}: {exc}"[:200]
                    got = self._progress.get("bytes") or 0
                print(f"[feed] download attempt {attempt}/{self._tries} failed after {got} bytes: "
                      f"{self._progress['error']}", flush=True)
                if isinstance(exc, FeedUnavailable) and "HTTP 4" in str(exc):
                    break                                 # a 404 / 403 will not fix itself in seconds
                if attempt < self._tries and self._retry_pause:
                    time.sleep(self._retry_pause * 2 ** (attempt - 1))
        with self._lock:
            self._record_failure()
        raise last  # type: ignore[misc]

    def _stream_via_fetch(self, url, headers, dest, on_progress=None):
        """Adapter: an injected one-shot `fetch` (the unit tests' fake origin) as a stream."""
        status, hdrs, body = self._fetch(url, headers)
        if body:
            dest.write(body)
            if on_progress:
                on_progress(len(body), len(body))
        return status, hdrs

    def _on_progress(self, got: int, total) -> None:
        self._progress["bytes"] = got
        self._progress["total"] = total

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
        """One attempt: the parted download when the manifest lists parts, else the single file."""
        if self._parts:
            man = self._read_manifest()
            if man is not None:
                return self._download_parts(man)
        return self._download_single()

    def _read_manifest(self) -> dict | None:
        """The manifest's "parts" block (plus generated_at), or None when the feed has no parts
        (no manifest, a 404, an older builder). A network error raises: the attempt failed."""
        try:
            status, _, body = self._manifest_fetch(f"{self.base_url}/{MANIFEST_FILE}",
                                                   {"Accept": "application/json",
                                                    "Cache-Control": "no-cache"})
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                return None
            raise
        if status != 200 or not body:
            return None
        try:
            man = loads_maybe_gz(body)
        except Exception:                                 # noqa: BLE001 - not a manifest
            return None
        parts = man.get("parts") if isinstance(man, dict) else None
        if not (isinstance(parts, dict) and parts.get("version") and isinstance(parts.get("files"), list)
                and parts["files"] and all(isinstance(f, dict) and f.get("url") and f.get("sha256")
                                           for f in parts["files"])):
            return None
        return {**parts, "generated_at": man.get("generated_at"),
                "feed_version": man.get("feed_version", FEED_VERSION)}

    @property
    def parts_root(self) -> Path:
        return self.cache_dir / PARTS_DIR

    def _part_path(self, version: str, url: str) -> Path:
        return self.parts_root / _safe(version) / _safe(Path(url).name)

    def _download_parts(self, man: dict) -> bool:
        """Fetch the parts still missing for this version (two at a time), each verified by sha256,
        then join them into jobs.json.gz and swap it in. Returns True when a new list landed."""
        version = str(man["version"])
        files = man["files"]
        now = self._clock()
        with self._lock:
            if self._state.get("parts_version") == version and self.jobs_path.exists():
                # The same rows as the cached list: nothing to download. A rebuild with unchanged
                # rows still moves generated_at, which the board shows as "updated".
                self._state["last_check"] = now
                if man.get("generated_at"):
                    self._state["generated_at"] = man["generated_at"]
                    if self._header:
                        self._header["generated_at"] = man["generated_at"]
                self._clear_failures()
                self._save_state()
                return False
        vdir = self.parts_root / _safe(version)
        vdir.mkdir(parents=True, exist_ok=True)
        for old in self.parts_root.iterdir():             # another version's parts are dead weight
            if old.is_dir() and old.name != vdir.name:
                shutil.rmtree(old, ignore_errors=True)
        sizes = [int(f.get("bytes") or 0) for f in files]
        done = {i for i, f in enumerate(files) if _part_ok(self._part_path(version, f["url"]), f)}
        inflight: dict[int, int] = {}
        total = sum(sizes) or None

        def report(i: int | None = None, got: int | None = None, finished: bool = False):
            # Two part threads report at once: every change to `done` / `inflight` is under the lock.
            with self._lock:
                if i is not None:
                    if finished:
                        done.add(i)
                    if got is None or finished:
                        inflight.pop(i, None)
                    else:
                        inflight[i] = got
                self._progress.update({
                    "parts_done": len(done), "parts_total": len(files), "total": total,
                    "bytes": sum(sizes[k] for k in done) + sum(inflight.values())})

        report()
        todo = [i for i in range(len(files)) if i not in done]

        def fetch_one(i: int) -> None:
            f = files[i]
            dest = self._part_path(version, f["url"])
            last: Exception | None = None
            for t in range(self._part_tries):
                fd, tmp = tempfile.mkstemp(dir=str(vdir), prefix=dest.name + ".", suffix=".part")
                try:
                    with os.fdopen(fd, "wb") as out:
                        def prog(got, _total, i=i):
                            report(i, got)
                        status, _ = self._stream(f"{self.base_url}/{f['url']}", {}, out, prog)
                    if status != 200:
                        raise OSError(f"feed part {f['url']} returned status {status}")
                    if not _part_ok(Path(tmp), f):
                        raise OSError(f"feed part {f['url']} failed its sha256 check")
                    os.replace(tmp, dest)
                    report(i, finished=True)
                    return
                except Exception as exc:                  # noqa: BLE001 - this part is retried
                    last = exc
                    report(i)
                    if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                        # A 404 means the manifest moved on (a new build replaced these parts):
                        # stop this attempt, so the next one re-reads the manifest.
                        raise OSError(f"feed part {f['url']} is gone (status {exc.code}); the "
                                      "feed was rebuilt, so the next try re-reads the manifest") from exc
                    print(f"[feed] part {i + 1}/{len(files)} try {t + 1}/{self._part_tries} failed: "
                          f"{type(exc).__name__}: {exc}"[:240], flush=True)
                    if t + 1 < self._part_tries and self._part_pause:
                        time.sleep(self._part_pause * 2 ** t)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            raise last  # type: ignore[misc]

        if todo:
            with ThreadPoolExecutor(max_workers=PART_WORKERS, thread_name_prefix="feed-part") as pool:
                errors = []
                for fut in [pool.submit(fetch_one, i) for i in todo]:
                    try:
                        fut.result()
                    except Exception as exc:              # noqa: BLE001 - raised below
                        errors.append(exc)
            if errors:
                # Kept: every part that did arrive. The next try fetches only the rest.
                raise errors[0]
        return self._assemble(man, version, now)

    def _assemble(self, man: dict, version: str, now: float) -> bool:
        """Join the verified parts in order and swap the list in (under the lock)."""
        jobs: list = []
        for f in man["files"]:
            part = loads_maybe_gz(self._part_path(version, f["url"]).read_bytes())
            if not isinstance(part, dict) or not isinstance(part.get("jobs"), list) \
                    or str(part.get("version")) != version:
                shutil.rmtree(self.parts_root / _safe(version), ignore_errors=True)
                raise FeedUnavailable(f"feed part {f['url']} has an unexpected shape")
            jobs.extend(part["jobs"])
        if man.get("count") is not None and int(man["count"]) != len(jobs):
            shutil.rmtree(self.parts_root / _safe(version), ignore_errors=True)
            raise FeedUnavailable(f"feed parts hold {len(jobs)} rows, the manifest says {man['count']}")
        data = {"feed_version": man.get("feed_version", FEED_VERSION),
                "generated_at": man.get("generated_at"), "count": len(jobs),
                "attribution": man.get("attribution") or {}, "jobs": jobs}
        # Serialized OUTSIDE the lock (a few seconds for ~96k rows): requests keep being served.
        body = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                             .encode("utf-8"), compresslevel=5, mtime=0)
        with self._lock:
            if not jobs and self._cached_rows_exist():    # same guard as the single file
                self._state.update({"last_check": now, "parts_version": version})
                self._clear_failures()
                self._save_state()
                return False
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.jobs_path, body)
            del body
            self._state.update({"last_check": now, "generated_at": data.get("generated_at"),
                                "parts_version": version})
            self._state.pop("etag", None)                 # the single file's ETag no longer describes the cache
            self._clear_failures()
            self._save_state()
            self._set(data, self.jobs_path.stat().st_mtime)
        shutil.rmtree(self.parts_root, ignore_errors=True)  # joined: the parts are no longer needed
        return True

    def _download_single(self) -> bool:
        """One attempt: stream to a uniquely named temp file (no lock held while bytes flow), then
        validate and publish under the lock by atomic rename."""
        headers = {"Accept": "application/json"}
        with self._lock:
            if self._state.get("etag") and self.jobs_path.exists():
                headers["If-None-Match"] = self._state["etag"]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.cache_dir), prefix=JOBS_FILE + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                status, hdrs = self._stream(f"{self.base_url}/{JOBS_FILE}", headers, f,
                                            self._on_progress)
            with self._lock:
                return self._publish(status, hdrs or {}, Path(tmp))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _publish(self, status: int, hdrs: dict, tmp: Path) -> bool:
        now = self._clock()
        if status == 304:
            self._state["last_check"] = now
            self._clear_failures()
            self._save_state()
            return False
        if status != 200:
            raise FeedUnavailable(f"feed returned HTTP {status}")
        body = tmp.read_bytes()
        data = loads_maybe_gz(body)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise FeedUnavailable("feed file has an unexpected shape")
        if not data.get("jobs") and self._cached_rows_exist():
            # A crawl that errored on every board still publishes a valid, empty list. Keep the
            # rows we have, note the check so this is not retried every request, and let the
            # caller decide (ui/app.py treats a persistently empty feed as absent).
            self._state.update({"last_check": now})
            self._clear_failures()
            self._save_state()
            return False
        if body[:2] == b"\x1f\x8b":
            os.replace(tmp, self.jobs_path)               # already the stored gzip: just rename
        else:
            _atomic_write(self.jobs_path, gzip.compress(body, mtime=0))
        del body
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
        self._state.pop("parts_version", None)            # ...and the parted path would skip it
        self._state.pop("last_check", None)
        try:
            self._save_state()
        except Exception:                                 # noqa: BLE001
            traceback.print_exc()

    def jobs(self) -> tuple[list[dict], dict]:
        """(rows, header). Never downloads on the caller's thread: when a refresh is due it starts
        one in the background and waits at most `wait` seconds for it (a fast link gets the new
        list now; a slow one gets the cached copy, or FeedLoading when there is none yet). A
        failed refresh falls back to the cached copy; no copy and no download raises
        FeedUnavailable."""
        with self._lock:
            self._load_cached()      # drops a corrupt file first, so the refresh below is due now
            running = self.start_refresh()
            started = float(self._progress.get("started") or 0)
        # Wait at most `wait` seconds from the download's START: a fast link lands it for every
        # request in that window, while the polls of a long download answer at once.
        left = (started + self._wait - time.time()) if running else 0
        if left > 0:
            self._dl_done.wait(left)
        with self._lock:
            self._load_cached()
            if self._jobs is None:
                if self.downloading():
                    raise FeedLoading("the job list is still downloading")
                raise FeedUnavailable("feed download failed and nothing is cached"
                                      if self._state.get("fail_count") else "no cached feed")
            return self._jobs, self._header

    def refreshed_at(self) -> str | None:
        return (self._header or {}).get("generated_at") or self._state.get("generated_at")

    def get(self, source_id: str) -> dict | None:
        """One cached slim row by source_id, or None (also None when the feed is unavailable).
        The index is built UNDER the lock, from the list currently loaded and keyed to its mtime:
        built outside it from the list jobs() returned, a reload on another thread in between
        (which nulls the index) left an index of the superseded list in place for an hour, and
        the detail view said "no longer in the feed" for roles the board was showing."""
        try:
            self.jobs()                     # outside the lock: it may wait briefly on a download
        except FeedUnavailable:
            return None
        with self._lock:
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
    from .ats import (ats_disabled, fetch_json, freehire_fulltext, html_to_text,
                      smartrecruiters_detail, valid_board_id)
    fetch = fetch or fetch_json
    sid = str(job.get("source_id") or "")
    parts = sid.split(":", 2)
    if len(parts) != 3:
        return ""
    source, board, jid = parts
    if ats_disabled(source):
        return ""                 # e.g. SmartRecruiters: its robots.txt disallows us (DISABLED_ATS)
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
    from .ats import ats_disabled, fetch_json, smartrecruiters_fill
    from .quality import is_list_only
    if not is_list_only(job) or ats_disabled(job.get("source") or ""):
        return {}                 # a disabled ATS (DISABLED_ATS) gets no request
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


def _safe(name: str) -> str:
    """A manifest-supplied name as one plain path component (never a path out of the cache)."""
    keep = "".join(c for c in str(name) if c.isalnum() or c in "-_.")
    return keep.strip(".") or "x"


def _sha_ok(path: Path, sha256: str) -> bool:
    """Does the file at `path` exist with exactly this sha256?"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest() == str(sha256).lower()
    except OSError:
        return False


def _part_ok(path: Path, f: dict) -> bool:
    """Is the file at `path` this part, byte for byte (sha256 of the stored gzip), or, when a CDN
    re-encoded or inflated it in transit, the same JSON inside (raw_sha256)?"""
    if _sha_ok(path, f.get("sha256") or ""):
        return True
    raw_sha = f.get("raw_sha256")
    if not raw_sha:
        return False
    try:
        body = path.read_bytes()
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return hashlib.sha256(body).hexdigest() == str(raw_sha).lower()
    except Exception:                                     # noqa: BLE001 - unreadable = not this part
        return False


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
