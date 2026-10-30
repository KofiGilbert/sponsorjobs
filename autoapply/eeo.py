"""Voluntary self-identification (EEO): the person's saved answer, on each form's own wording.

The person answers gender, Hispanic or Latino, race, veteran status and disability ONCE (on Your
answers, or when the agent asks). Every form words the same choice differently: "Male" is "Man"
or "M", "I am not a protected veteran" is a plain "No" to "Are you a protected veteran?", and
Workday's "Ethnicity" is one COMBINED list of races and the Hispanic or Latino choice ("Asian (Not
Hispanic or Latino) (United States of America)"). ``choose`` maps the saved answers to the one
option that says the same thing, in code, with no model.

Never a guess: when no option says it, or several do, the answer is None and the field stays
open for the person. A saved "Yes" is not a protected-veteran self-identification (a "Veteran"
question can mean any service), "not a protected veteran" is never "not a veteran" (a veteran who
is not protected served), and a combined race list is only answered when both the race and the
Hispanic or Latino answer are known (or either is a decline).
"""
from __future__ import annotations

import re

from autoapply.answers import normalize

# The "decline to self-identify" choice in its usual wordings, the federal disability form's
# "I do not want to answer" (CC-305) and Workday's "Not Declared" included. Mirrored in
# shell-electron/agent.js and extension/agent_view.js DECLINE_RE.
DECLINE = re.compile(r"decline|prefer not|do not wish|don['’]?t wish|not wish to"
                     r"|(do not|don['’]?t) want to (answer|disclose|say|self[- ]?identify|provide)"
                     r"|choose not|rather not|not to (say|answer|disclose)|not declared", re.I)

# The canonical question for each self-identification key (how the library is asked for it).
QUESTIONS = {"@gender": "Gender", "@sex": "Sex", "@race": "Race", "@ethnicity": "Ethnicity",
             "@hispanic_latino": "Are you Hispanic or Latino?",
             "@veteran": "Veteran status", "@disability": "Disability status"}
# The keys a race or ethnicity question reads together (a combined list needs both halves).
RACE_KEYS = ("@race", "@ethnicity", "@hispanic_latino")
KEYS = tuple(QUESTIONS)


def decline_option(options) -> str | None:
    """The one "decline to self-identify" option, or None."""
    hits = [str(o) for o in (options or []) if DECLINE.search(str(o))]
    return hits[0] if len(hits) == 1 else None


def _n(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#$ ]+", " ", str(s or "").lower())).strip()


def _core(s: str) -> str:
    """An option's own words: lowercase, without its parenthesised parts ("(United States of
    America)", "(he/him)"), punctuation as spaces."""
    s = str(s or "").lower().replace("’", "'")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- value classes
_I_AM = r"(i identify as |i am |identify as |i m )?(a |an )?"
_GENDER = (
    ("male", re.compile(_I_AM + r"(male|man|m|men|cis ?(gender )?(male|man))")),
    ("female", re.compile(_I_AM + r"(female|woman|f|women|cis ?(gender )?(female|woman))")),
    ("nonbinary", re.compile(_I_AM + r"(non ?binary|enby|gender ?queer|gender ?non ?conforming|gender ?non ?binary)"
                             r"(( or | and | )(non ?binary|gender ?non ?conforming|non ?conforming|gender ?queer))?")),
)
_RACE = (
    ("american_indian", re.compile(r"(american indian|alaska n?|alaskan native|native american)\b")),
    ("asian", re.compile(r"asian\b(?! indian)")),
    ("black", re.compile(r"(black|african american)\b")),
    ("pacific", re.compile(r"(native hawaiian|(other )?pacific islander|hawaiian)\b")),
    ("white", re.compile(r"(white|caucasian)\b")),
    ("two_or_more", re.compile(r"(two or more|2 or more|multi ?racial|multiple races|mixed race|biracial)\b")),
)


def gender_class(text: str) -> str | None:
    """male, female, nonbinary or decline for a gender answer or option; None for anything else."""
    if DECLINE.search(str(text or "")):
        return "decline"
    c = _core(text)
    for cls, rx in _GENDER:
        if rx.fullmatch(c):
            return cls
    return None


def race_class(text: str) -> str | None:
    """The race an answer or option names (american_indian, asian, black, pacific, white,
    two_or_more), None for anything else (a Hispanic or Latino choice included)."""
    c = _core(text)
    for cls, rx in _RACE:
        if rx.match(c):
            return cls
    return None


def hispanic_class(text: str, *, bare: bool = True) -> str | None:
    """yes, no or decline for the Hispanic or Latino question: "Yes" / "Hispanic or Latino",
    "No" / "Not Hispanic or Latino", or the decline. ``bare`` lets a plain Yes or No count (on
    the Hispanic question itself, never on a race list)."""
    t = str(text or "")
    if DECLINE.search(t):
        return "decline"
    full, c = _n(t), _core(t)
    if re.search(r"\b(not|non) (hispanic|latin)", full):
        return "no"
    if re.match(r"(hispanic|latin[oax]|latine)\b", c):
        return "yes"
    if bare and re.match(r"no\b", c):
        return "no"
    if bare and re.match(r"yes\b", c):
        return "yes"
    return None


def veteran_class(text: str, *, saved: bool = False) -> str | None:
    """The choice a veteran answer or option makes:

    * ``not``: not a PROTECTED veteran ("I am not a protected veteran"), which says nothing about
      any other service;
    * ``not_veteran``: never served ("I am not a veteran"), so not a protected veteran either;
    * ``veteran_not_protected``: served, not protected ("I am a veteran but not a protected
      veteran");
    * ``protected``; ``veteran`` (any service: "I am a veteran"); ``decline``;
    * ``no`` / ``yes``: a plain option, read with its question (see choose). Never a saved
      answer: "Yes" to a "Veteran" question can mean any service."""
    t = str(text or "")
    if DECLINE.search(t):
        return "decline"
    c = _core(t)
    if re.search(r"\bveteran\b.*\bnot an? protected veterans?\b|\bveteran\b.*\bnot protected\b", c):
        return "veteran_not_protected"
    if re.search(r"\bnot an? protected veterans?\b", c):
        return "not"
    if re.search(r"\bnot an? veterans?\b|\bnever served\b|\bhave not served\b", c):
        return "not_veteran"
    if re.search(r"\bidentify as (one or more of the classifications of )?(an? )?protected veterans?\b"
                 r"|\bi am an? protected veteran\b|^protected veterans?$", c):
        return "protected"
    if not saved:                    # "Are you a protected veteran?" Yes / No
        if re.match(r"no\b", c):
            return "no"
        if c == "yes":
            return "yes"
    if re.fullmatch(r"(yes )?i am an? veteran|veteran", c):
        return "veteran"             # any service: true of a protected veteran too (see choose)
    return None


def disability_class(text: str) -> str | None:
    """yes, no or decline for the disability self-identification (CC-305): "Yes, I have a
    disability...", "No, I do not have a disability...", a plain Yes or No. Never an
    accommodation question's answer."""
    t = str(text or "")
    if DECLINE.search(t):
        return "decline"
    c = _core(t)
    if "accommodat" in c:
        return None
    if re.match(r"(yes|i have a disability|i had a disability)\b", c):
        return "yes"
    if re.match(r"(no|i do not have a disability|i don t have a disability)\b", c):
        return "no"
    return None


def value_class(key: str, text: str, *, saved: bool = False) -> str | None:
    """The choice an answer or option makes on the question ``key`` ("@gender" ...), or None."""
    if not str(text or "").strip():
        return None
    if key in ("@gender", "@sex"):
        return gender_class(text)
    if key == "@race":
        return "decline" if DECLINE.search(str(text)) else race_class(text)
    if key in ("@ethnicity", "@hispanic_latino"):
        return hispanic_class(text)
    if key == "@veteran":
        return veteran_class(text, saved=saved)
    if key == "@disability":
        return disability_class(text)
    return "decline" if DECLINE.search(str(text)) else None


# --------------------------------------------------------------------------- the person
def person(answers: dict) -> tuple[str | None, str | None]:
    """(race, hispanic) from the saved answers: the race class (or "decline") and yes / no /
    "decline" for Hispanic or Latino. The Hispanic or Latino question's own answer first, else
    an "Ethnicity" answer that says it. An answer picked on a combined list says both ("Asian
    (Not Hispanic or Latino)"), so each fills in what the other key leaves open."""
    ra = str((answers or {}).get("@race") or "").strip()
    ea = str((answers or {}).get("@ethnicity") or "").strip()
    ha = str((answers or {}).get("@hispanic_latino") or "").strip()
    race = ("decline" if DECLINE.search(ra) else race_class(ra)) if ra else None
    hisp = hispanic_class(ha) if ha else None
    if hisp is None and ea:
        hisp = hispanic_class(ea)
    if not ra and ea and not DECLINE.search(ea):        # only when nothing of its own is saved
        race = race_class(ea)
    if hisp is None and ra and re.search(r"hispanic|latin", ra, re.I):
        hisp = hispanic_class(ra, bare=False)
    return race, hisp


def saved(library, question: str = "") -> tuple[str, dict, dict]:
    """(the question's key, {key: answer}, {key: library row key}) for the person's usable
    self-identification answers, plus the answer to ``question`` itself when it has one."""
    answers: dict = {}
    rows: dict = {}
    key = normalize(question) if question else ""
    if library is None:
        return key, answers, rows
    asks = dict(QUESTIONS)
    if question:
        asks["_q"] = question
    for k, q in asks.items():
        try:
            hit = library.lookup(q)
        except Exception:          # noqa: BLE001 - a library hiccup only means nothing saved
            hit = None
        a = str((hit or {}).get("answer") or "").strip()
        if not a:
            continue
        kk = (key or str(hit.get("key") or "")) if k == "_q" else k
        if k == "_q":
            key = kk
        answers[kk] = a
        rows[kk] = str(hit.get("key") or kk)        # the row's own key (an older one, at times)
    return key, answers, rows


# --------------------------------------------------------------------------- choosing
def _one(hits: list, prefer=None) -> str | None:
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1 and prefer:
        best = [h for h in hits if prefer(h)]
        if len(best) == 1:
            return best[0]
    return None


def _race_or_ethnicity(key: str, opts: list, answers: dict) -> str | None:
    race, hisp = person(answers)
    has_race = any(race_class(o) for o in opts if not DECLINE.search(o))
    has_hisp = any(hispanic_class(o, bare=False) == "yes" for o in opts if not DECLINE.search(o))
    if has_race and has_hisp:                       # one combined list (Workday's "Ethnicity")
        if race == "decline" or hisp == "decline":
            return decline_option(opts)
        if hisp == "yes":
            return _one([o for o in opts if not DECLINE.search(o)
                         and hispanic_class(o, bare=False) == "yes" and not race_class(o)])
        if hisp == "no" and race:
            hits = [o for o in opts if not DECLINE.search(o) and race_class(o) == race
                    and hispanic_class(o, bare=False) != "yes"]
            return _one(hits, prefer=lambda o: hispanic_class(o, bare=False) == "no")
        return None                                 # the other half is not known: ask
    if has_race:                                    # a race list
        if race == "decline":
            return decline_option(opts)
        if not race:
            return None
        hits = [o for o in opts if not DECLINE.search(o) and race_class(o) == race
                and not (hispanic_class(o, bare=False) == "no" and hisp != "no")]
        return _one(hits, prefer=lambda o: _core(o) == _core(answers.get("@race") or ""))
    # the Hispanic or Latino question ("Yes" / "No", or the words); a race question whose
    # options are neither races nor Hispanic or Latino is nobody's to answer here
    if key not in ("@ethnicity", "@hispanic_latino") and not any(re.search(r"hispanic|latin[oax]|latine", o, re.I)
                                                                  for o in opts):
        return None
    if hisp == "decline":
        return decline_option(opts)
    if hisp not in ("yes", "no"):
        return None
    return _one([o for o in opts if not DECLINE.search(o) and hispanic_class(o) == hisp])


# A question about PROTECTED veteran status, where a plain Yes or No says it.
_PROTECTED_Q = re.compile(r"protected", re.I)


def _veteran(want: str, opts: list, a: str, question: str) -> str | None:
    """The veteran option that says what the saved choice says, never more. "I am not a
    protected veteran" is not "I am not a veteran" (a veteran who is not protected served), and
    a plain No only answers a question that asks about protected veterans."""
    cls = {o: value_class("@veteran", o) for o in opts}
    protected_q = bool(_PROTECTED_Q.search(question or ""))
    order = {
        "not": ["not"] + (["no"] if protected_q else []),
        "not_veteran": ["not_veteran", "not", "no"],
        "veteran_not_protected": ["veteran_not_protected", "veteran", "not"] + (["no"] if protected_q else []),
        "protected": ["protected", "yes", "veteran"],
    }.get(want, [])
    for c in order:
        hits = [o for o in opts if cls[o] == c]
        if hits:
            return _one(hits, prefer=lambda o: _core(o) == _core(a))
    return None


def choose(key: str, options, answers: dict, question: str = "") -> str | None:
    """The one option of a self-identification question (``key`` is its normalized key, and
    ``question`` its words when known) that the person's saved ``answers`` ({key: answer}) name:
    the saved words themselves, else the option that makes the same choice in the form's own
    words, else the page's own decline choice for a saved decline. None when nothing fits, or
    more than one option does."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    answers = answers or {}
    a = str(answers.get(key) or "").strip()
    if not opts:
        return None
    eq = [o for o in opts if a and _n(o) == _n(a)]
    if key in RACE_KEYS:
        # Both halves first: a Hispanic or Latino person gets the Hispanic choice on a combined
        # list even where their race is a choice of its own; the saved words otherwise.
        return _race_or_ethnicity(key, opts, answers) or (eq[0] if eq else None)
    if eq:
        return eq[0]
    if not a:
        return None
    if DECLINE.search(a):
        return decline_option(opts)
    want = value_class(key, a, saved=True)
    if not want or want in ("decline", "veteran"):
        return None
    if key == "@veteran":
        return _veteran(want, opts, a, question)
    hits = [o for o in opts if value_class(key, o) == want]
    return _one(hits, prefer=lambda o: _core(o) == _core(a))
