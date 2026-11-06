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


# --------------------------------------------------------------------------- #
# One fact, one bullet (an entry never says the same thing twice)
# --------------------------------------------------------------------------- #
# A rewrite that has to reach a length will borrow from the entry's own sibling bullets (the
# grounding it is given), and the result can restate a sibling's whole opening: the Phoenix
# Contact resume (2026-11-05) showed "Last-Mile Operations Simulator" with two bullets that
# both began "Built/Used a Python discrete-event simulation of a 5,000-order-a-day,
# 250-driver delivery operation". Each bullet is one fact; a second copy of a fact wastes a
# line and reads as padding to a recruiter.
_FACT_TOKEN = re.compile(r"[a-z0-9$%+]+(?:[-,.'/][a-z0-9$%+]+)*")
_FACT_STOP = frozenset("a an and as at by for from in into of on or the to via with while "
                       "over under across its their his her our my this that these those "
                       "is was were be been it".split())
SHARED_RUN = 7          # this many words in a row, shared with a sibling, is the same fact
SAME_FACT_OVERLAP = 0.8  # or this share of the content words (near-identical sentences)


def _fact_tokens(text: str) -> list[str]:
    return _FACT_TOKEN.findall(str(text or "").lower())


def _longest_shared_run(a: list[str], b: list[str]) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            if x == y:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _content_overlap(a: list[str], b: list[str]) -> float:
    ca = {t for t in a if t not in _FACT_STOP}
    cb = {t for t in b if t not in _FACT_STOP}
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / min(len(ca), len(cb))


def shares_a_clause(a: str, b: str) -> bool:
    """True when two bullets share a run of ``SHARED_RUN`` words: one restates the other's
    clause ("a Python discrete-event simulation of a 5,000-order-a-day, 250-driver delivery
    operation"), whatever verb each opens with."""
    return _longest_shared_run(_fact_tokens(a), _fact_tokens(b)) >= SHARED_RUN


def same_fact(a: str, b: str) -> bool:
    """True when two bullets are near-identical: a shared clause AND most content words in
    common, or one is (almost) contained in the other. Strict on purpose: it decides whether
    a bullet of the person's own is dropped as a repeat."""
    ta, tb = _fact_tokens(a), _fact_tokens(b)
    if not ta or not tb:
        return False
    return (_longest_shared_run(ta, tb) >= SHARED_RUN
            and _content_overlap(ta, tb) >= SAME_FACT_OVERLAP)


def repeats_sibling(before: str, candidate: str, siblings) -> bool:
    """True when a rewrite restates a sibling bullet's clause that the bullet it replaces did
    not. The rewrite is then refused and the bullet keeps its wording."""
    for sib in siblings or ():
        sib = str(sib or "")
        if sib.strip() and shares_a_clause(candidate, sib) and not shares_a_clause(before, sib):
            return True
    return False


def dedupe_bullets(bullets: list) -> list:
    """``bullets`` with any bullet that repeats an EARLIER one in the same entry dropped
    (``same_fact``). Order kept; the first wording of a fact wins."""
    out: list = []
    for b in bullets or []:
        if isinstance(b, str) and any(isinstance(k, str) and same_fact(b, k) for k in out):
            continue
        out.append(b)
    return out
