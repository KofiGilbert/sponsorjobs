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


# ---------------------------------------------------------------------- stage triage
#
# A finer reading of ONE application-related message, for the Tracker's "From your inbox"
# strip: what kind of mail it is, the stage it argues for, and the phrase that decided it.
# Deterministic rules only; nothing here moves a card, the person clicks (CLAUDE.md §5).

KINDS = ("interview_invite", "rejection", "offer", "assessment", "application_received",
         "recruiter_outreach", "other")

# The stage each kind proposes. Recruiter outreach proposes nothing: a cold "are you open to
# roles?" is not a stage change, it is a reply to draft.
STAGE_FOR = {
    "interview_invite": "interviewing",
    "assessment": "interviewing",
    "rejection": "rejected",
    "offer": "offer",
    "application_received": "applied",
    "recruiter_outreach": "",
    "other": "",
}

# Order matters: the first rule that fires wins. A rejection beats an "interview" mention
# ("unfortunately, after your interview we decided..."), an offer beats an interview recap.
_OFFER_RE = re.compile(
    r"offer letter|offer of employment|job offer|pleased to (offer|extend)|extend (you )?an offer"
    r"|formal offer|we('d| would) like to offer you|offer you the (position|role)|letter of offer", re.I)
_ASSESSMENT_RE = re.compile(
    r"hackerrank|codility|codesignal|leetcode|take-?home (assignment|test|project|challenge|exercise)"
    r"|coding (challenge|assessment|test|exercise)|technical assessment|online assessment"
    r"|skills? (assessment|test)|assessment (link|invitation)|complete (the|this|your) assessment", re.I)
_INTERVIEW_RE = re.compile(
    r"interview (invitation|invite|with|for the|request)|invite you to (an |a )?(interview|call|chat|conversation)"
    r"|schedule (a|your|an) (call|time|interview|chat|conversation)|calendly\.com|calendar invite"
    r"|calendar invitation|book a time|pick a time|phone screen|technical screen|next steps?"
    r"|move forward with (your|an) (application|interview)|\binterview\b", re.I)
_RECEIVED_RE = re.compile(
    r"application (has been |was )?(received|submitted)|thank you for applying|thanks for applying"
    r"|we('ve| have) received your application|received your application|application confirmation"
    r"|successfully (submitted|applied)|your application to .{1,80} (has been|was) received", re.I)


def _first_phrase(text: str, pattern: re.Pattern) -> str:
    m = pattern.search(text)
    return (m.group(0) if m else "").strip()


def triage(msg: dict) -> dict:
    """{"kind", "stage", "phrase", "reason"} for one message.

    ``phrase`` is the exact wording the rule matched and ``reason`` is the one-line
    explanation shown next to the suggestion ("Subject says \"next steps\""). ``kind`` is
    "other" when no rule fires; a caller may then ask a model, but only for mail from an
    employer the person is already tracking."""
    subject = str(msg.get("subject") or "")
    body = str(msg.get("body") or "")
    text = f"{subject}\n{body}"
    for kind, pattern in (("rejection", _REJECTED_RE), ("offer", _OFFER_RE),
                          ("assessment", _ASSESSMENT_RE), ("interview_invite", _INTERVIEW_RE)):
        phrase = _first_phrase(text, pattern)
        if phrase:
            return _verdict(kind, phrase, subject)
    if classify(msg) != "verification":
        phrase = _first_phrase(text, _RECEIVED_RE)
        if phrase:
            return _verdict("application_received", phrase, subject)
    if classify(msg) == "recruiter":
        phrase = _first_phrase(text, _RECRUITER_STRONG_RE) or "recruiter wording"
        return _verdict("recruiter_outreach", phrase, subject)
    return {"kind": "other", "stage": "", "phrase": "", "reason": ""}


def _verdict(kind: str, phrase: str, subject: str) -> dict:
    where = "Subject" if phrase.lower() in subject.lower() else "Message"
    short = " ".join(phrase.split())[:80]
    return {"kind": kind, "stage": STAGE_FOR.get(kind, ""), "phrase": short,
            "reason": f'{where} says "{short}"'}
