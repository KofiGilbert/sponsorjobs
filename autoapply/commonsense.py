"""Common-sense screeners: simple questions the person's own records answer, so a run never
stops for them (a Workday "Application Questions" page asked five; four needed no person,
2026-11-17).

* **At least 18 / legal working age** ("Are you at least 18 years old?", "Are you over 18?",
  "Are you 18 years of age or older?", "Are you of legal age to work?"): the person's saved
  answer first (``@over_18``, as before); with none, Yes when the saved profile shows it
  (``adult_proof``): a higher-education entry (a bachelor's, master's, doctorate, associate
  degree, a university or college diploma) finished or started at least a year ago, or a job or
  school date at least 4 years before today. A high-school-only profile with a recent date, or a
  first job 2 years ago, stays a question. Never a No, never a guess.
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

* **How did you hear about us?**: the person's saved answer first (``@heard_about``); with
  none, "Job board" (``HEARD_DEFAULT``, origin ``common_sense``): someone applying through a job
  app found the job on a job board. On a list, the choice that says so is picked by
  autoapply/planner.py generic_sources ("Job board", "Other Job Board", else "Other").

* **Highest level of education completed** ("What is the highest level of education you have
  completed?", "Highest degree earned", "Education level", "Do you have a bachelor's degree or
  higher?"): the person's saved answer to the wording first; else the highest COMPLETED degree
  on the resume the agent built (``highest_education``: a degree still in progress or expected
  does not count), as the list's choice for that level (``education_option``: "Master's Degree
  (MA, MS)", "Graduate degree", "4-year degree"), else the nearest lower choice. Never a choice
  above what the profile holds. Origin ``history``.

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
HEARD_KEY = "@heard_about"
HEARD_DEFAULT = "Job board"        # with nothing saved; a saved answer always wins
HEARD_ORIGIN = "common_sense"

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


def _texts(profile: dict):
    """(kind, degree text, date text) for every job ("job") and school ("school") in the profile."""
    prof = profile if isinstance(profile, dict) else {}
    for org in prof.get("experience") or []:
        if not isinstance(org, dict):
            continue
        yield "job", "", org.get("dates")
        for role in org.get("roles") or []:
            if isinstance(role, dict):
                yield "job", "", role.get("dates")
    for ed in prof.get("education") or []:
        if isinstance(ed, dict):
            for t in (ed.get("date"), ed.get("dates")):
                yield "school", str(ed.get("degree") or ""), t


# Higher education by the DEGREE's own words (never the school's name): a bachelor's, master's,
# MBA, doctorate, associate degree, or a university or college diploma.
_HIGHER_LEVELS = frozenset({"doctorate", "masters", "bachelors", "associates"})
_HIGHER = re.compile(r"\b(university|college|graduate|postgraduate|undergraduate|higher national) (degree|diploma)\b"
                     r"|\b(degree|diploma)\b[^,;]{0,40}\b(university|college)\b", re.I)
_NOT_HIGHER = re.compile(r"\bhigh school\b|\bsecondary\b|\bged\b", re.I)
JOB_YEARS = 4          # a job or school date this far back: at least 18 now in practice (never lower)


def _higher_education(degree: str) -> bool:
    from autoapply.history import degree_level
    d = _squash(degree)
    if not d or _NOT_HIGHER.search(d):
        return False
    return degree_level(d) in _HIGHER_LEVELS or bool(_HIGHER.search(d))


def adult_proof(profile: dict, today: _dt.date | None = None) -> bool:
    """True when the saved profile shows the person is at least 18:

    (a) a higher-education entry (by its degree's words) whose date is in the past: finished, or
        started at least a year ago; or
    (b) a job or school date at least 4 years before today.
    A date with no month counts as that year's December. False with no dates, a high-school-only
    profile with a recent date, or a first job under 4 years ago (it stays a question)."""
    today = today or _dt.date.today()
    now = (today.year, today.month)
    key = lambda p: (int(p["y"]), int(p.get("m") or 0) or 12)      # noqa: E731
    back = lambda years: (today.year - years, today.month)         # noqa: E731
    for kind, degree, text in _texts(profile):
        if not str(text or "").strip():
            continue
        span = parse_span(text)
        start, end = span.get("start"), span.get("end")
        pts = [key(p) for p in (start, end) if p and p.get("y") and int(p["y"]) >= 1900]
        if any(p <= back(JOB_YEARS) for p in pts):
            return True                                             # (b)
        if kind == "school" and _higher_education(degree) and start and start.get("y"):
            ranged = bool(end) or bool(span.get("current"))
            if end and key(end) < now:
                return True                                         # finished
            if not ranged and key(start) < now:
                return True                                         # one date, in the past: finished
            if ranged and key(start) <= back(1):
                return True                                         # started at least a year ago
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


# ---------------------------------------------------------------------------- highest education
# Levels low to high. "some college" is only ever a choice on a list, never read from a degree.
EDU_RANK = {"none": 0, "high school": 1, "some college": 2, "associates": 3, "bachelors": 4, "masters": 5,
            "doctorate": 6}
EDU_WORDS = {"high school": "High school diploma", "associates": "Associate degree", "bachelors": "Bachelor's degree",
             "masters": "Master's degree", "doctorate": "Doctorate"}
_EDU_Q = re.compile(
    r"\bhighest (level|degree|grade|form) of (education|schooling|school|degree)\b"
    r"|\bhighest (education|educational|academic|degree|qualification)\b"
    r"|\b(education|educational|degree) (level|attainment)\b|\blevel of (education|schooling)\b", re.I)
# Another question that mentions education: what a job requires, a parent's, a field, a school.
_EDU_NOT = re.compile(r"\b(parents?|mother|father|guardian|spouse|require[sd]?|minimum|preferred|field of study|major"
                      r"|which (school|university|college)|name of|gpa|grade point|pursuing|enrolled|plan to|willing)\b", re.I)
_DEGREE_YN = re.compile(
    r"\b(do you (currently )?(have|hold|possess)|have you (earned|completed|obtained|received|attained|been awarded)"
    r"|did you (earn|complete|obtain|receive|graduate with)|are you (a )?(holder|graduate))\b[^?]{0,40}?"
    r"\b(?P<what>high school diploma|ged|associate'?s?|bachelor'?s?|undergraduate|4[- ]year|four[- ]year|college"
    r"|master'?s?|graduate|doctorate|doctoral|ph\.?d)\b[^?]{0,30}?\b(degree|diploma|ged|ph\.?d|doctorate)", re.I)
# "... a bachelor's degree in Accounting?": the field decides, which a level alone does not say.
_DEGREE_IN = re.compile(r"\b(degree|diploma|doctorate|ph\.?d)\b(\s*\([^)]*\))?(\s+or (higher|above|equivalent|greater))?"
                        r"\s+(in|from|with)\b", re.I)
_PENDING = re.compile(r"\b(expected|anticipated|in progress|ongoing|candidate|pursuing|incomplete|current(ly)?|present"
                      r"|enrolled|coursework|did not (complete|finish|graduate)|no degree)\b", re.I)
# A list's choices, by level. Checked top to bottom: the first that fits names the choice's level.
_EDU_OPTION = [
    ("", re.compile(r"\bsome (graduate|post ?graduate|master|doctoral)|\bprofessional\b|\bj\.?d\b|\bjuris\b|\bm\.?d\b"
                    r"|\bother\b|\bprefer\b|\bdecline\b|\bpost ?doc", re.I)),          # never picked by level
    ("none", re.compile(r"\b(no|less than|did not (complete|finish)|without|not completed?|some) (a )?(high school|secondary)"
                        r"|\bno (formal )?(education|schooling|diploma)\b", re.I)),
    ("some college", re.compile(r"\bsome (college|university|undergraduate)|\bno degree\b|\bvocational\b|\btrade\b"
                                r"|\btechnical (school|college|certificate|diploma)\b|\bcertificate\b|\bcertification\b", re.I)),
    ("doctorate", re.compile(r"\bdoctor(ate|al)?\b|\bph\.?\s?d\b|\bd\.?phil\b|\bed\.?d\b", re.I)),
    ("masters", re.compile(r"\bmaster'?s?\b|\bmba\b|\b(post ?)?graduate degree\b|\bpostgraduate\b|\bm\.?s\.?c?\b", re.I)),
    ("bachelors", re.compile(r"\bbachelor'?s?\b|\b(4|four)[- ]year\b|\bundergraduate degree\b|\b(college|university) "
                             r"(degree|graduate)\b|\bb\.?s\.?c?\b|\bb\.?a\b", re.I)),
    ("associates", re.compile(r"\bassociate'?s?\b|\b(2|two)[- ]year\b|\ba\.?a\.?s?\b", re.I)),
    ("high school", re.compile(r"\bhigh school\b|\bged\b|\bsecondary\b|\bhs diploma\b", re.I)),
]


def education_question(label: str) -> str:
    """"level" for "what is your highest level of education (completed / attained)?", "highest
    degree earned", "education level"; the level asked ("bachelors") for "Do you have a
    bachelor's degree (or higher)?"; else "". Never a question about what the job requires, a
    field of study, a school, or a degree in a named subject."""
    lbl = _squash(label)
    if not lbl or len(lbl) > 300 or _EDU_NOT.search(lbl) or _SOMEONE_ELSE.search(lbl):
        return ""
    m = _DEGREE_YN.search(lbl)
    if m and not _DEGREE_IN.search(lbl):
        w = m.group("what").lower()
        for level, words in (("high school", ("high school", "ged")), ("associates", ("associate",)),
                             ("bachelors", ("bachelor", "undergraduate", "4", "four", "college")),
                             ("masters", ("master", "graduate")), ("doctorate", ("doctor", "ph"))):
            if any(w.startswith(x) for x in words):
                return level
    if m or _NOT_YES_NO.search(lbl) and not re.match(r"^\W*(what|which|please (select|provide|state|specify|enter))\b", lbl, re.I):
        return ""
    return "level" if _EDU_Q.search(lbl) else ""


def highest_education(profile: dict, today: _dt.date | None = None) -> str:
    """The highest degree level the saved profile shows as COMPLETED ("masters", "bachelors",
    "associates", "high school", ...), "" when it shows none. A degree still in progress (a date
    range to Present or into the future, a single date in the future, "Expected 2027",
    "Candidate") does not count; a degree with no date counts (it is on the resume as held)."""
    from autoapply.history import degree_level
    today = today or _dt.date.today()
    now = (today.year, today.month)
    best = ""
    for ed in (profile if isinstance(profile, dict) else {}).get("education") or []:
        if not isinstance(ed, dict):
            continue
        degree = _squash(ed.get("degree"))
        level = degree_level(degree)
        if level not in EDU_RANK:
            continue
        when = _squash(ed.get("date") or ed.get("dates"))
        if _PENDING.search(degree) or _PENDING.search(when):
            continue
        span = parse_span(when) if when else {}
        if span.get("current"):
            continue
        last = span.get("end") or span.get("start")
        if last and last.get("y") and (int(last["y"]), int(last.get("m") or 0) or 1) > now:
            continue                                    # it ends in the future
        if EDU_RANK[level] > EDU_RANK.get(best, -1):
            best = level
    return best


def education_level_of(option: str) -> str:
    """The level a list's choice names ("Master's Degree (MA, MS)" -> "masters", "Some College,
    No Degree" -> "some college"), "" for one that names none or is never picked by level
    ("Professional degree", "Other")."""
    o = _squash(option)
    for level, rx in _EDU_OPTION:
        if rx.search(o):
            return level
    return ""


def education_option(level: str, options) -> str | None:
    """The choice for ``level``: the one that names it, else the nearest LOWER choice (a
    bachelor's on a list that stops at "Associate degree"). Never a higher one, never "No high
    school diploma" for someone who holds more."""
    rank = EDU_RANK.get(level)
    if not rank:
        return None
    best = None
    for o in [str(o) for o in (options or []) if str(o).strip()]:
        r = EDU_RANK.get(education_level_of(o))
        if not r or r > rank:
            continue
        if best is None or r > best[0]:
            best = (r, o)
    return best[1] if best else None


def education_answer(label: str, options, level: str, field_type: str = "select") -> tuple[str, str] | None:
    """(answer, "history") for an education-level question from the highest completed degree
    ``level`` (highest_education), else None (the person's). A list: its choice for the level.
    A Yes / No question ("Do you have a bachelor's degree or higher?"): Yes when the level
    reaches it, No when the profile's completed education stops short. A text box: the level in
    words ("Master's degree")."""
    kind = education_question(label)
    if not kind or level not in EDU_RANK:
        return None
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if kind != "level":
        yes = EDU_RANK[level] >= EDU_RANK[kind]
        if opts:
            v = workauth.pick_yes_no(yes, opts)
            return (v, "history") if v else None
        return ("Yes" if yes else "No"), "history"
    if opts:
        v = education_option(level, opts)
        return (v, "history") if v else None
    if (field_type or "").lower() == "checkbox" or level not in EDU_WORDS:
        return None
    return EDU_WORDS[level], "history"
