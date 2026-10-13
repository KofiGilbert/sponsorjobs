"""Per-stage email drafts for a tracked job (CLAUDE.md §5: drafted for the PERSON to send).

One model call, grounded in the saved profile, the job and its stage. The draft comes back
as an editable subject and body; the person copies it or opens it in Gmail's compose window
and sends it themselves. Nothing here sends mail, creates a Gmail draft, or touches the
mailbox.
"""

from __future__ import annotations

from urllib.parse import quote

DRAFT_TYPES = ("follow_up", "thank_you", "interview_reply", "referral", "negotiation", "decline")

# What each type is for, in the words the picker shows and the prompt is given.
DRAFT_LABELS = {
    "follow_up": "Follow-up after applying",
    "thank_you": "Thank-you after an interview",
    "interview_reply": "Reply to an interview invite with availability",
    "referral": "Ask a contact for a referral",
    "negotiation": "Reply to an offer (negotiate)",
    "decline": "Decline politely",
}

# The types that make the most sense at each stage, first is the default in the picker.
TYPES_FOR_STAGE = {
    "saved": ("referral", "follow_up"),
    "applied": ("follow_up", "referral", "interview_reply"),
    "interviewing": ("thank_you", "interview_reply", "follow_up", "decline"),
    "offer": ("negotiation", "thank_you", "decline"),
    "rejected": ("thank_you", "follow_up"),
    "archived": ("follow_up",),
}


def gmail_compose_url(to: str = "", subject: str = "", body: str = "") -> str:
    """The Gmail compose link the Open in Gmail button opens in the person's browser. Every
    field is percent-encoded (quote with no safe characters, so '&', '+', '#' and newlines
    survive the query string)."""
    q = []
    if to:
        q.append("to=" + quote(to, safe=""))
    q.append("su=" + quote(subject or "", safe=""))
    q.append("body=" + quote(body or "", safe=""))
    return "https://mail.google.com/mail/?view=cm&" + "&".join(q)


def draft_email(llm, kind: str, job: dict, profile: dict, contact: dict | None = None,
                stage: str = "") -> dict:
    """{"subject", "body", "to"} for one draft type. ``contact`` (name, role, email) is required
    for a referral and used as the recipient for every type when it has an email."""
    if kind not in DRAFT_TYPES:
        raise ValueError(f"type must be one of {', '.join(DRAFT_TYPES)}")
    if kind == "referral" and not (contact and contact.get("name")):
        raise ValueError("a referral request needs a named contact from the card")
    stage = stage or job.get("stage") or ""
    out = llm.draft_stage_email(kind, job, profile or {}, contact or {}, stage) or {}
    from tailoring.conform import strip_ai_dashes
    subject = strip_ai_dashes(str(out.get("subject") or "")).strip()[:200]
    body = strip_ai_dashes(str(out.get("body") or "")).strip()
    return {"type": kind, "subject": subject, "body": body,
            "to": str((contact or {}).get("email") or "").strip()}
