"""What Auto does when it does not know (docs/apply-agent.md section 31).

The owner's decision (2026-11-19): "For auto apply, we can't stop to ask the user anything. ...
if it does require, we will use a common sense approach to pick the best option possible and
proceed with the application." Stopping to ask belongs to Assisted and Hybrid.

So in Auto a question nothing answers (no saved answer, nothing in the person's material, no
common-sense default) gets a BEST GUESS, flagged for the person, and the run goes on to Ready to
send. ``choose`` is the engine's own guess for a question whose choices it can read, in this
order of preference:

1. an explicit neutral choice the form offers ("Prefer not to say", "Decline to self-identify",
   "I do not wish to answer", "N/A");
2. the choice that is true for the great majority of applicants and commits the person to the
   least: No to relatives at the employer, a conflict of interest, a non-compete, a conviction or
   pending charge, a dismissal, a government or public office; Yes to being at least 18, to a
   background check or drug screen the job requires, to being able to do the job's functions;
3. the first choice that commits the person to nothing ("Other", "Not applicable", "None").

A self-identification question only ever takes its decline choice. What is never guessed
(``qclass.never_guess``: an ID number, an SSN, a date of birth, bank or payment details, a
password, a typed signature) and consents (autoapply/consents.py) are not this module's.
A question it cannot settle from the choices is left to the brain's own best guess, which
autoapply/protocol.py checks the same way (``accept``).

Every guess is recorded as ``source: best_guess`` with its reason and the question's class, shown
in Review answers as "Best guess. Check it.", and counted on the application; one on a legal
question keeps the agent from sending that application alone (autoapply/modes.py).
"""
from __future__ import annotations

import re

from autoapply import eeo, qclass, workauth

SOURCE = "best_guess"
# (2026-11-19) A work-authorization question the saved profile answers, on a form that words its
# choices so none can be matched to it: still a best guess, but the flag says why.
WA_SAVED_REASON = "your work authorization is saved but this form words it differently"


def reason_for(question: str, work_auth=None, job_country: str = "US") -> str:
    """The reason a best guess carries when it is not the usual one: the work-authorization
    profile holds this answer, but the form's choices could not be matched to it. "" otherwise."""
    kind = workauth.classify(question)
    if not kind or not work_auth:
        return ""
    country = workauth.question_country(question, job_country)
    return WA_SAVED_REASON if workauth.fact(kind, work_auth, country) is not None else ""
_NEUTRAL = re.compile(r"^\W*(n\s?/\s?a|not applicable|none of the above|none|other|unsure|not sure|unknown|do(n'?t| not) know)\W*$", re.I)
_NON_COMMITTAL = re.compile(r"^\W*(other|not applicable|n\s?/\s?a|none|none of the above|not sure|unsure|maybe|undecided|flexible|open|negotiable)\b", re.I)
# The majority answer, by the question's words: (pattern, the answer, the reason).
_MAJORITY = (
    (re.compile(r"relative|related to|family member|spouse|domestic partner|household|close personal", re.I), False,
     "most applicants have no relative or close connection at the employer"),
    (re.compile(r"conflicts? of interest|government official|public official|politically exposed|public office", re.I), False,
     "most applicants have no such conflict or office"),
    (re.compile(r"non-?compet|non-?solicit|restrictive covenant|confidentiality agreement|bound by|any agreement", re.I), False,
     "most applicants are under no such agreement"),
    (re.compile(r"convict|criminal|felon|misdemeanor|pending charge|arrest|plead|guilty|offen[cs]e", re.I), False,
     "most applicants have no conviction or pending charge"),
    (re.compile(r"suspended|sanctioned|debarred|excluded from|precluded from|exclusion list|\boig\b", re.I), False,
     "most applicants were never excluded or sanctioned from a health care or government program"),
    (re.compile(r"terminat|dismiss|\bfired\b|discharg|asked to resign|resign(ed)? in lieu|involuntar|separated from|disciplin", re.I), False,
     "most applicants were never dismissed or asked to resign"),
    (re.compile(r"previously (applied|worked|employed|interviewed)|(applied|worked|been employed|interviewed) (here|for|at|with)\b.*\b(before|previously|past)|former employee", re.I), False,
     "most applicants are new to the employer"),
    (re.compile(r"at least 18|over (the age of )?18|18 years (of age|or older)|legal (working )?age", re.I), True,
     "an applicant for this job is an adult"),
    (re.compile(r"(background|reference) (check|screen|investigation)|drug (test|screen)|pre-?employment screen", re.I), True,
     "most applicants accept the checks a job requires"),
    (re.compile(r"essential functions|perform the (duties|functions)|with or without (a )?reasonable accommodation", re.I), True,
     "an applicant expects to be able to do the job"),
    (re.compile(r"e-?verify|proof of (identity|eligibility)|provide (documentation|documents)", re.I), True,
     "an applicant can show the documents a new hire must"),
)


def neutral_choice(options) -> str | None:
    """The form's own neutral choice: a decline ("Prefer not to say", "I do not wish to answer"),
    else a lone "N/A" / "None of the above". None when there is none or several."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    d = eeo.decline_option(opts)
    if d:
        return d
    hit = [o for o in opts if _NEUTRAL.match(o) and not re.match(r"^\W*other\W*$", o, re.I)]
    return hit[0] if len(hit) == 1 else None


def majority(question: str):
    """(True | False, reason) for a question the great majority answers one way, else None."""
    q = " ".join(str(question or "").split())
    for rx, yes, why in _MAJORITY:
        if rx.search(q):
            return yes, why
    return None


def choose(question: str, options, *, company: str = "", employers=()) -> tuple[str, str] | None:
    """(answer, reason) for a question whose choices are known, by the order in the module's
    words; None when the question is never guessed, is a consent, has no choices, or the choices
    settle nothing (the brain's own best guess then, checked by ``accept``)."""
    q = " ".join(str(question or "").split())
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if not q or not opts or qclass.never_guess(q) or qclass.is_consent(q):
        return None
    n = neutral_choice(opts)
    if qclass.is_self_id(q, opts):
        return (n, "self-identification is voluntary: the form's own decline choice") if n else None
    if n:
        return n, "nothing saved answers this: the form's own neutral choice"
    m = majority(q)
    if m is not None:
        v = workauth.pick_yes_no(m[0], opts)
        if v:
            return v, m[1]
    soft = [o for o in opts if _NON_COMMITTAL.match(o)]
    if soft:
        return soft[0], "nothing saved answers this: the choice that commits to nothing"
    return None


def accept(question: str, value: str, options, *, typed: bool = False, reason: str = "") -> tuple[bool, str]:
    """(ok, why not) for a best guess the brain made itself. Never on a never-guessed field or a
    consent. Where the form offers a neutral choice, that is the guess (always, on a
    self-identification question). A typed guess is a few words: never a number, a date or a
    name made up for the person."""
    q = " ".join(str(question or "").split())
    v = " ".join(str(value or "").split())
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if qclass.never_guess(q):
        return False, "never"
    if qclass.is_consent(q):
        return False, "consent"
    n = neutral_choice(opts)
    if qclass.is_self_id(q, opts):
        if (n and v.lower() == n.lower()) or (not n and eeo.DECLINE.search(v)):
            return True, ""
        if n:
            return False, f'a self-identification question only takes its decline choice ("{n[:60]}")'
        return False, "a self-identification question with no decline choice is never guessed; leave it"
    if n and v.lower() != n.lower() and not typed:
        m = majority(q)
        if m is None or workauth.polarity(v) is not m[0]:
            return False, f'the form offers a neutral choice ("{n[:60]}"): a best guess takes that one'
    if typed:
        if len(v.split()) > 8 or len(v) > 60:
            return False, "a typed best guess is a few words"
        if re.search(r"\d", v) and qclass.question_class(q) == qclass.FACTUAL:
            return False, "a number or a date about the person is never made up; leave the box, or type N/A if the box takes it"
        if re.search(r"\d{3,}", v):
            return False, "a typed best guess never carries a long number"
    return True, ""
