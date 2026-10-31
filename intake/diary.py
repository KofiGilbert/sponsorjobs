"""The chat diary: every turn the person typed or the app noted, kept verbatim (CLAUDE.md §4b, §5).

One append-only JSONL file per person at ``<palace_dir>/transcripts/<person>.jsonl`` (the folder
name is historical: the files stay where they always were, so nothing moves on disk). Each line is
``{"at", "role", "content"}``. A turn is written the moment it happens, with no model and no
network, so a restart mid-conversation loses nothing (docs/applicant-brain.md, section 12).

Recall is a plain keyword search over the diary, in the standard library: the words a query
shares with each stored line, ranked by how many it shares and then newest first. It replaced
MemPalace's semantic index, which never ran in the installed app.

A diary line is addressed as ``diary:<file>:<line>`` (1-based line in the file), the
``source_ref`` a fact carries when a turn produced it.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

# Roles whose lines are the person's own material (or a clean note about it). The app's own
# questions ("agent"/"assistant") are kept in the diary but never recalled as memories.
RECALL_ROLES = ("user", "note")

# Line counts per diary file, keyed by path and checked against the file size, so appending
# a turn knows its line number without re-reading the whole file every time.
_LOCK = threading.Lock()
_LINES: dict[str, tuple[int, int]] = {}

_WORD = re.compile(r"[a-z0-9][a-z0-9+#]*(?:[./-][a-z0-9+#]+)*")

# Common English words and words every job post uses, which say nothing about fit.
_STOP = frozenset("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing done down during each either else etc ever
every few for from further get got had has have having he her here hers him his how i if in into
is it its itself just let like made make many may me more most much must my no nor not now of off
on once only or other our ours out over own per please same she should so some such than that the
their them then there these they this those through to too under until up upon us very via was we
well were what when where which while who whom why will with within without would yes yet you
your yours
ability able across apply applicant applicants based benefit benefits best candidate candidates
company day days desired environment equal etc experience experienced year years full help ideal
including job jobs join looking new opportunity plus position preferred qualification
qualifications related required requirement requirements responsibilities responsibility role
roles seeking skill skills strong team teams time using work working world
""".split())


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "_", (name or "default").strip().lower()).strip("_")
    return s or "default"


def _stem(w: str) -> str:
    """A light, symmetric suffix trim so 'migrations'/'migration' and 'dashboards'/'dashboard'
    meet. Applied to both sides, so it only has to be consistent, not linguistically right.
    Tech tokens with symbols ('node.js', 'c++') are left whole."""
    if not w.isalpha():
        return w
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def keywords(text: str) -> set[str]:
    """The meaningful words of ``text``: lower case, stop words dropped, lightly stemmed.
    Keeps tech tokens whole ('c++', 'c#', 'node.js', 'ci/cd')."""
    out = set()
    for w in _WORD.findall((text or "").lower()):
        w = w.strip(".-/")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(_stem(w))
    return out


class Diary:
    """One person's diary. ``person`` names the file, so a returning person's history
    accumulates across sessions."""

    def __init__(self, palace_dir, person: str = "default", agent: str = "resume-agent"):
        self.person = _slug(person)
        self.agent = agent
        self.dir = Path(palace_dir)
        self.transcripts = self.dir / "transcripts"
        self.store = self.dir / "store"      # MemPalace's old ChromaDB folder, only ever removed by Erase
        self.log_path = self.transcripts / f"{self.person}.jsonl"
        self.last_ref = ""                   # the diary ref of the last line this instance wrote
        try:
            self.transcripts.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ refs
    def ref(self, line: int) -> str:
        """The ``source_ref`` of one line of this diary: ``diary:default.jsonl:212``."""
        return f"diary:{self.log_path.name}:{int(line)}"

    def _line_count(self) -> int:
        """Lines already in the file (caller holds _LOCK)."""
        key = str(self.log_path)
        try:
            size = self.log_path.stat().st_size
        except OSError:
            _LINES.pop(key, None)
            return 0
        cached = _LINES.get(key)
        if cached and cached[0] == size:
            return cached[1]
        with open(self.log_path, "rb") as fh:
            n = sum(1 for _ in fh)
        _LINES[key] = (size, n)
        return n

    # ------------------------------------------------------------------ write
    def remember_turn(self, role: str, content: str) -> str:
        """Append one turn verbatim, as it happens. Returns its ``diary:<file>:<line>`` ref, or
        "" when there was nothing to keep or the write failed. Never raises."""
        content = (content or "").strip()
        if not content:
            return ""
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "role": role, "content": content}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            with _LOCK:
                self.transcripts.mkdir(parents=True, exist_ok=True)
                n = self._line_count()
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                _LINES[str(self.log_path)] = (self.log_path.stat().st_size, n + 1)
        except OSError:
            return ""
        self.last_ref = self.ref(n + 1)
        return self.last_ref

    def remember_summary(self, summary: str) -> str:
        """Keep a CLEAN, app-written note (a fact summary of an uploaded resume, say). Use this,
        never ``remember_turn`` with raw or OCR'd text, so recall stays high quality."""
        return self.remember_turn("note", summary)

    def ingest_note(self, text: str, source: str = "note") -> int:
        """Add a career-memory entry the person wants kept: a note, an event they shared, or text
        pulled from a document. Long text is split into paragraphs of up to about 1,200
        characters, each its own recallable line. Returns the number of lines stored."""
        text = (text or "").strip()
        if not text:
            return 0
        tag = _slug(source) if (source and source != "note") else ""
        chunks: list[str] = []
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            if buf and len(buf) + len(para) + 2 > 1200:
                chunks.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            chunks.append(buf)
        for ch in chunks:
            self.remember_summary(f"[{source}] {ch}" if tag else ch)
        return len(chunks)

    # ------------------------------------------------------------------ read
    def _lines(self) -> list[tuple[int, dict]]:
        """(1-based line number, record) for every readable line."""
        out: list[tuple[int, dict]] = []
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                for i, raw in enumerate(fh, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(d, dict):
                        out.append((i, d))
        except OSError:
            pass
        return out

    def load_history(self) -> list[dict]:
        """Every turn ever recorded for this person, oldest first."""
        return [{"role": d.get("role", "user"), "content": d.get("content", "")} for _i, d in self._lines()]

    def recall(self, query: str, n: int = 5) -> list[dict]:
        """The diary lines most relevant to ``query`` by plain word overlap, best first.

        Only the person's own lines and app notes are searched (not the app's questions). Lines
        are ranked by how many distinct query words they share, then newest first; the same text
        kept twice is returned once (its newest copy). Each hit is ``{text, similarity, ref}``:
        ``similarity`` is the share of the query's words found, where four words count as a
        full match (so 0.25 is one shared word, 0.5 two), and ``ref`` is its diary line."""
        q = keywords(query)
        if not q or n <= 0:
            return []
        full = min(4, len(q))
        scored = []
        seen: set[str] = set()
        for i, d in reversed(self._lines()):               # newest first, so ties keep that order
            if d.get("role") not in RECALL_ROLES:
                continue
            text = str(d.get("content") or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            shared = len(q & keywords(text))
            if shared:
                scored.append((shared, -i, text, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [{"text": text, "similarity": round(min(1.0, shared / full), 3), "ref": self.ref(i)}
                for shared, _neg, text, i in scored[:n]]

    # ------------------------------------------------------------------ erase
    def erase(self) -> None:
        """Erase this person's diary (user-invoked, local, irreversible), and the old MemPalace
        store folder if it is still on disk."""
        with _LOCK:
            _LINES.pop(str(self.log_path), None)
            try:
                if self.log_path.exists():
                    self.log_path.unlink()
            except OSError:
                pass
        _remove_store(self.store)
        self.last_ref = ""


_REF = re.compile(r"^diary:([A-Za-z0-9_.-]+\.jsonl):(\d+)$")


def forget_refs(palace_dir, refs) -> int:
    """Forget the diary lines these refs name (``diary:<file>:<line>``): each line is blanked in
    place, so every other line keeps its number and every other ref still points at the right
    turn. The file is rewritten whole (a temporary file, then a rename), so the words are gone
    from it. Returns how many lines were blanked. (Phase 6: Forget with "Also delete the chat
    lines where I said this".)"""
    by_file: dict[str, set[int]] = {}
    for ref in refs or ():
        m = _REF.match(str(ref or "").strip())
        if m:
            by_file.setdefault(m.group(1), set()).add(int(m.group(2)))
    tdir = Path(palace_dir) / "transcripts"
    n = 0
    with _LOCK:
        for name, lines in by_file.items():
            path = tdir / name
            if path.parent != tdir or not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                continue
            hit = 0
            for i in sorted(lines):
                if 1 <= i <= len(raw) and raw[i - 1].strip():
                    raw[i - 1] = "\n"
                    hit += 1
            if not hit:
                continue
            tmp = path.with_name(path.name + ".tmp")
            try:
                tmp.write_text("".join(raw), encoding="utf-8")
                tmp.replace(path)
            except OSError:
                continue
            _LINES.pop(str(path), None)
            n += hit
    return n


def stats(palace_dir, person: str = "default") -> dict:
    """{lines, first, last}: how many turns the person's diary keeps and the dates of the first
    and last (ISO dates, "" when empty). Counts and dates only, for the memory page."""
    d = Diary(palace_dir, person=person)
    rows = d._lines()
    ats = [str(r.get("at") or "")[:10] for _i, r in rows if r.get("at")]
    return {"lines": len(rows), "first": min(ats) if ats else "", "last": max(ats) if ats else ""}


def _remove_store(store: Path) -> None:
    try:
        if store.exists():
            shutil.rmtree(store, ignore_errors=True)
    except OSError:
        pass


def erase_all(palace_dir) -> list[str]:
    """Erase my memory: every diary file (the person's and any leftover apply-agent scratch
    diaries) and the old MemPalace store folder. Returns the names removed (no contents)."""
    root = Path(palace_dir)
    gone: list[str] = []
    tdir = root / "transcripts"
    with _LOCK:
        for f in sorted(tdir.glob("*.jsonl")) if tdir.is_dir() else []:
            _LINES.pop(str(f), None)
            try:
                f.unlink()
                gone.append(f.name)
            except OSError:
                pass
    store = root / "store"
    if store.exists():
        _remove_store(store)
        if not store.exists():
            gone.append("store")
    return gone
