"""The in-app mailbox on the person's own machine (docs/mail-alias.md).

Mail sent to the person's SponsorJobs address is held by the broker only until this app has
downloaded it (inbox/relay.py ``sync``); from then on it lives here, in a small SQLite file in
the app's data directory, and nowhere else. One row per message, keyed by the broker's cursor
so a page that is downloaded twice is stored once.

A deleted message keeps a bare tombstone row (cursor and flags, no sender, subject or body), so
a re-sent page can never bring it back.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .labels import LABELS

SNIPPET = 140

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_cursor    INTEGER NOT NULL UNIQUE,
    alias            TEXT NOT NULL DEFAULT '',
    sender           TEXT NOT NULL DEFAULT '',
    sender_name      TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    text             TEXT NOT NULL DEFAULT '',
    html             TEXT,
    received_at      INTEGER NOT NULL DEFAULT 0,
    read             INTEGER NOT NULL DEFAULT 0,
    archived         INTEGER NOT NULL DEFAULT 0,
    deleted          INTEGER NOT NULL DEFAULT 0,
    label            TEXT NOT NULL DEFAULT 'other',
    label_source     TEXT NOT NULL DEFAULT 'auto',
    record_id        INTEGER,
    job_id           TEXT,
    company          TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT '',
    matched_by       TEXT NOT NULL DEFAULT '',
    has_attachments  INTEGER NOT NULL DEFAULT 0,
    attachment_names TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS messages_listing ON messages(deleted, archived, received_at);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_LIST_COLS = ("id, remote_cursor, alias, sender, sender_name, subject, substr(text, 1, 600) AS head, "
              "received_at, read, archived, label, label_source, record_id, job_id, company, role, "
              "matched_by, has_attachments")
_FULL_COLS = ("id, remote_cursor, alias, sender, sender_name, subject, text, html, received_at, read, "
              "archived, label, label_source, record_id, job_id, company, role, matched_by, "
              "has_attachments, attachment_names")


def snippet_of(text: str, n: int = SNIPPET) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n].rstrip() + "..."


def _names(raw) -> list[str]:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in v if isinstance(x, str)] if isinstance(v, list) else []


class MailboxStore:
    """The local mailbox. Safe to use from the request threads and the sync thread at once."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sync state ----------------------------------------------------------------------------
    def cursor(self, alias: str = "") -> int:
        """The highest broker cursor whose message is committed here: what the next sync call
        acknowledges (and so lets the broker delete). Kept per alias: a cursor learned on one
        account must never acknowledge mail on another that this machine has not downloaded."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM sync_state WHERE key=?", (f"cursor:{alias}",)).fetchone()
        try:
            return max(0, int(row["value"])) if row else 0
        except (TypeError, ValueError):
            return 0

    def state(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO sync_state(key, value) VALUES(?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self._conn.commit()

    def add_page(self, messages: list[dict], cursor: int, alias: str = "") -> list[int]:
        """Write one downloaded page and move the cursor, in ONE transaction, and return the
        local ids of the messages that are new. A message whose cursor is already here (the
        page was fetched before a crash and fetched again) is left exactly as it is."""
        new: list[int] = []
        with self._lock:
            try:
                for m in messages or []:
                    if not isinstance(m, dict):
                        continue
                    try:
                        rc = int(m.get("cursor"))
                    except (TypeError, ValueError):
                        continue
                    names = [str(n)[:200] for n in (m.get("attachment_names") or []) if isinstance(n, str)][:50]
                    try:
                        at = int(m.get("at") or 0)
                    except (TypeError, ValueError):
                        at = 0
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO messages(remote_cursor, alias, sender, sender_name, subject, text, "
                        "html, received_at, has_attachments, attachment_names) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rc, alias or "", str(m.get("sender") or "")[:320], str(m.get("sender_name") or "")[:200],
                         str(m.get("subject") or "")[:998], str(m.get("text") or ""),
                         (str(m["html"]) if m.get("html") else None), at or int(time.time()),
                         1 if (m.get("has_attachments") or names) else 0, json.dumps(names)))
                    if cur.rowcount:
                        new.append(int(cur.lastrowid))
                have = self.cursor(alias)
                self._conn.execute(
                    "INSERT INTO sync_state(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"cursor:{alias}", str(max(have, int(cursor or 0)))))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return new

    def ids_after(self, local_id) -> list[int]:
        """The local ids above ``local_id`` (oldest first): what has been stored since."""
        try:
            floor = int(local_id or 0)
        except (TypeError, ValueError):
            floor = 0
        with self._lock:
            return [r["id"] for r in self._conn.execute(
                "SELECT id FROM messages WHERE id > ? AND deleted=0 ORDER BY id", (floor,))]

    # -- labels and matches --------------------------------------------------------------------
    def set_auto(self, mid: int, *, label: str | None = None, record_id=None, job_id: str | None = None,
                 company: str | None = None, role: str | None = None, matched_by: str | None = None) -> None:
        """What the app worked out for a message: its label and its application. An automatic
        label never replaces one the person set."""
        with self._lock:
            if label in LABELS:
                self._conn.execute("UPDATE messages SET label=?, label_source='auto' "
                                   "WHERE id=? AND label_source != 'person'", (label, mid))
            if job_id is not None:
                self._conn.execute("UPDATE messages SET record_id=?, job_id=?, company=?, role=?, matched_by=? WHERE id=?",
                                   (record_id, job_id or None, company or "", role or "", matched_by or "", mid))
            self._conn.commit()

    def update(self, mid: int, *, read=None, archived=None, label=None) -> dict | None:
        """The person's own changes. A label set here is theirs and is never overwritten."""
        if label is not None and label not in LABELS:
            raise ValueError(f"label must be one of {', '.join(LABELS)}")
        with self._lock:
            if read is not None:
                self._conn.execute("UPDATE messages SET read=? WHERE id=? AND deleted=0", (1 if read else 0, mid))
            if archived is not None:
                self._conn.execute("UPDATE messages SET archived=? WHERE id=? AND deleted=0", (1 if archived else 0, mid))
            if label is not None:
                self._conn.execute("UPDATE messages SET label=?, label_source='person' WHERE id=? AND deleted=0",
                                   (label, mid))
            self._conn.commit()
        return self.get(mid)

    def mark_all_read(self) -> int:
        with self._lock:
            n = self._conn.execute("UPDATE messages SET read=1 WHERE read=0 AND deleted=0").rowcount
            self._conn.commit()
        return n

    def delete(self, mid: int) -> bool:
        """Remove a message for good. Its content is wiped; only the cursor stays, as a tombstone."""
        with self._lock:
            n = self._conn.execute(
                "UPDATE messages SET deleted=1, sender='', sender_name='', subject='', text='', html=NULL, "
                "attachment_names='[]', company='', role='', job_id=NULL, record_id=NULL "
                "WHERE id=? AND deleted=0", (mid,)).rowcount
            self._conn.commit()
        return bool(n)

    # -- reading -------------------------------------------------------------------------------
    @staticmethod
    def _list_row(r) -> dict:
        return {"id": r["id"], "sender": r["sender"], "sender_name": r["sender_name"], "subject": r["subject"],
                "snippet": snippet_of(r["head"]), "at": r["received_at"], "read": bool(r["read"]),
                "archived": bool(r["archived"]), "label": r["label"], "label_source": r["label_source"],
                "record_id": r["record_id"], "job_id": r["job_id"], "company": r["company"], "role": r["role"],
                "has_attachments": bool(r["has_attachments"])}

    def get(self, mid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {_FULL_COLS} FROM messages WHERE id=? AND deleted=0", (mid,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "cursor": r["remote_cursor"], "alias": r["alias"], "sender": r["sender"],
                "sender_name": r["sender_name"], "subject": r["subject"], "snippet": snippet_of(r["text"][:600]),
                "at": r["received_at"], "read": bool(r["read"]), "archived": bool(r["archived"]),
                "label": r["label"], "label_source": r["label_source"], "record_id": r["record_id"],
                "job_id": r["job_id"], "company": r["company"], "role": r["role"], "matched_by": r["matched_by"],
                "has_attachments": bool(r["has_attachments"]), "text": r["text"], "html": r["html"],
                "attachment_names": _names(r["attachment_names"])}

    def list(self, *, label: str = "", unread: bool = False, q: str = "", archived: bool = False,
             page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
        """(one page of messages newest first, how many match in all)."""
        where, params = ["deleted=0", "archived=?"], [1 if archived else 0]
        if label in LABELS:
            where.append("label=?"); params.append(label)
        if unread:
            where.append("read=0")
        q = " ".join(str(q or "").split())[:200]
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(subject LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' OR sender_name LIKE ? ESCAPE '\\' "
                         "OR company LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\')")
            params += [like] * 5
        per_page = max(1, min(200, int(per_page or 50)))
        page = max(1, int(page or 1))
        cond = " AND ".join(where)
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) AS n FROM messages WHERE {cond}", params).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT {_LIST_COLS} FROM messages WHERE {cond} ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page]).fetchall()
        return [self._list_row(r) for r in rows], int(total)

    def unread_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE deleted=0 AND archived=0 AND read=0").fetchone()["n"])

    def label_counts(self) -> dict[str, int]:
        """How many messages (not archived) carry each label."""
        out = {name: 0 for name in LABELS}
        with self._lock:
            for r in self._conn.execute(
                    "SELECT label, COUNT(*) AS n FROM messages WHERE deleted=0 AND archived=0 GROUP BY label"):
                if r["label"] in out:
                    out[r["label"]] = int(r["n"])
        return out

    def export(self) -> list[dict]:
        """Every kept message without its HTML, for the person's own data export."""
        with self._lock:
            ids = [r["id"] for r in self._conn.execute("SELECT id FROM messages WHERE deleted=0 ORDER BY received_at")]
        out = []
        for mid in ids:
            m = self.get(mid)
            if m:
                m.pop("html", None)
                out.append(m)
        return out
