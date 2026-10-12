"""Saved search strategies: a named set of Jobs-page filters, kept locally, with a per-strategy
"new since you last looked" digest and plain search URLs for the other boards.

Three pieces, deliberately separate:

* ``StrategyStore``: a small SQLite table (same local db as the watchlist) holding each strategy's
  name, its filters (the same facets ``sourcing.filters.apply_facets`` takes), which one is the
  default, and ``last_seen``: when the person last looked at that strategy's results.
* The "open the same search on the other boards" links live in ``ui/boardlinks.py``, not here:
  sourcing/ never names those sites (the RED-lane guard in tests/test_sourcing.py keeps it so).
* ``new_since``: the digest. Jobs from OUR OWN feed that first arrived after the strategy's
  ``last_seen`` and match its filters. Pure, takes the job list and the clock as inputs so it is
  testable with pinned timestamps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sourcing.filters import apply_facets

# The facet fields a strategy stores, with their neutral (off) values. Anything else a client
# sends is dropped, so a stale or hand-edited body cannot smuggle unknown keys into the store.
FILTER_FIELDS: dict[str, object] = {
    "q": "", "loc": "", "days": 0, "remote": "", "visa": [], "level": "", "pay": 0,
    "sponsored": False,
}

def _now_iso(now: datetime | None = None) -> str:
    """UTC timestamp in the watchlist's own format ('YYYY-MM-DD HH:MM:SS'), so first_seen and
    last_seen compare as plain strings AND as datetimes."""
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s) -> datetime | None:
    """A naive-UTC datetime from any timestamp shape our feeds write: sqlite's CURRENT_TIMESTAMP
    ('2026-10-11 09:30:00'), ISO with a T, a trailing Z or offset, fractional seconds, or a bare
    date. None when it is blank or unreadable (an unreadable first_seen is never counted as new)."""
    if isinstance(s, datetime):
        dt = s
    else:
        s = (str(s or "")).strip()
        if not s:
            return None
        s = s.replace("T", " ")
        if s.endswith("Z"):
            s = s[:-1]
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def normalize_filters(raw: dict | None) -> dict:
    """Coerce a client body into the stored filter shape: known keys only, typed, trimmed."""
    raw = raw or {}
    out: dict = {}
    out["q"] = str(raw.get("q") or "").strip()
    out["loc"] = str(raw.get("loc") or "").strip()
    try:
        out["days"] = max(0, int(raw.get("days") or 0))
    except (TypeError, ValueError):
        out["days"] = 0
    out["remote"] = str(raw.get("remote") or "").strip().lower()
    visa = raw.get("visa") or []
    if isinstance(visa, str):
        visa = visa.split(",")
    out["visa"] = [str(v).strip() for v in visa if str(v).strip()]
    out["level"] = str(raw.get("level") or "").strip().lower()
    try:
        out["pay"] = max(0, int(raw.get("pay") or 0))
    except (TypeError, ValueError):
        out["pay"] = 0
    out["sponsored"] = str(raw.get("sponsored", "")).strip().lower() in ("1", "true", "yes", "on")
    return out


# ---------------------------------------------------------------- the digest (pure)
def new_since(jobs: list[dict], strategy: dict) -> list[dict]:
    """Jobs from our feed that first arrived AFTER the strategy's last_seen and match its filters,
    newest first. The strategy's date facet is replaced by the last_seen cutoff (that IS the
    window), every other facet applies as on the board. A job with no readable first_seen is not
    new (we cannot say when it arrived), and a strategy with no last_seen has nothing new, so one
    that was never looked at starts clean rather than dumping the whole board on the person."""
    since = parse_ts(strategy.get("last_seen"))
    if since is None:
        return []
    f = dict(FILTER_FIELDS)
    f.update(normalize_filters(strategy.get("filters") or {}))
    f["days"] = 0
    fresh = []
    for j in jobs:
        seen = parse_ts(j.get("first_seen"))
        if seen is not None and seen > since:
            fresh.append(j)
    matched = apply_facets(fresh, q=f["q"], loc=f["loc"], days=0, remote=f["remote"],
                           visa=f["visa"], level=f["level"], pay=f["pay"], sponsored=f["sponsored"])
    matched.sort(key=lambda j: str(j.get("first_seen") or ""), reverse=True)
    return matched


def digest(jobs: list[dict], strategy: dict, limit: int = 30) -> dict:
    """The /new payload: count, the newest arrival stamp, and the first `limit` rows."""
    rows = new_since(jobs, strategy)
    return {"strategy_id": strategy.get("id"), "since": strategy.get("last_seen"),
            "count": len(rows),
            "newest_at": (str(rows[0].get("first_seen")) if rows else None),
            "jobs": rows[:max(0, int(limit))]}


# ---------------------------------------------------------------- the store
class StrategyStore:
    """Named filter sets in the local SQLite db. One row per strategy; at most one is default."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=8000")
        self._migrate()

    def _migrate(self) -> None:
        """Create the table, then add any column an older db lacks (same ALTER-on-missing pattern
        the watchlist uses, so an upgrade never needs a manual step)."""
        c = self._conn
        c.execute("""CREATE TABLE IF NOT EXISTS search_strategy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            filters TEXT NOT NULL DEFAULT '{}',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(search_strategy)")}
        # `last_seen`: when the person last looked at this strategy's results. The digest counts
        # arrivals after it. Older rows get their updated_at, so they do not start with a flood.
        if "last_seen" not in cols:
            c.execute("ALTER TABLE search_strategy ADD COLUMN last_seen TEXT")
            c.execute("UPDATE search_strategy SET last_seen=updated_at WHERE last_seen IS NULL")
        c.commit()

    @staticmethod
    def _row(r) -> dict:
        try:
            filters = json.loads(r["filters"] or "{}")
        except (TypeError, ValueError):
            filters = {}
        f = dict(FILTER_FIELDS)
        f.update(normalize_filters(filters))
        return {"id": r["id"], "name": r["name"], "filters": f,
                "is_default": bool(r["is_default"]), "created_at": r["created_at"],
                "updated_at": r["updated_at"], "last_seen": r["last_seen"]}

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM search_strategy ORDER BY is_default DESC, name COLLATE NOCASE").fetchall()
        return [self._row(r) for r in rows]

    def get(self, sid: int) -> dict | None:
        r = self._conn.execute("SELECT * FROM search_strategy WHERE id=?", (int(sid),)).fetchone()
        return self._row(r) if r else None

    def default(self) -> dict | None:
        r = self._conn.execute("SELECT * FROM search_strategy WHERE is_default=1 LIMIT 1").fetchone()
        return self._row(r) if r else None

    def create(self, name: str, filters: dict | None, is_default: bool = False,
               now: datetime | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("a strategy needs a name")
        ts = _now_iso(now)
        f = normalize_filters(filters)
        c = self._conn
        if is_default:
            c.execute("UPDATE search_strategy SET is_default=0")
        cur = c.execute(
            "INSERT INTO search_strategy (name, filters, is_default, created_at, updated_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name[:120], json.dumps(f), 1 if is_default else 0, ts, ts, ts))
        c.commit()
        return self.get(cur.lastrowid)

    def update(self, sid: int, name: str | None = None, filters: dict | None = None,
               is_default: bool | None = None, now: datetime | None = None) -> dict | None:
        cur = self.get(sid)
        if cur is None:
            return None
        ts = _now_iso(now)
        sets, vals = ["updated_at=?"], [ts]
        if name is not None and name.strip():
            sets.append("name=?"); vals.append(name.strip()[:120])
        if filters is not None:
            sets.append("filters=?"); vals.append(json.dumps(normalize_filters(filters)))
        c = self._conn
        if is_default is not None:
            if is_default:
                c.execute("UPDATE search_strategy SET is_default=0")
            sets.append("is_default=?"); vals.append(1 if is_default else 0)
        vals.append(int(sid))
        c.execute(f"UPDATE search_strategy SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        return self.get(sid)

    def set_default(self, sid: int) -> dict | None:
        return self.update(sid, is_default=True)

    def mark_seen(self, sid: int, now: datetime | None = None) -> dict | None:
        if self.get(sid) is None:
            return None
        self._conn.execute("UPDATE search_strategy SET last_seen=? WHERE id=?", (_now_iso(now), int(sid)))
        self._conn.commit()
        return self.get(sid)

    def delete(self, sid: int) -> bool:
        cur = self._conn.execute("DELETE FROM search_strategy WHERE id=?", (int(sid),))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:   # noqa: BLE001 - closing twice is fine
            pass
