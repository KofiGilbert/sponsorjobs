"""SQLite-backed UsageStore for the managed-AI broker (backend/metering.py).

InMemoryStore is the offline test double; this is the persistent store a real deployment uses so
a user's pass, usage, and purchased credits survive a broker restart. It implements the exact
same UsageStore contract, so the Meter and broker are unchanged: swap InMemoryStore for this and
nothing else moves. SQLite matches the app's existing local-state choice (CLAUDE.md sec 5).

Connections are opened per operation (metering write volume is tiny) so it is safe under Flask's
threaded dev server without a shared-connection thread guard. Counters use upserts; credits are
clamped at zero, mirroring InMemoryStore semantics exactly.
"""

from __future__ import annotations

import sqlite3

from backend.metering import DEFAULT_PLAN, LEGACY_PLANS, UsageStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user           TEXT PRIMARY KEY,
    plan           TEXT NOT NULL DEFAULT '{default}',
    credit_seconds INTEGER NOT NULL DEFAULT 0,
    pass_until     INTEGER NOT NULL DEFAULT 0,
    pass_seconds   INTEGER NOT NULL DEFAULT 0,
    pass_packages  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    user        TEXT NOT NULL,
    period      TEXT NOT NULL,
    avatar_used INTEGER NOT NULL DEFAULT 0,
    llm_tokens  INTEGER NOT NULL DEFAULT 0,
    packages    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user, period)
);
CREATE TABLE IF NOT EXISTS processed_events (
    key  TEXT PRIMARY KEY,
    at   INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
""".format(default=DEFAULT_PLAN)


class SqliteUsageStore(UsageStore):
    def __init__(self, path: str) -> None:
        self.path = str(path)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Bring a DB made before the passes (2026-10-02) up to date: add the pass columns and the
        free-package counter, and move accounts on a retired subscription tier to free."""
        def cols(table):
            return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        have = cols("users")
        for col in ("pass_until", "pass_seconds", "pass_packages"):
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if "packages" not in cols("usage"):
            c.execute("ALTER TABLE usage ADD COLUMN packages INTEGER NOT NULL DEFAULT 0")
        marks = ",".join("?" for _ in LEGACY_PLANS)
        c.execute(f"UPDATE users SET plan=? WHERE plan IN ({marks})", (DEFAULT_PLAN, *LEGACY_PLANS))

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA journal_mode=WAL")   # concurrent readers while a writer commits
        return c

    # -- plan --------------------------------------------------------------- #
    def get_plan_name(self, user: str) -> str:
        with self._conn() as c:
            row = c.execute("SELECT plan FROM users WHERE user=?", (user,)).fetchone()
        return row[0] if row else DEFAULT_PLAN

    def set_plan_name(self, user: str, plan: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO users(user, plan) VALUES(?, ?) "
                "ON CONFLICT(user) DO UPDATE SET plan=excluded.plan",
                (user, plan))

    # -- the pass (per-user; does not reset with the month) ------------------ #
    def get_pass(self, user: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT plan, pass_until, pass_seconds, pass_packages FROM users "
                            "WHERE user=?", (user,)).fetchone()
        if not row:
            return {"name": DEFAULT_PLAN, "until": 0, "seconds": 0, "packages": 0}
        return {"name": row[0], "until": row[1], "seconds": row[2], "packages": row[3]}

    def set_pass(self, user: str, name: str, until: int, seconds: int, packages: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO users(user, plan, pass_until, pass_seconds, pass_packages) "
                "VALUES(?, ?, ?, MAX(0, ?), MAX(0, ?)) ON CONFLICT(user) DO UPDATE SET "
                "plan=excluded.plan, pass_until=excluded.pass_until, "
                "pass_seconds=excluded.pass_seconds, pass_packages=excluded.pass_packages",
                (user, name, int(until), int(seconds), int(packages)))

    def add_pass_balance(self, user: str, seconds: int, packages: int) -> None:
        with self._conn() as c:   # one atomic statement, clamped at zero
            c.execute("UPDATE users SET pass_seconds = MAX(0, pass_seconds + ?), "
                      "pass_packages = MAX(0, pass_packages + ?) WHERE user=?",
                      (int(seconds), int(packages), user))

    # -- per-period counters ------------------------------------------------ #
    def get_avatar_used(self, user: str, period: str) -> int:
        return self._get_counter(user, period, "avatar_used")

    def add_avatar_used(self, user: str, period: str, seconds: int) -> None:
        self._add_counter(user, period, "avatar_used", seconds)

    def get_llm_tokens(self, user: str, period: str) -> int:
        return self._get_counter(user, period, "llm_tokens")

    def add_llm_tokens(self, user: str, period: str, tokens: int) -> None:
        self._add_counter(user, period, "llm_tokens", tokens)

    def get_packages_used(self, user: str, period: str) -> int:
        return self._get_counter(user, period, "packages")

    def add_packages_used(self, user: str, period: str, n: int) -> None:
        self._add_counter(user, period, "packages", n)

    _COUNTER_COLS = ("avatar_used", "llm_tokens", "packages")

    def _get_counter(self, user: str, period: str, col: str) -> int:
        assert col in self._COUNTER_COLS   # col is a fixed internal literal, never user input; guard anyway
        with self._conn() as c:
            row = c.execute(
                f"SELECT {col} FROM usage WHERE user=? AND period=?", (user, period)).fetchone()
        return row[0] if row else 0

    def _add_counter(self, user: str, period: str, col: str, amount: int) -> None:
        assert col in self._COUNTER_COLS   # col is a fixed internal literal (avatar_used | llm_tokens | packages)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO usage(user, period, {col}) VALUES(?, ?, ?) "
                f"ON CONFLICT(user, period) DO UPDATE SET {col} = {col} + excluded.{col}",
                (user, period, amount))

    # -- purchased credits (per-user, do not reset; clamp at zero) ---------- #
    def get_credit_seconds(self, user: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT credit_seconds FROM users WHERE user=?", (user,)).fetchone()
        return row[0] if row else 0

    def add_credit_seconds(self, user: str, seconds: int) -> None:
        # One atomic statement (clamp + increment in SQL) so a concurrent purchase and heartbeat
        # deduction can't lose an update via read-modify-write. Insert clamps; conflict increments.
        with self._conn() as c:
            c.execute(
                "INSERT INTO users(user, credit_seconds) VALUES(?, MAX(0, ?)) "
                "ON CONFLICT(user) DO UPDATE SET credit_seconds = MAX(0, credit_seconds + ?)",
                (user, seconds, seconds))

    # -- idempotency for Stripe webhooks (a credit pack is granted once per session id) -- #
    def has_processed(self, key: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM processed_events WHERE key=?", (key,)).fetchone() is not None

    def mark_processed(self, key: str) -> bool:
        """Record ``key`` atomically; True when it was new, False when already processed (so two
        concurrent deliveries of the same event cannot both grant the pack)."""
        with self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO processed_events(key) VALUES(?)", (key,))
            return cur.rowcount == 1


# --------------------------------------------------------------------------------------------- #
# The Telegram relay's tables (backend/telegram_relay.py, docs/notify.md). Same DB file, its own
# tables, and nothing else: a hashed one-time link code, the account -> chat binding, and the
# button taps / replies waiting for the app to poll (deleted on ack or after 7 days).
# --------------------------------------------------------------------------------------------- #
_TELEGRAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_links (
    code       TEXT PRIMARY KEY,            -- sha256 of the one-time code, never the code itself
    account    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_chats (
    account   TEXT PRIMARY KEY,
    chat_id   INTEGER NOT NULL UNIQUE,
    username  TEXT,
    paused    INTEGER NOT NULL DEFAULT 0,
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '',
    text    TEXT NOT NULL DEFAULT '',
    at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS telegram_events_account ON telegram_events(account, id);
"""


class SqliteTelegramStore:
    """Persistent twin of telegram_relay.InMemoryTelegramStore (same method contract)."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        with self._conn() as c:
            c.executescript(_TELEGRAM_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def add_link(self, code_hash: str, account: str, expires_at: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO telegram_links(code, account, expires_at) VALUES(?, ?, ?)",
                      (code_hash, account, expires_at))

    def take_link(self, code_hash: str, now: float) -> str | None:
        with self._conn() as c:
            c.execute("DELETE FROM telegram_links WHERE expires_at <= ?", (now,))
            row = c.execute("SELECT account FROM telegram_links WHERE code = ?", (code_hash,)).fetchone()
            if not row:
                return None
            cur = c.execute("DELETE FROM telegram_links WHERE code = ?", (code_hash,))
            return row[0] if cur.rowcount == 1 else None      # one-time even under a race

    def bind_chat(self, account: str, chat_id: int, username: str | None, linked_at: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM telegram_chats WHERE chat_id = ? OR account = ?", (chat_id, account))
            c.execute("INSERT INTO telegram_chats(account, chat_id, username, paused, linked_at) "
                      "VALUES(?, ?, ?, 0, ?)", (account, chat_id, username, linked_at))

    def get_chat(self, account: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT chat_id, username, paused, linked_at FROM telegram_chats "
                            "WHERE account = ?", (account,)).fetchone()
        if not row:
            return None
        return {"chat_id": row[0], "username": row[1], "paused": bool(row[2]), "linked_at": row[3]}

    def account_for_chat(self, chat_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT account FROM telegram_chats WHERE chat_id = ?", (chat_id,)).fetchone()
        return row[0] if row else None

    def set_paused(self, account: str, paused: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE telegram_chats SET paused = ? WHERE account = ?", (1 if paused else 0, account))

    def unbind(self, account: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM telegram_chats WHERE account = ?", (account,))
            c.execute("DELETE FROM telegram_events WHERE account = ?", (account,))

    def add_event(self, account: str, type_: str, data: str, text: str, at: float) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO telegram_events(account, type, data, text, at) VALUES(?, ?, ?, ?, ?)",
                            (account, type_, data or "", text or "", at))
            return int(cur.lastrowid)

    def ack_and_list(self, account: str, after: int, older_than: float, limit: int) -> list[dict]:
        with self._conn() as c:
            c.execute("DELETE FROM telegram_events WHERE at < ?", (older_than,))
            c.execute("DELETE FROM telegram_events WHERE account = ? AND id <= ?", (account, after))
            rows = c.execute("SELECT id, type, data, text, at FROM telegram_events WHERE account = ? "
                             "ORDER BY id LIMIT ?", (account, limit)).fetchall()
        return [{"id": r[0], "account": account, "type": r[1], "data": r[2], "text": r[3], "at": r[4]}
                for r in rows]
