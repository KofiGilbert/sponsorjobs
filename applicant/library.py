"""The answer library and the prefs answers, read from the applicant brain (Phase 1).

``BrainLibrary`` has the same methods as ``autoapply.answers.AnswerLibrary`` (lookup, save,
best, candidates, get_key, update, delete, mark_used, list, count, close) and the same rules:
the same keys (``answers.normalize``), the same ranking (``answers.best_row``), the person's
own answer never replaced by a drafted one, per-job questions never saved, unusable answers
kept but never handed out. Its rows are the brain's ``fact`` rows of the answer-library view
(``store = 'answers.db'``): a canonical key as it is (``@salary``), a wording key as
``q:<wording key>``. A changed answer adds a fact and marks the old one replaced; a delete is a
hard delete of the fact and its history.

Mirroring (docs/applicant-brain.md, section 11): while ``meta.mirror_legacy = 1`` every write is
copied one way into ``answers.db``, row for row (same key, question, answer, source, dates and
use counts), so the browser extension, any older code path and a rollback all see every answer.

Catch-up: ``catch_up_library`` and ``catch_up_prefs`` carry into the brain any change a legacy
store holds that the brain did not see (a write made while ``SPONSORJOBS_BRAIN=0``, a row an
older build wrote, a mirror write that failed). With mirroring on, the two are equal and the
catch-up changes nothing.

The prefs answers (``app_prefs.json``) live in the same table under ``store = 'app_prefs.json'``:
one ``workauth`` fact per country, ``relocate``, ``@eeo_decline``, ``accept_required_terms``
(stored only when "always") and the flat answers
(``@salary`` from desired_salary, ``@start_date``, ``@heard_about``, ``@years_experience``,
``@over_18``). ``prefs_view`` turns them back into the prefs shape the app reads.

Personal data: nothing here leaves the machine, and no value is logged.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from applicant import keys as K
from applicant.store import ACTIVE, NEEDS_CHECK, REPLACED, STORE_ANSWERS, STORE_PREFS, Store
from autoapply import answers as A


def _locked(fn):
    """A write and its mirror run under the store's lock: a catch-up in another thread never
    lands between them (it would copy the older answers.db row back over the new answer)."""
    def wrapper(self, *a, **k):
        with self.store.lock:
            return fn(self, *a, **k)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def _code(lkey: str, answer: str) -> str:
    """(Phase 3) The normalized value of a choice key (docs section 5.1 ``code``): a yearly salary
    in whole units, a country by its name, a self-identification choice's code. "" otherwise."""
    try:
        a = str(answer or "")
        if lkey == "@salary":
            from autoapply.basics import parse_salary
            n = parse_salary(a)
            return str(n) if n else ""
        if lkey in ("@citizenship", "@citizenship2"):
            from autoapply import countries
            return countries.canonical(a)
        if lkey in ("@gender", "@sex", "@race", "@ethnicity", "@hispanic_latino", "@veteran", "@disability"):
            from autoapply import eeo
            if lkey == "@hispanic_latino":
                return eeo.hispanic_class(a) or ""
            return eeo.value_class(lkey, a, saved=True) or ""
    except Exception:          # noqa: BLE001 - the code is a convenience, the value is the truth
        return ""
    return ""


def _lib_status(lkey: str, question: str, answer: str) -> tuple[str, str]:
    """A library row the agent can never use (a raw chat reply an older run saved) is kept as
    needs_check (raw_reply). It is never deleted; the ranking still skips it exactly as the
    answer library does."""
    canon = A.normalize(question)
    if A.unusable(lkey, answer) or (canon.startswith("@") and A.unusable(canon, answer)):
        return NEEDS_CHECK, "raw_reply"
    return ACTIVE, ""


class BrainLibrary:
    """The answer library over the brain's facts. Same methods and answers as AnswerLibrary."""

    def __init__(self, store: Store, mirror_path=None):
        self.store = store
        self.mirror_path = str(mirror_path) if mirror_path else ""
        self.path = self.mirror_path
        self._stuck_keys: dict | None = None
        self._legacy_conn = None
        # (Phase 3) Where this library's writes come from (card, answers_page, extension...),
        # kept as the fact's source ref. The fact's source stays the library's own you/drafted.
        self.via = ""

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self):
        """A connection to answers.db itself, for older code that reads or writes that file
        directly. What it writes reaches the brain through the catch-up on the next open."""
        if self._legacy_conn is None:
            if not self.mirror_path:
                raise AttributeError("conn: this BrainLibrary has no legacy answers.db")
            A.AnswerLibrary(self.mirror_path).close()          # the table exists
            self._legacy_conn = sqlite3.connect(self.mirror_path)
            self._legacy_conn.execute("PRAGMA secure_delete=ON")
            self._legacy_conn.row_factory = sqlite3.Row
        return self._legacy_conn

    def close(self):
        """The store stays open (one connection per process); only a legacy connection closes."""
        if self._legacy_conn is not None:
            try:
                self._legacy_conn.close()
            except Exception:
                pass
            self._legacy_conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _current(self, lkey: str):
        rows = self.store.current(K.fact_key(lkey), "", STORE_ANSWERS)
        return rows[0] if rows else None

    def _rows(self):
        return self.store.q("SELECT * FROM fact WHERE person='default' AND store=? AND subject=''"
                            " AND status IN (?,?) ORDER BY id", (STORE_ANSWERS, ACTIVE, NEEDS_CHECK))

    @staticmethod
    def _row(f) -> dict:
        q = str(f["question"] or "")
        return {"key": K.library_key(f["key"]), "question": A.clean_label(q) or q, "answer": f["value"],
                "source": f["source"], "updated_at": f["updated_at"], "last_used": f["last_used"],
                "uses": f["uses"]}

    def _put(self, lkey: str, question: str, answer: str, source: str, *, created_at, updated_at,
             uses: int, last_used, legacy_import: bool = False, ref: str = "") -> int:
        """Write the current value of a library key: a new fact, the old one marked replaced."""
        status, reason = _lib_status(lkey, question, answer)
        with self.store.tx() as c:
            old = [r["id"] for r in self.store.current(K.fact_key(lkey), "", STORE_ANSWERS)]
            new_id = self.store.add_fact(
                K.fact_key(lkey), answer, source=source, store=STORE_ANSWERS, question=question,
                source_ref=ref or f"{STORE_ANSWERS}:{lkey}", status=status, check_reason=reason, uses=uses,
                last_used=last_used, created_at=created_at, updated_at=updated_at, replace=False,
                legacy_import=legacy_import, code=_code(lkey, answer))
            if old:
                c.execute(f"UPDATE fact SET status=?, replaced_by=? WHERE id IN ({','.join('?' * len(old))})",
                          (REPLACED, new_id, *old))
        self._stuck_keys = None
        return new_id

    # ------------------------------------------------------------------ the mirror
    def _mirror(self, *lkeys: str) -> None:
        """Copy these keys' current rows into answers.db exactly (or remove a deleted one)."""
        if not self.mirror_path or self.store.meta("mirror_legacy", "1") != "1":
            return
        rows = {k: self._current(k) for k in lkeys}
        try:
            legacy = A.AnswerLibrary(self.mirror_path)
        except sqlite3.Error:
            self.store.log("mirror_failed", store=STORE_ANSWERS, n=len(lkeys))
            return
        try:
            for k, f in rows.items():
                if f is None:
                    legacy.conn.execute("DELETE FROM answers WHERE key=?", (k,))
                else:
                    # An UPDATE in place where the row exists (its place in the table stays, as
                    # with the library's own writes), an INSERT otherwise.
                    vals = (f["question"], f["value"], f["source"], f["created_at"], f["updated_at"],
                            f["last_used"], f["uses"], k)
                    if not legacy.conn.execute(
                            "UPDATE answers SET question=?, answer=?, source=?, created_at=?, updated_at=?,"
                            " last_used=?, uses=? WHERE key=?", vals).rowcount:
                        legacy.conn.execute(
                            "INSERT INTO answers (question, answer, source, created_at, updated_at, last_used,"
                            " uses, key) VALUES (?,?,?,?,?,?,?,?)", vals)
            legacy.conn.commit()
        except sqlite3.Error:
            self.store.log("mirror_failed", store=STORE_ANSWERS, n=len(lkeys))
        finally:
            legacy.close()

    # ------------------------------------------------------------------ AnswerLibrary's methods
    @_locked
    def save(self, question: str, answer: str, source: str = "you", *, company: str = "",
             ref: str = "") -> dict | None:
        """AnswerLibrary.save, plus (Phase 3) ``ref``: where the answer came from (an ask id, the
        card), kept as the fact's source ref (``via`` when not given)."""
        ref = str(ref or self.via or "")
        raw = str(question or "").strip()
        q = (A.clean_label(raw) or raw)[:A.MAX_Q]
        a = str(answer or "").strip()[:A.MAX_A]
        source = source if source in A.SOURCES else "you"
        key = A.normalize(q)
        if not key or not a or A.is_per_job(q, company) or A.unusable(key, a):
            return None
        if K.sensitivity(K.fact_key(key), q) == K.EEO and source in K.EEO_REFUSED_SOURCES:
            return None                     # a self-identification answer is the person's own only
        now = time.time()
        cur = self._current(key)
        if cur is not None:
            if source == "drafted" and cur["source"] == "you":
                return None
            self._put(key, q, a, source, created_at=cur["created_at"], updated_at=now,
                      uses=cur["uses"], last_used=cur["last_used"], ref=ref)
        else:
            self._put(key, q, a, source, created_at=now, updated_at=now, uses=0, last_used=None, ref=ref)
        self._mirror(key)
        return self.get_key(key)

    def get_key(self, key: str) -> dict | None:
        f = self._current(key) if key else None
        return self._row(f) if f is not None else None

    def _stuck(self) -> dict:
        if self._stuck_keys is None:
            out: dict = {}
            for f in self._rows():
                lk = K.library_key(f["key"])
                if lk.startswith("@"):
                    continue
                k = A.normalize(f["question"])
                if k.startswith("@") and k != lk:
                    out.setdefault(k, []).append(lk)
            self._stuck_keys = out
        return self._stuck_keys

    def candidates(self, key: str) -> list[dict]:
        rows = []
        r = self.get_key(key) if key else None
        if r:
            rows.append(r)
        if key.startswith("@"):
            for k in self._stuck().get(key, []):
                x = self.get_key(k)
                if x:
                    rows.append(x)
        return rows

    def best(self, key: str) -> dict | None:
        return A.best_row(key, self.candidates(key))

    def lookup(self, question: str) -> dict | None:
        key = A.normalize(question)
        return self.best(key) if key else None

    @_locked
    def mark_used(self, key: str):
        f = self._current(key)
        if f is None:
            return
        self.store.update_fact(f["id"], uses=int(f["uses"] or 0) + 1, last_used=time.time())
        self._mirror(key)

    @_locked
    def update(self, key: str, *, question: str | None = None, answer: str | None = None) -> dict | None:
        cur = self.get_key(key)
        if cur is None:
            return None
        q = (question if question is not None else cur["question"]).strip()[:A.MAX_Q]
        a = (answer if answer is not None else cur["answer"]).strip()[:A.MAX_A]
        if not a or not A.normalize(q):
            return None
        new_key = A.normalize(q)
        raw = self._current(key)
        same_words = A.clean_label(q).lower() == A.clean_label(str(raw["question"] if raw else "")).lower()
        if new_key != key and same_words and self.get_key(new_key) is not None:
            if A.unusable(new_key, a):
                return None
            self._put(key, raw["question"], a, "you", created_at=raw["created_at"], updated_at=time.time(),
                      uses=raw["uses"], last_used=raw["last_used"])
            self._mirror(key)
            return self.get_key(key)
        row = self.save(q, a, "you")
        if row is not None and new_key != key:
            self.delete(key)
        return row

    @_locked
    def delete(self, key: str) -> bool:
        n = self.store.forget(K.fact_key(key), "", STORE_ANSWERS) if key else 0
        self._stuck_keys = None
        if n:
            self._mirror(key)
        return n > 0

    def list(self, query: str = "") -> list[dict]:
        rows = sorted(self._rows(), key=lambda f: (f["source"] != "you", -float(f["updated_at"] or 0)))
        out = [self._row(f) for f in rows]
        q = str(query or "").strip().lower()
        if q:
            out = [r for r in out if q in r["question"].lower() or q in r["answer"].lower()]
        return out

    def count(self) -> int:
        return len(self._rows())


# ---------------------------------------------------------------------------- catch-up
def legacy_rows(conn) -> list[dict]:
    """Every answers.db row, in table order."""
    return [dict(r) for r in conn.execute(
        "SELECT key, question, answer, source, created_at, updated_at, last_used, uses FROM answers ORDER BY rowid")]


def catch_up_library(store: Store, rows: list[dict]) -> dict:
    """Make the brain's answer-library view equal these answers.db rows. Returns counts."""
    lib = BrainLibrary(store)
    n = {"added": 0, "changed": 0, "used": 0, "removed": 0}
    with store.tx():
        have: dict = {}
        for f in lib._rows():
            have.setdefault(K.library_key(f["key"]), []).append(f)
        seen = set()
        for r in rows:
            k = str(r["key"] or "")
            if not k or k in seen:
                continue
            seen.add(k)
            cur = have.get(k) or []
            f = cur[-1] if cur else None
            same = f is not None and len(cur) == 1 and (
                f["question"], f["value"], f["source"], f["created_at"], f["updated_at"]) == (
                r["question"], r["answer"], r["source"], r["created_at"], r["updated_at"])
            if f is None or not same:
                lib._put(k, str(r["question"] or ""), str(r["answer"] or ""), str(r["source"] or "you"),
                         created_at=r["created_at"], updated_at=r["updated_at"], uses=int(r["uses"] or 0),
                         last_used=r["last_used"], legacy_import=True)
                n["changed" if f is not None else "added"] += 1
            elif (int(f["uses"] or 0), f["last_used"]) != (int(r["uses"] or 0), r["last_used"]):
                store.update_fact(f["id"], uses=int(r["uses"] or 0), last_used=r["last_used"])
                n["used"] += 1
        for k in set(have) - seen:                    # deleted in the legacy store: forget here too
            store.forget(K.fact_key(k), "", STORE_ANSWERS)
            n["removed"] += 1
    return n


def catch_up_library_file(store: Store, path, prefs: dict | None = None) -> dict:
    """Open answers.db as the app always has (re-keyed by the current rules, seeded once from
    the prefs answers) and carry its rows into the brain."""
    with store.lock:
        legacy = A.AnswerLibrary(path)
        try:
            if prefs is not None:
                try:
                    A.seed_once(legacy, prefs)
                except Exception:
                    pass
            return catch_up_library(store, legacy_rows(legacy.conn))
        finally:
            legacy.close()


# ---------------------------------------------------------------------------- the prefs answers
def prefs_facts(prefs: dict | None) -> dict:
    """{(key, subject): (value, field)}: the brain facts the prefs answers hold."""
    from autoapply import workauth as W
    p = prefs or {}
    out: dict = {}
    wa = W.load(p)
    for cc, e in sorted((wa.get("countries") or {}).items()):
        out[("workauth", cc)] = (json.dumps(e, sort_keys=True), "work_auth")
    if wa.get("relocate"):
        out[("relocate", "")] = (wa["relocate"], "work_auth.relocate")
    if p.get("eeo_decline"):
        out[("@eeo_decline", "")] = ("Yes", "eeo_decline")
    if p.get("accept_required_terms") == "always":          # "ask" is the default, never stored
        out[("accept_required_terms", "")] = ("always", "accept_required_terms")
    for fld, key in K.PREFS_FIELDS:
        v = str(p.get(fld) or "").strip()
        if v:
            out[(key, "")] = (v, fld)
    return out


def catch_up_prefs(store: Store, prefs: dict | None, source: str = "you") -> dict:
    """Make the brain's prefs view hold exactly these prefs answers. Returns counts."""
    want = prefs_facts(prefs)
    n = {"added": 0, "changed": 0, "removed": 0}
    with store.tx():
        have = {(f["key"], f["subject"]): f for f in store.facts(store=STORE_PREFS, status=(ACTIVE, NEEDS_CHECK))}
        for (key, subject), (value, fld) in want.items():
            f = have.get((key, subject))
            if f is not None and f["value"] == value:
                continue
            src = source if not (K.sensitivity(key) == K.EEO and source in K.EEO_REFUSED_SOURCES) else "you"
            store.add_fact(key, value, source=src, subject=subject, store=STORE_PREFS,
                           source_ref=f"{STORE_PREFS}:{fld}", confirmed=1)
            n["changed" if f is not None else "added"] += 1
        for (key, subject) in set(have) - set(want):   # cleared in the prefs: forgotten here too
            store.forget(key, subject, STORE_PREFS)
            n["removed"] += 1
    return n


def prefs_view(store: Store) -> dict:
    """The prefs answers as the app reads them (the app_prefs.json shape), from the brain."""
    from autoapply import workauth as W
    cur = {(f["key"], f["subject"]): f["value"]
           for f in store.facts(store=STORE_PREFS, status=(ACTIVE, NEEDS_CHECK))}
    countries = {}
    for (key, subject), v in cur.items():
        if key == "workauth":
            try:
                countries[subject] = json.loads(v)
            except ValueError:
                continue
    wa = W.clean({"countries": countries, "relocate": cur.get(("relocate", ""), "")})
    view = {"work_auth": wa, **W.legacy_prefs(wa), "eeo_decline": cur.get(("@eeo_decline", "")) == "Yes",
            "accept_required_terms": "always" if cur.get(("accept_required_terms", "")) == "always" else "ask"}
    for fld, key in K.PREFS_FIELDS:
        view[fld] = cur.get((key, ""), "")
    return view


def read_prefs_file(path) -> dict:
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}
