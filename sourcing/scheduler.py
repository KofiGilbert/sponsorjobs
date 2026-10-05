"""The auto-updater ("central kitchen"): a background loop that keeps the job feed fresh on
its own, so nobody has to press a button.

On a timer it runs one maintenance pass:
  1. REFRESH every watched board (pull new postings, update existing ones),
  2. PRUNE roles that have vanished from their board (filled/closed -> dead links),
  3. occasionally RE-DISCOVER new sponsor boards (its own, slower cadence).

The same logic serves both shapes of the product: a daemon thread inside a local install,
or the loop of one central service that many users pull from. It's OFF unless explicitly
enabled (RESUME_AGENT_AUTOUPDATE=1), so it never spins up during tests or a quick dev run.
Everything is injectable (clock, sleep, the refresh/discover callables, the watchlist
factory) so the pass is unit-testable offline with no threads and no network.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

# How often the robot pulls fresh jobs. Env-tunable (JOBS_REFRESH_HOURS) so freshness can be
# cranked up on the server WITHOUT a redeploy -- e.g. hourly once we're on a paid feed whose
# quota can sustain it. Default 6h: safe for the free API tiers (a tighter cadence would burn
# the free monthly call budget). The freshest single win is fetching date-sorted (ats.py).
REFRESH_EVERY = int(float(os.environ.get("JOBS_REFRESH_HOURS", "6")) * 3600)
# The FAST loop pulls only the free, keyless freehire bulk feed (the high-volume source) on a tight
# cadence, so first_seen stays current and the board reads "New Xm ago" without re-hitting the slow
# third-party company boards every tick. freehire publishes NO rate limit and each fast tick is only
# a SMALL freshest-slice pull (~15 requests, see refresh_freehire_only/JOBS_FAST_ROWS), so 10 min is
# both fresh and a polite client. Env-tunable (JOBS_FAST_REFRESH_MIN); 0 disables the fast loop.
FAST_REFRESH_EVERY = int(float(os.environ.get("JOBS_FAST_REFRESH_MIN", "10")) * 60)
DISCOVER_EVERY = 7 * 24 * 3600    # look for brand-new sponsor boards weekly
PRUNE_DAYS = 21                   # drop roles unseen for 3 weeks (filled/closed)


class AutoUpdater:
    """Keeps the feed current. `make_watchlist()` returns a fresh Watchlist (opened per pass,
    so the background thread never shares a SQLite connection with request threads).
    `refresh_fn(watchlist)` pulls jobs; `discover_fn()` (optional) finds new boards."""

    def __init__(self, make_watchlist, refresh_fn, discover_fn=None,
                 refresh_every=REFRESH_EVERY, discover_every=DISCOVER_EVERY,
                 prune_days=PRUNE_DAYS, fast_fn=None, fast_every=FAST_REFRESH_EVERY,
                 clock=time.monotonic, log=print):
        self._make_watchlist = make_watchlist
        self._refresh_fn = refresh_fn
        self._discover_fn = discover_fn
        self._refresh_every = refresh_every
        self._discover_every = discover_every
        self._prune_days = prune_days
        # Optional light loop (e.g. freehire-only) on its own faster cadence. Off when fast_fn is
        # None or fast_every <= 0, so existing single-loop callers are unchanged.
        self._fast_fn = fast_fn
        self._fast_every = fast_every
        self._clock = clock
        self._log = log
        self._last_discover = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fast_thread: threading.Thread | None = None

    def run_once(self) -> dict:
        """One maintenance pass (refresh -> prune -> maybe discover). NEVER raises: a bad
        board or a network blip must not kill the loop, so failures are logged and skipped."""
        summary: dict = {}
        w = self._make_watchlist()
        try:
            summary["refresh"] = self._refresh_fn(w)
            summary["pruned"] = w.prune_stale(self._prune_days)
        except Exception:                                    # noqa: BLE001 - keep the loop alive
            traceback.print_exc()
            summary["error"] = "refresh"
        finally:
            try:
                w.close()
            except Exception:                                # noqa: BLE001
                pass
        if self._discover_fn and (self._clock() - self._last_discover) >= self._discover_every:
            try:
                summary["discover"] = self._discover_fn()
            except Exception:                                # noqa: BLE001
                traceback.print_exc()
                summary["error"] = "discover"
            self._last_discover = self._clock()              # even on failure, don't hammer
        return summary

    def run_fast_once(self) -> dict:
        """One LIGHT pass (the fast feed only). NEVER raises, for the same reason run_once doesn't."""
        w = self._make_watchlist()
        try:
            return {"fast": self._fast_fn(w)}
        except Exception:                                    # noqa: BLE001 - keep the loop alive
            traceback.print_exc()
            return {"error": "fast"}
        finally:
            try:
                w.close()
            except Exception:                                # noqa: BLE001
                pass

    def _fast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_fast_once()
            except Exception:                                # noqa: BLE001 - belt and suspenders
                traceback.print_exc()
            self._stop.wait(self._fast_every)                # sleeps, but wakes instantly on stop()

    def _loop(self) -> None:
        # Do NOT run the heavy discovery sweep on the first pass: on a shared server that would
        # spike load the moment the robot is switched on. The first pass just refreshes the
        # seeded boards (light); the first discovery waits one `discover_every`.
        self._last_discover = self._clock()
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:                                # noqa: BLE001 - belt and suspenders
                traceback.print_exc()
            self._stop.wait(self._refresh_every)             # sleeps, but wakes instantly on stop()

    def start(self) -> None:
        """Launch the background daemon thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-updater", daemon=True)
        self._thread.start()
        self._log("[auto-updater] started: refresh every "
                  f"{self._refresh_every // 3600}h, discover every "
                  f"{self._discover_every // 86400}d, prune after {self._prune_days}d")
        if self._fast_fn and self._fast_every > 0:
            self._fast_thread = threading.Thread(target=self._fast_loop,
                                                 name="auto-updater-fast", daemon=True)
            self._fast_thread.start()
            self._log(f"[auto-updater] fast feed loop every {self._fast_every // 60}m")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in (self._thread, self._fast_thread):
            if t:
                t.join(timeout=timeout)
