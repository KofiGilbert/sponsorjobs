"""Local store for produced-CV records that populate the dashboard.

Separate from the PROFILE store (which holds the reusable answers): this records
each *output* — the job it was tailored to, the role/company, the JD-match, and
the path to the compiled PDF. Local SQLite, same local-first design as the rest.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CVRecords:
    db_path: str | Path

    def __post_init__(self) -> None:
        self.db_path = str(self.db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cv_record (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                role       TEXT NOT NULL,
                company    TEXT,
                coverage   REAL,
                pdf_path   TEXT,
                jd_label   TEXT,
                data       TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._conn.commit()

    def add(self, role: str, company: str, coverage: float, pdf_path: str,
            jd_label: str = "", data: dict | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO cv_record(role, company, coverage, pdf_path, jd_label, data) "
            "VALUES(?,?,?,?,?,?)",
            (role, company, coverage, pdf_path, jd_label, json.dumps(data or {})),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM cv_record ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, record_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM cv_record WHERE id=?", (record_id,)
        ).fetchone()
        return dict(row) if row else None

    def merge_data(self, record_id: int, patch: dict) -> dict:
        """Shallow-merge ``patch`` into a record's JSON ``data`` bag and persist it.
        The package screen keeps a record's cover letter, screening answers, coverage
        detail and applied-status here (no schema migration needed). Returns the merged
        data."""
        row = self.get(record_id)
        if row is None:
            raise KeyError(f"no cv_record {record_id}")
        data = {}
        try:
            data = json.loads(row.get("data") or "{}")
        except (TypeError, ValueError):
            data = {}
        data.update(patch)
        self._conn.execute("UPDATE cv_record SET data=? WHERE id=?",
                           (json.dumps(data), record_id))
        self._conn.commit()
        return data

    def set_coverage(self, record_id: int, coverage: float) -> bool:
        """Update the JD-match column after an accepted edit changed the CV."""
        cur = self._conn.execute("UPDATE cv_record SET coverage=? WHERE id=?",
                                 (float(coverage), record_id))
        self._conn.commit()
        return cur.rowcount > 0

    def rename(self, record_id: int, role: str) -> bool:
        """Rename a record (the Saved CVs manager's inline rename)."""
        cur = self._conn.execute("UPDATE cv_record SET role=? WHERE id=?",
                                 (role, record_id))
        self._conn.commit()
        return cur.rowcount > 0

    def delete(self, record_id: int) -> bool:
        """Remove a record. Returns True if a row was deleted. (The caller cleans up the
        record's artifact files — the compiled CV / cover-letter PDF + text.)"""
        cur = self._conn.execute("DELETE FROM cv_record WHERE id=?", (record_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
