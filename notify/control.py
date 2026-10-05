"""Persisted control state for the apply engine (P3): the tiny shared store the Telegram
commands write and the auto-apply loop reads, so the person can stop / skip a run from their
phone while it's running in the app.

One small JSON file, atomic .tmp replace (matching the preps/reviews/screens stores). Two things
live here:

* ``paused`` - the run halts gracefully after the current item until ``/resume``.
* ``skip`` - a one-shot "skip the current item" flag, plus the set of specific application ids the
  person asked to skip.

Handling notes are NOT here: they belong ON the application record (see ``merge_data``), so they
travel with the record and are read when it is (re)built.
"""

from __future__ import annotations

import json
from pathlib import Path


class ControlState:
    def __init__(self, path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- pause / resume ---------------------------------------------------- #
    def paused(self) -> bool:
        return bool(self._load().get("paused"))

    def set_paused(self, value: bool) -> None:
        d = self._load()
        d["paused"] = bool(value)
        self._save(d)

    # -- skip -------------------------------------------------------------- #
    def request_skip(self, rid=None) -> None:
        """No id -> skip the current in-progress item (one shot). An id -> add that specific
        application to the skip set so the engine and queue leave it alone."""
        d = self._load()
        if rid is None:
            d["skip_current"] = True
        else:
            ids = set(d.get("skip_ids") or [])
            ids.add(int(rid))
            d["skip_ids"] = sorted(ids)
        self._save(d)

    def take_skip_current(self) -> bool:
        """Read AND clear the one-shot skip-current flag, so it applies to exactly one item."""
        d = self._load()
        if d.get("skip_current"):
            d["skip_current"] = False
            self._save(d)
            return True
        return False

    def is_skipped(self, rid) -> bool:
        try:
            return int(rid) in set(self._load().get("skip_ids") or [])
        except (TypeError, ValueError):
            return False

    def skipped_ids(self) -> list:
        return list(self._load().get("skip_ids") or [])
