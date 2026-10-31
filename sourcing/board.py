"""The job tracker board: one stage per job the person saved, tailored, or applied to.

Local-first (CLAUDE.md §5). The board lives on the SAME ``sourced_job`` rows the Jobs page and
the Saved list already use (a few extra columns, migrated in place like ``saved`` was), plus two
small tables: ``tracker_activity`` (every stage change: from, to, when, note) and
``tracker_contact`` (the people met along the way). Nothing here talks to the network.

``BoardMixin`` is mixed into ``sourcing.watchlist.Watchlist`` so one connection serves both the
feed and the board. The pure functions at the bottom (funnel metrics, the claims export, the
inbox stage suggestion) take plain rows and a pinned ``today`` so they are exact under test.

Stages, in funnel order: saved, applied, interviewing, offer, rejected, archived. A job's stage
is NULL until the person touches it (bookmark, tailor, apply); the board only shows touched rows.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

# "approved" = reviewed and approved in the app, not submitted yet (submit/stages.py); a failed
# auto-submit stays here, marked "Needs you", until the person submits it.
STAGES = ("saved", "approved", "applied", "interviewing", "offer", "rejected", "archived")
RANK = {s: i for i, s in enumerate(STAGES)}
# How far an application got, for honest funnel counts. Rejected and archived are endings, not
# progress, so the furthest stage is always one of these; a job that interviewed and was then
# rejected keeps "interviewing" here even though its card now sits in Closed.
PROGRESS = ("saved", "approved", "applied", "interviewing", "offer")
PROGRESS_RANK = {s: i for i, s in enumerate(PROGRESS)}
# How many recent stage changes the side panel lists (each one can be walked back with Undo).
HISTORY_SHOWN = 5
# Stages where a "did they see it?" nudge still helps. A new Applied job gets a default follow-up
# this many days out (the same window submit/tracker.py uses for a record's nudge).
FOLLOW_UP_DAYS = 6
NUDGE_STAGES = ("applied", "interviewing")
# Rows the board creates for a resume tailored from a pasted JD (no sourced posting behind it).
# They are keyed ``tailored:<record id>`` and never appear on the Jobs feed.
TAILORED_SOURCE = "tailored"


def now_iso() -> str:
    """Local wall-clock time with its offset. The board's dates are compared against
    ``date.today()`` (local), so a UTC stamp near midnight would file tonight's application
    under tomorrow and drop it from "this week"."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def date_of(value) -> date | None:
    """A stored ISO timestamp / date / sqlite CURRENT_TIMESTAMP -> date (None if unusable)."""
    if not value:
        return None
    s = str(value)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def migrate_board(c: sqlite3.Connection) -> None:
    """Idempotent: add the board columns to sourced_job and create the side tables."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(sourced_job)")}
    for name, ddl in (("stage", "TEXT"), ("stage_at", "TEXT"), ("applied_at", "TEXT"),
                      ("follow_up_at", "TEXT"), ("notes", "TEXT DEFAULT ''"),
                      ("furthest_stage", "TEXT")):
        if name not in cols:
            c.execute(f"ALTER TABLE sourced_job ADD COLUMN {name} {ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS tracker_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        from_stage TEXT, to_stage TEXT NOT NULL, at TEXT NOT NULL, note TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS tracker_activity_job ON tracker_activity(source_id)")
    # Undo needs what the row looked like before the change (the dates a move rewrites), whether
    # the change was walked back, and what caused it (an inbox suggestion id, so undoing an inbox
    # move puts the suggestion back).
    acols = {r[1] for r in c.execute("PRAGMA table_info(tracker_activity)")}
    for name, ddl in (("prev_stage_at", "TEXT"), ("prev_applied_at", "TEXT"),
                      ("prev_follow_up_at", "TEXT"), ("undone", "INTEGER DEFAULT 0"),
                      ("undone_at", "TEXT"), ("ref", "TEXT DEFAULT ''")):
        if name not in acols:
            c.execute(f"ALTER TABLE tracker_activity ADD COLUMN {name} {ddl}")
    # Mail the app has tied to an application: the thread lane of inbox matching reads it, and
    # the record says how each message was matched (thread, domain, company).
    c.execute("""CREATE TABLE IF NOT EXISTS tracker_mail (
        message_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, thread_id TEXT DEFAULT '',
        rfc_id TEXT DEFAULT '', matched_by TEXT DEFAULT '', label TEXT DEFAULT '',
        subject TEXT DEFAULT '', at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS tracker_mail_job ON tracker_mail(source_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS tracker_contact (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        name TEXT NOT NULL, role TEXT DEFAULT '', email TEXT DEFAULT '', linkedin TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("CREATE INDEX IF NOT EXISTS tracker_contact_job ON tracker_contact(source_id)")
    if "furthest_stage" not in cols:
        _backfill_furthest(c)


def _furthest(stages) -> str | None:
    best = None
    for st in stages:
        if st in PROGRESS_RANK and (best is None or PROGRESS_RANK[st] > PROGRESS_RANK[best]):
            best = st
    return best


def _backfill_furthest(c: sqlite3.Connection) -> None:
    """First open after the column arrived: every board job's furthest stage from its log."""
    seen: dict[str, list[str]] = {}
    for r in c.execute("SELECT source_id, to_stage FROM tracker_activity"):
        seen.setdefault(r[0], []).append(r[1])
    for r in c.execute("SELECT source_id, stage FROM sourced_job WHERE stage IS NOT NULL AND stage<>''").fetchall():
        best = _furthest(seen.get(r[0], []) + [r[1]])
        if best:
            c.execute("UPDATE sourced_job SET furthest_stage=? WHERE source_id=?", (best, r[0]))


_BOARD_COLS = ("source_id, source, company, title, location, remote, url, posted_at, salary, "
               "saved, sponsorship_stated, stage, stage_at, applied_at, follow_up_at, notes, "
               "furthest_stage")


class BoardMixin:
    """Stage, notes, contacts, follow-up and activity for sourced_job rows. Expects ``self._conn``
    (sqlite3 with Row factory) and ``self.upsert_jobs`` from the Watchlist host class."""

    _conn: sqlite3.Connection

    # -- stage ----------------------------------------------------------- #
    def track_job(self, source_id: str, stage: str = "saved", note: str = "",
                  now: str | None = None, job: dict | None = None,
                  applied_at: str | None = None) -> dict | None:
        """Put a job on the board at ``stage`` if it is not on it yet. ``job`` is a snapshot to
        insert first when the row is not local (a kitchen role, or a resume tailored from a pasted
        JD). A job already on the board is left where it is. Returns the board row (or None)."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if job and job.get("source_id"):
            self.upsert_jobs([{
                "source_id": job["source_id"], "source": job.get("source", ""),
                "company": job.get("company", ""), "title": job.get("title", ""),
                "location": job.get("location", ""), "remote": job.get("remote", ""),
                "url": job.get("url", ""), "jd_text": job.get("jd_text", ""),
                "posted_at": job.get("posted_at", ""), "salary": job.get("salary", ""),
            }])
        row = self._conn.execute(
            "SELECT stage FROM sourced_job WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            return None
        if row["stage"] in STAGES:
            return self.board_job(source_id)
        return self.move_stage(source_id, stage, note=note, now=now, applied_at=applied_at)

    def move_stage(self, source_id: str, stage: str, note: str = "", now: str | None = None,
                   applied_at: str | None = None, ref: str = "") -> dict:
        """Move one job to ``stage`` and log it. Entering Applied stamps applied_at (kept on a
        later re-entry) and arms a default follow-up; a closed stage (rejected / archived) clears
        the follow-up because a nudge no longer helps. Moving to the stage it is already in is a
        no-op (no activity row)."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        now = now or now_iso()
        cur = self._conn.execute(
            "SELECT stage, stage_at, applied_at, follow_up_at, furthest_stage FROM sourced_job "
            "WHERE source_id=?", (source_id,)).fetchone()
        if cur is None:
            raise KeyError(f"no job {source_id}")
        prev = cur["stage"] if cur["stage"] in STAGES else None
        if prev == stage:
            return self.board_job(source_id)
        sets, params = ["stage=?", "stage_at=?"], [stage, now]
        best = _furthest([cur["furthest_stage"], stage])
        if best and best != cur["furthest_stage"]:
            sets.append("furthest_stage=?"); params.append(best)
        if stage == "applied":
            applied = cur["applied_at"] or applied_at or now
            sets.append("applied_at=?"); params.append(applied)
            if not cur["follow_up_at"]:
                d = date_of(applied) or date_of(now)
                sets.append("follow_up_at=?")
                params.append((d + timedelta(days=FOLLOW_UP_DAYS)).isoformat() if d else None)
        elif stage in ("rejected", "archived"):
            sets.append("follow_up_at=NULL")
        elif stage in ("saved", "approved"):
            # Back to Saved (or Approved) means "not applied after all" (the review queue's stage_patch does
            # the same for a record), so the funnel stops counting it and the nudge is dropped.
            sets.append("applied_at=NULL"); sets.append("follow_up_at=NULL")
        elif RANK[stage] > RANK["applied"] and not cur["applied_at"]:
            # Interviewing / offer straight from Saved: it was applied to, just not logged.
            sets.append("applied_at=?"); params.append(applied_at or now)
        params.append(source_id)
        self._conn.execute(f"UPDATE sourced_job SET {', '.join(sets)} WHERE source_id=?", params)
        self._log_change(source_id, prev, stage, now, note, cur, ref)
        self._conn.commit()
        return self.board_job(source_id)

    def _log_change(self, source_id, prev, stage, now, note, cur, ref="") -> None:
        """One timeline row, carrying the dates as they were so Undo can put them back."""
        self._conn.execute(
            "INSERT INTO tracker_activity(source_id, from_stage, to_stage, at, note, prev_stage_at, "
            "prev_applied_at, prev_follow_up_at, ref) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, prev, stage, now, (note or "")[:300], cur["stage_at"], cur["applied_at"],
             cur["follow_up_at"], (ref or "")[:160]))

    def untrack(self, source_id: str, note: str = "", now: str | None = None) -> None:
        """Take a job off the board (stage NULL), logging the exit so the timeline is honest."""
        cur = self._conn.execute(
            "SELECT stage, stage_at, applied_at, follow_up_at FROM sourced_job WHERE source_id=?",
            (source_id,)).fetchone()
        if cur is None or cur["stage"] not in STAGES:
            return
        self._conn.execute(
            "UPDATE sourced_job SET stage=NULL, stage_at=?, follow_up_at=NULL WHERE source_id=?",
            (now or now_iso(), source_id))
        self._log_change(source_id, cur["stage"], "", now or now_iso(), note, cur)
        self._conn.commit()

    # -- undo ------------------------------------------------------------ #
    def undo_last(self, source_id: str, now: str | None = None) -> dict | None:
        """Walk back the newest stage change that is not already undone: the stage and the dates
        it rewrote (stage_at, applied_at, follow_up_at) return to what they were, the timeline
        row is kept but marked undone, and the furthest stage is worked out again from the
        changes that still stand. Calling it again walks back the one before. Returns
        {"activity": the undone row, "job": the board row (None when the job left the board)},
        or None when there is nothing to undo."""
        row = self._conn.execute(
            "SELECT * FROM tracker_activity WHERE source_id=? AND COALESCE(undone,0)=0 "
            "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
        if row is None:
            return None
        back = row["from_stage"] if row["from_stage"] in STAGES else None
        self._conn.execute(
            "UPDATE sourced_job SET stage=?, stage_at=?, applied_at=?, follow_up_at=? WHERE source_id=?",
            (back, row["prev_stage_at"], row["prev_applied_at"], row["prev_follow_up_at"], source_id))
        if back is None:
            # Off the board again: a bookmark that only put it there goes too (set_saved keeps
            # the two in step the same way).
            self._conn.execute("UPDATE sourced_job SET saved=0 WHERE source_id=?", (source_id,))
        elif back == "saved" and not row["to_stage"]:
            # Undoing "Removed from saved jobs" brings the bookmark back with the card.
            self._conn.execute("UPDATE sourced_job SET saved=1 WHERE source_id=?", (source_id,))
        self._conn.execute("UPDATE tracker_activity SET undone=1, undone_at=? WHERE id=?",
                           (now or now_iso(), row["id"]))
        self._refresh_furthest(source_id)
        self._conn.commit()
        return {"activity": dict(row), "job": self.board_job(source_id) if back else None}

    def _refresh_furthest(self, source_id: str) -> None:
        stages = [r[0] for r in self._conn.execute(
            "SELECT to_stage FROM tracker_activity WHERE source_id=? AND COALESCE(undone,0)=0",
            (source_id,))]
        cur = self._conn.execute("SELECT stage FROM sourced_job WHERE source_id=?", (source_id,)).fetchone()
        if cur is not None:
            stages.append(cur[0])
        self._conn.execute("UPDATE sourced_job SET furthest_stage=? WHERE source_id=?",
                           (_furthest(stages), source_id))

    def history(self, source_id: str, limit: int = HISTORY_SHOWN) -> list[dict]:
        """The newest stage changes that still stand, newest first, each with what Undo would
        put back. Only the first one is the next Undo."""
        rows = self._conn.execute(
            "SELECT id, from_stage, to_stage, at, note, ref FROM tracker_activity "
            "WHERE source_id=? AND COALESCE(undone,0)=0 ORDER BY id DESC LIMIT ?",
            (source_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    # -- mail tied to an application -------------------------------------- #
    def record_mail(self, source_id: str, message_id: str, *, thread_id: str = "", rfc_id: str = "",
                    matched_by: str = "", label: str = "", subject: str = "",
                    now: str | None = None) -> None:
        """Remember that one message belongs to this application, so a later reply in the same
        thread matches it first. A message already recorded keeps its first record."""
        mid = str(message_id or "").strip()[:200]
        if not mid or not source_id:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO tracker_mail(message_id, source_id, thread_id, rfc_id, matched_by, "
            "label, subject, at) VALUES(?,?,?,?,?,?,?,?)",
            (mid, source_id, str(thread_id or "")[:200], str(rfc_id or "").strip("<> ")[:300],
             str(matched_by or "")[:20], str(label or "")[:40], str(subject or "")[:200],
             now or now_iso()))
        self._conn.commit()

    def forget_mail(self, message_id: str) -> int:
        """"Not this": the message is no longer tied to the application, so its thread stops
        matching it."""
        cur = self._conn.execute("DELETE FROM tracker_mail WHERE message_id=?", (str(message_id or ""),))
        self._conn.commit()
        return cur.rowcount

    def thread_index(self) -> dict[str, str]:
        """{key: source_id} for every recorded message: its mailbox thread id and its own
        Message-ID, the two things a reply in the same conversation carries. Only jobs still on
        the board count."""
        out: dict[str, str] = {}
        for r in self._conn.execute(
                "SELECT m.source_id, m.thread_id, m.rfc_id FROM tracker_mail m JOIN sourced_job j "
                "ON j.source_id=m.source_id WHERE j.stage IS NOT NULL AND j.stage<>'' ORDER BY m.at"):
            thread = str(r["thread_id"] or "").strip().lower()
            rfc = str(r["rfc_id"] or "").strip().strip("<>").lower()
            if thread:
                out[f"t:{thread}"] = r["source_id"]
            if rfc:
                out[f"m:{rfc}"] = r["source_id"]
        return out

    def known_mail(self, message_ids) -> set[str]:
        """Which of these messages the app has already tied to an application (and labelled)."""
        ids = [str(m or "")[:200] for m in message_ids if m]
        out: set[str] = set()
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            out.update(r[0] for r in self._conn.execute(
                f"SELECT message_id FROM tracker_mail WHERE message_id IN ({','.join('?' * len(chunk))})", chunk))
        return out

    # -- the person's Export and Erase (docs/applicant-brain.md, section 10) -- #
    def tracker_export(self) -> dict:
        """What the Tracker remembers about the person: the mail tied to each application, the
        stage history (with what Undo would restore), the contacts and each tracked card."""
        q = lambda sql: [dict(r) for r in self._conn.execute(sql).fetchall()]  # noqa: E731
        return {"mail": q("SELECT * FROM tracker_mail ORDER BY at"),
                "history": q("SELECT * FROM tracker_activity ORDER BY id"),
                "contacts": q("SELECT * FROM tracker_contact ORDER BY id"),
                "cards": q("SELECT source_id, company, title, url, stage, stage_at, applied_at, "
                           "follow_up_at, notes, furthest_stage FROM sourced_job "
                           "WHERE stage IS NOT NULL AND stage<>''")}

    def tracker_erase(self, applications: bool = False) -> int:
        """Erase what the Tracker learned from the person's mail: every tracker_mail row, and the
        inbox's words in the stage history (an inbox move's note quotes the subject). With
        ``applications`` the applications go too: the whole stage history, the contacts, and
        every card's stage, dates and notes. ``secure_delete`` zeroes the freed bytes; the caller
        vacuums the file afterwards. Returns the rows touched."""
        c = self._conn
        c.execute("PRAGMA secure_delete=ON")
        n = c.execute("DELETE FROM tracker_mail").rowcount
        if applications:
            n += c.execute("DELETE FROM tracker_activity").rowcount
            n += c.execute("DELETE FROM tracker_contact").rowcount
            n += c.execute("UPDATE sourced_job SET stage=NULL, stage_at=NULL, applied_at=NULL, "
                           "follow_up_at=NULL, notes='', furthest_stage=NULL "
                           "WHERE stage IS NOT NULL OR notes<>''").rowcount
        else:
            n += c.execute("UPDATE tracker_activity SET note='' WHERE note<>'' AND "
                           "(ref<>'' OR note LIKE 'From your inbox%')").rowcount
        c.commit()
        return n

    def mail_for(self, source_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT message_id, thread_id, matched_by, label, subject, at FROM tracker_mail "
            "WHERE source_id=? ORDER BY at DESC", (source_id,)).fetchall()]

    # -- reads ----------------------------------------------------------- #
    def board_job(self, source_id: str) -> dict | None:
        row = self._conn.execute(
            f"SELECT {_BOARD_COLS} FROM sourced_job WHERE source_id=?", (source_id,)).fetchone()
        return self._row(row) if row else None

    def board_jobs(self, stage: str | None = None) -> list[dict]:
        """Every job on the board (stage set), newest stage change first; or one stage's."""
        q = f"SELECT {_BOARD_COLS} FROM sourced_job WHERE stage IS NOT NULL AND stage<>''"
        params: list = []
        if stage:
            q += " AND stage=?"; params.append(stage)
        q += " ORDER BY stage_at DESC, company COLLATE NOCASE"
        return [self._row(r) for r in self._conn.execute(q, params).fetchall()]

    def stage_of(self, source_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT stage FROM sourced_job WHERE source_id=?", (source_id,)).fetchone()
        return row["stage"] if row and row["stage"] in STAGES else None

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["sponsorship_stated"] = None if d.get("sponsorship_stated") is None \
            else bool(d["sponsorship_stated"])
        d["saved"] = bool(d.get("saved"))
        d["notes"] = d.get("notes") or ""
        return d

    # -- notes / follow-up ------------------------------------------------ #
    def set_notes(self, source_id: str, notes: str) -> None:
        self._conn.execute("UPDATE sourced_job SET notes=? WHERE source_id=?",
                           ((notes or "")[:20000], source_id))
        self._conn.commit()

    def set_follow_up(self, source_id: str, when: str | None) -> str | None:
        """Set (ISO date) or clear (None / "") the follow-up date. Returns what was stored."""
        d = date_of(when) if when else None
        if when and d is None:
            raise ValueError("follow-up must be a date like 2026-10-18")
        self._conn.execute("UPDATE sourced_job SET follow_up_at=? WHERE source_id=?",
                           (d.isoformat() if d else None, source_id))
        self._conn.commit()
        return d.isoformat() if d else None

    def due_followups(self, today: date) -> list[dict]:
        """Jobs whose follow-up date has arrived and that are still worth nudging about."""
        rows = self._conn.execute(
            f"SELECT {_BOARD_COLS} FROM sourced_job WHERE follow_up_at IS NOT NULL AND follow_up_at<>'' "
            f"AND stage IN ({','.join('?' * len(NUDGE_STAGES))}) ORDER BY follow_up_at",
            NUDGE_STAGES).fetchall()
        out = []
        for r in rows:
            d = date_of(r["follow_up_at"])
            if d and d <= today:
                out.append(self._row(r))
        return out

    # -- contacts -------------------------------------------------------- #
    def add_contact(self, source_id: str, name: str, role: str = "", email: str = "",
                    linkedin: str = "") -> int:
        name = (name or "").strip()[:120]
        if not name:
            raise ValueError("a contact needs a name")
        cur = self._conn.execute(
            "INSERT INTO tracker_contact(source_id, name, role, email, linkedin) VALUES(?,?,?,?,?)",
            (source_id, name, (role or "").strip()[:120], (email or "").strip()[:200],
             (linkedin or "").strip()[:300]))
        self._conn.commit()
        return int(cur.lastrowid)

    def remove_contact(self, contact_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM tracker_contact WHERE id=?", (int(contact_id),))
        self._conn.commit()
        return cur.rowcount > 0

    def contacts(self, source_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT id, name, role, email, linkedin, created_at FROM tracker_contact "
            "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()]

    # -- activity -------------------------------------------------------- #
    def activity(self, source_id: str) -> list[dict]:
        """The job's timeline, newest first."""
        return [dict(r) for r in self._conn.execute(
            "SELECT id, from_stage, to_stage, at, note FROM tracker_activity "
            "WHERE source_id=? AND COALESCE(undone,0)=0 ORDER BY id DESC", (source_id,)).fetchall()]

    def funnel_rows(self) -> list[dict]:
        """One row per board job with the dates the funnel math needs: when it was applied to,
        and when it FIRST reached interviewing / offer / rejected (from the activity log)."""
        firsts: dict[tuple[str, str], str] = {}
        for r in self._conn.execute(
                "SELECT source_id, to_stage, MIN(at) AS at FROM tracker_activity "
                "WHERE to_stage IN ('interviewing','offer','rejected') AND COALESCE(undone,0)=0 "
                "GROUP BY source_id, to_stage"):
            firsts[(r["source_id"], r["to_stage"])] = r["at"]
        out = []
        for r in self._conn.execute(
                "SELECT source_id, stage, applied_at, furthest_stage FROM sourced_job "
                "WHERE stage IS NOT NULL AND stage<>''"):
            sid = r["source_id"]
            out.append({
                "source_id": sid, "stage": r["stage"], "applied_at": r["applied_at"],
                "furthest_stage": r["furthest_stage"],
                "interviewed_at": firsts.get((sid, "interviewing")),
                "offered_at": firsts.get((sid, "offer")),
                "rejected_at": firsts.get((sid, "rejected")),
            })
        return out


# ---------------------------------------------------------------------- pure math

def week_start(d: date) -> date:
    """Monday of the ISO week containing ``d``."""
    return d - timedelta(days=d.weekday())


def funnel_metrics(rows: list[dict], today: date, weekly_goal: int = 0, weeks: int = 12) -> dict:
    """The numbers at the top of the Tracker page, from ``funnel_rows()`` and a pinned clock.

    applied_total counts every job with an applied date (whatever stage it is in now);
    interviews / offers count jobs that EVER reached that stage (the first time in the log, or
    the furthest stage kept on the row), so a rejection after an interview still counts as an
    interview, and an undone move does not. Conversion is interviews / applied and offers /
    interviews, in percent. Median days is applied -> first interview over jobs with both."""
    ws = week_start(today)
    applied_dates = [d for d in (date_of(r.get("applied_at")) for r in rows) if d]
    applied_total = len(applied_dates)
    applied_week = sum(1 for d in applied_dates if ws <= d <= today)
    interviews = sum(1 for r in rows if r.get("interviewed_at") or _reached(r, "interviewing"))
    offers = sum(1 for r in rows if r.get("offered_at") or _reached(r, "offer"))
    rejected = sum(1 for r in rows if r.get("rejected_at") or r.get("stage") == "rejected")
    gaps = sorted((date_of(r["interviewed_at"]) - date_of(r["applied_at"])).days
                  for r in rows if date_of(r.get("interviewed_at")) and date_of(r.get("applied_at")))
    median = None
    if gaps:
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else round((gaps[mid - 1] + gaps[mid]) / 2, 1)
    series = []
    for i in range(weeks - 1, -1, -1):
        start = ws - timedelta(weeks=i)
        end = start + timedelta(days=6)
        series.append({"week": start.isoformat(),
                       "count": sum(1 for d in applied_dates if start <= d <= end)})
    goal = max(0, int(weekly_goal or 0))
    return {
        "applied_week": applied_week,
        "applied_total": applied_total,
        "interviews": interviews,
        "offers": offers,
        "rejected": rejected,
        "pct_interview": _pct(interviews, applied_total),
        "pct_offer": _pct(offers, interviews),
        "median_days_to_interview": median,
        "weeks": series,
        "goal": {"target": goal, "done": applied_week,
                 "pct": min(100, _pct(applied_week, goal)) if goal else 0},
        "week_start": ws.isoformat(),
    }


def _reached(row: dict, stage: str) -> bool:
    """Did this application ever get as far as ``stage``? From its furthest stage, so a job that
    went straight from Applied to Offer counts as an interview too, and one moved back after an
    interview still counts."""
    got = row.get("furthest_stage")
    return got in PROGRESS_RANK and PROGRESS_RANK[got] >= PROGRESS_RANK[stage]


def _pct(num: int, den: int) -> float:
    return round(num * 100.0 / den, 1) if den else 0.0


# What an unemployment-insurance work-search log asks for, in most US states: the date, the
# employer, how they were contacted (a website counts), the position, and the result.
EXPORT_COLUMNS = ("Date applied", "Employer", "Position", "Location", "Contact method",
                  "Website or job link", "Result", "Notes")

_RESULT = {"applied": "Application submitted, awaiting reply", "interviewing": "Interview",
           "offer": "Job offer", "rejected": "Not selected", "archived": "Closed"}


def export_rows(jobs: list[dict]) -> list[dict]:
    """The rows for the claims CSV: every board job with an applied date, oldest first."""
    out = []
    for j in jobs:
        d = date_of(j.get("applied_at"))
        if not d:
            continue
        out.append({
            "Date applied": d.isoformat(),
            "Employer": j.get("company") or "",
            "Position": j.get("title") or "",
            "Location": j.get("location") or "",
            "Contact method": "Online application",
            "Website or job link": j.get("url") or "",
            "Result": _RESULT.get(j.get("stage") or "", ""),
            "Notes": " ".join((j.get("notes") or "").split())[:500],
        })
    out.sort(key=lambda r: r["Date applied"])
    return out


def export_csv(jobs: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, lineterminator="\r\n")
    w.writeheader()
    for r in export_rows(jobs):
        w.writerow(r)
    return buf.getvalue()


# ---------------------------------------------------------------------- inbox suggestion

_CO_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|sa|ag|group|holdings|technologies|technology|labs)\b\.?")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def company_key(name: str) -> str:
    """'Acme Robotics, Inc.' -> 'acme robotics'. Short enough to find in a From domain."""
    s = _NON_ALNUM.sub(" ", (name or "").lower().replace("&", " and "))
    s = _CO_SUFFIX.sub(" ", s)
    return " ".join(s.split())


def _employer_keys(name: str) -> list[str]:
    """The forms a company name can take in a From address, subject or body: the board's
    short key and the sponsor database's normal form (sourcing/sponsors.normalize_employer),
    de-duplicated, shortest last so the fullest spelling is tried first."""
    keys = [company_key(name)]
    try:
        from sourcing.sponsors import normalize_employer_variants
        keys.extend(normalize_employer_variants(name))
    except Exception:   # the sponsor module is optional for the board
        pass
    out = []
    for k in keys:
        k = (k or "").strip()
        if len(k) >= 3 and k not in out:
            out.append(k)
    return sorted(out, key=len, reverse=True)


def match_job(text: str, jobs: list[dict]) -> dict | None:
    """The first board job whose company name appears in ``text`` (a From address, a subject,
    a snippet; dots, dashes and underscores count as spaces so jobs@acme-robotics.com matches
    "Acme Robotics"). None when no tracked employer is named."""
    low = (text or "").lower()
    squashed = low.replace(".", " ").replace("-", " ").replace("_", " ")
    for j in jobs:
        for key in _employer_keys(j.get("company") or ""):
            if key in squashed or key.replace(" ", "") in squashed.replace(" ", ""):
                return j
    return None


def suggest_stage(entry: dict, jobs: list[dict]) -> dict | None:
    """A SUGGESTED stage move for one recruiter reply the inbox surfaced, or None.

    The reply is matched to a board job by company name (in the sender, subject or snippet;
    the domain in the address counts, so mail from jobs@acme.com matches Acme). A rejection
    suggests Rejected; any other recruiter reply suggests Interviewing. Never forced: the
    person clicks, the board moves. Nothing is suggested for a job already at or past the
    suggested stage, or one that is closed."""
    status = entry.get("status") or ""
    target = "rejected" if status == "rejected" else "interviewing"
    text = " ".join(str(entry.get(k) or "") for k in ("from", "sender", "subject", "snippet"))
    j = match_job(text, jobs)
    if j is None or not stage_move_makes_sense(j.get("stage") or "", target):
        return None
    return {"source_id": j["source_id"], "company": j.get("company") or "",
            "title": j.get("title") or "", "from_stage": j.get("stage") or "", "stage": target}


def stage_move_makes_sense(current: str, target: str) -> bool:
    """Would suggesting ``target`` for a job at ``current`` tell the person anything new?
    Not for a closed job (rejected / archived), not when it is already at or past the
    target. Rejected is the exception: a rejection after an interview is still news."""
    if current in ("rejected", "archived") or target not in RANK:
        return False
    if target == "rejected":
        return True
    return RANK.get(current, -1) < RANK[target]
