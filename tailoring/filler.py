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


def tacked_on_terms(original: str, candidate: str, count: bool = True) -> list[str]:
    """Terms a rewrite bolts on as a list, rather than saying what happened: a leading keyword
    wall, a trailing comma list, or a closing "using/leveraging/with ..." clause that names a
    tool or skill the original bullet did not, and (with ``count``) three or more new terms at
    once. A bullet written new names its tools in the sentence, so it is checked without
    ``count``. Empty means the rewrite is clean."""
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
    if count and len(new_terms) >= 3:  # a fill adds a real detail, never a set of keywords
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
    if _longest_shared_run(ta, tb) >= SHARED_RUN and _content_overlap(ta, tb) >= SAME_FACT_OVERLAP:
        return True
    # The same figures tell the same fact however it is reworded: "Built an AI-assisted pipeline
    # that turns 280,000+ daily job postings into 145,000+ sponsorship-matched roles" was written
    # beside "...processed 280,000+ job postings a day ... producing 145,000+ sponsorship-matched
    # roles" on a real page (2026-11-10). Two shared distinctive figures, or one with a third of
    # the content words in common, is one fact.
    shared = _figures(a) & _figures(b)
    if shared and (_numbers(a) ^ _numbers(b)):
        # Each states a number the other does not: the shared figures are the setting, not the
        # fact. "Built a simulation of a 5,000-order, 250-driver operation" and "Ran it ... a
        # demand-shaped roster beat a flat one by about 5 points, matching about 10 drivers" are
        # two facts (Kofi's Google page lost the second as a repeat, 2026-11-10).
        return False
    if len(shared) >= 2:
        return True
    # One shared figure counts only when it is a large count (280,000, not 20% or 250), with
    # a third of the content words in common: two different 20% results are two facts.
    one = next(iter(shared), "")
    return bool(one and one.isdigit() and len(one) >= 4 and _content_overlap(ta, tb) >= 0.34)


_FIGURE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{3,}|\d+(?:\.\d+)?%)\+?")


def figures_of(text: str) -> set[str]:
    """The distinctive figures in a bullet (280,000+, 145,000+, 6.5%). A bullet that carries
    one is the strongest fact in its role: the fill and the page's cuts keep it first."""
    return _figures(text)


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Every number a bullet states, small ones too ("about 5 points", "10 drivers"); a year on
    its own is not one."""
    out = set()
    for m in _NUMBER.finditer(str(text or "")):
        f = m.group(0).replace(",", "").rstrip(".")
        if f and not re.fullmatch(r"(?:19|20)\d\d", f):
            out.add(f)
    return out


def has_figure(text: str) -> bool:
    """Does a bullet state a result in numbers? Any number but a year: "280,000+", "6.5%", and
    also "about 5 points of on-time delivery ... about 10 drivers" (Last-Mile's result bullet,
    which the page's cuts took because its numbers are small, Kofi's Google page 2026-11-10).
    The page's cuts keep such a bullet while one without a number can go instead."""
    return bool(_numbers(text))


def _figures(text: str) -> set[str]:
    """The distinctive figures in a bullet (280,000+, 145,000+, 6.5%): three digits or more, or
    a percentage; a year on its own is not one."""
    out = set()
    for m in _FIGURE.finditer(str(text or "")):
        f = m.group(1).replace(",", "")
        if re.fullmatch(r"(?:19|20)\d\d", f):
            continue
        out.add(f)
    return out


# A rewrite that BORROWS this many words in a row from a sibling (more than the bullet it
# replaces already shared with it) takes that sibling's clause. Seen on a real build
# (2026-11-06): a bullet split into "...track growth, usage, and unit costs." and "Turn the
# reporting results into roadmap and spend decisions." was then lengthened line by line until
# each part carried the other's clause ("...then turn those results into roadmap and spend
# decisions", "...results on growth, usage and unit costs into roadmap and spend decisions").
BORROWED_RUN = 5


def repeats_sibling(before: str, candidate: str, siblings) -> bool:
    """True when a rewrite restates a sibling bullet's clause that the bullet it replaces did
    not: a shared run of ``SHARED_RUN`` words, or a run of ``BORROWED_RUN`` words longer than
    the bullet it replaces shared with that sibling. The rewrite is then refused and the bullet
    keeps its wording."""
    cand, bef = _fact_tokens(candidate), _fact_tokens(before)
    for sib in siblings or ():
        sib = str(sib or "")
        if not sib.strip():
            continue
        if shares_a_clause(candidate, sib) and not shares_a_clause(before, sib):
            return True
        st = _fact_tokens(sib)
        run = _longest_shared_run(cand, st)
        if run >= BORROWED_RUN and run > _longest_shared_run(bef, st):
            return True
    return False


# A resize that adds this many content words a sibling already has (and the bullet did not)
# has taken that sibling's point, even in other connecting words (``borrows_from_sibling``).
BORROWED_WORDS = 3


def _content_stems(text: str) -> set[str]:
    return {t.rstrip("s") for t in _fact_tokens(text) if t not in _FACT_STOP}


def borrows_from_sibling(before: str, candidate: str, siblings) -> bool:
    """True when a resize ADDS ``BORROWED_WORDS`` or more content words that one sibling bullet
    already says ("...so company leaders can make better roadmap and spend decisions" next to
    "Turn the reporting results into roadmap and spend decisions", a real build, 2026-11-06).
    For the line fit, which lengthens a bullet from its entry's context: the added detail must
    be the bullet's own, never the next bullet's point."""
    added = _content_stems(candidate) - _content_stems(before)
    return any(len(added & _content_stems(str(sib or ""))) >= BORROWED_WORDS
               for sib in siblings or ())


def dedupe_bullets(bullets: list) -> list:
    """``bullets`` with any bullet that repeats an EARLIER one in the same entry dropped
    (``same_fact``). Order kept; the first wording of a fact wins."""
    out: list = []
    for b in bullets or []:
        if isinstance(b, str) and any(isinstance(k, str) and same_fact(b, k) for k in out):
            continue
        out.append(b)
    return out


# --------------------------------------------------------------------------- #
# A bullet the page fill adds must say something of its own (Kofi, 2026-11-10)
# --------------------------------------------------------------------------- #
# The staircase fill grew a role by splitting its bullets and writing more, and a real D365 build
# came out with thin parts that restate each other: "Led business intelligence across the
# company's products, owning the function that covers every product line and the data the
# company uses to track them." beside "Set the reporting direction for the product, growth, and
# finance teams...", and "Built an AI-assisted pipeline that processes 280,000+ daily job
# postings, turning a very large raw feed into a structured set of listings." Each new bullet
# must carry a fact (a tool, a number, an outcome) no other bullet of the role says, in words
# the person's material states, not words added to reach a length.

# Words that join a sentence without carrying a fact.
_GLUE = frozenset("that which who whom them they then there than such also each every all both "
                  "one more very what when where whose so".split())
# A new bullet needs this many content words (or one figure) that no other bullet of the role says.
DISTINCT_MIN = 2
# A new bullet whose content words are this share or more already on the role's other bullets
# restates them.
RESTATE_SHARE = 0.5
# ...unless it adds at least this many content words of its own (a bullet may carry the role's
# context, its tools and its platform).
RESTATE_FRESH = 4
# Words that carry no checkable content on their own: a line made only of these (and joining
# words) says nothing ("Drove strategic alignment and operational excellence across teams").
_BUZZ = frozenset("""
strategic strategy alignment aligned excellence operational value values impact impactful success
successful outcome outcomes synergy synergies stakeholder stakeholders initiative initiatives
effort efforts various key critical core best practice practices high quality results result
drive drove driving deliver delivered delivering support supported supporting ensure ensured
enable enabled foster fostered leverage leveraged team teams cross functional organization
organizational business overall growth culture vision mission goal goals objective objectives
""".split())
# A bullet needs at least this many content words beyond buzzwords to say something concrete.
CONCRETE_MIN = 3


def _stem(t: str) -> str:
    t = t.lower()
    if t.endswith("'s"):
        t = t[:-2]
    if t.endswith("ss"):
        return t
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def fact_stems(text: str, skip_lead: bool = False) -> list[str]:
    """The content words of ``text``, stemmed (``turns``, ``turned`` and ``turning`` are one),
    without joining words. ``skip_lead`` drops the opening verb, which a new bullet is free to
    choose."""
    toks = _fact_tokens(text)
    if skip_lead and toks:
        toks = toks[1:]
    return [_stem(t) for t in toks if t not in _FACT_STOP and t not in _GLUE]


def hollow(text: str) -> bool:
    """True for empty buzzword filler: a line with no concrete action or object, fewer than
    ``CONCRETE_MIN`` content words that are not buzzwords."""
    return len({t for t in fact_stems(text, skip_lead=True) if t not in _BUZZ}) < CONCRETE_MIN


def new_bullet_problems(new: str, siblings, material: str = "", restate: bool = True,
                        max_new: int | None = None) -> list[str]:
    """Why a bullet the fill ADDS to a role (a part of a split, or a bullet written for the
    role) may not go on the page; empty means it may.

    * "adds no fact of its own": no figure and fewer than ``DISTINCT_MIN`` content words that
      the role's other bullets (``siblings``) do not already say;
    * "restates the role's other bullets": ``RESTATE_SHARE`` or more of its content words are
      already on them (not checked with ``restate`` off: a part of a split may carry its
      source's context, the platform or the tools, so it reads on its own);
    * "empty filler": no concrete action or object (``hollow``).

    A bullet written like a professional resume writer would, from the role, the company, the
    person's skills and projects and the job's duties, is NOT refused for using words the
    person's material does not (Kofi, 2026-11-10). ``material`` and ``max_new`` are kept for
    callers and not used."""
    from .fact_gate import figures

    mine = set(fact_stems(new, skip_lead=True))
    if not mine:
        return ["empty"]
    sibs = [str(s) for s in siblings or () if str(s).strip()]
    said = {t for s in sibs for t in fact_stems(s)}
    out: list[str] = []
    new_figs = figures(new) - {f for s in sibs for f in figures(s)}
    fresh = mine - said
    if not new_figs and len(fresh) < DISTINCT_MIN:
        out.append("adds no fact of its own")
    elif (restate and sibs and len(mine & said) / len(mine) >= RESTATE_SHARE and not new_figs
          and len(fresh) < RESTATE_FRESH):
        out.append("restates the role's other bullets")
    if hollow(new):
        out.append("empty filler")
    return out


# --------------------------------------------------------------------------- #
# Padding that adds no fact (Kofi, 2026-11-10, a real D365 build)
# --------------------------------------------------------------------------- #
# Stock tails a model reaches a length with: "for stakeholders across the business" on three
# bullets of one role, "for BlackOrigin Inc.'s products", "at Stanbic Bank Ghana".
# "to stakeholders across the products" says who the work was for and stays; "for stakeholders"
# ending the sentence, or "... across the business/organization", says nothing.
_STOCK_TAIL = re.compile(
    r"\b(?:for|to|with)\s+(?:the\s+|its\s+|their\s+)?(?:key\s+)?stakeholders"
    r"(?:\s+across\s+the\s+(?:business|company|organi[sz]ation|bank)\b|\s*[.;]?\s*$)"
    r"|\bacross\s+the\s+(?:business|organi[sz]ation)\b", re.I)
_CORP = re.compile(r"\b(?:inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|group)\.?$", re.I)
# A run of this many words in a row, with at least two content words, said again on another
# bullet of the same role is a repeated phrase.
REPEAT_RUN = 4


def employer_names(org: str) -> list[str]:
    """How a bullet could name its own employer: the full name, the name without "Inc." and
    the like, and an acronym in brackets ("CSIR")."""
    org = str(org or "").strip()
    if not org:
        return []
    out = {org}
    core = re.sub(r"\s*\([^)]*\)\s*", " ", org).strip(" ,.")
    core = _CORP.sub("", core).strip(" ,.")
    if len(core) >= 3:
        out.add(core)
    out |= {m for m in re.findall(r"\(([A-Z][A-Za-z&]{1,9})\)", org)}
    return sorted(out, key=len, reverse=True)


def role_padding(new: str, siblings, employer: str = "", source: str = "",
                 repeats_from_source: bool = True, context: str = "",
                 allowed: str = "") -> list[str]:
    """Padding a bullet added to a role carries that states no fact: its own employer's name
    (the role's heading already says it), a stock tail ("for stakeholders across the
    business"), or a phrase another bullet of the role already says. Anything ``source`` (the
    person's own bullet it came from) already says is theirs and allowed. For a repeated
    phrase, the words of ``context`` (the role's title and company) do not count, and a phrase
    ``allowed`` holds (the role's own saved bullets) may recur."""
    new, source = str(new or ""), str(source or "")
    out: list[str] = []
    for name in employer_names(employer):
        if re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", new, re.I) and \
                not re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", source, re.I):
            out.append("names its own employer")
            break
    m = _STOCK_TAIL.search(new)
    if m and not _STOCK_TAIL.search(source):
        out.append("stock tail: " + m.group(0))
    mine = _fact_tokens(new)
    # A part of a split may not carry its source's phrase into two bullets: with
    # ``repeats_from_source`` off, the source does not excuse a repeat.
    src = (_fact_tokens(source) if repeats_from_source else []) + ["|"] + _fact_tokens(allowed)
    ctx = set(_fact_tokens(context))
    for sib in siblings or ():
        st = _fact_tokens(str(sib or ""))
        run = _shared_phrase(mine, st, src, ctx)
        if run:
            out.append("repeats a phrase of another bullet: " + " ".join(run))
            break
    return out


def _shared_phrase(a: list[str], b: list[str], allowed: list[str], ctx=frozenset()) -> list:
    """A run of ``REPEAT_RUN`` or more words in both ``a`` and ``b``, with two content words,
    that ``allowed`` does not hold (the run; empty when there is none)."""
    allowed_s = " " + " ".join(allowed) + " "
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k >= REPEAT_RUN:
                run = a[i:i + k]
                if sum(t not in _FACT_STOP and t not in ctx for t in run) >= 2 and \
                        (" " + " ".join(run[:REPEAT_RUN]) + " ") not in allowed_s:
                    return run
    return []
