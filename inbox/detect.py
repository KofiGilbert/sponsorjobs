"""Classify the person's own mail as it relates to their applications (CLAUDE.md §5).

Read-first: we only care about two kinds of message — an application's **email
verification / confirmation** (so an autonomous apply can complete) and a **recruiter
reply** (so we can surface it and draft an answer). Everything else is ignored. Pure
functions over message dicts, so this is fully testable offline.

A message dict is: {"from": str, "subject": str, "body": str, "date": str, "id": str}.
"""

from __future__ import annotations

import re

# Subject/body wording that marks an application email-verification / confirmation.
_VERIFY_RE = re.compile(
    r"verify (your )?(e-?mail|account|address)|confirm (your )?(e-?mail|application|account)"
    r"|complete (your )?application|activate (your )?account|email verification"
    r"|please confirm|verify it'?s you", re.I)

_LINK_RE = re.compile(r'https?://[^\s"\'<>)\]]+', re.I)
# A link that itself looks like a verify/confirm action.
_VERIFY_LINK_RE = re.compile(r"verif|confirm|activat|validate|token|/auth/|magic", re.I)

# Automated senders we never treat as a human recruiter reply.
_NOREPLY_RE = re.compile(r"no-?reply|do-?not-?reply|donotreply|notifications?@|mailer@|automated@", re.I)
# STRONG, recruiter-specific wording — any ONE of these is enough to treat a (non-automated)
# message as a real hiring-team reply worth drafting an answer to.
_RECRUITER_STRONG_RE = re.compile(
    r"next steps|your application|schedule (a|your|an) (call|time|interview|chat)"
    r"|set up a (call|time)|recruiter|hiring team|talent acquisition|phone screen"
    r"|coding challenge|technical screen|move forward|we'?d love to|reviewed your"
    r"|interview (invitation|invite|with|for the)", re.I)
# WEAK, GENERIC cues that also appear in ordinary personal mail. A single one is NOT enough
# (a friend's "how did your interview go?" must stay 'other', §5 read-first) — require TWO
# distinct weak cues before classifying as a recruiter reply and sending the body to the LLM.
_RECRUITER_WEAK_RES = [
    re.compile(r"\binterview\b", re.I),
    re.compile(r"\bavailability\b|\bavailable\b", re.I),
    re.compile(r"\btalent\b", re.I),
    re.compile(r"\bschedule\b", re.I),
    re.compile(r"\bposition\b|\brole\b|\bopportunity\b|\bopening\b", re.I),
]


# What an ATS status mail says. Vocabulary informed by the candidate-facing status
# templates real applicant-tracking systems send (OpenCATS ships "Not in Consideration"
# as a status-change body, and a generic "your status ... has been changed" wrapper).
_REJECTED_RE = re.compile(
    r"not (in consideration|selected|moving forward|be moving forward|proceed)"
    r"|no longer (under consideration|being considered)|decided (not to|to move forward with other)"
    r"|other candidates|pursue other (candidates|applicants)|unfortunately|regret to inform"
    r"|position has been filled|will not be (moving|proceeding)", re.I)
_ADVANCED_RE = re.compile(
    r"next (step|round|stage)|move forward|moving forward|schedule|invite you"
    r"|interview|offer (letter|of employment)|pleased to|congratulations", re.I)


def application_status(msg: dict) -> str:
    """'rejected' | 'advanced' | '' from the message wording. A rejection wins when both
    match ("unfortunately ... we have decided to move forward with other candidates")."""
    text = f"{msg.get('subject', '')}\n{msg.get('body', '')}"
    if _REJECTED_RE.search(text):
        return "rejected"
    if _ADVANCED_RE.search(text):
        return "advanced"
    return ""


def classify(msg: dict) -> str:
    """Return 'verification' | 'recruiter' | 'other' for one message."""
    text = f"{msg.get('subject', '')}\n{msg.get('body', '')}"
    if _VERIFY_RE.search(text) and _LINK_RE.search(text):
        return "verification"
    frm = (msg.get("from") or "").lower()
    if not _NOREPLY_RE.search(frm):
        # A strong recruiter-specific phrase, OR at least two distinct generic cues — never a
        # lone generic word, so unrelated personal mail is never drafted on (§5 read-first).
        if _RECRUITER_STRONG_RE.search(text):
            return "recruiter"
        if sum(bool(p.search(text)) for p in _RECRUITER_WEAK_RES) >= 2:
            return "recruiter"
    return "other"


def verification_link(msg: dict) -> str:
    """The verify/confirm link to visit for a verification message (best candidate first)."""
    links = _LINK_RE.findall(msg.get("body", "") or "")
    for link in links:
        if _VERIFY_LINK_RE.search(link):
            return link.rstrip(".,)")
    return (links[0].rstrip(".,)") if links else "")


def sender_name(msg: dict) -> str:
    """The human-readable name from a 'Name <addr@x>' From header, else the address."""
    frm = (msg.get("from") or "").strip()
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', frm)
    if m:
        return m.group(1).strip()
    return frm
