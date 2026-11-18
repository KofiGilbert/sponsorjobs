"""Common-sense screeners: simple questions the person's own records answer, so a run never
stops for them (a Workday "Application Questions" page asked five; four needed no person,
2026-11-17).

* **At least 18 / legal working age** ("Are you at least 18 years old?", "Are you over 18?",
  "Are you 18 years of age or older?", "Are you of legal age to work?"): the person's saved
  answer first (``@over_18``, as before); with none, Yes when the saved profile PROVES it: a job
  or a school date that lies at least 18 years before today (``adult_proof``). No dates, or none
  that old: it stays a question. Never a No, never a guess.
* **Previously applied to / interviewed with this company** ("Have you previously applied for
  employment with Acme or any of its predecessor firms?"): from the person's own records: No
  when the Tracker and the saved applications hold no earlier application to this company, Yes
  when they do (``applied_before``; the application being filled is not counted). Records that
  could not be read answer nothing. A Yes is never given to a question about a time window or
  about this very position (the records do not say which): that one stays the person's.
* **Relatives employed by the company**: the agent cannot know this. Apply setup asks ONCE ("Do
  any of your relatives work at companies you apply to?", saved under ``@relatives_at_employer``).
  A saved No answers No on every form; nothing saved, or "Yes, ask me each time", leaves the
  question for the person. Never a default. A question about a government or public official, a
  competitor, a client or a referral is another question and stays theirs.

Acknowledgements worded as a Yes / No choice live in autoapply/consents.py. Category rules on the
question's words, never one site's markup. Pure: the caller (ui/app.py) reads the stores.
"""
from __future__ import annotations

import datetime as _dt
import re

from autoapply import workauth
from autoapply.answers import normalize
from autoapply.history import parse_span

ADULT_KEY = "@over_18"
RELATIVES_KEY = "@relatives_at_employer"
ADULT_YEARS = 18

# A question that asks for a detail, never a yes or no ("If yes, when did you apply?").
_NOT_YES_NO = re.compile(
    r"^\W*(if (yes|so|you|applicable)\b|please (list|describe|provide|explain|specify|tell|name|state|identify|enter)\b"
    r"|when\b|which\b|what\b|where\b|how\b|why\b|who\b|whom\b|list\b|describe\b|name\b|names\b|date\b|dates\b"
    r"|relationship\b|position\b)", re.I)


def _squash(label) -> str:
    return " ".join(str(label or "").split())


# ---------------------------------------------------------------------------- at least 18
# The inverse or a conditional ("Are you under 18?", "If you are under 18, do you have a work
# permit?", "Are you between 18 and 21?", "Are you 21 or older?"): never answered from dates.
_NOT_ADULT = re.compile(r"\b(under|younger|below|less than|not yet|minors?|between|if (you are|you're|under)|permit"
                        r"|parent|guardian|21|birth)\b", re.I)


def is_adult_question(label: str) -> bool:
    """True for "are you at least 18 / of legal age to work" in its plain yes or no wordings."""
    lbl = _squash(label)
    return bool(lbl) and not _NOT_ADULT.search(lbl) and normalize(lbl) == ADULT_KEY


def _points(profile: dict):
    """Every (year, month) the saved profile's jobs and schools state (month 0 when unknown)."""
    prof = profile if isinstance(profile, dict) else {}
    texts = []
    for org in prof.get("experience") or []:
        if not isinstance(org, dict):
            continue
        texts.append(org.get("dates"))
        for role in org.get("roles") or []:
            if isinstance(role, dict):
                texts.append(role.get("dates"))
    for ed in prof.get("education") or []:
        if isinstance(ed, dict):
            texts.extend((ed.get("date"), ed.get("dates")))
    for t in texts:
        if not str(t or "").strip():
            continue
        span = parse_span(t)
        for p in (span.get("start"), span.get("end")):
            if p and p.get("y"):
                yield int(p["y"]), int(p.get("m") or 0)


def adult_proof(profile: dict, today: _dt.date | None = None) -> bool:
    """True when a job or school date in the saved profile lies at least 18 years before today:
    the person was alive then, so they are at least 18 now. A date with no month counts as that
    year's December. False with no dates, or none that old (it stays a question)."""
    today = today or _dt.date.today()
    cutoff = (today.year - ADULT_YEARS, today.month)
    for y, m in _points(profile):
        if 1900 <= y and (y, m or 12) < cutoff:
            return True
    return False


def adult_answer(proof) -> tuple[str, str] | None:
    """("Yes", "history") when the profile proves it, else None."""
    return ("Yes", "history") if proof else None


# ---------------------------------------------------------------------------- applied here before
_APPLY = re.compile(r"\b(appl(?:ied|y|ying)|applications?|applicant|candidate|interview(?:ed|ing|s)?)\b", re.I)
_PAST = re.compile(r"\b(previous(?:ly)?|before|in the past|ever|prior|past|earlier|already|last|within|former)\b", re.I)
_ASKS_YOU = re.compile(r"\b(have|did|are|were|had) you\b|^\W*(previous(ly)?|prior|past|former|ever|applied)\b", re.I)
_HERE = re.compile(r"\b(us|here|this (?:company|firm|organi[sz]ation|employer)|our (?:company|firm|organi[sz]ation|team)"
                   r"|the (?:company|firm))\b", re.I)
# Another employer, or something else one applies for.
_ELSEWHERE = re.compile(
    r"\b(other|another|different|any) (companies|company|firms?|employers?|organi[sz]ations?)\b|\bvisa\b|sponsor|\bloans?\b"
    r"|licen[cs]e|clearance|unemployment|benefits?|citizenship|green card|permit|asylum|scholarship|grant\b", re.I)
_SOMEONE_ELSE = re.compile(r"relative|related|family|friend|spouse|anyone|someone|refer|partner", re.I)
_ALSO_WORKED = re.compile(r"\b(work(ed|ing)?|been employed|employed|employees?|contractors?|interns?|interned)\b", re.I)
# A Yes the records cannot give: within a time window, or to this very position.
_WINDOW = re.compile(r"\b(days?|weeks?|months?|years?)\b|\bsince\b|\brecent(ly)?\b", re.I)
_THIS_JOB = re.compile(r"\b(this|the same|same) (position|role|job|requisition|opening|posting|vacancy)\b", re.I)
_NAME = re.compile(r"(?<!^)(?<![.?!]\s)\b[A-Z][A-Za-z0-9&'.-]+")


def applied_question(label: str, company: str = "") -> str:
    """"applied" for "have you applied to / interviewed with this company before?", "both" when
    the same question also asks whether the person worked there ("applied to or worked for"),
    else "". The company is named, or said as "us" / "this company", or no employer is named."""
    lbl = _squash(label)
    if not lbl or len(lbl) > 400 or not (_APPLY.search(lbl) and _PAST.search(lbl) and _ASKS_YOU.search(lbl)):
        return ""
    if _NOT_YES_NO.search(lbl) or _SOMEONE_ELSE.search(lbl) or _ELSEWHERE.search(lbl) or workauth.classify(lbl):
        return ""
    from autoapply.planner import names_company
    if not (names_company(lbl, company) or _HERE.search(lbl) or not _NAME.search(lbl)):
        return ""                                      # it names another employer
    return "both" if _ALSO_WORKED.search(lbl) else "applied"


def applied_before(company: str, earlier, same=None) -> tuple[str, str] | None:
    """("Yes" | "No", "tracker") for "have you applied to <company> before?", from the companies
    of the person's EARLIER applications (``earlier``: names; the application being filled is
    left out by the caller). None when the records could not be read (``earlier`` is None) or
    no company is known: then it stays a question."""
    if earlier is None or not _squash(company):
        return None
    if same is None:
        from applicant.facts import same_company as same
    return ("Yes" if any(same(c, company) for c in earlier if _squash(c)) else "No"), "tracker"


def applied_answer(label: str, kind: str, applied_here, worked_here=None) -> tuple[str, str] | None:
    """(Yes | No, origin) for an applied-here question of ``kind`` (applied_question), else None
    (the person's). ``applied_here``: applied_before's verdict; ``worked_here``: the worked-here
    verdict (applicant/facts.py), needed for a "both" question: Yes when either says Yes, No only
    when both say No. A worked-here Yes read from the resume (origin ``confirm``) is never used."""
    lbl = _squash(label)
    if not kind or not applied_here:
        return None
    yn, origin = applied_here
    if yn == "Yes":
        return None if (_WINDOW.search(lbl) or _THIS_JOB.search(lbl)) else ("Yes", origin)
    if kind == "both":
        wh = worked_here if worked_here and worked_here[1] != "confirm" else None
        if not wh:
            return None
        return (wh[0], wh[1] or "library") if wh[0] == "Yes" else ("No", origin)
    return "No", origin


# ---------------------------------------------------------------------------- relatives employed here
_REL = re.compile(r"\b(relatives?|family members?|members? of your (immediate |extended )?(family|household)"
                  r"|related (to|by blood|by marriage)|spouse|domestic partner|next of kin)\b", re.I)
_REL_WORK = re.compile(r"\b(employ(ed|ees?|ment)?|work(s|ing|ed)?|affiliated|staff|attorneys?|partners? (at|of|in))\b", re.I)
# Another question: a public official, a competitor or client, a referral, an emergency contact.
_REL_NOT = re.compile(r"\bgovernment|\bofficials?\b|\b(public|elected|political) (office|figure)|politically exposed|\bmilitary\b"
                      r"|competitors?|clients?|customers?|vendors?|suppliers?|auditors?|refer(red|ral|ence)|emergency", re.I)


def relatives_question(label: str) -> bool:
    """True for "are any of your relatives employed by this company?" in its yes or no wordings
    ("Do you have family members who work at Acme?", "Are you related to anyone employed by
    Acme?"). Never a question about a public official, a competitor, a client or a referral."""
    lbl = _squash(label)
    if not lbl or len(lbl) > 400 or _NOT_YES_NO.search(lbl) or _REL_NOT.search(lbl):
        return False
    return bool(_REL.search(lbl) and _REL_WORK.search(lbl))


def relatives_saved(library) -> str:
    """The person's standing answer from Apply setup: "No" (no relative works at a company they
    apply to: answered No everywhere), "ask" (Yes, ask me each time), or "" (never answered)."""
    if library is None or not hasattr(library, "best"):
        return ""
    try:
        row = library.best(RELATIVES_KEY)
    except Exception:          # noqa: BLE001 - nothing saved then
        row = None
    ans = _squash((row or {}).get("answer"))
    if not ans:
        return ""
    return "No" if re.match(r"^\W*no\b", ans, re.I) else "ask"


def relatives_answer(library) -> tuple[str, str] | None:
    """("No", "library") when the person saved a standing No, else None (theirs to answer)."""
    return ("No", "library") if relatives_saved(library) == "No" else None
