"""The applicant brain's store: ``data/applicant.db`` (docs/applicant-brain.md, section 5).

One SQLite file, Python's built-in ``sqlite3``. One connection per process per file, in WAL
mode, behind one lock (the ConversationMemory pattern). The whole schema (version 1) is created
here, so later phases fill tables and never change them.

Three rules live in this file, not in its callers:

* sensitivity is set from the key registry (applicant/keys.py); a caller cannot pass it;
* a self-identification (EEO) value is refused from chat, resume, drafted, derived or the
  model; a legal value from the resume or chat waits as ``needs_check``;
* forgetting is a hard delete: the fact, its replaced history and the fill rows pointing at it.

The event log keeps ids, keys and kinds, never a value.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from applicant import keys as K

SCHEMA_VERSION = 1
PERSON = "default"

# The legacy home a fact belongs to while the brain runs beside the older stores (Phase 1 to 6):
# the answer library, the prefs file, or none (a brain-only fact). Each legacy view reads only
# its own facts, so every answer stays exactly what that store gave before; a write to such a
# fact is mirrored one way into that file. Phase 3 joins the views into one truth.
STORE_ANSWERS = "answers.db"
STORE_PREFS = "app_prefs.json"
STORES = (STORE_ANSWERS, STORE_PREFS, "")

ACTIVE, REPLACED, NEEDS_CHECK = "active", "replaced", "needs_check"
CHECK_REASONS = ("raw_reply", "conflict", "expired", "unconfirmed_legal", "drafted_eeo", "from_chat")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    words TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'you',
    source_ref TEXT NOT NULL DEFAULT '',
    store TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    confirmed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    check_reason TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    replaced_by INTEGER,
    uses INTEGER NOT NULL DEFAULT 0,
    last_used REAL,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS fact_lookup ON fact (person, key, subject, status);
CREATE INDEX IF NOT EXISTS fact_ref ON fact (source_ref);
CREATE INDEX IF NOT EXISTS fact_store ON fact (person, store, status);

CREATE TABLE IF NOT EXISTS rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'default',
    action TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '[]',
    scope_kind TEXT NOT NULL DEFAULT 'all',
    scope_value TEXT NOT NULL DEFAULT '',
    words TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    times_applied INTEGER NOT NULL DEFAULT 0,
    last_applied_at REAL,
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS application (
    id INTEGER PRIMARY KEY,
    person TEXT NOT NULL DEFAULT 'default',
    company TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    job_family TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    company_type TEXT NOT NULL DEFAULT '',
    shown TEXT NOT NULL DEFAULT '[]',
    rules TEXT NOT NULL DEFAULT '[]',
    created_at REAL,
    submitted_at REAL
);

CREATE TABLE IF NOT EXISTS fill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'default',
    application_id INTEGER,
    label TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL DEFAULT '',
    fact_id INTEGER,
    origin TEXT NOT NULL DEFAULT '',
    at REAL
);
CREATE INDEX IF NOT EXISTS fill_fact ON fill (fact_id);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    at REAL
);

CREATE TABLE IF NOT EXISTS document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'app',
    facts INTEGER NOT NULL DEFAULT 0,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS label_map (
    label TEXT NOT NULL,
    options_hash TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    how TEXT NOT NULL DEFAULT 'rule',
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (label, options_hash)
);

CREATE TABLE IF NOT EXISTS company_class (
    company TEXT PRIMARY KEY,
    industry TEXT NOT NULL DEFAULT '',
    industry_label TEXT NOT NULL DEFAULT '',
    industry_source TEXT NOT NULL DEFAULT '',
    company_type TEXT NOT NULL DEFAULT 'unknown',
    type_source TEXT NOT NULL DEFAULT '',
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

TABLES = ("fact", "rule", "application", "fill", "event", "document", "label_map", "company_class", "meta")


class RefusedWrite(ValueError):
    """The store refused a write (an EEO value from a source that may not set one)."""


class Store:
    """The brain's tables over one SQLite file. Thread-safe: one connection, one lock."""

    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        # isolation_level=None: we open and close transactions ourselves (tx()), so a whole
        # migration is one transaction that a failed check rolls back.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
        # A forgotten value is overwritten on disk, not left in a free page ("forget my
        # ethnicity" must really delete).
        self.conn.execute("PRAGMA secure_delete=ON")
        self.conn.executescript(_SCHEMA)
        if self.meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ------------------------------------------------------------------ plumbing
    @property
    def lock(self):
        """The store's one lock. Hold it across a brain write and its mirror, or a legacy read
        and its catch-up, so no other thread lands between the two."""
        return self._lock

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    @contextmanager
    def tx(self):
        """One transaction; nested calls join the outer one. An exception rolls it back."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    def q(self, sql: str, args=()) -> list:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def meta(self, k: str, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def set_meta(self, k: str, v) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, None if v is None else str(v)))

    # ------------------------------------------------------------------ events (ids and kinds only)
    def log(self, kind: str, ref: str = "", **detail) -> None:
        """An audit line. ``detail`` holds counts, keys and ids only; a caller never passes a
        value (and the store keeps only numbers, short ids and booleans to be sure)."""
        safe = {k: v for k, v in detail.items()
                if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 80 and k in _ID_FIELDS)}
        with self.tx() as c:
            c.execute("INSERT INTO event (person, kind, ref, detail, at) VALUES (?,?,?,?,?)",
                      (PERSON, kind, str(ref or "")[:200], json.dumps(safe), time.time()))

    # ------------------------------------------------------------------ facts
    def add_fact(self, key: str, value, *, source: str, subject: str = "", store: str = "",
                 question: str = "", words: str = "", code: str = "", source_ref: str = "",
                 confidence: float | None = None, confirmed: int | None = None, status: str = ACTIVE,
                 check_reason: str = "", valid_from: str = "", valid_to: str = "", uses: int = 0,
                 last_used=None, created_at=None, updated_at=None, replace: bool = True,
                 legacy_import: bool = False, sensitivity=None) -> int:
        """Add one value. For a single-valued key, the current value of the same key, subject
        and store is marked replaced (kept as history) when ``replace``. Returns the new id.

        ``sensitivity`` is accepted only to be ignored: it always comes from the registry.
        ``legacy_import`` copies a row that already exists in a legacy store exactly as it is
        there (the migration and the catch-up): an EEO answer a legacy store holds from a source
        the brain would refuse is kept as ``needs_check`` (``drafted_eeo``) rather than lost,
        because the legacy store still hands it out until Phase 3 asks the person."""
        del sensitivity                                        # never the caller's to set
        key = str(key or "").strip()
        if not key:
            raise ValueError("a fact needs a key")
        source = str(source or "you")
        sens = K.sensitivity(key, question)
        if sens == K.EEO:
            words = ""                                         # never kept for EEO keys
            if source in K.EEO_REFUSED_SOURCES:
                if not legacy_import:
                    raise RefusedWrite(f"{key}: a self-identification answer never comes from {source}")
                status, check_reason = NEEDS_CHECK, "drafted_eeo"
        if sens == K.LEGAL and source in K.LEGAL_PROPOSAL_SOURCES and status == ACTIVE:
            status, check_reason = NEEDS_CHECK, "unconfirmed_legal"
        conf = K.confidence_for(source) if confidence is None else float(confidence)
        conf_flag = (1 if source not in K.UNCONFIRMED_SOURCES else 0) if confirmed is None else int(confirmed)
        now = time.time()
        val = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        with self.tx() as c:
            if replace and not K.is_multi(key) and status == ACTIVE:
                # (Phase 3) A value from a source less sure than the person's confirmed current
                # value never replaces it: it waits as needs_check (conflict) for them to settle.
                top = c.execute("SELECT confidence, value FROM fact WHERE person=? AND key=? AND subject=? AND store=?"
                                " AND status=? AND confirmed=1 ORDER BY id DESC LIMIT 1",
                                (PERSON, key, str(subject or ""), store, ACTIVE)).fetchone()
                if top is not None and float(top["confidence"] or 0) > conf and top["value"] != val:
                    status, check_reason, replace = NEEDS_CHECK, "conflict", False
            cur = c.execute(
                "INSERT INTO fact (person, key, subject, value, code, question, words, source, source_ref,"
                " store, confidence, confirmed, status, check_reason, sensitivity, valid_from, valid_to,"
                " uses, last_used, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PERSON, key, str(subject or ""), val, str(code or ""), str(question or ""), str(words or ""),
                 source, str(source_ref or ""), store, conf, conf_flag, status, check_reason, sens,
                 str(valid_from or ""), str(valid_to or ""), int(uses or 0), last_used,
                 now if created_at is None else created_at, now if updated_at is None else updated_at))
            new_id = int(cur.lastrowid)
            if replace and not K.is_multi(key):
                c.execute("UPDATE fact SET status=?, replaced_by=? WHERE person=? AND key=? AND subject=?"
                          " AND store=? AND id<>? AND status IN (?,?)",
                          (REPLACED, new_id, PERSON, key, str(subject or ""), store, new_id, ACTIVE, NEEDS_CHECK))
        return new_id

    def update_fact(self, fact_id: int, **cols) -> None:
        """Change bookkeeping columns of one fact in place (uses, last_used, status, a confirmation
        that raises its confidence). A changed VALUE is a new fact (add_fact), never an update."""
        allowed = {"uses", "last_used", "status", "check_reason", "confirmed", "code", "source_ref", "confidence"}
        sets = {k: v for k, v in cols.items() if k in allowed}
        if not sets:
            return
        if sets.get("status") == REPLACED:
            sets.setdefault("updated_at", time.time())
        with self.tx() as c:
            c.execute(f"UPDATE fact SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                      (*sets.values(), int(fact_id)))

    def current(self, key: str, subject: str = "", store: str = "") -> list:
        """The current rows (active or needs_check) of a key, subject and store, newest first."""
        return self.q("SELECT * FROM fact WHERE person=? AND key=? AND subject=? AND store=?"
                      " AND status IN (?,?) ORDER BY id DESC", (PERSON, key, subject, store, ACTIVE, NEEDS_CHECK))

    def facts(self, *, store: str | None = None, key: str | None = None, status=None) -> list:
        sql, args = "SELECT * FROM fact WHERE person=?", [PERSON]
        if store is not None:
            sql += " AND store=?"
            args.append(store)
        if key is not None:
            sql += " AND key=?"
            args.append(key)
        if status is not None:
            sts = (status,) if isinstance(status, str) else tuple(status)
            sql += f" AND status IN ({','.join('?' * len(sts))})"
            args.extend(sts)
        return self.q(sql + " ORDER BY id", args)

    def forget(self, key: str, subject: str = "", store: str = "") -> int:
        """Hard delete every row of a key, subject and store (the value and its replaced
        history) and the fill rows that point at them. Returns how many facts went."""
        with self.tx() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM fact WHERE person=? AND key=? AND subject=? AND store=?",
                                           (PERSON, key, subject, store)).fetchall()]
            if not ids:
                return 0
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM fill WHERE fact_id IN ({marks})", ids)
            c.execute(f"DELETE FROM fact WHERE id IN ({marks})", ids)
        self.log("fact_forgotten", key=key if K.sensitivity(key) != K.EEO else "eeo", n=len(ids))
        return len(ids)

    def forget_id(self, fact_id: int) -> int:
        """Forget one fact by id, with its history (same key, subject and store)."""
        r = self.q("SELECT key, subject, store FROM fact WHERE id=?", (int(fact_id),))
        return self.forget(r[0]["key"], r[0]["subject"], r[0]["store"]) if r else 0

    # ------------------------------------------------------------------ status and export
    def counts(self) -> dict:
        """Counts only, never values: facts by status and by store, and rows per table."""
        out: dict = {"facts": {}, "by_store": {}, "tables": {}}
        for r in self.q("SELECT status, COUNT(*) n FROM fact GROUP BY status"):
            out["facts"][r["status"]] = r["n"]
        for r in self.q("SELECT store, COUNT(*) n FROM fact WHERE status<>'replaced' GROUP BY store"):
            out["by_store"][r["store"] or "brain"] = r["n"]
        for t in TABLES:
            if t != "meta":
                out["tables"][t] = self.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"]
        return out

    def export(self) -> dict:
        """Every table as JSON rows, for the person's own Export (local only)."""
        return {t: [dict(r) for r in self.q(f"SELECT * FROM {t}")] for t in TABLES}


_ID_FIELDS = frozenset({"key", "file", "store", "outcome", "source", "kind", "subject_kind"})

# ---------------------------------------------------------------------------- one per process
_OPEN: dict[str, Store] = {}
_OPEN_LOCK = threading.Lock()
_MAX_OPEN = 6


def open_store(path) -> Store:
    """The process's one Store for this file (opened on first use). A handful stay open at
    once; the oldest is closed when a test suite walks through many throwaway data folders."""
    p = str(Path(path).resolve()) if str(path) != ":memory:" else ":memory:"
    with _OPEN_LOCK:
        st = _OPEN.get(p)
        if st is None:
            st = Store(p)
            _OPEN[p] = st
            while len(_OPEN) > _MAX_OPEN:
                old = next(iter(_OPEN))
                _OPEN.pop(old).close()
        else:
            _OPEN[p] = _OPEN.pop(p)                    # most recently used last
        return st


def close_store(path) -> None:
    """Close and forget the process's Store for this file (before Erase deletes it)."""
    p = str(Path(path).resolve())
    with _OPEN_LOCK:
        st = _OPEN.pop(p, None)
    if st is not None:
        st.close()


def db_files(path) -> list[Path]:
    """The brain file and the WAL files SQLite keeps beside it."""
    p = Path(path)
    return [p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")]
