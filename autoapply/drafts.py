"""Written answers for the apply agent: an open-ended question on a form is drafted from the
person's real history, for them to read before Send (docs/apply-agent.md section 30).

"Please tell us what you liked and disliked about your last role. What interests you most about
this opportunity?" used to be left for the person. Drafting screening answers from the resume and
the posting is what this product does (CLAUDE.md sections 1 and 8), and the Review page already
has the drafter (drafting/drafter.py ``screening_answers``: one first-person answer per question,
grounded ONLY in the profile and the job, never an invented employer, number or date). This module
is the thin part the agent adds:

* which question is drafted: ``qclass.is_essay`` (motivation, interest in the role, experience
  with something, a strength, a project), on a text box. Never a legal or saved-only question
  (salary, a start date, a reason for leaving, relocation, a consent), never a follow-up that
  only applies to a Yes, never the cover letter box (it has its own path);
* how long: a limit the question states ("in 150 words or less", "max 500 characters"), else
  about 600 characters, a short paragraph that fits a form's box;
* how it is asked for (``guidance``): plain first person, no dashes, inside the limit, and, for
  a question whose honest answer could hurt the person if guessed (what they disliked, a
  weakness, a conflict, a failure): neutral and professional, no person or employer spoken ill
  of, what they would like more of said in terms of what this role offers, and no event that the
  resume does not show;
* what comes back is cleaned (``clean``): dashes out, any sentence that carries a figure found
  in neither the profile nor the posting is dropped (tailoring/fact_gate.py), and the text is cut
  at a sentence end inside the limit. An empty result is no draft: the question is then the
  person's.

The draft is saved on the application's screening answers (``by: "agent"``), typed through a
placeholder like any saved answer, and shown in Review answers as "Drafted. Written for this
job. Read it." An application that holds one is never sent by the agent alone
(autoapply/modes.py ``may_send_alone``).
"""
from __future__ import annotations

import re

DEFAULT_CHARS = 600
MAX_CHARS = 1500
MIN_CHARS = 120

_WORDS = re.compile(r"(?:max(?:imum)?|up to|no more than|at most|within|under|in|limit(?:ed)? to|of)\s*(?:of\s*)?(\d{2,4})\s*words", re.I)
_WORDS_AFTER = re.compile(r"(\d{2,4})\s*words?\s*(?:or (?:less|fewer)|max(?:imum)?|limit)", re.I)
_CHARS = re.compile(r"(\d{2,5})\s*(?:characters|chars)", re.I)
_SENTENCES = re.compile(r"(\d)\s*(?:to|-)\s*(\d)\s*sentences|(\d)\s*sentences", re.I)
# A question whose honest answer could hurt the person if it were guessed.
_DELICATE = re.compile(r"dislike|least (like|enjoy|favou?rite)|weakness|shortcoming|conflict|disagree|difficult (boss|manager|colleague|coworker)"
                       r"|fail(ed|ure)?\b|mistake|criticism|frustrat|did not (like|enjoy)", re.I)
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def limit_for(question: str) -> int:
    """How many characters the answer may be: the limit the question states (words count as six
    characters each, a sentence as 140), else DEFAULT_CHARS. Always within MIN_CHARS..MAX_CHARS."""
    q = str(question or "")
    n = 0
    m = _CHARS.search(q)
    if m:
        n = int(m.group(1))
    else:
        m = _WORDS.search(q) or _WORDS_AFTER.search(q)
        if m:
            n = int(m.group(1)) * 6
        else:
            m = _SENTENCES.search(q)
            if m:
                n = int(m.group(2) or m.group(3)) * 140
    return max(MIN_CHARS, min(MAX_CHARS, n or DEFAULT_CHARS))


def delicate(question: str) -> bool:
    return bool(_DELICATE.search(str(question or "")))


def guidance(question: str, limit: int) -> str:
    """What the drafter is told for one form question, beyond its own rules."""
    lines = [f"Write it for the application form's box: plain first person, at most {limit} characters, "
             "no bullet points, no heading, no em or en dashes.",
             "Answer every part of the question, in the order it asks.",
             "Use only what the profile and the job show. No number, employer, date or event that is not there."]
    if delicate(question):
        lines.append("Part of this question asks for something that could count against the applicant (a dislike, a "
                     "weakness, a conflict, a failure). Keep that part neutral and professional: speak ill of no "
                     "person and no employer, name nothing that happened unless the profile shows it, and say it as "
                     "what the applicant would like more of (scope, ownership, the kind of work this role offers).")
    return " ".join(lines)


def clean(text: str, limit: int, sources=()) -> str:
    """A draft as it may go on a form: one paragraph, no dashes, no sentence carrying a figure that
    none of ``sources`` (the profile, the posting, the question) holds, cut at a sentence end
    inside ``limit``. "" when nothing usable is left."""
    from tailoring.conform import strip_ai_dashes
    from tailoring.fact_gate import figures, source_figures
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    t = strip_ai_dashes(t).replace("—", ", ").replace("–", "-")
    known = source_figures(*sources) if sources else None
    kept = []
    for s in _SENT_END.split(t):
        s = s.strip()
        if not s:
            continue
        if known is not None and figures(s) - known:
            continue                                   # a figure nobody supplied: the sentence goes
        kept.append(s)
    out = ""
    for s in kept:
        if len(out) + len(s) + 1 > limit:
            break
        out = (out + " " + s).strip()
    if not out and kept:
        out = kept[0][:limit].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out if len(out) >= 20 else ""


def draft(jd_text: str, profile: dict, llm, question: str, *, limit: int | None = None) -> str:
    """One drafted answer for ``question`` through the Review page's drafter
    (drafting.screening_answers), with this module's guidance and cleaning. "" on any failure."""
    from drafting import screening_answers
    q = " ".join(str(question or "").split())
    if not q or llm is None:
        return ""
    n = limit or limit_for(q)
    asked = q + "\n(How to write this answer: " + guidance(q, n) + ")"
    try:
        got = screening_answers(jd_text or "", profile or {}, llm, [asked])
        text = str(((got.get("answers") or [{}])[0]).get("answer") or "")
    except Exception:          # noqa: BLE001 - no draft: the question stays the person's
        return ""
    return clean(text, n, sources=(profile or {}, jd_text or "", q))
