"""Local storage for the company watchlist, match criteria, and sourced jobs.

Local-first (CLAUDE.md §5): plain SQLite in the app's db. Jobs are deduped by a
stable ``source_id`` and flagged ``is_new`` on first appearance so the dashboard
can highlight fresh matches.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .quality import is_list_only

DEFAULT_CRITERIA = {"titles": [], "locations": [], "remote": "any"}


def _spon_int(v) -> int | None:
    """Normalize a sponsorship_stated value to the DB's tri-state int: 1 (stated yes), 0 (stated
    no), or None (posting silent / unknown). None never overwrites a known flag on re-ingest."""
    if v is None:
        return None
    return 1 if v else 0


def _spon_bool(v) -> bool | None:
    """The stored tri-state int back to True / False / None for the JSON the board serves."""
    return None if v is None else bool(v)


@dataclass
class Watchlist:
    db_path: str | Path

    def __post_init__(self) -> None:
        self.db_path = str(self.db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Concurrency: the crawler writes from a background thread while request threads read the SAME
        # file. In SQLite's default rollback-journal mode a writer BLOCKS every reader, which showed up
        # as "database is locked" 503s on the hosted feed once the fast refresh loop made writes
        # frequent. WAL lets readers keep serving the last committed snapshot WHILE a writer works;
        # busy_timeout makes the rare writer-vs-writer collision wait instead of failing immediately.
        # journal_mode=WAL persists on the file; the rest are per-connection. (:memory: can't use WAL
        # and has no cross-connection concurrency anyway, so it just gets the busy_timeout.)
        self._conn.execute("PRAGMA busy_timeout=8000")            # wait up to 8s for a lock
        if self.db_path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")   # safe under WAL, far less fsync stall
            except sqlite3.OperationalError:
                pass                                              # keep working even if a PRAGMA is refused
        c = self._conn
        c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
            ats TEXT NOT NULL, board_id TEXT NOT NULL, active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ats, board_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS criteria (
            id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS sourced_job (
            source_id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT,
            location TEXT, remote TEXT, url TEXT, jd_text TEXT, posted_at TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP, is_new INTEGER DEFAULT 1,
            dismissed INTEGER DEFAULT 0, last_seen TEXT DEFAULT CURRENT_TIMESTAMP)""")
        # `last_seen` (added later) marks the most recent refresh that still saw a job, so the
        # auto-updater can prune roles that have disappeared (filled/closed). Back-fill on
        # older DBs so pruning has a sane baseline instead of deleting everything.
        cols = {r[1] for r in c.execute("PRAGMA table_info(sourced_job)")}
        if "last_seen" not in cols:
            c.execute("ALTER TABLE sourced_job ADD COLUMN last_seen TEXT")
            c.execute("UPDATE sourced_job SET last_seen=COALESCE(first_seen, CURRENT_TIMESTAMP)")
        # `salary` (added later): the aggregator feeds carry a pay string; older DBs just get an
        # empty column, so the board shows "Salary TBD" until the row is next refreshed.
        if "salary" not in cols:
            c.execute("ALTER TABLE sourced_job ADD COLUMN salary TEXT DEFAULT ''")
        # `saved` (added later): the user's personal bookmark. Lives locally (never in the shared
        # kitchen) -- saving a kitchen role snapshots it into this table so the Saved list survives
        # even after the role ages out of the live feed.
        if "saved" not in cols:
            c.execute("ALTER TABLE sourced_job ADD COLUMN saved INTEGER DEFAULT 0")
        # `sponsorship_stated` (added later): whether the POSTING itself states visa sponsorship
        # (from freehire's enrichment). Tri-state: 1 = stated yes, 0 = stated no, NULL = the posting
        # is silent (unknown). Distinct from the DOL sponsor-history badge; older rows stay NULL
        # until their next refresh re-ingests the flag.
        if "sponsorship_stated" not in cols:
            c.execute("ALTER TABLE sourced_job ADD COLUMN sponsorship_stated INTEGER")
        c.commit()

    # -- watchlist companies -------------------------------------------- #
    def add_company(self, company: str, ats: str, board_id: str) -> None:
        from .ats import valid_board_id_for
        board_id = board_id.strip()
        if not valid_board_id_for(ats, board_id):   # never store a value that could escape the host on fetch
            raise ValueError(f"invalid board id {board_id!r}: must be a plain board slug")
        self._conn.execute(
            "INSERT INTO watchlist(company, ats, board_id) VALUES(?,?,?) "
            "ON CONFLICT(ats, board_id) DO UPDATE SET company=excluded.company, active=1",
            (company.strip(), ats.strip().lower(), board_id.strip()),
        )
        self._conn.commit()

    def remove_company(self, wid: int) -> None:
        self._conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
        self._conn.commit()

    def companies(self, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM watchlist"
        if active_only:
            q += " WHERE active=1"
        q += " ORDER BY company COLLATE NOCASE"
        return [dict(r) for r in self._conn.execute(q).fetchall()]

    def is_empty(self) -> bool:
        return self._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

    # -- criteria ------------------------------------------------------- #
    def get_criteria(self) -> dict:
        row = self._conn.execute("SELECT data FROM criteria WHERE id=1").fetchone()
        if not row:
            return dict(DEFAULT_CRITERIA)
        data = json.loads(row[0])
        return {**DEFAULT_CRITERIA, **data}

    def set_criteria(self, criteria: dict) -> None:
        clean = {
            "titles": [t.strip() for t in criteria.get("titles", []) if t.strip()],
            "locations": [l.strip() for l in criteria.get("locations", []) if l.strip()],
            "remote": criteria.get("remote", "any") or "any",
        }
        self._conn.execute(
            "INSERT INTO criteria(id, data) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (json.dumps(clean),),
        )
        self._conn.commit()

    # -- sourced jobs --------------------------------------------------- #
    def upsert_jobs(self, jobs: list[dict]) -> tuple[int, int]:
        """Insert matched jobs; keep existing (dedupe by source_id). Returns
        (new_count, total_seen)."""
        new = 0
        for j in jobs:
            spon = _spon_int(j.get("sponsorship_stated"))
            exists = self._conn.execute(
                "SELECT 1 FROM sourced_job WHERE source_id=?", (j["source_id"],)
            ).fetchone()
            if exists:
                # refresh volatile fields + stamp last_seen (still live), keep first_seen /
                # is_new / dismissed. COALESCE keeps a known sponsorship flag if a later pull is
                # silent (NULL), so a stated-sponsor role never loses its badge on re-ingest.
                # A blank jd_text never overwrites a stored one: list-only feeds (SmartRecruiters,
                # Workday) re-ingest rows without the body their detail fetch already filled.
                jd = j.get("jd_text") or ""
                if is_list_only(j) and not jd.strip():
                    # A list-only row WITHOUT a body is the bare list entry. Its url (the
                    # synthetic jobs.smartrecruiters.com/{board}/{id}), location ("3 Locations")
                    # and posted_at (the "30+ Days Ago" floor) are weaker than what the detail
                    # fetch already stored on this row, so they fill blanks only. A row that
                    # carries a body (the detail view persisting its enrichment) updates them.
                    self._conn.execute(
                        "UPDATE sourced_job SET title=?, "
                        "location=CASE WHEN location IS NULL OR location='' THEN ? ELSE location END, "
                        "remote=CASE WHEN remote IS NULL OR remote='' THEN ? ELSE remote END, "
                        "url=CASE WHEN url IS NULL OR url='' THEN ? ELSE url END, "
                        "posted_at=CASE WHEN posted_at IS NULL OR posted_at='' THEN ? ELSE posted_at END, "
                        "salary=?, sponsorship_stated=COALESCE(?, sponsorship_stated), "
                        "last_seen=CURRENT_TIMESTAMP WHERE source_id=?",
                        (j["title"], j["location"], j["remote"], j["url"], j["posted_at"],
                         j.get("salary", ""), spon, j["source_id"]),
                    )
                    continue
                self._conn.execute(
                    "UPDATE sourced_job SET title=?, location=?, remote=?, url=?, "
                    "jd_text=CASE WHEN ?<>'' THEN ? ELSE jd_text END, posted_at=?, salary=?, "
                    "sponsorship_stated=COALESCE(?, sponsorship_stated), "
                    "last_seen=CURRENT_TIMESTAMP WHERE source_id=?",
                    (j["title"], j["location"], j["remote"], j["url"],
                     jd, jd, j["posted_at"], j.get("salary", ""), spon, j["source_id"]),
                )
            else:
                new += 1
                self._conn.execute(
                    "INSERT INTO sourced_job(source_id, source, company, title, "
                    "location, remote, url, jd_text, posted_at, salary, sponsorship_stated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (j["source_id"], j["source"], j["company"], j["title"],
                     j["location"], j["remote"], j["url"], j["jd_text"], j["posted_at"],
                     j.get("salary", ""), spon),
                )
        self._conn.commit()
        return new, len(jobs)

    def prune_stale(self, days: int = 21) -> int:
        """Delete jobs not seen by a refresh in `days` days: a role that has vanished from
        its board is filled or closed, so keeping it would feed people dead links. Uses
        last_seen (stamped on every refresh that re-sees a job). Conservative window so a
        single missed refresh never drops a live role. Returns how many were pruned."""
        cur = self._conn.execute(
            "DELETE FROM sourced_job WHERE last_seen < datetime('now', ?)",
            (f"-{int(days)} days",))
        self._conn.commit()
        return cur.rowcount

    def list_jobs(self, include_dismissed: bool = False, order: str = "default",
                  limit: int | None = None, since_days: int | None = None,
                  saved_only: bool = False) -> list[dict]:
        """List sourced jobs.

        order="recent" sorts by the POSTING date (newest first), not first_seen (when WE
        happened to crawl it) -- so a job posted 5 months ago never sits on page 1 just
        because we discovered its board yesterday. Undated rows sink below dated ones.
        since_days drops DATED rows older than the window (a 4-month-old posting is usually
        filled/dead; undated rows are kept since we can't judge them). saved_only returns just
        the user's bookmarked roles (newest-saved first). limit caps the result so the hosted
        feed stays fast and bounded instead of recomputing over tens of thousands of rows."""
        # `has_jd`: whether the row holds a description, without shipping the description. A
        # list-only feed's row (Workday) is stored before its JD is fetched, and the accessibility
        # check reads the JD, so the board must know which rows have actually been checked
        # (sourcing.quality.is_jd_checked) -- the slim row carries that one bit, not the body.
        cols = ("source_id, source, company, title, location, remote, url, "
                "posted_at, salary, first_seen, is_new, dismissed, saved, sponsorship_stated, "
                "(jd_text IS NOT NULL AND jd_text <> '') AS has_jd")
        q = f"SELECT {cols} FROM sourced_job"
        conds, params = [], []
        if not include_dismissed:
            conds.append("dismissed=0")
        if saved_only:
            conds.append("saved=1")
        if since_days:
            conds.append("(posted_at IS NULL OR posted_at = '' "
                         "OR substr(posted_at, 1, 10) >= date('now', ?))")
            params.append(f"-{int(since_days)} days")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        if order == "recent":
            q += (" ORDER BY (posted_at IS NOT NULL AND posted_at != '') DESC, "
                  "posted_at DESC, first_seen DESC")
        else:
            q += " ORDER BY is_new DESC, first_seen DESC, company COLLATE NOCASE"
        if limit:
            q += f" LIMIT {int(limit)}"
        out = []
        for r in self._conn.execute(q, params).fetchall():
            d = dict(r)
            d["sponsorship_stated"] = _spon_bool(d.get("sponsorship_stated"))
            d["has_jd"] = bool(d.get("has_jd"))
            out.append(d)
        return out

    def get_job(self, source_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM sourced_job WHERE source_id=?", (source_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["sponsorship_stated"] = _spon_bool(d.get("sponsorship_stated"))
        return d

    def source_ids_with_jd(self, source: str) -> set[str]:
        """source_ids of one feed's rows that already hold a description -- what a list-only
        crawl (Workday) may skip fetching detail for."""
        return {r[0] for r in self._conn.execute(
            "SELECT source_id FROM sourced_job WHERE source=? AND jd_text<>''", (source,)).fetchall()}

    def get_jd_texts(self, source_ids) -> dict[str, str]:
        """{source_id: jd_text} for the given ids (chunked so a 50k-row list never exceeds SQLite's
        bound-parameter limit). Used by the static-feed builder to shard the descriptions."""
        ids = [s for s in source_ids if s]
        out: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for sid, jd in self._conn.execute(
                    f"SELECT source_id, jd_text FROM sourced_job WHERE source_id IN ({marks})",
                    chunk).fetchall():
                out[sid] = jd or ""
        return out

    def backfill_salaries(self, extract) -> int:
        """Fill the salary column for rows that have a JD but no stored pay, using `extract`
        (salary_from_text). Network-free and cheap on repeat (only touches empty-salary rows), so
        a minimum-pay filter and top-paid sort work on rows stored before ingest-time extraction
        existed, without waiting for a full re-fetch. `extract` is injected to keep this module
        free of a sourcing.ats import. Returns how many rows were filled."""
        rows = self._conn.execute(
            "SELECT source_id, jd_text FROM sourced_job "
            "WHERE (salary IS NULL OR salary='') AND jd_text IS NOT NULL AND jd_text!=''"
        ).fetchall()
        n = 0
        for r in rows:
            s = extract(r["jd_text"])
            if s:
                self._conn.execute("UPDATE sourced_job SET salary=? WHERE source_id=?",
                                   (s, r["source_id"]))
                n += 1
        if n:
            self._conn.commit()
        return n

    def prune_source(self, source: str) -> int:
        """Delete every row from a given feed source. Used to retire Adzuna, whose API only
        licensed a ~500-char preview of each posting -- once we stop ingesting it, the existing
        truncated rows should go too rather than linger for the 21-day stale window. Returns how
        many were removed."""
        cur = self._conn.execute("DELETE FROM sourced_job WHERE source=?", (source,))
        self._conn.commit()
        return cur.rowcount

    def dismiss(self, source_id: str) -> None:
        self._conn.execute(
            "UPDATE sourced_job SET dismissed=1 WHERE source_id=?", (source_id,))
        self._conn.commit()

    def set_saved(self, source_id: str, saved: bool, job: dict | None = None) -> None:
        """Bookmark (or un-bookmark) a role. When the row isn't in the local DB yet (it came
        straight from the shared kitchen), `job` carries a snapshot to insert first, so the Saved
        list can render it later even after it ages out of the live feed. Un-saving keeps the row
        (harmless) and just clears the flag."""
        if saved and job and job.get("source_id"):
            self.upsert_jobs([{
                "source_id": job["source_id"], "source": job.get("source", ""),
                "company": job.get("company", ""), "title": job.get("title", ""),
                "location": job.get("location", ""), "remote": job.get("remote", ""),
                "url": job.get("url", ""), "jd_text": job.get("jd_text", ""),
                "posted_at": job.get("posted_at", ""), "salary": job.get("salary", ""),
            }])
        self._conn.execute("UPDATE sourced_job SET saved=? WHERE source_id=?",
                           (1 if saved else 0, source_id))
        self._conn.commit()

    def saved_ids(self) -> set[str]:
        """The set of currently-bookmarked source_ids, for overlaying `saved` onto a feed whose
        rows come from the kitchen (which holds no personal state)."""
        return {r[0] for r in self._conn.execute(
            "SELECT source_id FROM sourced_job WHERE saved=1")}

    def mark_seen(self, source_id: str) -> None:
        self._conn.execute(
            "UPDATE sourced_job SET is_new=0 WHERE source_id=?", (source_id,))
        self._conn.commit()

    def clear_new(self) -> None:
        self._conn.execute("UPDATE sourced_job SET is_new=0")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
