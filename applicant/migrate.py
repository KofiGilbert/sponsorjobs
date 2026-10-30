"""Migration of the five memory stores into the applicant brain (docs/applicant-brain.md, s. 11).

Runs at start-up, safe to run again, deletes nothing:

1. Back up answers.db, app_prefs.json and resume_agent.db to ``data/brain-backup-<date>/``
   (the first time only).
2. Plan: read every legacy store READ ONLY (the answer library from a temporary copy, re-keyed
   by the current rules exactly as the app would) and count per source and outcome. Counts
   only, never values. ``--dry-run`` stops here and writes nothing in the data folder.
3. Apply in one transaction. A second run finds everything in place and adds nothing.
4. Verify: for every answer-library key the brain's answer equals the legacy library's, and the
   brain's work authorization, decline and prefs answers equal the prefs file's. Any mismatch
   rolls the whole transaction back; the app stays on the legacy stores and tries again on the
   next start (three tries at most).
5. Activate: ``meta.brain_active = 1`` and ``meta.mirror_legacy = 1``; the counts go to
   ``meta.report``.

Rollback: ``python -m applicant.migrate --data <dir> --rollback`` (or the environment variable
``SPONSORJOBS_BRAIN=0``) puts every reader back on the legacy stores, which hold every write
thanks to the mirror. ``--apply`` turns the brain back on (catching up on anything written
meanwhile). The brain file stays for diagnosis.

    python -m applicant.migrate --data data --dry-run
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from applicant import keys as K
from applicant.library import (BrainLibrary, catch_up_library, catch_up_prefs, legacy_rows, prefs_view,
                               read_prefs_file)
from applicant.store import (ACTIVE, NEEDS_CHECK, REPLACED, Store, close_store, db_files, open_store)
from autoapply import answers as A

BRAIN_FILE = "applicant.db"
MAX_TRIES = 3
ENV = "SPONSORJOBS_BRAIN"
_PROFILE_REF = "resume_agent.db:conversation_memory"


def brain_path(data_dir) -> Path:
    return Path(data_dir) / BRAIN_FILE


def env_off() -> bool:
    return os.environ.get(ENV, "").strip() == "0"


# ---------------------------------------------------------------------------- reading, read only
def _ro(path: Path):
    """A read-only connection (never writes, never creates the file)."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


class Snapshot:
    """Everything the legacy stores hold, read without changing them. The answer library is
    read from a temporary copy opened by AnswerLibrary, so its rows carry the keys the app
    itself would use (re-keyed by the current rules)."""

    def __init__(self, data_dir):
        data = Path(data_dir)
        self.tmp = tempfile.mkdtemp(prefix="sj-brain-")
        self.library = None
        src = data / "answers.db"
        if src.exists():
            dst = Path(self.tmp) / "answers.db"
            con = _ro(src)
            try:
                out = sqlite3.connect(str(dst))
                con.backup(out)
                out.close()
            finally:
                con.close()
            self.library = A.AnswerLibrary(dst)
        self.rows = legacy_rows(self.library.conn) if self.library else []
        self.prefs = read_prefs_file(data / "app_prefs.json")
        self.profile: dict = {}
        self.memory_facts: list = []
        db = data / "resume_agent.db"
        if db.exists():
            try:
                con = _ro(db)
                con.row_factory = sqlite3.Row
                try:
                    r = con.execute("SELECT profile FROM conversation_memory WHERE name='default'").fetchone()
                    self.profile = json.loads(r["profile"]) if r and r["profile"] else {}
                except (sqlite3.Error, ValueError):
                    self.profile = {}
                try:
                    self.memory_facts = [dict(x) for x in con.execute(
                        "SELECT type, key, value, confidence, status FROM memory_facts WHERE name='default' ORDER BY id")]
                except sqlite3.Error:
                    self.memory_facts = []
                con.close()
            except sqlite3.Error:
                pass
        self.transcripts: list[tuple[str, int]] = []
        tdir = data / "palace" / "transcripts"
        if tdir.is_dir():
            for f in sorted(tdir.glob("*.jsonl")):
                try:
                    with f.open("rb") as fh:
                        n = sum(1 for _ in fh)
                except OSError:
                    n = 0
                self.transcripts.append((f.name, n))

    def close(self):
        if self.library is not None:
            self.library.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------- the plan
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _profile_items(profile: dict) -> list[tuple[str, str, object, str]]:
    """(key, subject, value, part) for the open facts in the resume profile: contact details,
    each role, school and project with its dates, and the skills. The profile JSON itself is
    untouched (it stays the resume material until Phase 6)."""
    p = profile if isinstance(profile, dict) else {}
    ident = p.get("identity") if isinstance(p.get("identity"), dict) else {}
    out: list = []
    for src, key in (("name", "name"), ("email", "email"), ("phone", "phone"), ("address", "address"),
                     ("location", "location"), ("linkedin", "@linkedin"), ("github", "github"),
                     ("blog", "@portfolio"), ("website", "@portfolio")):
        v = str(ident.get(src) or "").strip()
        if v and not any(o[0] == key for o in out):
            out.append((key, "", v, "contact"))
    for entry in p.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        org = entry.get("org") or entry.get("company") or ""
        roles = entry["roles"] if isinstance(entry.get("roles"), list) else [entry]
        for role in roles:
            if not isinstance(role, dict):
                continue
            title = role.get("title") or ""
            if not (_norm(org) or _norm(title)):
                continue
            val = {"org": org, "title": title}
            for k in ("dates", "start", "end", "location"):
                if role.get(k) or entry.get(k):
                    val[k] = role.get(k) or entry.get(k)
            out.append(("role", f"role:{_norm(org)}|{_norm(title)}", val, "role"))
    for e in p.get("education") or []:
        if isinstance(e, dict) and _norm(e.get("school")):
            val = {k: e.get(k) for k in ("school", "degree", "date", "location") if e.get(k)}
            out.append(("school", f"school:{_norm(e.get('school'))}", val, "school"))
    for e in p.get("projects") or []:
        if isinstance(e, dict):
            name = e.get("title") or e.get("name") or e.get("org") or ""
            if _norm(name):
                val = {k: e.get(k) for k in ("title", "name", "org", "dates", "link") if e.get(k)}
                out.append(("project", f"project:{_norm(name)}", val, "project"))
    skills = p.get("skills")
    names: list = []
    if isinstance(skills, dict):
        for v in skills.values():
            names += [x.strip() for x in str(v or "").split(",")]
    elif isinstance(skills, list):
        names = [str(x or "").strip() for x in skills]
    seen = set()
    for s in names:
        if _norm(s) and _norm(s) not in seen:
            seen.add(_norm(s))
            out.append(("skill", f"skill:{_norm(s)}", s, "skill"))
    return out


_NOTE_KEYS = frozenset({"preference", "constraint", "fact"})


def _canonical(key: str) -> str:
    """The registry key a chat fact's key names ("salary" is @salary, "email" is email), or ""."""
    k = key.lstrip("@")
    if K.get("@" + k):
        return "@" + k
    if K.get(k) and k not in K.ITEM_KEYS and k not in _NOTE_KEYS:
        return k
    return ""


def _memory_fact_item(mf: dict):
    """(key, subject, outcome) for one memory_facts row. A fact whose key names a canonical key
    is proposed for that key (needs_check); a self-identification one is refused (chat never
    sets one); any other row is a chat note under its type."""
    typ = str(mf.get("type") or "fact")
    k = str(mf.get("key") or "").strip()
    if str(mf.get("status") or "active") != "active":
        return None, None, "skipped_retracted"
    if not k or not str(mf.get("value") or "").strip():
        return None, None, "skipped_empty"
    canon = _canonical(k) if typ == "fact" else ""
    if canon:
        if K.sensitivity(canon) == K.EEO:
            return None, None, "refused_eeo"
        return canon, "", NEEDS_CHECK
    return (typ if typ in _NOTE_KEYS else "fact"), k, ACTIVE


def plan_counts(snap: Snapshot) -> dict:
    """Per source, per outcome. Counts only."""
    c: dict = {s: {} for s in ("answers.db", "app_prefs.json", "conversation_memory", "memory_facts", "transcripts")}

    def bump(src, outcome, n=1):
        c[src][outcome] = c[src].get(outcome, 0) + n
    from applicant.library import _lib_status, prefs_facts
    for r in snap.rows:
        if not str(r["key"] or "").strip() or not str(r["answer"] or "").strip():
            bump("answers.db", "skipped_empty")
            continue
        st, _ = _lib_status(r["key"], str(r["question"] or ""), str(r["answer"] or ""))
        if K.sensitivity(K.fact_key(r["key"]), str(r["question"] or "")) == K.EEO \
                and r["source"] in K.EEO_REFUSED_SOURCES:
            st = NEEDS_CHECK
        bump("answers.db", st)
    pf = prefs_facts(snap.prefs)
    if pf:
        bump("app_prefs.json", ACTIVE, len(pf))
    for key, _s, _v, part in _profile_items(snap.profile):
        bump("conversation_memory", part)
    for mf in snap.memory_facts:
        _k, _s, outcome = _memory_fact_item(mf)
        bump("memory_facts", outcome)
    for name, _n in snap.transcripts:
        bump("transcripts", "scratch" if name.startswith("autoapply_") else "indexed")
    return c


# ---------------------------------------------------------------------------- writing
def _upsert(st: Store, key: str, subject: str, value, **kw) -> bool:
    """A brain-only fact, idempotent: the same value already current means nothing to do; a new
    value replaces the old one for that key and subject (kept as history)."""
    val = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    cur = st.current(key, subject, "")
    if cur and cur[0]["value"] == val:
        return False
    with st.tx() as c:
        new_id = st.add_fact(key, val, subject=subject, store="", replace=False, **kw)
        old = [r["id"] for r in cur]
        if old:
            c.execute(f"UPDATE fact SET status=?, replaced_by=? WHERE id IN ({','.join('?' * len(old))})",
                      (REPLACED, new_id, *old))
    return True


def _write(st: Store, snap: Snapshot) -> None:
    catch_up_library(st, snap.rows)
    catch_up_prefs(st, snap.prefs, source="you")
    with st.tx() as c:
        for r in snap.rows:                                # each wording, worked out once
            lk = A.normalize(str(r["question"] or ""))
            label = " ".join(A.clean_label(str(r["question"] or "")).lower().split())
            if label and lk:
                c.execute("INSERT OR IGNORE INTO label_map (label, options_hash, key, how, confirmed, created_at,"
                          " updated_at) VALUES (?, '', ?, 'rule', 1, ?, ?)",
                          (label, K.fact_key(lk), r["created_at"], r["updated_at"]))
        for key, subject, value, _part in _profile_items(snap.profile):
            _upsert(st, key, subject, value, source="resume", source_ref=_PROFILE_REF)
        for mf in snap.memory_facts:
            key, subject, outcome = _memory_fact_item(mf)
            if key is None:
                continue
            kw = {"source": "chat", "source_ref": f"resume_agent.db:memory_facts:{mf.get('type')}:{mf.get('key')}",
                  "confidence": float(mf.get("confidence") or 0.7)}
            if outcome == NEEDS_CHECK:
                kw.update(status=NEEDS_CHECK, check_reason="from_chat")
            _upsert(st, key, subject, str(mf.get("value") or ""), **kw)
        for name, n in snap.transcripts:
            if not st.q("SELECT 1 FROM event WHERE kind='diary_indexed' AND ref=? AND detail LIKE ?",
                        (name, f'%"lines": {n},%')):
                st.log("diary_indexed", ref=name, lines=n, scratch=name.startswith("autoapply_"))


# ---------------------------------------------------------------------------- verify
def _sig(r) -> tuple | None:
    return None if r is None else (r["key"], r["question"], r["answer"], r["source"])


def verify(st: Store, snap: Snapshot) -> list[str]:
    """The names of whatever differs between the legacy stores and the brain (keys only)."""
    bad: list[str] = []
    brain = BrainLibrary(st)
    if snap.library is not None:
        legacy = snap.library
        keys = {r["key"] for r in snap.rows} | {"@" + k for k, _ in A._CANON}
        for k in sorted(keys):
            if _sig(legacy.best(k)) != _sig(brain.best(k)):
                bad.append(f"best:{k}")
            if sorted(map(_sig, legacy.candidates(k))) != sorted(map(_sig, brain.candidates(k))):
                bad.append(f"candidates:{k}")
        if sorted(map(_sig, legacy.list())) != sorted(map(_sig, brain.list())) or legacy.count() != brain.count():
            bad.append("list")
    elif brain.count():
        bad.append("list")
    from autoapply import workauth as W
    view = prefs_view(st)
    if W.load(view) != W.load(snap.prefs):
        bad.append("prefs:work_auth")
    if bool(view.get("eeo_decline")) != bool(snap.prefs.get("eeo_decline")):
        bad.append("prefs:eeo_decline")
    if view.get("accept_required_terms") != ("always" if snap.prefs.get("accept_required_terms") == "always" else "ask"):
        bad.append("prefs:accept_required_terms")
    for fld, _key in K.PREFS_FIELDS:
        if str(view.get(fld) or "").strip() != str(snap.prefs.get(fld) or "").strip():
            bad.append(f"prefs:{fld}")
    return bad


# ---------------------------------------------------------------------------- the steps
def backup(data_dir) -> str:
    """Copy the three legacy files into data/brain-backup-<date>/ (a new folder each time)."""
    data = Path(data_dir)
    files = [f for name in ("answers.db", "app_prefs.json", "resume_agent.db")
             for f in ([data / name] + (db_files(data / name)[1:] if name.endswith(".db") else [])) if f.exists()]
    if not files:
        return ""
    base = data / f"brain-backup-{_dt.date.today().isoformat()}"
    dest, i = base, 2
    while dest.exists():
        dest, i = Path(f"{base}-{i}"), i + 1
    dest.mkdir(parents=True)
    for f in files:
        shutil.copy2(f, dest / f.name)
    return dest.name


class _Mismatch(Exception):
    def __init__(self, bad):
        super().__init__(f"{len(bad)} mismatches")
        self.bad = bad


def dry_run(data_dir) -> dict:
    """The plan's counts, with no write anywhere in the data folder."""
    snap = Snapshot(data_dir)
    try:
        return {"ok": True, "dry_run": True, "counts": plan_counts(snap)}
    finally:
        snap.close()


def apply(data_dir, *, check=None) -> dict:
    """Back up (first time), write the plan in one transaction, verify, activate."""
    check = check or verify
    data = Path(data_dir)
    st = open_store(brain_path(data))
    if not st.meta("backup_dir"):
        st.set_meta("backup_dir", backup(data) or "none")
    snap = Snapshot(data)
    try:
        counts = plan_counts(snap)
        try:
            with st.tx():
                _write(st, snap)
                bad = check(st, snap)
                if bad:
                    raise _Mismatch(bad)
                st.set_meta("brain_active", "1")
                if st.meta("mirror_legacy") is None:
                    st.set_meta("mirror_legacy", "1")
                st.set_meta("rolled_back", "0")
                st.set_meta("migrate_tries", "0")
                st.set_meta("report", json.dumps(counts, sort_keys=True))
                st.set_meta("migrated_at", _dt.datetime.now().isoformat(timespec="seconds"))
        except _Mismatch as m:
            tries = int(st.meta("migrate_tries") or 0) + 1
            st.set_meta("migrate_tries", str(tries))
            st.log("migration_mismatch", n=len(m.bad), tries=tries)
            return {"ok": False, "active": False, "counts": counts, "mismatches": len(m.bad),
                    "where": m.bad[:20], "tries": tries}
        st.log("migration", n=sum(sum(v.values()) for v in counts.values()))
        return {"ok": True, "active": True, "counts": counts, "mismatches": 0}
    finally:
        snap.close()


def rollback(data_dir) -> dict:
    """Readers go back to the legacy stores. The brain file stays."""
    st = open_store(brain_path(data_dir))
    st.set_meta("brain_active", "0")
    st.set_meta("rolled_back", "1")
    st.log("rollback")
    with _LOCK:
        _TRIED.discard(str(Path(data_dir).resolve()))
    return {"ok": True, "active": False}


def status(data_dir) -> dict:
    """Counts only: whether the brain is on, the migration report, rows per table."""
    p = brain_path(data_dir)
    if not p.exists():
        return {"ok": True, "exists": False, "active": False, "env_off": env_off()}
    st = open_store(p)
    try:
        report = json.loads(st.meta("report") or "{}")
    except ValueError:
        report = {}
    return {"ok": True, "exists": True, "env_off": env_off(),
            "active": st.meta("brain_active") == "1" and not env_off(),
            "mirror": st.meta("mirror_legacy", "1") == "1", "rolled_back": st.meta("rolled_back") == "1",
            "schema": int(st.meta("schema_version") or 0), "tries": int(st.meta("migrate_tries") or 0),
            "migrated_at": st.meta("migrated_at") or "", "report": report, **st.counts()}


# ---------------------------------------------------------------------------- at start-up
_LOCK = threading.Lock()
_TRIED: set[str] = set()


def ensure(data_dir) -> Store | None:
    """The brain for this data folder when it is on, migrating it the first time (once per
    process, three tries over all starts). None means: read the legacy stores."""
    if env_off():
        return None
    key = str(Path(data_dir).resolve())
    with _LOCK:
        st = open_store(brain_path(key))
        if st.meta("brain_active") == "1":
            return st
        if st.meta("rolled_back") == "1" or key in _TRIED or int(st.meta("migrate_tries") or 0) >= MAX_TRIES:
            return None
        _TRIED.add(key)
        try:
            res = apply(key)
        except Exception as exc:                    # never blocks the app: legacy stores carry on
            print(f"applicant brain: migration did not run ({type(exc).__name__})", file=sys.stderr)
            return None
        return st if res.get("ok") else None


def forget_process_state(data_dir) -> None:
    """Close the brain and forget this process's migration attempt (before Erase deletes it)."""
    key = str(Path(data_dir).resolve())
    with _LOCK:
        _TRIED.discard(key)
    close_store(brain_path(key))


# ---------------------------------------------------------------------------- command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m applicant.migrate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data", help="the data folder (default: ./data)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="count what would move; write nothing")
    g.add_argument("--apply", action="store_true", help="migrate (or catch up) and turn the brain on")
    g.add_argument("--rollback", action="store_true", help="put every reader back on the legacy stores")
    g.add_argument("--status", action="store_true", help="counts only")
    a = ap.parse_args(argv)
    data = Path(a.data)
    if not data.is_dir():
        print(f"No data folder at {data}", file=sys.stderr)
        return 2
    if a.dry_run:
        out = dry_run(data)
    elif a.rollback:
        out = rollback(data)
    elif a.apply:
        st = open_store(brain_path(data))
        st.set_meta("rolled_back", "0")
        st.set_meta("migrate_tries", "0")
        out = apply(data)
    else:
        out = status(data)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
