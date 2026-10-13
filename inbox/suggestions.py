"""Stage suggestions from the person's own inbox (CLAUDE.md §5, read-first).

A scanned message that names an employer on the Tracker board and reads like an interview
invite, a rejection, an offer, an assessment, or an application confirmation becomes ONE
suggestion: "Move <company> to <stage>", with the phrase that decided it. The person clicks
Move or Not this; nothing moves on its own.

Suggestions live in a small local JSON file keyed by message id, with a status
(pending / accepted / dismissed), so the same mail never asks twice: a rescan sees the id
is already known and leaves it alone.

Deterministic rules decide (inbox/detect.triage). An optional model pass runs ONLY for mail
the rules left as "other" AND that names a tracked employer, so the model never reads
unrelated mail.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sourcing.board import match_job, stage_move_makes_sense

from . import detect

STATUSES = ("pending", "accepted", "dismissed")
MAX_KEPT = 500


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def suggestion_id(msg: dict) -> str:
    """A stable id for one message: the mailbox id when there is one, else a hash of the
    headers (an imported batch may carry no ids)."""
    mid = str(msg.get("id") or "").strip()
    if mid:
        return re.sub(r"[^A-Za-z0-9_.:-]", "_", mid)[:120]
    raw = "|".join(str(msg.get(k) or "") for k in ("from", "subject", "date"))
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def build_suggestions(messages: list[dict], jobs: list[dict], llm=None) -> list[dict]:
    """One suggestion per message that (a) names a tracked employer and (b) argues for a stage
    the job is not at yet. ``jobs`` are board rows (source_id, company, title, stage)."""
    out: list[dict] = []
    for msg in messages or []:
        text = " ".join(str(msg.get(k) or "") for k in ("from", "subject", "body"))[:4000]
        job = match_job(text, jobs)
        if job is None:
            continue
        t = detect.triage(msg)
        if t["kind"] == "other" and llm is not None:
            t = _model_triage(llm, msg, job.get("company") or "") or t
        stage = t.get("stage") or ""
        if not stage or not stage_move_makes_sense(job.get("stage") or "", stage):
            continue
        out.append({
            "id": suggestion_id(msg),
            "kind": t["kind"],
            "stage": stage,
            "from_stage": job.get("stage") or "",
            "source_id": job["source_id"],
            "company": job.get("company") or "",
            "title": job.get("title") or "",
            "subject": str(msg.get("subject") or "")[:200],
            "sender": detect.sender_name(msg),
            "from": str(msg.get("from") or "")[:200],
            "date": str(msg.get("date") or "")[:60],
            "snippet": " ".join(str(msg.get("body") or "").split())[:240],
            "reason": t.get("reason") or "",
            "phrase": t.get("phrase") or "",
            "status": "pending",
            "created_at": _now(),
        })
    return out


def _model_triage(llm, msg: dict, company: str) -> dict | None:
    """Ask the model what an unclassified message from a TRACKED employer is. Any failure or
    an unknown answer leaves the rules' verdict in place."""
    try:
        got = llm.classify_application_mail(
            str(msg.get("subject") or ""), str(msg.get("body") or "")[:3000],
            str(msg.get("from") or ""), company) or {}
    except Exception:
        return None
    kind = str(got.get("kind") or "other")
    if kind not in detect.KINDS or kind == "other":
        return None
    phrase = " ".join(str(got.get("phrase") or "").split())[:80]
    reason = str(got.get("reason") or "").strip()[:160] or (f'Reads as {kind.replace("_", " ")}: "{phrase}"' if phrase else f"Reads as {kind.replace('_', ' ')}")
    return {"kind": kind, "stage": detect.STAGE_FOR.get(kind, ""), "phrase": phrase, "reason": reason}


class SuggestionStore:
    """The local JSON file of suggestions. Keyed by id; a known id is never overwritten, so a
    dismissed or accepted suggestion stays that way across rescans."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items[-MAX_KEPT:], indent=1), encoding="utf-8")

    def all(self) -> list[dict]:
        return self._load()

    def pending(self) -> list[dict]:
        return [s for s in self._load() if s.get("status", "pending") == "pending"]

    def get(self, sid: str) -> dict | None:
        return next((s for s in self._load() if s.get("id") == sid), None)

    def add(self, suggestions: list[dict]) -> list[dict]:
        """Add the NEW ones (unknown ids) and return them. Known ids are left untouched."""
        items = self._load()
        known = {s["id"] for s in items}
        new = [s for s in suggestions if s.get("id") and s["id"] not in known]
        if new:
            items.extend(new)
            self._save(items)
        return new

    def set_status(self, sid: str, status: str) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        items = self._load()
        hit = None
        for s in items:
            if s.get("id") == sid:
                s["status"] = status
                s["decided_at"] = _now()
                hit = s
        if hit is not None:
            self._save(items)
        return hit

    def drop_for_job(self, source_id: str, status: str = "dismissed") -> int:
        """When one suggestion for a job is accepted, the other pending ones for the SAME
        stage are settled too (two "interview" mails from one employer are one move)."""
        items = self._load()
        n = 0
        for s in items:
            if s.get("source_id") == source_id and s.get("status", "pending") == "pending":
                s["status"] = status
                s["decided_at"] = _now()
                n += 1
        if n:
            self._save(items)
        return n
