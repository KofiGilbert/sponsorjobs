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
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from autoapply import commonsense, qclass
from autoapply.asks import is_filler

SOURCES = ("profile", "resume", "saved_answer", "common_sense")
# The Review answers origin each source shows as (ui/static/app.js aaAnsFrom).
ORIGIN = {"profile": "profile", "resume": "history", "saved_answer": "library", "common_sense": "common_sense"}
MAX_EVIDENCE = 160
HARD5 = "hard limit 5 (legal and attestation questions are answered only from the person's saved answer)"

_ID = re.compile(r"\b(loc|skills|years|links|languages|(?:job|edu|ans)\d{1,2})\b", re.I)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_LONG_DIGITS = re.compile(r"\d{7,}")
_NEGATIVE = re.compile(r"\b(no|not|none|never|nothing|without|absent|isn'?t|aren'?t|doesn'?t|does not|did not|didn'?t)\b", re.I)
_NONE_WORD = re.compile(r"^\W*(n\s?/\s?a|none|not applicable|no)\W*$", re.I)
_YES = re.compile(r"^\W*(yes|y|true)\b", re.I)
_NO = re.compile(r"^\W*(no|n|false|none|n\s?/\s?a|not applicable)\b", re.I)
# Words that say nothing about WHICH fact was used.
_GENERIC = frozenset(
    "a an the and or of to in on at for with by from is are was were be been has have had this that these those "
    "person persons applicant candidate their they them his her its my your resume profile saved answer answers "
    "block material fact facts history work worked working job jobs role roles title employer employers employed "
    "company companies named name listed lists list shows show says say states state per see cites cited current "
    "currently most recent latest degree degrees education school schools completed complete highest level "
    "question asks asked asking form field option options choice choices value type typed select selected pick "
    "picked yes no not none never nothing any all other only also there here where when which what who how as "
    "it so if then than does did do done held holds hold same matches match matching found find anything".split())


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


def meta(action: dict) -> dict:
    """{source, evidence, class} as the brain wrote them, cleaned ("" for what it left out)."""
    a = action if isinstance(action, dict) else {}
    src = str(a.get("source") or "").strip().lower().replace(" ", "_")
    cls = str(a.get("class") or a.get("klass") or "").strip().lower().replace(" ", "_")
    ev = " ".join(str(a.get("evidence") or "").split())[:MAX_EVIDENCE]
    return {"source": src if src in SOURCES else "", "evidence": ev, "class": cls if cls in qclass.CLASSES else ""}


def _fact_text(f) -> str:
    return " ".join(str(v) if not isinstance(v, list) else " ".join(str(x) for x in v)
                    for k, v in (f.show or {}).items() if k not in ("id", "about"))


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
    return bool(commonsense._NONE_ASKED.search(q)) or qclass.question_class(q) == qclass.COMMON and bool(qclass._FOLLOW_UP.search(q))


def check(action: dict, question: str, options=(), material=None, *, saved: bool = False) -> Verdict:
    """May this answer go on the page? ``action``: the brain's type / select / click with its
    source, evidence and class; ``question``: the field's own label; ``options``: the choices the
    view shows for it; ``material``: autoapply/material.Material; ``saved``: the value is the
    person's saved answer for this question (a placeholder), which always passes."""
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
    if not (m["source"] and m["evidence"] and m["class"]):
        return Verdict(False, "an answer you choose yourself needs source, evidence and class on the action "
                       "(the answer protocol). Add them, or leave the field.", klass)
    if m["class"] == qclass.LEGAL:
        return Verdict(False, HARD5 + ": you classed this as legal, so only a saved answer fills it.", qclass.LEGAL, hard="5")
    if op == "type" and _LONG_DIGITS.search(value):
        return Verdict(False, "hard limit 6: a phone, an ID or a card number is never typed as free text; "
                       "use the placeholder if there is one.", klass, hard="6")
    filler = op == "type" and is_filler(value)
    if filler and not _NONE_WORD.match(value):
        return Verdict(False, "that is filler, not an answer. Leave an optional box empty; a required one "
                       "is the person's (needs-answers).", klass)
    common = m["class"] == qclass.COMMON or m["source"] == "common_sense"
    if common:
        if klass in (qclass.FACTUAL, qclass.PREFERENCE):
            return Verdict(False, "this question asks a " + ("fact about" if klass == qclass.FACTUAL else "preference of")
                           + " the person, so common sense does not answer it. Cite the material (source profile, "
                           "resume or saved_answer), or report needs-answers.", klass)
        if filler and not _invites_none(q):
            return Verdict(False, "a stand-in such as N/A is an answer only where the question asks for it. "
                           "Leave an optional box empty; a required one is the person's.", klass)
        if op == "type" and len(_tokens(value)) > 6:
            return Verdict(False, "a common-sense answer is a few words, never a paragraph.", klass)
        return Verdict(True, klass=qclass.COMMON)
    found, negative = evidence_found(m["evidence"], material)
    # Worked here before, employer not in the work history: the history itself says No.
    if qclass.worked_here(q, company):
        if _NO.match(value) or (op != "type" and not _YES.match(value)):
            return Verdict(True, klass=qclass.FACTUAL)
        return Verdict(False, "the work history does not list this employer, so Yes is not supported.", klass)
    if not found and not getattr(material, "block", ""):
        return Verdict(False, "nothing about the person is loaded on this step, so nothing can be cited. Leave an "
                       "optional field; a required one shows in <left> with the material next step.", klass)
    if not found:
        why = ("the thing you say is absent is in <person>" if negative
               else "the evidence you cite is not in <person> (name the fact: its id, or the employer, degree or year)")
        return Verdict(False, why + ". Answer from the material, or report needs-answers.", klass)
    if negative and _YES.match(value):
        return Verdict(False, "the evidence says the material has nothing like that, which does not support Yes.", klass)
    if filler and not (negative or _invites_none(q)):
        return Verdict(False, "a stand-in such as N/A is an answer only where the question asks for it or "
                       "nothing in the material applies.", klass)
    if op == "type" and not filler and not negative and not _typed_supported(value, q, material):
        return Verdict(False, "what you typed is not in <person>. Type the fact as the material gives it, "
                       "or report needs-answers.", klass)
    return Verdict(True, klass=klass or m["class"])


def record(action: dict, verdict: Verdict) -> dict:
    """What rides on a checked action to the shell and the Review answers list: the source (and
    the origin label it shows as), the evidence and the class. Never the answer itself."""
    m = meta(action)
    return {"source": m["source"], "origin": ORIGIN.get(m["source"], "ai"), "evidence": m["evidence"],
            "class": verdict.klass or m["class"]}
