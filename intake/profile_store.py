"""Saved & reusable PROFILE storage (CLAUDE.md §4b).

The person answers intake once; their answers are saved locally (SQLite) and
reused. On a new application they can:

* **reuse**  — take the saved profile as-is,
* **refresh** — replace it with fresh information,
* **add**    — merge new/adjusted fields on top of what's saved.

All state is local; there is no server (CLAUDE.md §5).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = "data/resume_agent.db"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins on conflicts).

    Lists are replaced, not concatenated — the person's newest answer for a
    field is authoritative.
    """
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class ProfileStore:
    db_path: str | Path = DEFAULT_DB

    def __post_init__(self) -> None:
        self.db_path = str(self.db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the local web server serves requests on
        # different threads but access is effectively serialized (single user).
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile (
                name       TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.commit()

    # -- basic io -------------------------------------------------------- #
    def save(self, profile: dict, name: str = "default") -> None:
        self._conn.execute(
            "INSERT INTO profile(name, data) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET data=excluded.data, "
            "updated_at=CURRENT_TIMESTAMP",
            (name, json.dumps(profile)),
        )
        self._conn.commit()

    def load(self, name: str = "default") -> dict | None:
        row = self._conn.execute(
            "SELECT data FROM profile WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def exists(self, name: str = "default") -> bool:
        return self.load(name) is not None

    # -- the three modes (§4b) ------------------------------------------ #
    def reuse(self, name: str = "default") -> dict:
        """Return the saved profile as-is."""
        profile = self.load(name)
        if profile is None:
            raise KeyError(f"no saved profile named {name!r}")
        return profile

    def refresh(self, fresh: dict, name: str = "default") -> dict:
        """Replace the saved profile entirely with ``fresh`` and return it."""
        self.save(fresh, name)
        return fresh

    def add(self, additions: dict, name: str = "default") -> dict:
        """Merge ``additions`` onto the saved profile, persist, and return it."""
        base = self.load(name) or {}
        merged = _deep_merge(base, additions)
        self.save(merged, name)
        return merged

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProfileStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
