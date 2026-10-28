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


# ---------------------------------------------------------------------- what the apply agent waits for
#
# An unattended run (docs/apply-agent.md milestone 3) waits on three kinds of mail: the link
# that verifies a new portal account, a one-time code, and a password-reset link. Pure rules
# over one message dict; the route filters by ``about_site`` BEFORE any of these run, so
# mail about anything else is never inspected (CLAUDE.md section 5).

# Words that sit next to a one-time code. "verif" covers verify/verification.
_CODE_CUE_RE = re.compile(
    r"\bcode\b|verif|one[- ]time|\bpin\b|passcode|\botp\b|confirm|security|authenticat", re.I)
# Digit runs that are not a code: a phone number, a zip after a state, an order/ticket
# number, a year. They are blanked before the scan so they never win.
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]?\d{4}\b")
_ZIP_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
_REFERENCE_RE = re.compile(
    r"\b(?:order|ticket|case|invoice|tracking|account|reference|ref|req(?:uisition)?|job|"
    r"application)\s*(?:number|no\.?|id|#)?\s*[:#]?\s*#?\s*\d{4,8}\b", re.I)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_CODE_RE = re.compile(r"(?<![\d-])(\d{4,8})(?![\d-])")
_WINDOW = 70


def _blank_non_codes(text: str) -> str:
    for pat in (_PHONE_RE, _ZIP_RE, _REFERENCE_RE, _YEAR_RE):
        text = pat.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _code_in(text: str) -> str:
    """The first 4 to 8 digit run with a code cue within ``_WINDOW`` characters of it."""
    clean = _blank_non_codes(text or "")
    for m in _CODE_RE.finditer(clean):
        around = clean[max(0, m.start() - _WINDOW): m.end() + _WINDOW]
        if _CODE_CUE_RE.search(around):
            return m.group(1)
    return ""


def otp_code(msg: dict) -> str:
    """The one-time code a message carries, or "". The subject wins when it has one
    ("482913 is your verification code"); the body is scanned only then."""
    return _code_in(str(msg.get("subject") or "")) or _code_in(str(msg.get("body") or ""))


_RESET_WORDS_RE = re.compile(
    r"reset|forgot|forgotten|new password|change (your )?password|recover(y)? (your )?(account|password)",
    re.I)
_RESET_LINK_RE = re.compile(r"reset|forgot|password|passwd|recover", re.I)


def password_reset_link(msg: dict) -> str:
    """The link to a password reset, or "": its URL says reset/forgot/password, or the words
    around it do."""
    body = str(msg.get("body") or "")
    links = list(_LINK_RE.finditer(body))
    for m in links:
        if _RESET_LINK_RE.search(m.group(0)):
            return m.group(0).rstrip(".,)")
    for m in links:
        around = body[max(0, m.start() - 120): m.start()] + body[m.end(): m.end() + 60]
        if _RESET_WORDS_RE.search(around):
            return m.group(0).rstrip(".,)")
    return ""


# Second-level labels under a two-letter country code that are registries, not sites:
# jobs.acme.co.uk is acme.co.uk, not co.uk.
_REGISTRY_SLDS = {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne"}
# ATS families whose mail comes from a different domain than the career site: the host
# suffixes a site may use, and the sender domains that family mails from.
_ATS_FAMILIES = (
    (("myworkdayjobs.com", "myworkday.com", "workday.com"),
     ("myworkday.com", "myworkdayjobs.com", "workday.com")),
    (("greenhouse.io",), ("greenhouse.io", "greenhouse-mail.io")),
    (("lever.co",), ("lever.co", "hire.lever.co")),
    (("ashbyhq.com",), ("ashbyhq.com",)),
    (("icims.com",), ("icims.com",)),
    (("smartrecruiters.com",), ("smartrecruiters.com",)),
    (("workable.com",), ("workable.com", "workablemail.com")),
    (("taleo.net",), ("taleo.net",)),
    (("successfactors.com", "successfactors.eu"), ("successfactors.com", "successfactors.eu", "sap.com")),
    (("jobvite.com",), ("jobvite.com",)),
)


def registrable_domain(host: str) -> str:
    """acme.wd5.myworkdayjobs.com -> myworkdayjobs.com; jobs.acme.co.uk -> acme.co.uk."""
    labels = [p for p in str(host or "").lower().strip().strip(".").split(".") if p]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _REGISTRY_SLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def sender_domain(msg: dict) -> str:
    m = re.search(r"[\w.+-]+@([\w.-]+)", str(msg.get("from") or ""))
    return m.group(1).lower().rstrip(".") if m else ""


def _under(domain: str, suffixes) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def about_site(msg: dict, host: str, company: str = "") -> bool:
    """Is this message from the site (or employer) the agent is applying on? True when the
    sender's registrable domain is the host's, when the company name appears in the From or
    the subject, or when the sender is the ATS family that host belongs to. The gate every
    search result passes before it is read any further."""
    host = str(host or "").lower().strip()
    if host.startswith("http"):
        host = re.sub(r"^https?://", "", host).split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    sender = sender_domain(msg)
    if host and sender and registrable_domain(sender) == registrable_domain(host):
        return True
    name = str(company or "").strip().lower()
    if len(name) >= 2:
        haystack = f"{msg.get('from') or ''}\n{msg.get('subject') or ''}".lower()
        if name in haystack:
            return True
    if host and sender:
        for hosts, senders in _ATS_FAMILIES:
            if _under(host, hosts) and _under(sender, senders):
                return True
    return False


def kind_of(msg: dict) -> str:
    """"verify" | "code" | "reset" | "" for one message the agent may be waiting on."""
    text = f"{msg.get('subject') or ''}\n{msg.get('body') or ''}"
    if _RESET_WORDS_RE.search(text) and password_reset_link(msg):
        return "reset"
    if otp_code(msg):
        return "code"
    if _VERIFY_RE.search(text) and _LINK_RE.search(text):
        return "verify"
    return ""
