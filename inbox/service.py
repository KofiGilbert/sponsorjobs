"""Scan the person's own inbox for application-related mail and act on it (CLAUDE.md §5).

Read-first, send-on-consent. ``scan_inbox`` reads a batch of the person's messages,
keeps only the two kinds that relate to their applications (an application's email
verification, and a recruiter reply), and returns them for the review queue. It:

  * surfaces each **verification** with the link to visit (an autonomous apply may
    complete an application's OWN verification link; otherwise the person clicks it), and
  * drafts a reply to each **recruiter** message from the person's PROFILE, for the
    person to review, edit, and send.

Nothing is sent here. Drafting uses the injected ``llm`` so it is testable offline, and
messages are plain dicts so no live mailbox is needed. See ``inbox/gmail.py`` for the
OAuth-backed adapter that produces those dicts from the person's real Gmail.
"""

from __future__ import annotations

from . import detect


def scan_inbox(messages, profile=None, llm=None, draft_replies=True) -> dict:
    """Classify ``messages`` and build the review payload.

    Returns ``{"verifications": [...], "recruiters": [...], "scanned": int,
    "other": int}``. A recruiter entry carries a drafted ``reply`` only when both an
    ``llm`` and ``draft_replies`` are provided; otherwise ``reply`` is "" so the person
    still sees the message and can draft on demand.
    """
    profile = profile or {}
    verifications: list[dict] = []
    recruiters: list[dict] = []
    other = 0

    for msg in messages or []:
        kind = detect.classify(msg)
        if kind == "verification":
            verifications.append({
                "id": msg.get("id", ""),
                "from": msg.get("from", ""),
                "subject": msg.get("subject", ""),
                "date": msg.get("date", ""),
                "link": detect.verification_link(msg),
            })
        elif kind == "recruiter":
            status = detect.application_status(msg)
            reply = ""
            # A rejection gets no drafted reply: there is nothing to answer, and a model
            # call spent on it is money for a message the person only needs to see.
            if draft_replies and llm is not None and status != "rejected":
                reply = _safe_draft(llm, msg, profile)
            recruiters.append({
                "id": msg.get("id", ""),
                "from": msg.get("from", ""),
                "sender": detect.sender_name(msg),
                "subject": msg.get("subject", ""),
                "date": msg.get("date", ""),
                "snippet": (msg.get("body", "") or "")[:280],
                "reply": reply,
                "status": status,
            })
        else:
            other += 1

    return {
        "verifications": verifications,
        "recruiters": recruiters,
        "scanned": len(messages or []),
        "other": other,
    }


def draft_reply(msg: dict, profile: dict, llm) -> str:
    """Draft (or re-draft) a reply to one recruiter message — used by the on-demand endpoint."""
    return _safe_draft(llm, msg, profile or {})


def _safe_draft(llm, msg: dict, profile: dict) -> str:
    """Draft a reply, never letting an LLM error abort the whole scan."""
    try:
        return llm.draft_email_reply(
            msg.get("subject", ""), msg.get("body", ""), msg.get("from", ""), profile
        ) or ""
    except Exception:
        return ""
