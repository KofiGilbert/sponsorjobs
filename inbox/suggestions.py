"""Stage suggestions from the person's own inbox (CLAUDE.md §5, read-first).

A scanned message tied to an application on the Tracker board that reads like an interview
request, a rejection, an offer or an acknowledgement becomes ONE suggestion: "Move <company> to
<stage>", with the phrase that decided it; a message that asks questions also carries them, and
a reply can be drafted from the person's saved facts. The person clicks Move or Not this.

Suggestions live in a small local JSON file keyed by message id, with a status
(pending / accepted / dismissed), so the same mail never asks twice: a rescan sees the id
is already known and leaves it alone.

Matching (inbox/matching.py) runs first: only mail tied to a tracked application by its
thread, its sender's domain or the employer's name is read any further. Then one label per
message (inbox/replies.py): the model's when one is available, the rules' otherwise. Personal
mail that only names the employer never reaches the model. A plain acknowledgement matched by
thread or domain is the one move the app makes by itself, shown in the strip with Undo.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sourcing.board import stage_move_makes_sense

from . import detect

# "auto": a plain acknowledgement the app applied itself (still undoable from the strip).
STATUSES = ("pending", "accepted", "dismissed", "auto")
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


def build_suggestions(messages: list[dict], jobs: list[dict], llm=None,
                      threads: dict[str, str] | None = None, on_match=None) -> list[dict]:
    """One suggestion per message that (a) belongs to an application on the board (thread,
    then sender domain, then company name, inbox/matching.py) and (b) either argues for a stage
    the job is not at yet or asks the person something. ``jobs`` are board rows (source_id,
    company, title, stage, url); ``threads`` is Watchlist.thread_index().

    Each message gets one label (inbox/replies.py): the model's when ``llm`` is given, checked
    against the rules, else the rules'. ``auto`` is True only for a plain acknowledgement matched
    by thread or domain, the one kind the caller may apply without a tap.

    ``on_match(msg, job, lane, label)`` is called for every message tied to an application,
    whether or not it becomes a suggestion, so the caller can remember it (thread matching, and
    never labelling the same message twice)."""
    from .replies import STAGE_FOR_LABEL, classify_reply, own_text, rules_reply
    from .matching import COMPANY, DOMAIN, THREAD, match_application
    from .matching import _FREE_MAIL  # noqa: PLC2701 - one list of personal mail domains

    out: list[dict] = []
    for msg in messages or []:
        if detect.classify(msg) == "verification":
            continue            # an application's own verification link is handled on its own
        job, lane = match_application(msg, jobs, threads)
        if job is None:
            continue
        rules = rules_reply(msg)
        sender = detect.registrable_domain(detect.sender_domain(msg))
        if lane == COMPANY and rules["label"] == "other" and (not sender or sender in _FREE_MAIL):
            # Personal mail that only names the employer ("how did the Acme thing go?") is not
            # application mail: it never reaches the model (section 5, read-first).
            continue
        v = classify_reply(msg, llm=llm, company=job.get("company") or "", rules=rules)
        label = v["label"]
        if on_match is not None:
            on_match(msg, job, lane, label)
        if label == "other":
            continue
        target = STAGE_FOR_LABEL.get(label, "")
        move = bool(target) and stage_move_makes_sense(job.get("stage") or "", target)
        if not move and not v["questions"]:
            continue            # nothing new to tell the person
        out.append({
            "id": suggestion_id(msg),
            "label": label,
            "kind": v["kind"],
            "by": v.get("by") or "rules",
            "stage": target if move else "",
            "from_stage": job.get("stage") or "",
            "source_id": job["source_id"],
            "company": job.get("company") or "",
            "title": job.get("title") or "",
            "matched_by": lane,
            "subject": str(msg.get("subject") or "")[:200],
            "sender": detect.sender_name(msg),
            "from": str(msg.get("from") or "")[:200],
            "date": str(msg.get("date") or "")[:60],
            "snippet": " ".join(str(msg.get("body") or "").split())[:240],
            # The new part of the message, kept locally so a reply can be drafted later.
            "excerpt": own_text(str(msg.get("body") or ""))[:2000],
            "questions": v["questions"],
            "interview_time": v["interview_time"],
            "reason": v.get("reason") or "",
            "phrase": v.get("phrase") or "",
            "message_id": str(msg.get("id") or suggestion_id(msg))[:200],
            "thread_id": str(msg.get("thread_id") or "")[:200],
            "rfc_id": str(msg.get("rfc_id") or "")[:300],
            "auto": bool(move and v.get("plain_ack") and lane in (THREAD, DOMAIN)),
            "status": "pending",
            "created_at": _now(),
        })
    return out


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

    def shown(self) -> list[dict]:
        """What the "From your inbox" strip lists: every pending suggestion, plus the moves the
        app made on its own that the person has not acknowledged yet (so they can Undo)."""
        return [s for s in self._load() if s.get("status", "pending") in ("pending", "auto")
                and not s.get("seen")]

    def update(self, sid: str, **fields) -> dict | None:
        """Merge fields into one suggestion (a drafted reply, the "seen" flag)."""
        items = self._load()
        hit = None
        for s in items:
            if s.get("id") == sid:
                s.update(fields)
                hit = s
        if hit is not None:
            self._save(items)
        return hit

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
