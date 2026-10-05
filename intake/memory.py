"""Persistent local memory for the intelligent intake (CLAUDE.md §4b).

The simplest store that reliably remembers a person across sessions: one SQLite
row per person holding their full PROFILE, the accumulated ESSENTIALS (the facts
the agent extracted), and the CONVERSATION history. On return, the agent loads
this and re-asks nothing — it only asks about what's new or changed, and refines
the stored profile as more is shared.

Deliberately not a knowledge graph — a JSON blob per person is enough and stays
easy to reason about. Local-first: nothing leaves the machine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Structured facts / preferences / constraints the LLM distills from the chat. This is the
# AUTHORITATIVE layer (MemPalace stays the episodic transcript + semantic recall): a row can be
# deduped, updated, retracted, and read deterministically at build time (e.g. honor an active
# "don't mention X" constraint via WHERE status='active', which semantic recall can miss).
FACT_TYPES = ("fact", "preference", "constraint")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ConversationMemory:
    db_path: str | Path

    def __post_init__(self) -> None:
        self.db_path = str(self.db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the threaded web server share one connection; the lock
        # below then SERIALIZES every execute+commit so concurrent request threads can't raise
        # ("recursive use"/"changed thread") or interleave a write and drop an autosave.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_memory (
                name        TEXT PRIMARY KEY,
                profile     TEXT,
                essentials  TEXT,
                history     TEXT,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Authoritative structured facts/preferences, one row per item (see FACT_TYPES). Deduped
        # by (name, type, key); UNIQUE makes the upsert an update-in-place, not a duplicate.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_facts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                provenance  TEXT,
                confidence  REAL DEFAULT 1.0,
                status      TEXT DEFAULT 'active',
                created_at  TEXT,
                updated_at  TEXT,
                UNIQUE(name, type, key)
            )
            """
        )
        self._conn.commit()

    def exists(self, name: str = "default") -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM conversation_memory WHERE name=?", (name,)
            ).fetchone()
        return row is not None

    def save(self, name: str, profile: dict, essentials: dict, history: list) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversation_memory(name, profile, essentials, history) "
                "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "profile=excluded.profile, essentials=excluded.essentials, "
                "history=excluded.history, updated_at=CURRENT_TIMESTAMP",
                (name, json.dumps(profile), json.dumps(essentials), json.dumps(history)),
            )
            self._conn.commit()

    def delete(self, name: str) -> None:
        """Drop a person's row. Used to clean up throwaway scratch profiles (e.g. the
        auto-apply batch, which tailors into a temporary row so the real 'default'
        profile is never mutated)."""
        with self._lock:
            self._conn.execute("DELETE FROM conversation_memory WHERE name=?", (name,))
            self._conn.commit()

    def load(self, name: str = "default") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT profile, essentials, history FROM conversation_memory WHERE name=?",
                (name,),
            ).fetchone()
        if not row:
            return None
        return {
            "profile": json.loads(row[0]) if row[0] else {},
            "essentials": json.loads(row[1]) if row[1] else {},
            "history": json.loads(row[2]) if row[2] else [],
        }

    # ---------------------------------------------------------------- structured facts
    @staticmethod
    def _fact_row(r) -> dict:
        return {"id": r[0], "name": r[1], "type": r[2], "key": r[3], "value": r[4],
                "provenance": r[5], "confidence": r[6], "status": r[7],
                "created_at": r[8], "updated_at": r[9]}

    _FACT_COLS = "id, name, type, key, value, provenance, confidence, status, created_at, updated_at"

    def upsert_fact(self, name: str, type: str, key: str, value: str,
                    provenance: str = "", confidence: float = 1.0) -> dict:
        """Insert a fact/preference/constraint, or UPDATE the existing one with the same
        (name, type, key). Re-adding reactivates a retracted row (the person said it again).
        Returns the stored row. Empty key or unknown type is ignored (returns {})."""
        key = (key or "").strip().lower()
        type = (type or "").strip().lower()
        if not key or type not in FACT_TYPES:
            return {}
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_facts(name, type, key, value, provenance, confidence, "
                "status, created_at, updated_at) VALUES(?,?,?,?,?,?,'active',?,?) "
                "ON CONFLICT(name, type, key) DO UPDATE SET value=excluded.value, "
                "provenance=excluded.provenance, confidence=excluded.confidence, "
                "status='active', updated_at=excluded.updated_at",
                (name, type, key, value, provenance, confidence, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                f"SELECT {self._FACT_COLS} FROM memory_facts WHERE name=? AND type=? AND key=?",
                (name, type, key),
            ).fetchone()
        return self._fact_row(row) if row else {}

    def list_facts(self, name: str = "default", status: str | None = "active",
                   types: tuple | None = None) -> list[dict]:
        """Facts for a person. status=None returns every row (for export); a type filter
        narrows to e.g. preferences+constraints. Newest first."""
        q = f"SELECT {self._FACT_COLS} FROM memory_facts WHERE name=?"
        args: list = [name]
        if status is not None:
            q += " AND status=?"
            args.append(status)
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args.extend(types)
        q += " ORDER BY updated_at DESC, id DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._fact_row(r) for r in rows]

    def active_preferences(self, name: str = "default") -> list[dict]:
        """Active preferences AND constraints, the rules the tailoring selection must honor."""
        return self.list_facts(name, status="active", types=("preference", "constraint"))

    def retract_fact(self, name: str, fact_id: int) -> None:
        """Soft-delete: mark a row retracted (it changed / is no longer true) but keep it."""
        with self._lock:
            self._conn.execute(
                "UPDATE memory_facts SET status='retracted', updated_at=? WHERE name=? AND id=?",
                (_now_iso(), name, fact_id),
            )
            self._conn.commit()

    def delete_fact(self, name: str, fact_id: int) -> None:
        """Hard-delete one fact (user control)."""
        with self._lock:
            self._conn.execute("DELETE FROM memory_facts WHERE name=? AND id=?", (name, fact_id))
            self._conn.commit()

    def erase_facts(self, name: str = "default") -> int:
        """Delete every fact for a person (part of a full memory erase). Returns rows removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM memory_facts WHERE name=?", (name,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        self._conn.close()
