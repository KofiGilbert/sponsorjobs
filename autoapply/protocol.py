"""The answer protocol: the apply agent may answer a form question itself, and code checks it
before the answer reaches the page (docs/apply-agent.md section 29, "The agent decides").

An action that answers a question with the brain's own words (a ``type`` of text that is not a
placeholder, a ``select``, a click on a radio, a checkbox or an option) carries:

    source    profile | resume | saved_answer | common_sense
    evidence  the short fact it used ("edu1: MBA, 2019", "no employer named Acme in work history")
    class     factual | preference | common_sense | legal

``check`` decides, from the question (autoapply/qclass.py) and the person's material
(autoapply/material.py), never from the brain's word alone:

* a **legal** question (hard limit 5) is never answered this way: only the person's saved answer
  for exactly that question fills it, and that travels as a placeholder ({{answer_N}}), which the
  brain cannot read. The class the brain claims does not matter: the question decides;
* a **factual** or **preference** answer must cite something that is in the block: the cited fact
  id exists, or the evidence's own words (an employer, a degree, a year) are found in it. A
  "nothing like that" answer ("No", "N/A") may cite an absence, and then the thing named must
  really be absent. A typed answer must itself be made of the material's words;
* **common sense** is for a question with no fact about the person that matters legally (how
  they heard about the job, the kind of phone, a follow-up that does not apply). Never on a
  question the classifier reads as factual, a preference or legal;
* a stand-in ("N/A", "None") is an answer only where the question invites it or does not apply;
  filler ("In", "TBD", "see resume") never is.

A refusal is returned WITH ITS REASON, as an observation for the brain's next step; nothing is
stripped in silence. Pure: no store, no network.

(2026-11-18, after the first real run: five steps were lost to the FORMAT of an answer.) A format
slip never costs a step. The three fields may sit on the action or in a ``proof`` object; a
missing ``class`` is the question's own (it decides anyway); a missing ``source`` or ``evidence``
is filled in here when the answer's own words are found in the block (the matching fact id is
attached), or when the question is one common sense answers. Only an answer that is truly
ungrounded, or legal, is refused.

Common-sense DEFAULTS (the owner: common sense first), each checked here and flagged in Review
answers like every answer the agent chose: full-time when the posting is full-time or says
nothing; the office the posting names, else the one in the person's own city or state, else the
first listed; Yes to a routine condition of the job (overtime, days on site, travel, a shift)
that the posting itself states. They cite the posting (``source: posting``, ids ``post`` and
``req1``...) or common sense. Never a default: salary, a reason for leaving, a start date,
relocation, a consent, anything legal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from autoapply import commonsense, qclass
from autoapply.asks import is_filler

SOURCES = ("profile", "resume", "saved_answer", "common_sense", "posting", "best_guess")
# The Review answers origin each source shows as (ui/static/app.js aaAnsFrom).
ORIGIN = {"profile": "profile", "resume": "history", "saved_answer": "library", "common_sense": "common_sense",
          "posting": "job", "best_guess": "best_guess"}
# The source a fact of each kind stands for, when the engine attaches the evidence itself.
KIND_SOURCE = {"location": "profile", "links": "profile", "job": "resume", "school": "resume", "skills": "resume",
               "years": "resume", "languages": "resume", "answer": "saved_answer", "posting": "posting"}
MAX_EVIDENCE = 160
HARD5 = "hard limit 5 (legal and attestation questions are answered only from the person's saved answer)"
EXAMPLE = ('{"op": "select", "index": 5, "option": "Master\'s Degree", "source": "resume", '
           '"evidence": "edu1: Master of Science, 2018", "class": "factual"}')

_ID = re.compile(r"\b(loc|skills|years|links|languages|post|(?:job|edu|ans|req)\d{1,2})\b", re.I)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_LONG_DIGITS = re.compile(r"\d{7,}")
_NEGATIVE = re.compile(r"\b(no|not|none|never|nothing|without|absent|isn'?t|aren'?t|doesn'?t|does not|did not|didn'?t)\b", re.I)
_NONE_WORD = re.compile(r"^\W*(n\s?/\s?a|none|not applicable|no)\W*$", re.I)
_YES = re.compile(r"^\W*(yes|y|true)\b", re.I)
_NO = re.compile(r"^\W*(no|n|false|none|n\s?/\s?a|not applicable)\b", re.I)
_FIRST = re.compile(r"\bfirst (one |office |location |option |choice )?(listed|on the list|in the list)\b", re.I)
# Words that say nothing about WHICH fact was used.
_GENERIC = frozenset(
    "a an the and or of to in on at for with by from is are was were be been has have had this that these those "
    "person persons applicant candidate their they them his her its my your resume profile saved answer answers "
    "block material fact facts history work worked working job jobs role roles title employer employers employed "
    "company companies named name listed lists list shows show says say states state per see cites cited current "
    "currently most recent latest degree degrees education school schools completed complete highest level "
    "question asks asked asking form field option options choice choices value type typed select selected pick "
    "picked yes no not none never nothing any all other only also there here where when which what who how as "
    "it so if then than does did do done held holds hold same matches match matching found find anything "
    "posting posted common sense default office offices location locations city time full part".split())


def _n(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#$ ]+", " ", str(s or "").lower())).strip()


def _tokens(s) -> list:
    """The words of a text that could name a fact (no generic word, no fact id, two letters up)."""
    return [w for w in _n(_ID.sub(" ", str(s or ""))).split() if w not in _GENERIC and (len(w) >= 2 or w.isdigit())]


def _has(text_n: str, token: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text_n))


@dataclass
class Verdict:
    ok: bool
    reason: str = ""          # why not, in words the brain can act on
    klass: str = ""           # the class the question has (qclass), or the brain's when unknown
    hard: str = ""            # the hard limit that applied, "" for a plain rejection
    source: str = ""          # the source the answer stands on (the brain's, or the one attached here)
    evidence: str = ""        # the evidence (the brain's, or the fact the engine matched itself)


def meta(action: dict) -> dict:
    """{source, evidence, class} as the brain wrote them, cleaned ("" for what it left out).
    Read from the action itself or from a ``proof`` (or ``answer``) object on it, whichever
    holds them: either shape is fine."""
    a = action if isinstance(action, dict) else {}
    inner = next((a[k] for k in ("proof", "answer", "protocol") if isinstance(a.get(k), dict)), {})
    get = lambda *keys: next((str(d.get(k)) for d in (a, inner) for k in keys if d.get(k) not in (None, "", True, False)), "")  # noqa: E731
    src = get("source", "origin").strip().lower().replace(" ", "_").replace("-", "_")
    src = {"saved": "saved_answer", "answer": "saved_answer", "library": "saved_answer", "history": "resume", "cv": "resume",
           "job": "posting", "job_posting": "posting", "jd": "posting", "commonsense": "common_sense",
           "guess": "best_guess", "bestguess": "best_guess", "my_best_guess": "best_guess"}.get(src, src)
    cls = get("class", "klass", "category").strip().lower().replace(" ", "_").replace("-", "_")
    cls = {"commonsense": "common_sense", "fact": "factual"}.get(cls, cls)
    ev = " ".join(get("evidence", "reason", "fact").split())[:MAX_EVIDENCE]
    return {"source": src if src in SOURCES else "", "evidence": ev, "class": cls if cls in qclass.CLASSES else ""}


def _fact_text(f) -> str:
    return " ".join(str(v) if not isinstance(v, list) else " ".join(str(x) for x in v)
                    for k, v in (f.show or {}).items() if k not in ("id", "about"))


def ground(value: str, material):
    """The fact of the block that an answer's own words come from, or None: the fact that holds
    the most of the answer's naming words, when it holds at least half of them (and any year the
    answer gives). This is how a missing ``evidence`` is filled in by the engine."""
    words = [w for w in _tokens(value) if not w.isdigit() and len(w) >= 3]
    years = _YEAR.findall(str(value or ""))
    best = None
    for f in getattr(material, "facts", []) or []:
        text = _n(_fact_text(f))
        if any(y not in text for y in years):
            continue
        hit = sum(1 for w in words if _has(text, w))
        if (hit and hit * 2 >= len(words)) or (not words and years):
            if best is None or hit > best[0]:
                best = (hit, f)
    return best[1] if best else None


def evidence_found(evidence: str, material) -> tuple[bool, bool]:
    """(found, negative) for an evidence line against the material. Found: a cited fact id exists
    (and, when the line says more, some of those words are in that fact), or the line's own words
    are in the block and every year it gives is too. Negative ("no employer named Acme ..."):
    found means the thing named is really ABSENT from the block."""
    ev = str(evidence or "")
    block_n = _n(getattr(material, "block", "") or "")
    by_id = {f.id.lower(): f for f in getattr(material, "facts", []) or []}
    ids = [m.group(1).lower() for m in _ID.finditer(ev)]
    words = _tokens(ev)
    negative = bool(_NEGATIVE.search(ev))
    if negative:
        named = [w for w in words if not w.isdigit()]
        return (not any(_has(block_n, w) for w in named)), True
    if any(y not in block_n for y in _YEAR.findall(ev)):
        return False, False
    cited = [by_id[i] for i in ids if i in by_id]
    if ids and not cited:
        return False, False                              # an id that does not exist
    if cited:
        if not words:
            return True, False
        text = _n(" ".join(_fact_text(f) for f in cited))
        return any(_has(text, w) for w in words), False
    return bool(words) and any(_has(block_n, w) for w in words if not w.isdigit() or len(w) == 4), False


def _typed_supported(text: str, question: str, material) -> bool:
    """A typed answer is made of the material's own words: every year it gives is in the block,
    and at least half of its naming words are in the block or the question."""
    block_n = _n(getattr(material, "block", "") or "")
    if any(y not in block_n for y in _YEAR.findall(str(text))):
        return False
    words = [w for w in _tokens(text) if not w.isdigit()]
    if not words:
        return True                                      # a Yes, a No, a small number
    pool = block_n + " " + _n(question)
    return sum(1 for w in words if _has(pool, w)) * 2 >= len(words)


def _invites_none(question: str) -> bool:
    q = " ".join(str(question or "").split())
    return bool(commonsense._NONE_ASKED.search(q)) or (qclass.question_class(q) == qclass.COMMON and bool(qclass._FOLLOW_UP.search(q)))


# --------------------------------------------------------------------------- common-sense defaults
def _place_words(text: str) -> set:
    return {w for w in _n(text).split() if len(w) >= 3 and w not in {"the", "and", "city", "area", "office", "metro", "greater"}}


def _names_place(value: str, place_text: str) -> bool:
    """Does an office choice name a place the text names ("New York" in "Dallas; New York City")?"""
    v, t = _n(value), _n(place_text)
    if not v or not t:
        return False
    if _has(t, v) or any(_has(v, part.strip()) for part in re.split(r"[;|/]| or | and ", t) if len(part.strip()) >= 3):
        return True
    words = _place_words(value)
    return bool(words) and all(_has(t, w) for w in words)


def default_answer(kind: str, value: str, question: str, options, material) -> tuple[bool, str, str]:
    """(ok, evidence, reason) for a common-sense default of ``kind`` (qclass.default_kind):

    * employment type: the posting's own type; full-time when the posting says full-time or
      names none;
    * office: one the posting's location names, else one in the person's own city or state, else
      the first one listed;
    * a condition of the job (overtime, days on site, travel, a shift): Yes, and only when the
      posting itself states that condition. A No, or a condition the posting does not state, is
      the person's to answer."""
    post = getattr(material, "posting", {}) or {}
    place = getattr(material, "place", {}) or {}
    v = " ".join(str(value or "").split())
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if kind == qclass.EMPLOYMENT:
        want = post.get("type") or "full-time"
        said = next((name for name in ("full-time", "part-time", "contract", "internship")
                     if re.search(name.replace("-", "[- ]?"), v, re.I) or (name == "contract" and re.search(r"temp", v, re.I))), "")
        if said == want:
            return True, ("post: employment type " + want) if post.get("type") else "common sense: full-time when the posting names no type", ""
        return False, "", (f"the posting is {want}" if post.get("type") else "the posting names no employment type, so the default is full-time")
    if kind == qclass.OFFICE:
        if post.get("location") and _names_place(v, post["location"]):
            return True, "post: location " + str(post["location"])[:80], ""
        city, state = place.get("city") or "", place.get("state") or ""
        if (city and _has(_n(v), _n(city))) or (state and (_has(_n(v), _n(state)) or (place.get("code") and re.search(r"(^|[\s,(-])" + re.escape(str(place["code"])) + r"($|[\s,)-])", v)))):
            return True, "loc: the person's own city or state", ""
        if opts and _n(opts[0]) == _n(v):
            return True, "common sense: the first office listed (none matches the posting or the person's city)", ""
        if not post.get("location") and not city and not opts:
            return True, "common sense: no location is known for the posting or the person", ""
        return False, "", ("pick the office the posting names (" + str(post.get("location") or "none stated")[:80]
                           + "), else the one in the person's city or state, else the first one listed")
    if kind == qclass.CONDITION:
        asked = qclass.condition_names(question)
        reqs = [(f.id, f.data.get("requirement") or "") for f in getattr(material, "facts", []) or []
                if f.kind == "posting" and f.data.get("requirement")]
        hit = next((fid for fid, text in reqs if asked & qclass.condition_names(text)), "")
        if not hit:
            return False, "", "the posting does not state this as a condition of the role, so it is the person's to answer"
        if not _YES.match(v):
            return False, "", "only Yes is a default here (the posting states it); a No is the person's to say"
        return True, hit + ": the posting states it", ""
    return False, "", "no default for this question"


# --------------------------------------------------------------------------- the check
NEVER = ("never guessed, in any mode: an ID number, a date of birth, bank or payment details, a password or a typed "
         "signature is filled only from the person's saved value. Leave it; if the form requires it, this "
         "application cannot be done by the agent")


def check(action: dict, question: str, options=(), material=None, *, saved: bool = False, auto: bool = False) -> Verdict:
    """May this answer go on the page? The check of ``_check``, and in Auto (``auto``; the owner,
    2026-11-19: Auto never stops to ask) one more door: an answer nothing supports is taken as a
    flagged BEST GUESS (autoapply/guess.py) instead of being refused, so the run goes on. Never
    for a never-guessed field, a consent, a long number or filler; where the form offers a
    neutral choice the guess is that one (always, on a self-identification question)."""
    v = _check(action, question, options, material, saved=saved)
    if v.ok or saved or not auto:
        return v
    from autoapply import guess
    op = str(action.get("op") or "")
    value = str(action.get("text") if op == "type" else action.get("option") or action.get("value") or "")
    if v.hard == "6" or (op == "type" and is_filler(value) and not _NONE_WORD.match(value)):
        return v
    ok, why = guess.accept(question, value, options, typed=op == "type")
    if ok:
        said = meta(action)["evidence"]
        m = guess.majority(question)
        return Verdict(True, klass=v.klass or meta(action)["class"], source=guess.SOURCE,
                       evidence=(said or (m[1] if m else "") or "nothing saved or in the material answers this")[:MAX_EVIDENCE])
    if why == "never":
        return Verdict(False, NEVER + ".", v.klass, hard="9")
    if why == "consent":
        return v
    return Verdict(False, "Auto takes a best guess here, but not this one: " + why + ".", v.klass)


def _check(action: dict, question: str, options=(), material=None, *, saved: bool = False) -> Verdict:
    """May this answer go on the page? ``action``: the brain's type / select / click with its
    source, evidence and class (any of them may be missing); ``question``: the field's own label;
    ``options``: the choices the view shows for it; ``material``: autoapply/material.Material;
    ``saved``: the value is the person's saved answer for this question (a placeholder), which
    always passes."""
    q = " ".join(str(question or "").split())
    company = getattr(material, "company", "") or ""
    employers = getattr(material, "employers", []) or []
    klass = qclass.question_class(q, options, company=company, employers=employers)
    if saved:
        return Verdict(True, klass=klass or "factual")
    m = meta(action)
    op = str(action.get("op") or "")
    value = str(action.get("text") if op == "type" else action.get("option") or action.get("value") or "")
    if klass == qclass.LEGAL:
        if qclass.worked_here(q, company) and qclass.employer_in_history(company, employers):
            return Verdict(False, HARD5 + ": the work history lists this employer, so the person confirms it. "
                           "Leave it; report needs-answers if the page will not move on.", klass, hard="5")
        return Verdict(False, HARD5 + ": no saved answer is in <left> for this question. Leave it; report "
                       "needs-answers if it is required.", klass, hard="5")
    if m["class"] == qclass.LEGAL:
        return Verdict(False, HARD5 + ": you classed this as legal, so only a saved answer fills it.", qclass.LEGAL, hard="5")
    if op == "type" and _LONG_DIGITS.search(value):
        return Verdict(False, "hard limit 6: a phone, an ID or a card number is never typed as free text; "
                       "use the placeholder if there is one.", klass, hard="6")
    filler = op == "type" and is_filler(value)
    if filler and not _NONE_WORD.match(value):
        return Verdict(False, "that is filler, not an answer. Leave an optional box empty; a required one "
                       "is the person's (needs-answers).", klass)
    src, ev = m["source"], m["evidence"]
    yes = lambda **kw: Verdict(True, klass=kw.pop("klass", klass or m["class"]), source=kw.get("source", src),   # noqa: E731
                               evidence=kw.get("evidence", ev))
    # A common-sense default (employment type, an office, a condition the posting states): the
    # rule decides, whatever fields came with the answer.
    kind = qclass.default_kind(q)
    if kind and src in ("", "common_sense", "posting"):
        ok, found_ev, why = default_answer(kind, value, q, options, material)
        if ok:
            return yes(source="posting" if found_ev.startswith(("post", "req")) else ("profile" if found_ev.startswith("loc") else "common_sense"),
                       evidence=found_ev, klass=qclass.COMMON)
        return Verdict(False, "common sense does not settle this one: " + why + ". Otherwise report needs-answers.", klass)
    # Worked here before, employer not in the work history: the history itself says No.
    if qclass.worked_here(q, company):
        if _NO.match(value) or (op != "type" and not _YES.match(value)):
            return yes(source=src or "resume", evidence=ev or "no employer of that name in the work history", klass=qclass.FACTUAL)
        return Verdict(False, "the work history does not list this employer, so Yes is not supported.", klass)
    common = m["class"] == qclass.COMMON or src == "common_sense" or (not src and klass == qclass.COMMON)
    if common and not (not src and not ev and ground(value, material) is not None and klass != qclass.COMMON):
        if klass in (qclass.FACTUAL, qclass.PREFERENCE):
            return Verdict(False, "this question asks a " + ("fact about" if klass == qclass.FACTUAL else "preference of")
                           + " the person, so common sense does not answer it. Cite the material (source profile, "
                           "resume or saved_answer), or report needs-answers.", klass)
        if filler and not _invites_none(q):
            return Verdict(False, "a stand-in such as N/A is an answer only where the question asks for it. "
                           "Leave an optional box empty; a required one is the person's.", klass)
        if op == "type" and len(_tokens(value)) > 6:
            return Verdict(False, "a common-sense answer is a few words, never a paragraph.", klass)
        return yes(source=src or "common_sense", evidence=ev or "common sense for this kind of question", klass=qclass.COMMON)
    # Factual and preference answers stand on the block. Evidence the brain gave is checked; with
    # none (or with one that is not found) the engine looks for the answer's own words itself.
    found, negative = evidence_found(ev, material) if ev else (False, False)
    fact = None
    if not found:
        fact = ground(value, material)
        if fact is not None:
            found, negative = True, False
            ev = f"{fact.id}: matched by the engine"
            src = src or KIND_SOURCE.get(fact.kind, "resume")
    if not found and not getattr(material, "block", ""):
        return Verdict(False, "nothing about the person is loaded on this step, so nothing can be cited. Leave an "
                       "optional field; a required one shows in <left> with the material next step.", klass)
    if not found:
        why = ("the thing you say is absent is in <person>" if negative
               else "nothing in <person> supports this answer" if not m["evidence"]
               else "the evidence you cite is not in <person> (name the fact: its id, or the employer, degree or year)")
        return Verdict(False, why + ". Answer from the material, like " + EXAMPLE + ", or report needs-answers.", klass)
    if negative and _YES.match(value):
        return Verdict(False, "the evidence says the material has nothing like that, which does not support Yes.", klass)
    if filler and not (negative or _invites_none(q)):
        return Verdict(False, "a stand-in such as N/A is an answer only where the question asks for it or "
                       "nothing in the material applies.", klass)
    if op == "type" and not filler and not negative and not _typed_supported(value, q, material):
        return Verdict(False, "what you typed is not in <person>. Type the fact as the material gives it, "
                       "or report needs-answers.", klass)
    if not src:
        cited = next((f for f in getattr(material, "facts", []) or [] if re.search(r"\b" + re.escape(f.id) + r"\b", ev, re.I)), None)
        src = KIND_SOURCE.get(cited.kind, "resume") if cited is not None else "resume"
    return yes(source=src, evidence=ev)


def record(action: dict, verdict: Verdict) -> dict:
    """What rides on a checked action to the shell and the Review answers list: the source (and
    the origin label it shows as), the evidence and the class. Never the answer itself."""
    m = meta(action)
    src = verdict.source or m["source"]
    return {"source": src, "origin": ORIGIN.get(src, "ai"), "evidence": verdict.evidence or m["evidence"],
            "class": verdict.klass or m["class"]}
