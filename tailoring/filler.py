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


# --------------------------------------------------------------------------- #
# Tacked-on term lists (CLAUDE.md section 8: no keyword stuffing)
# --------------------------------------------------------------------------- #
# A rewrite that reaches a length by bolting tools onto the end ("..., using Variance Analysis
# and A/B Testing.", "..., leveraging SQL, Python and Tableau.") or by putting a keyword wall in
# front ("SQL, Python, Tableau: Built ...") reads as stuffing to a recruiter, even when every
# term is real. A term may come into a bullet only as part of what the sentence says happened.
_CONNECTOR = re.compile(
    r"(?:,\s*|\s+)(?:using|leveraging|utili[sz]ing|employing|applying|via|through|"
    r"with tools such as|with tools like|with|including|such as|across)\s+(?P<tail>[^.;]*)$",
    re.IGNORECASE)
_LEAD_WALL = re.compile(r"^\s*(?:[A-Z][\w+#./&-]*(?:\s+[\w+#./&-]+){0,2}\s*,\s*)+"
                        r"[A-Z][\w+#./&-]*(?:\s+[\w+#./&-]+){0,2}\s*:\s")
_TAIL_LIST = re.compile(r",\s*(?P<tail>(?:[^,.;]{1,30},\s*)+(?:and\s+)?[^,.;]{1,30})\.?\s*$")


def _terms(text: str) -> set[str]:
    from .keywords import vocab_skills
    from .fact_gate import claims
    out = {t.lower() for t in vocab_skills(text)}
    out |= {c.lower() for c in claims(text) if any(ch.isupper() for ch in c[1:]) or c.isupper()}
    return {t for t in out if len(t) > 1}


def tacked_on_terms(original: str, candidate: str) -> list[str]:
    """Terms a rewrite bolts on as a list, rather than saying what happened: a leading keyword
    wall, a trailing comma list, or a closing "using/leveraging/with ..." clause that names a
    tool or skill the original bullet did not. Empty means the rewrite is clean."""
    orig, cand = str(original or ""), str(candidate or "")
    new_terms = _terms(cand) - _terms(orig)
    if _LEAD_WALL.search(cand) and not _LEAD_WALL.search(orig):
        return sorted(new_terms) or ["keyword list"]
    if not new_terms:
        return []
    body = cand.rstrip().rstrip(".!")
    hits: set[str] = set()
    for pat in (_CONNECTOR, _TAIL_LIST):
        m = pat.search(body)
        if m and m.group("tail").strip() not in orig:
            tail = m.group("tail").lower()
            hits |= {t for t in new_terms if t in tail}
    if len(new_terms) >= 3:            # a fill adds a real detail, never a set of keywords
        hits |= new_terms
    return sorted(hits)
