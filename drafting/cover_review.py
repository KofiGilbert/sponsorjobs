"""An HONEST craft check on a drafted cover letter -- the same idea as the resume pre-send review,
applied to the letter. Deterministic (no model): fast, free, testable. It catches the things that
make a cover letter weak or risky before the person sends it:

- a generic template opener ("I am writing to apply for...") instead of a real hook,
- forgetting to name the company (reads like a mass mailer),
- being too short or too long,
- and -- the load-bearing one -- claiming a JD skill the profile cannot back (the fabrication guard,
  shared in spirit with the resume reviewer).

Returns a verdict + a categorized checklist the UI renders under the letter.
"""
from __future__ import annotations

import re

from tailoring.keywords import profile_text, skill_terms, term_present

# Openers that signal a template, not a crafted letter. Matched near the start (after any salutation).
_CLICHE_OPENERS = (
    "i am writing to apply", "i am writing to express", "i am excited to apply",
    "i would like to apply", "i am applying for", "please accept this",
    "i am writing in regard", "i am writing regarding", "i am writing to you regarding",
    "i am reaching out to apply", "i am interested in applying", "i am writing to submit",
    "with great interest i am", "i am thrilled to apply",
)


def _body_start(text: str) -> str:
    """The letter's opening, past any 'Dear ...,' salutation, lowercased -- what a hook check reads."""
    t = (text or "").strip()
    # Drop a leading salutation line ("Dear Hiring Team," / "Hello,") so the hook check sees the
    # real first sentence, not the greeting.
    t = re.sub(r"^\s*(dear|hello|hi|greetings)\b[^\n]*\n+", "", t, flags=re.I)
    return t.lower()[:90]


def review_cover_letter(text: str, company: str = "", profile: dict | None = None,
                        jd_text: str = "") -> dict:
    """Return an honest craft review of a cover letter. ``jd_text`` (optional) sharpens the
    fabrication guard to only JD-relevant skills; without it that check is skipped (prose alone is
    too noisy to judge)."""
    text = (text or "").strip()
    words = text.split()
    wc = len(words)
    checks: list[dict] = []

    head = _body_start(text)
    cliche = any(c in head for c in _CLICHE_OPENERS)
    checks.append({
        "id": "hook",
        "level": "flag" if cliche else "pass",
        "label": ("Opens with a generic template line, lead with a specific hook instead"
                  if cliche else "Opens with a real hook, not a template line"),
        "items": [],
    })

    co = (company or "").strip()
    named = bool(co) and co.lower() in text.lower()
    if co:
        checks.append({
            "id": "personalized",
            "level": "pass" if named else "flag",
            "label": (f"Names {co}, it reads written for them" if named
                      else f"Never names {co}, it reads like a mass mailer"),
            "items": [],
        })

    good_len = 150 <= wc <= 350
    checks.append({
        "id": "length",
        "level": "pass" if good_len else "warn",
        "label": (f"Good length ({wc} words)" if good_len else
                  f"A little short ({wc} words), add a concrete example" if wc < 150 else
                  f"Runs long ({wc} words), tighten it toward 300"),
        "items": [],
    })

    # Fabrication guard: a JD skill the letter claims that the profile cannot back. Only run when we
    # have the JD (so we judge CLAIMED job skills, not every noun in the prose).
    unbacked: list[str] = []
    if jd_text:
        prof = profile_text(profile or {})
        jd_terms = {t.lower() for t in skill_terms(jd_text)}
        for t in skill_terms(text):
            # Only judge SINGLE-WORD skills (a named technology like "Kubernetes"): a concrete,
            # checkable claim. Multi-word descriptors ("data pipelines") legitimately vary in
            # wording between a JD and a profile, so flagging them would cry wolf. This keeps the
            # guard high-precision -- it fires on a real unbacked tool, not a phrasing difference.
            if " " not in t and t.lower() in jd_terms and not term_present(t, prof):
                unbacked.append(t)
        # de-dupe, preserve order
        seen: set[str] = set()
        unbacked = [t for t in unbacked if not (t.lower() in seen or seen.add(t.lower()))]
        checks.append({
            "id": "honesty",
            "level": "flag" if unbacked else "pass",
            "label": ("A skill the letter claims that your profile does not back" if unbacked
                      else "Every skill the letter claims is backed by your profile"),
            "items": unbacked[:8],
        })

    # Verdict, honesty first (an unbacked claim is what costs an interview), then craft.
    if unbacked:
        verdict = "check"
        headline = "One thing to fix: a claim your profile does not back."
    elif cliche or (co and not named):
        verdict = "review"
        headline = "Honest, but the craft could be sharper."
    elif not good_len:
        verdict = "review"
        headline = "Reads well, just tune the length."
    else:
        verdict = "ready"
        headline = "Sharp, personal, and honest. Ready to send."

    return {"verdict": verdict, "headline": headline, "checks": checks,
            "unbacked_count": len(unbacked)}
