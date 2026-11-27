"""The Orb's local tables, in the app's own SQLite file.

* orb_nudge: one row per nudge (dedup_key unique): pending -> shown -> acted | dismissed | expired.
* orb_event: an append-only log of what the Orb did, ids and kinds only (never text), kept 180 days.
* orb_pref: small settings and preferences (on/off, muted, a kind turned off for a week).
* orb_usage: the chat's model turns and their estimated cost per day (the daily spend cap).
* orb_chat / orb_memory: the open chat's last turns, and short topic summaries of closed chats.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from orb import rules

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orb_nudge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    dedup_key   TEXT NOT NULL UNIQUE,
    priority    INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    shown_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    expires_at  TEXT,
    action_json TEXT
);
CREATE INDEX IF NOT EXISTS orb_nudge_status ON orb_nudge(status);
CREATE TABLE IF NOT EXISTS orb_event (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS orb_event_at ON orb_event(at);
CREATE TABLE IF NOT EXISTS orb_pref (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS orb_usage (
    day      TEXT PRIMARY KEY,
    ai_turns INTEGER NOT NULL DEFAULT 0,
    cents    REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orb_chat (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orb_memory (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    topics TEXT NOT NULL
);
"""

_COLS = "id, type, dedup_key, priority, text, created_at, shown_at, status, expires_at, action_json"


class OrbStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ prefs
    def pref(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM orb_pref WHERE key=?", (key,)).fetchone()
        if not row or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set_pref(self, key: str, value, now: datetime | None = None) -> None:
        now = now or datetime.now()
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM orb_pref WHERE key=?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO orb_pref(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE "
                    "SET value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), rules.iso(now)))
            self._conn.commit()

    def snoozed(self, now: datetime) -> set[str]:
        """The kinds turned off for now (dismissed twice in a row): a preference kept here, never in
        the memory facts, which feed the resume prompts."""
        out = set()
        for kind in rules.KINDS:
            until = rules.parse(self.pref(f"snooze:{kind}"))
            if until and until > now:
                out.add(kind)
        return out

    # ------------------------------------------------------------------ events
    def log(self, kind: str, ref: str = "", now: datetime | None = None) -> None:
        """Append one event: ids and kinds only, never what was said."""
        now = now or datetime.now()
        with self._lock:
            self._conn.execute("INSERT INTO orb_event(at, kind, ref) VALUES(?,?,?)",
                               (rules.iso(now), str(kind)[:40], str(ref or "")[:80]))
            self._conn.commit()

    def prune(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        cut = rules.iso(now - rules.EVENT_KEEP)
        with self._lock:
            n = self._conn.execute("DELETE FROM orb_event WHERE at < ?", (cut,)).rowcount
            n += self._conn.execute("DELETE FROM orb_nudge WHERE status IN ('expired','acted','dismissed') "
                                    "AND created_at < ?", (cut,)).rowcount
            self._conn.commit()
        return n

    def count_events(self, kind: str, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM orb_event WHERE kind=? AND at >= ?",
                                     (kind, rules.iso(since))).fetchone()
        return int(row[0] or 0)

    # ------------------------------------------------------------------ nudges
    @staticmethod
    def _row(r) -> dict:
        d = dict(zip(("id", "type", "dedup_key", "priority", "text", "created_at", "shown_at", "status",
                      "expires_at", "action_json"), r))
        try:
            d["action"] = json.loads(d.pop("action_json") or "null")
        except ValueError:
            d["action"] = None
        return d

    def nudges(self, statuses=("pending", "shown")) -> list[dict]:
        q = f"SELECT {_COLS} FROM orb_nudge"
        args: list = []
        if statuses:
            q += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = list(statuses)
        with self._lock:
            return [self._row(r) for r in self._conn.execute(q + " ORDER BY id", args).fetchall()]

    def get(self, nid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {_COLS} FROM orb_nudge WHERE id=?", (int(nid),)).fetchone()
        return self._row(r) if r else None

    def sync(self, candidates: list[dict], now: datetime) -> None:
        """Bring the table in line with what the data says right now.

        New keys become pending. A shown one whose data still holds comes back after its kind's
        repeat window. A pending or shown one whose data no longer holds (the follow-up was sent,
        the resume was used) or whose lifetime ran out expires."""
        keys = {c["key"]: c for c in candidates if c.get("key") and c.get("type") in rules.KINDS}
        stamp = rules.iso(now)
        with self._lock:
            have = {r[2]: self._row(r) for r in self._conn.execute(f"SELECT {_COLS} FROM orb_nudge").fetchall()}
            for key, c in keys.items():
                old = have.get(key)
                action = json.dumps(c.get("action")) if c.get("action") else None
                if old is None:
                    self._conn.execute(
                        "INSERT INTO orb_nudge(type, dedup_key, priority, text, created_at, status, expires_at, "
                        "action_json) VALUES(?,?,?,?,?,'pending',?,?)",
                        (c["type"], key, rules.KINDS[c["type"]]["priority"], str(c.get("text") or "")[:160],
                         stamp, rules.expires_at(c["type"], now), action))
                # shown (or acted on: the follow-up drafted but the data says it is still due) comes
                # back after its kind's repeat window
                elif old["status"] in ("shown", "acted") and rules.may_repeat(old["type"], old["shown_at"], now):
                    self._conn.execute("UPDATE orb_nudge SET status='pending', text=?, action_json=?, expires_at=? "
                                       "WHERE id=?", (str(c.get("text") or "")[:160], action,
                                                      rules.expires_at(c["type"], now), old["id"]))
                elif old["status"] in ("pending", "shown"):
                    self._conn.execute("UPDATE orb_nudge SET text=?, action_json=? WHERE id=?",
                                       (str(c.get("text") or "")[:160], action, old["id"]))
            for key, old in have.items():
                if old["status"] not in ("pending", "shown"):
                    continue
                gone = key not in keys
                late = (rules.parse(old["expires_at"]) or now + timedelta(days=1)) <= now
                if gone or late:
                    self._conn.execute("UPDATE orb_nudge SET status='expired' WHERE id=?", (old["id"],))
            self._conn.commit()

    def shown_today(self, now: datetime) -> tuple[int, str | None]:
        """Unasked bubbles shown today, and when the last one showed."""
        day = now.date().isoformat()
        with self._lock:
            n = self._conn.execute("SELECT COUNT(*) FROM orb_event WHERE kind='nudge_shown' AND substr(at,1,10)=?",
                                   (day,)).fetchone()[0]
            last = self._conn.execute("SELECT MAX(at) FROM orb_event WHERE kind='nudge_shown'").fetchone()[0]
        return int(n or 0), last

    def shown_kinds_today(self, now: datetime) -> dict[str, int]:
        """How many bubbles of each kind showed today (the event's ref is "kind:id")."""
        day = now.date().isoformat()
        with self._lock:
            rows = self._conn.execute("SELECT ref FROM orb_event WHERE kind='nudge_shown' AND substr(at,1,10)=?",
                                      (day,)).fetchall()
        out: dict[str, int] = {}
        for (ref,) in rows:
            k = str(ref or "").split(":")[0]
            if k:
                out[k] = out.get(k, 0) + 1
        return out

    def mark(self, nid: int, status: str, now: datetime) -> dict | None:
        """shown | acted | dismissed. Two dismissals of one kind in a row turn it off for a week."""
        row = self.get(nid)
        if not row or status not in ("shown", "acted", "dismissed"):
            return None
        with self._lock:
            if status == "shown":
                self._conn.execute("UPDATE orb_nudge SET status='shown', shown_at=? WHERE id=?",
                                   (rules.iso(now), row["id"]))
            else:
                self._conn.execute("UPDATE orb_nudge SET status=? WHERE id=?", (status, row["id"]))
            self._conn.commit()
        self.log("nudge_" + status, f"{row['type']}:{row['id']}", now)
        if status == "acted":
            self.set_pref(f"dismissed:{row['type']}", None, now)
        elif status == "dismissed":
            n = int(self.pref(f"dismissed:{row['type']}", 0) or 0) + 1
            if n >= rules.SNOOZE_AFTER:
                self.set_pref(f"snooze:{row['type']}", rules.iso(now + rules.SNOOZE_FOR), now)
                self.set_pref(f"dismissed:{row['type']}", None, now)
                self.log("nudge_snoozed", row["type"], now)
            else:
                self.set_pref(f"dismissed:{row['type']}", n, now)
        return self.get(nid)

    # ------------------------------------------------------------------ chat limits
    def chat_mode(self, now: datetime) -> str:
        last_hour = self.count_events("chat", now - timedelta(hours=1))
        with self._lock:
            row = self._conn.execute("SELECT cents FROM orb_usage WHERE day=?", (now.date().isoformat(),)).fetchone()
        return rules.chat_mode(last_hour=last_hour, spent_cents=float(row[0]) if row else 0.0)

    def add_spend(self, now: datetime, cents: float = rules.CHAT_TURN_CENTS) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO orb_usage(day, ai_turns, cents) VALUES(?,1,?) ON CONFLICT(day) DO UPDATE "
                               "SET ai_turns=ai_turns+1, cents=cents+excluded.cents", (now.date().isoformat(), cents))
            self._conn.commit()

    # ------------------------------------------------------------------ chat memory
    def add_turn(self, role: str, text: str, now: datetime) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO orb_chat(at, role, text) VALUES(?,?,?)",
                               (rules.iso(now), "you" if role == "user" else "assistant", str(text or "")[:600]))
            self._conn.execute("DELETE FROM orb_chat WHERE id NOT IN (SELECT id FROM orb_chat ORDER BY id DESC LIMIT ?)",
                               (rules.CHAT_KEEP,))
            self._conn.commit()

    def turns(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT at, role, text FROM orb_chat ORDER BY id").fetchall()
        return [{"at": a, "role": r, "text": t} for a, r, t in rows]

    def close_chat(self, topics: str, now: datetime) -> None:
        """The chat closed: its turns become one short topics line, the raw turns go."""
        with self._lock:
            if topics.strip():
                self._conn.execute("INSERT INTO orb_memory(at, topics) VALUES(?,?)", (rules.iso(now), topics.strip()[:400]))
                self._conn.execute("DELETE FROM orb_memory WHERE id NOT IN "
                                   "(SELECT id FROM orb_memory ORDER BY id DESC LIMIT ?)", (rules.MEMORY_KEEP,))
            self._conn.execute("DELETE FROM orb_chat")
            self._conn.commit()

    def memory(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT at, topics FROM orb_memory ORDER BY id DESC").fetchall()
        return [{"at": a, "topics": t} for a, t in rows]

    def erase(self) -> None:
        with self._lock:
            for t in ("orb_nudge", "orb_event", "orb_usage", "orb_chat", "orb_memory"):
                self._conn.execute(f"DELETE FROM {t}")
            self._conn.commit()
