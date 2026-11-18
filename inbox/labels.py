"""One label for each message in the in-app Inbox (docs/mail-alias.md).

  received   the employer confirms the application arrived
  rejected   "Not this time"
  interview  an interview or a call is asked for, or a scheduling link is sent
  action     the employer asks for something: a form, an assessment, a verification, a document
  other      anything else

Rules over the subject and the text only, no model call, so every message is labelled offline
the moment it is downloaded. The first rule that fires wins: rejected, interview, action,
received. Sentences that only say what MAY happen ("if selected, we will reach out to schedule
an interview") are dropped first, so a confirmation is never read as an interview or a rejection.

``worker/src/mail.ts`` (labelOf) is a port of these rules for the notice e-mail's subject.
Change the two together, the way detect.py and mail.ts already are.
"""

from __future__ import annotations

import re

from . import detect

LABELS = ("received", "rejected", "interview", "action", "other")

_CONDITIONAL_RE = re.compile(r"\b(?:if|should|whether|unless|in case|in the event)\b", re.I)
_OUTCOME_RE = re.compile(
    r"select|shortlist|chosen|match|\bfit\b|suitab|qualif|mov(?:e|ing) forward|proceed"
    r"|next (?:step|stage|round)|interview|successful|consider", re.I)
# A sentence that describes the hiring process in general is not an invitation.
_PROCESS_RE = re.compile(
    r"process (?:includes|consists|involves|typically|usually)|typically|usually|generally"
    r"|may include|what to expect", re.I)

_REJECT_RE = re.compile("|".join([
    r"\bnot (?:been |be )?(?:selected|chosen|shortlisted|successful)\b",
    r"\b(?:not|won'?t)\b[^.\n]{0,25}\b(?:mov(?:e|ing) (?:forward|ahead)|proceed(?:ing)?\b|progress(?:ing)? (?:your|you|with))",
    r"\bunable to (?:move forward|proceed with your (?:application|candidacy)|offer you)",
    r"decided to (?:pursue|move forward with|proceed with|go with|continue with|progress) (?:other|another|different)\b",
    r"(?:pursu|mov)(?:e|ing) (?:forward )?with (?:other|another) (?:candidates?|applicants?)",
    r"other (?:candidates|applicants) (?:whose|who|that)\b",
    r"regret to inform|we regret\b|regret that",
    r"(?:position|role|vacancy|job) has (?:now |already )?been (?:filled|closed|cancell?ed)",
    r"(?:position|role|vacancy|job|requisition) is no longer (?:available|open)",
    r"no longer (?:under consideration|being considered)",
    r"not (?:be )?(?:offering you|able to offer)",
]), re.I)
_UNFORTUNATELY_RE = re.compile(r"unfortunately[^.!?\n]{0,160}", re.I)
_UNFORTUNATELY_NEG_RE = re.compile(
    r"\b(?:not|unable|cannot|can'?t|won'?t|other candidates|another candidate|filled|closed)\b", re.I)
# "Unfortunately we cannot reply to every applicant" is a confirmation's small print.
_UNFORTUNATELY_SMALL_PRINT_RE = re.compile(
    r"(?:respond|reply|feedback|contact|get back|update)[^.]{0,40}(?:every|each|all|individual)"
    r"|(?:every|each|all|individual)[^.]{0,40}(?:respond|reply|feedback)", re.I)

_INTERVIEW_RE = re.compile("|".join([
    r"invit(?:e|ing|ed) you (?:to|for) (?:an? |the |our )?(?:[a-z-]+ ){0,3}(?:interview|call|chat|conversation|screen|meeting)",
    r"(?:schedule|scheduling|arrange|arranging|set up|setting up|book|booking) (?:an?|your|the|some) (?:[a-z-]+ ){0,3}(?:interview|call|chat|conversation|screen|time|meeting)",
    r"interview (?:invitation|invite|request|confirmation|details|availability|scheduled)",
    r"(?:would|'d) (?:like|love) to (?:interview|speak|chat|talk|meet|connect)",
    r"(?:phone|video|technical|onsite|on-site|virtual|final|first|second|panel|initial|recruiter|hiring manager) (?:interview|screen)\b",
    r"(?:your|share|send|provide)[a-z ]{0,20}availability",
    r"(?:book|pick|select|choose|find) a (?:time|slot)",
    r"calendly\.com|cal\.com/|goodtime\.io|savvycal\.com|calendar\.app\.google|youcanbook\.me|/scheduling?/",
    r"to the (?:next|interview) (?:stage|round|step)",
    r"next (?:step|stage|round) (?:is|will be|would be) (?:an?|the) [a-z -]{0,30}(?:interview|call|screen|conversation)",
]), re.I)

_ACTION_RE = re.compile("|".join([
    r"hackerrank|codility|codesignal|testgorilla|pymetrics|hirevue|take-?home",
    r"(?:online|technical|coding|skills?|aptitude|cognitive|personality|pre-?employment) (?:assessment|test|challenge|exercise|questionnaire)",
    r"complete (?:the|this|your|an?|our) (?:[a-z-]+ ){0,3}(?:assessment|questionnaire|form|test|survey|challenge|exercise|background check|paperwork|profile|application)",
    r"action (?:is )?(?:required|needed)",
    r"(?:additional|more|further|missing) (?:information|details|documents?|documentation) (?:is |are )?(?:required|needed|requested)",
    r"(?:please|kindly) (?:provide|upload|submit|send|attach|complete|fill|sign|review and sign)",
    r"(?:we|i) need (?:you to|your|a copy|some|the following)",
    r"background check|reference check",
    r"(?:finish|complete|continue|resume) your application|application is (?:incomplete|not complete)|incomplete application",
    r"verify your (?:e-?mail|identity|account)|confirm your (?:e-?mail|identity|account)",
]), re.I)
_CODE_WORD_RE = re.compile(r"\b(?:code|passcode|otp|pin)\b|one[- ]time", re.I)

_RECEIVED_RE = re.compile("|".join([
    r"application (?:has been |was |is )?(?:successfully )?(?:received|submitted|sent|complete)\b",
    r"(?:thank you|thanks) for (?:applying|your application|your interest|submitting|taking the time to apply)",
    r"received your (?:application|resume|submission)",
    r"application confirmation|successfully (?:submitted|applied)|we have your application",
    r"your application (?:to|for|with) \S",
    r"has been (?:received|submitted)",
]), re.I)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_SCAN = 20000        # characters of subject + text the rules read


def _flatten(text: str) -> str:
    """Wrapped lines joined: one line per paragraph, so "not\nselected" reads "not selected"."""
    return "\n".join(" ".join(p.split()) for p in re.split(r"\n\s*\n", text))


def _settled(text: str, also_process: bool = False) -> str:
    """The text with the sentences that decide nothing removed."""
    keep = []
    for s in _SENTENCE_SPLIT_RE.split(text):
        if _CONDITIONAL_RE.search(s) and _OUTCOME_RE.search(s):
            continue
        if also_process and _PROCESS_RE.search(s):
            continue
        keep.append(s)
    return "\n".join(keep)


def _says_rejected(text: str) -> bool:
    if _REJECT_RE.search(text):
        return True
    for m in _UNFORTUNATELY_RE.finditer(text):
        if _UNFORTUNATELY_NEG_RE.search(m.group(0)) and not _UNFORTUNATELY_SMALL_PRINT_RE.search(m.group(0)):
            return True
    return False


def decisive_text(text: str) -> str:
    """The message without the sentences that decide nothing ("if selected, we will reach out to
    schedule an interview", "our process typically includes a phone screen"). What the Tracker's
    own rules are given to read, so a confirmation's small print never suggests an interview."""
    return _settled(_flatten(str(text or "")[:_SCAN]), also_process=True)


def label_of(subject: str, text: str) -> str:
    """'received' | 'rejected' | 'interview' | 'action' | 'other' for one message."""
    subject, text = str(subject or ""), str(text or "")
    whole = _flatten(f"{subject}\n\n{text}"[:_SCAN])
    plain = _settled(whole)
    if _says_rejected(plain):
        return "rejected"
    if _INTERVIEW_RE.search(_settled(whole, also_process=True)):
        return "interview"
    kind = detect.kind_of({"subject": subject, "body": text})
    if kind in ("verify", "reset") or (kind == "code" and _CODE_WORD_RE.search(whole)):
        return "action"
    if _ACTION_RE.search(plain):
        return "action"
    if _RECEIVED_RE.search(whole):
        return "received"
    return "other"


# The Tracker stage each label argues for. The Inbox only hands a message to the Tracker's own
# suggestion path (inbox/suggestions.py) when that path's rules read the same stage.
STAGE_FOR_LABEL = {"received": "applied", "rejected": "rejected", "interview": "interviewing",
                   "action": "interviewing", "other": ""}
