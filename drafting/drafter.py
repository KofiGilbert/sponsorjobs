"""Orchestrate cover-letter and screening-answer drafting from PROFILE + JD.

Kept deliberately thin: the LLM backend does the writing (grounded, anti-fabrication
prompts live in ``llm/anthropic_client.py``); here we assemble inputs, attach the
JD-supported-skills grounding for the review report, and shape the result. No fabrication
happens here — we only surface which real, JD-relevant skills the profile supports.
"""

from __future__ import annotations

import re

from tailoring.keywords import supported_skills


def cover_letter(jd_text: str, profile: dict, llm, role: str = "", company: str = "",
                 tone: str = "professional") -> dict:
    """Draft one cover letter. Returns the letter text plus the grounding report
    (which JD-relevant skills the profile genuinely supports) and a word count."""
    text = (llm.draft_cover_letter(jd_text, profile, role=role, company=company,
                                   tone=tone) or "").strip()
    from drafting.cover_review import review_cover_letter
    return {
        "cover_letter": text,
        "role": role,
        "company": company,
        "skills_used": supported_skills(jd_text, profile),
        "word_count": len(text.split()),
        # An honest craft check (hook, names the company, length, no unbacked claim) the UI shows
        # under the letter, so the person sends something sharp and true, not just plausible.
        "review": review_cover_letter(text, company=company, profile=profile, jd_text=jd_text),
    }


def referral_message(role: str, company: str, profile: dict, llm, jd_text: str = "",
                     recipient_name: str = "", relationship: str = "") -> dict:
    """Draft one referral-request message the applicant sends to a real person at the
    target company. Returns the message text, a word count, and the JD-relevant skills the
    profile genuinely supports (so the person can see what it is leaning on). The applicant
    finds the recipient and sends it themselves; nothing is scraped or auto-sent."""
    text = (llm.draft_referral_message(role, company, profile, jd_text=jd_text,
                                       recipient_name=recipient_name,
                                       relationship=relationship) or "").strip()
    return {
        "message": text,
        "role": role,
        "company": company,
        "skills_used": supported_skills(jd_text, profile) if jd_text else [],
        "word_count": len(text.split()),
    }


# Screening questions an ATS uses as an automatic reject, not as a conversation. A wrong
# or careless answer to one of these ends the application before a person reads it, so
# each is marked for the review to show as "answer this one yourself, carefully".
# Detection is by the question's wording only; nothing here decides the answer.
# (Knock-out list adapted from career-ops, MIT; see NOTICES.md.)
_KNOCKOUT = [
    ("work_authorization", re.compile(
        r"authoriz|legally (able|permitted|eligible|allowed) to work|right to work"
        r"|eligible to work|work permit", re.I)),
    ("sponsorship", re.compile(r"sponsor|\bvisa\b|immigration", re.I)),
    ("years_experience", re.compile(
        r"\b\d+\+?\s*(?:or more\s*)?(years|yrs)|years of experience|how many years", re.I)),
    ("degree", re.compile(r"\b(degree|bachelor|master|phd|doctorate|diploma|graduated)\b", re.I)),
    ("salary", re.compile(r"salary|compensation|pay (expectation|range|requirement)", re.I)),
    ("location", re.compile(
        r"relocat|commut|on-?site|located (in|within)|within \d+ miles|reside|time zone", re.I)),
    ("clearance", re.compile(r"security clearance|\bclearance\b", re.I)),
    ("certification", re.compile(r"\b(certified|certification|licensed|license)\b", re.I)),
]


def knockout_kind(question: str) -> str | None:
    """Which automatic-reject category a screening question falls into, if any."""
    q = str(question or "")
    for kind, rx in _KNOCKOUT:
        if rx.search(q):
            return kind
    return None


def screening_answers(jd_text: str, profile: dict, llm, questions) -> dict:
    """Answer free-text screening questions, one grounded answer per question.
    Blank/whitespace questions are dropped; the rest keep their order. Each answer
    carries ``knockout`` (a category or None) so the review can single out the
    questions a wrong answer would auto-reject on."""
    questions = [str(q).strip() for q in (questions or []) if str(q).strip()]
    answers = llm.answer_screening_questions(jd_text, profile, questions) if questions else []
    paired = [{"question": q, "answer": (a or "").strip(), "knockout": knockout_kind(q)}
              for q, a in zip(questions, answers)]
    return {"answers": paired, "grounded_skills": supported_skills(jd_text, profile),
            "knockouts": [p for p in paired if p["knockout"]]}
