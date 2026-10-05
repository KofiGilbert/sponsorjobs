"""Volume cap + spacing for sanctioned submissions (CLAUDE.md §7).

Two courtesy controls so an approved batch doesn't fire all at once at a sanctioned API:

  * a hard **daily volume cap** (default 40/day) — a safety ceiling on how many
    applications we submit in a day, and
  * a minimum **spacing** between submissions (with a little bounded jitter) so we don't
    hammer a site's API in a burst.

This is rate-limit COURTESY for the site's OWN sanctioned API (CLAUDE.md §7 says to
"respect the site's rate limits"), NOT anti-detection evasion — we only ever submit through
verified, sanctioned channels, we identify honestly, and there is no stealth/prohibited-site
behavior here (that stays on the RED list). ``now``/``jitter`` are injected so the whole
thing is deterministic under test.

State persists to a small local JSON file: ``{"date": "YYYY-MM-DD", "count": int,
"last_ts": float}``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CAP = 40                # applications/day (competitive range ~30-50)
DEFAULT_MIN_GAP = 45.0          # seconds between submissions (base)
DEFAULT_JITTER = 20.0           # +[0, jitter) seconds, so it isn't a robotic exact interval


class RateLimiter:
    def __init__(self, store_path, cap: int = DEFAULT_CAP, min_gap: float = DEFAULT_MIN_GAP,
                 jitter: float = DEFAULT_JITTER, jitter_fn=None):
        self.store_path = Path(store_path)
        self.cap = int(cap)
        self.min_gap = float(min_gap)
        self.jitter = float(jitter)
        # A deterministic 0..1 source in tests; a bounded pseudo-jitter otherwise.
        self._jitter_fn = jitter_fn or (lambda: 0.5)

    def _load(self) -> dict:
        try:
            return json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"date": "", "count": 0, "last_ts": 0.0}   # no store yet — a fresh day
        except (OSError, ValueError):
            # The file EXISTS but is unreadable/corrupt (e.g. a truncated write from a crash).
            # Fail CLOSED: never silently reset the day's count to 0, which would re-open a
            # whole day's cap after a crash mid-day. allow() denies until the store is sound.
            return {"date": "", "count": 0, "last_ts": 0.0, "_corrupt": True}

    def _save(self, state: dict) -> None:
        # Atomic write: a crash mid-save leaves EITHER the old or the new complete file,
        # never a truncated one — so the daily cap can't be reset to 0 by a partial write.
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.parent / (self.store_path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.store_path)

    def _gap(self) -> float:
        return self.min_gap + self.jitter * max(0.0, min(1.0, self._jitter_fn()))

    def status(self, now_ts: float, today: str) -> dict:
        s = self._load()
        if s.get("_corrupt"):     # unreadable store — surface as fully spent (fail closed)
            return {"count": self.cap, "cap": self.cap, "remaining": 0, "seconds_until_next": 0.0}
        count = s["count"] if s.get("date") == today else 0
        remaining = max(0, self.cap - count)
        since = now_ts - float(s.get("last_ts") or 0.0)
        return {"count": count, "cap": self.cap, "remaining": remaining,
                "seconds_until_next": max(0.0, self._gap() - since) if s.get("last_ts") else 0.0}

    def allow(self, now_ts: float, today: str) -> tuple[bool, str]:
        """Can we submit right now? Checks the daily cap then the spacing. No side effects
        (call ``record``/``record_attempt`` after an attempt actually happens)."""
        s = self._load()
        if s.get("_corrupt"):     # can't verify the cap -> refuse rather than risk exceeding it
            return False, "store_unreadable"
        count = s["count"] if s.get("date") == today else 0
        if count >= self.cap:
            return False, "daily_cap"
        last = float(s.get("last_ts") or 0.0)
        if last and (now_ts - last) < self._gap():
            return False, "spacing"
        return True, "ok"

    def record(self, now_ts: float, today: str) -> None:
        """Count one SUCCESSFUL submission (advances the daily counter AND the spacing clock)."""
        s = self._load()
        count = (s["count"] if s.get("date") == today else 0) + 1
        self._save({"date": today, "count": count, "last_ts": now_ts})

    def record_attempt(self, now_ts: float, today: str) -> None:
        """Advance only the SPACING clock for an attempt that actually hit the site's API but
        did NOT succeed (a failure, a 429, or a needs-assist abort AFTER a live request). The
        daily counter is untouched — but the min-gap now applies to the next attempt, so a run
        of failing/rate-limited items can't hammer the API back-to-back (§7 'respect the site's
        rate limits'; it also honors the site's own 429 back-pressure with spacing)."""
        s = self._load()
        if s.get("_corrupt"):
            return
        count = s["count"] if s.get("date") == today else 0
        self._save({"date": today, "count": count, "last_ts": now_ts})
