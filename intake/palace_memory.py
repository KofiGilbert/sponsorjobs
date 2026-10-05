"""Durable verbatim + semantic memory, built on MemPalace (CLAUDE.md §4b, §5).

Two layers, one purpose — never forget anything the person told us, on any
entry point (New CV or Tailor-from-job):

* **Verbatim layer (always, synchronous, model-free).** Every conversation turn
  is appended to an append-only JSONL transcript the moment it happens, inside
  ``WebIntake.submit()`` — not only on Accept. This is the durability guarantee:
  it needs no model, no network, and survives a server restart mid-conversation.

* **Semantic layer (MemPalace).** Each turn is also indexed into a local
  MemPalace palace (ChromaDB + the ``minilm`` / all-MiniLM-L6-v2 ONNX embedder,
  Apache-2.0) so a CV build can *recall* the most relevant prior statements for
  the current JD. This is best-effort: if the model/embedder is unavailable, the
  turn is already safe in the transcript and the chat never breaks.

Local-first & vetted (per the MemPalace source audit): base install only, the
``minilm`` embedder (no Gemma), and none of MemPalace's optional network features
(no LLM-refine, no Wikipedia lookup) are ever invoked — only ``diary_write`` and
``search_memories``, which touch nothing but the local palace. The one and only
network call in the whole layer is ChromaDB's one-time ~80 MB minilm model
download on first use, cached under ``~/.cache/chroma`` thereafter.

Set ``RESUME_AGENT_PALACE_INDEX=0`` to keep the verbatim layer but skip semantic
indexing/recall (used by the offline test suite to stay fast and model-free).
"""

from __future__ import annotations

import json
import os
import re
import warnings
from datetime import datetime
from pathlib import Path


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "_", (name or "default").strip().lower()).strip("_")
    return s or "default"


class PalaceMemory:
    """Per-person durable memory. ``person`` keys both the transcript file and the
    palace ``wing``, so a returning person's history accumulates across sessions."""

    def __init__(self, palace_dir, person: str = "default", agent: str = "resume-agent"):
        self.person = _slug(person)
        self.agent = agent
        self.dir = Path(palace_dir)
        self.transcripts = self.dir / "transcripts"
        self.store = self.dir / "store"           # ChromaDB palace lives here
        self.log_path = self.transcripts / f"{self.person}.jsonl"
        # Index (embed) turns unless explicitly disabled (offline test suite).
        self.index = os.environ.get("RESUME_AGENT_PALACE_INDEX", "1") != "0"
        self._mp = None   # (service, search_memories) | False once resolution is attempted
        try:
            self.transcripts.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ mempalace
    def _mempalace(self):
        """Lazily import MemPalace and pin local-only, minilm settings. Returns
        (service, search_memories) or None if the semantic layer is unavailable."""
        if self._mp is None:
            try:
                # Pin the vetted embedder; never Gemma, never a config-file override.
                os.environ["MEMPALACE_EMBEDDING_MODEL"] = "minilm"
                warnings.filterwarnings("ignore", message=r".*has no recorded embedder identity.*")
                from mempalace import service as _service
                from mempalace.searcher import search_memories as _search
                self.store.mkdir(parents=True, exist_ok=True)
                self._mp = (_service, _search)
            except Exception:
                self._mp = False
        return self._mp or None

    @property
    def enabled(self) -> bool:
        """True when the semantic layer will actually be used."""
        return self.index and self._mempalace() is not None

    # ------------------------------------------------------------------ write
    def remember_turn(self, role: str, content: str) -> None:
        """Persist one turn verbatim (always) and index it semantically (best-effort).

        Called for every user and agent turn, as it happens. The transcript write
        is the durability guarantee; the palace write is best-effort and never
        allowed to lose a turn or break the conversation."""
        content = (content or "").strip()
        if not content:
            return
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "role": role, "content": content}
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # extremely unlikely; the semantic layer below is a second copy

        if not self.enabled:
            return
        try:
            service, _ = self._mp
            service.run_diary_write({
                "palace_path": str(self.store),
                "agent_name": self.agent,
                "entry": f"[{role}] {content}",
                "topic": "conversation",
                "wing": self.person,
            })
        except Exception:
            pass  # the turn is already durable in the transcript

    def remember_summary(self, summary: str) -> None:
        """Store a CLEAN, tool-authored fact summary (e.g. of an uploaded CV) into
        the durable memory — the transcript AND the semantic index. Use this, never
        ``remember_turn`` with raw/OCR'd text, so the palace stays high-quality."""
        self.remember_turn("note", summary)

    def ingest_note(self, text: str, source: str = "note") -> int:
        """Add a freeform CAREER-MEMORY entry the person wants kept: a note, an event they shared,
        or text pulled from a document. Stored durably (transcript) and indexed (palace) so it can be
        recalled later for CVs, cover letters, and 'tell me about a time' answers. Long text is split
        into coherent chunks so each stored memory is a searchable unit. Returns the number of chunks
        stored. This is the lifelong-diary capture path (CLAUDE.md §4b)."""
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
    def load_history(self) -> list[dict]:
        """Every turn ever recorded for this person, from the verbatim transcript.
        Model-free — this is what makes a mid-conversation restart lose nothing."""
        turns: list[dict] = []
        if not self.log_path.exists():
            return turns
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                turns.append({"role": d.get("role", "user"), "content": d.get("content", "")})
        except OSError:
            pass
        return turns

    def erase(self) -> None:
        """Erase this person's durable memory (user-invoked, local, irreversible): delete the
        verbatim transcript, and drop the local semantic index so recall returns nothing. The
        transcript is the authoritative copy, so its deletion is the guarantee; the palace store
        is best-effort (this app is single-user local, so wiping the wing store is correct)."""
        import shutil
        try:
            if self.log_path.exists():
                self.log_path.unlink()
        except OSError:
            pass
        self._mp = None   # force lazy re-init against the now-empty store
        try:
            if self.store.exists():
                shutil.rmtree(self.store, ignore_errors=True)
        except OSError:
            pass

    def recall(self, query: str, n: int = 5) -> list[dict]:
        """Semantically retrieve the most relevant prior statements for ``query``.
        Returns [] when the semantic layer is disabled/unavailable (the caller
        still has the full verbatim history from ``load_history``)."""
        if not self.enabled:
            return []
        query = (query or "").strip()
        if not query:
            return []
        try:
            _, search = self._mp
            res = search(query, str(self.store), wing=self.person, n_results=n)
            out = []
            for r in (res.get("results") or []):
                text = str(r.get("text", "")).strip()
                if text:
                    out.append({"text": text, "similarity": round(float(r.get("similarity", 0.0)), 3)})
            return out
        except Exception:
            return []
