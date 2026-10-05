"""Drafting: cover letters and screening-question answers from the person's PROFILE
and a target JD (CLAUDE.md §5, Phase 3).

Thin orchestrators over the LLM backend. They gather the JD terms the profile genuinely
supports (so a reviewer can see the letter/answers are grounded in real material, and
gaps are surfaced rather than papered over — CLAUDE.md §8), call the backend, and return
review-ready results. All content comes from the profile; the person reviews and owns it.
"""

from drafting.drafter import cover_letter, referral_message, screening_answers

__all__ = ["cover_letter", "referral_message", "screening_answers"]
