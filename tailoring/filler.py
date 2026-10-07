"""Padding detector for rewritten bullets.

A rewrite that has to reach a character target will, left to itself, reach it with hollow
phrases: "demonstrating the analytical rigor, requirements discipline, and stakeholder
communication essential to complex platform implementations and successful delivery outcomes".
That clause says nothing the recruiter can check, and recruiters read it as machine-written
(Kofi's first real run, 2026-10-07). This module is the deterministic guard: a rewrite that
introduces filler the original never had is rejected and the real bullet is kept.
"""
from __future__ import annotations

import re

# Phrases that carry no checkable content. Each is matched as a whole phrase, case-insensitive.
# A phrase already present in the ORIGINAL bullet is the person's own wording and is allowed.
FILLER_PHRASES: tuple[str, ...] = (
    "demonstrating", "showcasing", "underscoring", "highlighting", "exemplifying",
    "essential to", "essential for", "critical to", "critical for", "crucial to", "vital to",
    "instrumental in", "pivotal", "foundational", "foundation for",
    "ensuring", "enabling", "empowering", "driving", "fostering", "facilitating", "leveraging",
    "robust", "comprehensive", "seamless", "seamlessly", "holistic", "strategic alignment",
    "cutting-edge", "state-of-the-art", "world-class", "best-in-class", "industry-leading",
    "synerg", "stakeholder communication", "delivery outcomes", "successful outcomes",
    "business value", "operational excellence", "across the organization",
    "cross-functional alignment", "end-to-end ownership", "scalable solutions",
    "data-driven decision", "actionable insights", "thought leadership",
    "requirements discipline", "analytical rigor", "data-intensive environments",
)

_PATTERNS = tuple((p, re.compile(r"(?<![a-z])" + re.escape(p), re.IGNORECASE)) for p in FILLER_PHRASES)


def filler_added(original: str, candidate: str) -> list[str]:
    """Filler phrases present in ``candidate`` that ``original`` does not contain."""
    orig = str(original or "")
    cand = str(candidate or "")
    return [p for p, pat in _PATTERNS if pat.search(cand) and not pat.search(orig)]


def is_padded(original: str, candidate: str) -> bool:
    """True when a rewrite grew by adding hollow phrases. One filler phrase in a bullet that
    also grew is padding; a rewrite that did not grow may use one connective word (a shorter
    sentence that says "ensuring" is a style choice, not a filler clause), but two or more
    new filler phrases is padding at any length."""
    added = filler_added(original, candidate)
    if not added:
        return False
    grew = len(str(candidate or "")) > len(str(original or "")) * 1.1
    return grew or len(added) >= 2
