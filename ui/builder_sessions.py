"""Every resume chat, kept: the builder's sessions in local SQLite, like the Recents list in a chat app.

The live builder session lived only in memory (ui/app._SESSION), so a restart, an update or a crash
lost the resume being built and the chat that built it: Kofi lost his D365 Software Developer resume
that way (2026-11-10). The profile and the chat diary were saved, but not the session itself (the
job, its description, the template, the page and its PDF, the keyword edits, the stage).

The JOB is the unit: every session belongs to one tracker job (a sourced_job row, sourcing/board.py),
keyed by its ``job_id`` as well as its own id, so "open this job's resume chat" is one lookup. A job
can have several sessions over time (a retry, a second template); all are kept, newest first, and
the newest is the job's current one.

Every session is written here after each request that touched it, and restored on demand:
the most recent unfinished one when the engine starts, any one by id from the list. Saved and
abandoned sessions are kept too, so a chat stays findable after its job post has gone. Local only,
the same database as the profile; erasing memory erases these.

``dump`` turns a live WebIntake into JSON and ``restore`` builds one back from it, with no model
call and no recompile: the page that was built (tailoring.assembler.AssembleResult) is rebuilt
from its stored fields, and the PDF it points at is still in the build folder.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from pathlib import Path

STATUS_IN_PROGRESS, STATUS_SAVED, STATUS_APPLIED = "in_progress", "saved", "applied"

# The session's own state, as plain JSON. Hooks the app sets (github_status, start_build ...), the
# model, the memory store and the lock are not state: restore gets fresh ones.
_FIELDS = (
    "jd", "template_name", "jobname", "role", "company", "profile_name", "stage", "profile",
    "essentials", "history", "keyword_actions", "cv_selection", "_written", "_change_base",
    "_last_reverted", "source_job", "last_cover_letter", "last_screening", "session_choices",
    "recalled", "rule_proposals", "_pdf_v", "dirty", "variant_of", "choice_ask", "overlap_ask",
    "_choice_asked", "uploads", "action_tokens", "_labels", "_last_record_id", "last_missing",
    "last_declined", "_project_offered", "rank_state", "expired_noted", "job_id",
)
_SETS = ("kept_trims", "_born_rules")
# What a restored page does not need and would only bloat the row.
_DROP_COMPILE = ("log",)


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _dump_assembled(a) -> dict | None:
    if a is None:
        return None
    try:
        d = dataclasses.asdict(a)
    except TypeError:
        return None
    comp = d.get("compile") or {}
    for k in _DROP_COMPILE:
        comp.pop(k, None)
    return _jsonable(d)


def _load_assembled(d: dict | None):
    """An AssembleResult from its stored fields; None when the shape no longer fits (an update
    changed the class): the session then reopens unbuilt, with its chat, and builds again."""
    if not d:
        return None
    from tailoring.assembler import AssembleResult
    from tailoring.compiler import CompileResult, Overfull
    from tailoring.keywords import CoverageReport

    def only(cls, raw):
        names = {f.name for f in dataclasses.fields(cls)}
        return {k: v for k, v in (raw or {}).items() if k in names}

    try:
        comp = only(CompileResult, d.get("compile"))
        comp["overfulls"] = [Overfull(**only(Overfull, o)) for o in comp.get("overfulls") or []]
        for k in ("pdf_path", "tex_path"):
            if comp.get(k):
                comp[k] = Path(comp[k])
        fields = only(AssembleResult, d)
        fields["compile"] = CompileResult(**comp)
        fields["coverage"] = CoverageReport(**only(CoverageReport, d.get("coverage")))
        if fields.get("pdf_path"):
            fields["pdf_path"] = Path(fields["pdf_path"])
        a = AssembleResult(**fields)
    except (TypeError, ValueError):
        return None
    pdf = a.pdf_path or a.compile.pdf_path
    if not (pdf and Path(pdf).exists()):
        return None
    return a


def dump(session) -> dict:
    """The session as JSON-ready data (call under the session's lock)."""
    data = {}
    for k in _FIELDS:
        if not hasattr(session, k):
            continue
        try:
            data[k] = _jsonable(getattr(session, k))
        except (TypeError, ValueError):   # a cache keyed by tuples: rebuilt on use, never needed
            continue
    for k in _SETS:
        data[k] = sorted(_jsonable(list(getattr(session, k, None) or [])), key=str)
    data["assembled"] = _dump_assembled(getattr(session, "assembled", None))
    data["template_label"] = str((getattr(session, "manifest", None) or {}).get("display_name")
                                 or getattr(session, "template_name", "") or "")
    return data


def meta_of(session) -> dict:
    """The list row's columns: who, what template, how full the page is, how many of the job's
    keywords it carries, its PDF, and its status."""
    try:
        fill = session._page_fill_pct()
    except Exception:      # noqa: BLE001 - an unbuilt or odd page simply has no fill
        fill = None
    a = getattr(session, "assembled", None)
    try:
        keywords = round(a.coverage.coverage_ratio * 100) if a is not None else None
    except Exception:      # noqa: BLE001
        keywords = None
    rid = getattr(session, "_last_record_id", None)
    pdf = None
    if session.stage == "done" and rid:
        pdf = f"/api/cv.pdf?job=cv-{rid}"
    elif a is not None and (getattr(a, "pdf_path", None) or "") and Path(a.pdf_path).exists():
        pdf = f"/api/cv.pdf?job={session.jobname}"
    return {"role": str(session.role or ""), "company": str(session.company or ""),
            "template": str(session.template_name or ""),
            "template_label": str((session.manifest or {}).get("display_name")
                                  or session.template_name or ""),
            "fill": fill, "keywords": keywords, "pdf": pdf, "stage": str(session.stage or ""),
            "status": STATUS_SAVED if session.stage == "done" else STATUS_IN_PROGRESS,
            "job_id": str(getattr(session, "job_id", "")
                          or (session.source_job or {}).get("source_id") or ""),
            "record_id": rid}


def restore(data: dict, *, template_source: str, llm, memory, records, workdir, palace_dir=None):
    """A WebIntake rebuilt from ``dump``'s data: the same job, template, chat, page and stage."""
    from ui.session import WebIntake
    s = WebIntake(data.get("jd") or "", template_source, llm, memory, records, workdir,
                  profile_name=data.get("profile_name") or "default",
                  jobname=data.get("jobname") or "cv", palace_dir=palace_dir,
                  template_name=data.get("template_name"))
    for k in _FIELDS:
        if k in data and k != "_last_record_id":
            setattr(s, k, data[k])
    if data.get("_last_record_id"):
        s._last_record_id = data["_last_record_id"]
    for k in _SETS:
        setattr(s, k, set(data.get(k) or []))
    s.assembled = _load_assembled(data.get("assembled"))
    if s.assembled is None and s.stage in ("review", "drafting"):
        s.stage = "chatting"           # the page is gone: the chat stays, the build runs again
    # The diary reloads every turn ever said, this session's too: keep only the earlier ones.
    mine = {(t.get("role"), t.get("content")) for t in s.history or []}
    s.prior_history = [t for t in s.prior_history if (t.get("role"), t.get("content")) not in mine]
    return s


class SessionStore:
    """The ``builder_sessions`` table. One row per session id (the session's jobname)."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS builder_sessions ("
                " id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                " status TEXT NOT NULL, stage TEXT, role TEXT, company TEXT, template TEXT,"
                " template_label TEXT, fill INTEGER, job_id TEXT, record_id INTEGER, data TEXT NOT NULL,"
                " keywords INTEGER, pdf TEXT)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS builder_sessions_updated"
                               " ON builder_sessions(updated_at DESC)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS builder_sessions_job"
                               " ON builder_sessions(job_id, updated_at DESC)")

    def close(self):
        self._conn.close()

    def save(self, sid: str, meta: dict, data: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        body = json.dumps(data, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO builder_sessions (id, created_at, updated_at, status, stage, role,"
                " company, template, template_label, fill, job_id, record_id, data, keywords, pdf)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " updated_at=excluded.updated_at, status=excluded.status, stage=excluded.stage,"
                " role=excluded.role, company=excluded.company, template=excluded.template,"
                " template_label=excluded.template_label, fill=excluded.fill,"
                " job_id=excluded.job_id, record_id=excluded.record_id, data=excluded.data,"
                " keywords=excluded.keywords, pdf=excluded.pdf",
                (sid, now, now, meta.get("status") or STATUS_IN_PROGRESS, meta.get("stage"),
                 meta.get("role"), meta.get("company"), meta.get("template"),
                 meta.get("template_label"), meta.get("fill"), meta.get("job_id"),
                 meta.get("record_id"), body, meta.get("keywords"), meta.get("pdf")))

    @staticmethod
    def _row(r) -> dict:
        return {k: r[k] for k in ("id", "created_at", "updated_at", "status", "stage", "role",
                                  "company", "template", "template_label", "fill", "keywords",
                                  "pdf", "job_id", "record_id")}

    def list(self, q: str = "", limit: int = 50, status: str = "", job_id: str = "") -> list[dict]:
        """Newest first; ``q`` matches the role or the company, any case; ``job_id`` is one job's."""
        sql, args = "SELECT * FROM builder_sessions WHERE 1=1", []
        if job_id:
            sql += " AND job_id = ?"
            args.append(job_id)
        q = (q or "").strip()
        if q:
            sql += " AND (role LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')"
            pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [pat, pat]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(500, int(limit or 50))))
        with self._lock:
            return [self._row(r) for r in self._conn.execute(sql, args)]

    def get(self, sid: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM builder_sessions WHERE id = ?", (sid,)).fetchone()
        if r is None:
            return None
        out = self._row(r)
        try:
            out["data"] = json.loads(r["data"])
        except ValueError:
            out["data"] = {}
        return out

    def current_by_job(self, job_ids) -> dict[str, dict]:
        """{job id: its current (newest) session row} for the given jobs."""
        ids = [str(j) for j in dict.fromkeys(job_ids or []) if j]
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            sql = (f"SELECT * FROM builder_sessions WHERE job_id IN ({','.join('?' * len(part))})"
                   " ORDER BY updated_at DESC")
            with self._lock:
                for r in self._conn.execute(sql, part):
                    out.setdefault(r["job_id"], self._row(r))
        return out

    def by_record(self, record_ids) -> dict[int, dict]:
        """{record id: the session that saved it} for the given application records."""
        ids = [int(r) for r in dict.fromkeys(record_ids or []) if r]
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            sql = (f"SELECT * FROM builder_sessions WHERE record_id IN ({','.join('?' * len(part))})"
                   " ORDER BY updated_at DESC")
            with self._lock:
                for r in self._conn.execute(sql, part):
                    out.setdefault(int(r["record_id"]), self._row(r))
        return out

    def latest_unfinished(self) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT id FROM builder_sessions WHERE status = ?"
                                   " ORDER BY updated_at DESC LIMIT 1",
                                   (STATUS_IN_PROGRESS,)).fetchone()
        return self.get(r["id"]) if r else None

    def delete(self, sid: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM builder_sessions WHERE id = ?", (sid,)).rowcount > 0

    def clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM builder_sessions")
