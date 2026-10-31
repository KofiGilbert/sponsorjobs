"""Fact gate: figures and named claims a tailored resume states that the person's material never did.

Tailoring rewords aggressively, and the person owns the result (CLAUDE.md §8). What it
must never do silently is put a NUMBER on the page that the person did not supply: a
"40%" or "$2M" or "team of 12" that a model added to sound stronger is the one class of
edit a recruiter can check and a candidate cannot defend. Prose wording is a judgement
call; a figure is a fact.

So this is a deterministic pass, no model involved, that compares every figure in the
tailored bullets against every figure anywhere in the person's source material and
reports the ones with no origin. It FLAGS, it does not block: the review shows the person
exactly which figures to confirm or strike before anything is submitted.

Adapted from the fact-gate idea in career-ops (MIT, ``verify-cv-facts.mjs``), reduced to
the numeric core and rewritten for our profile shape. See NOTICES.md.

The CLAIM gate (``new_claims``) widens the same idea past numbers. A rewrite that names an
employer, a school, a tool, an acronym or a credential ("AWS Certified", "PhD", "PMP") the
person's own material never mentions is the same kind of checkable, indefensible fact as an
invented figure. Unlike figures, a claim is not left for the person to strike: the tailoring
pass reverts that bullet to the person's own wording (``gate_rewrite``), so an invented
credential is never published. Mirroring a JD tool the person's material does name passes.
"""

from __future__ import annotations

import re

# One figure: optional currency, digits with separators, optional decimals, optional
# magnitude/percent suffix, optional "+". "1,200", "$1.2M", "40%", "10+", "250-driver"
# all start with a match here.
_FIGURE = re.compile(
    r"(?<![A-Za-z0-9.])"
    r"(?:[$€£]\s?)?"
    r"\d[\d,]*(?:\.\d+)?"
    r"\s?(?:%|percent|k|K|M|MM|B|bn|m|x)?"
    r"\+?"
    r"(?![A-Za-z0-9.])"
)
_YEAR = re.compile(r"^(19|20)\d\d$")


def figures(text: str) -> set[str]:
    """The bare figures in ``text``: digits only, separators and suffixes stripped.

    Suffix and currency are dropped on purpose. "5+" against a source that says "5" is
    the same claim, and "$60M" against "60M" is too; keeping the decoration would flag
    honest rephrasings and bury the one invented number among them. Four-digit years
    are not figures: "2019 - 2021" is a date range, not a metric.
    """
    out: set[str] = set()
    for m in _FIGURE.finditer(str(text or "")):
        raw = re.sub(r"[^0-9.]", "", m.group(0))
        raw = raw.strip(".")
        if not raw or _YEAR.match(raw):
            continue
        out.add(raw)
    return out


def _strings(value) -> list[str]:
    """Every string anywhere inside a nested profile value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def source_figures(*sources) -> set[str]:
    """All figures the person's own material contains, across every section and any
    extra sources handed in (a transcript of what they typed, an uploaded CV's text)."""
    found: set[str] = set()
    for src in sources:
        for s in _strings(src):
            found |= figures(s)
    return found


def _bullets(profile: dict):
    """Yield (section, label, bullet) for every bullet in the tailorable sections."""
    for entry in profile.get("experience") or []:
        org = str(entry.get("org") or "")
        roles = entry["roles"] if isinstance(entry.get("roles"), list) else [entry]
        for role in roles:
            label = " · ".join(x for x in (org, str(role.get("title") or "")) if x)
            for b in role.get("bullets") or []:
                yield "experience", label, str(b)
    for entry in profile.get("projects") or []:
        label = str(entry.get("org") or entry.get("title") or "Project")
        for b in entry.get("bullets") or []:
            yield "projects", label, str(b)
    summary = profile.get("summary")
    if isinstance(summary, str) and summary.strip():
        yield "summary", "Summary", summary


def flag_new_figures(tailored: dict, *sources) -> list[dict]:
    """Figures in ``tailored`` bullets that appear in none of ``sources``.

    Returns one entry per offending bullet::

        {"section": "experience", "label": "Acme · Analyst",
         "bullet": "...", "figures": ["40", "12"]}

    An empty list means every number on the page traces back to something the person
    supplied. Order follows the page, so the review reads top to bottom.
    """
    known = source_figures(*sources)
    flagged: list[dict] = []
    for section, label, bullet in _bullets(tailored):
        new = sorted(figures(bullet) - known, key=lambda s: (len(s), s))
        if new:
            flagged.append({"section": section, "label": label,
                            "bullet": bullet, "figures": new})
    return flagged


def describe(flags: list[dict]) -> str:
    """One short line for the chat: which figures to confirm, and where. Entries the tailoring
    pass already reverted (``action == "reverted"``) are told as such: nothing to strike."""
    reverted = [f for f in flags or [] if f.get("action") == "reverted"]
    flags = [f for f in flags or [] if f.get("action") != "reverted" and f.get("figures")]
    head = describe_reverted(reverted)
    if not flags:
        return head
    return (head + " " if head else "") + _describe_figures(flags)


def _describe_figures(flags: list[dict]) -> str:
    bits = []
    for f in flags[:4]:
        bits.append(f"{', '.join(f['figures'])} ({f['label']})")
    more = f" and {len(flags) - 4} more" if len(flags) > 4 else ""
    return ("A few figures on this draft don't appear in your saved material, so please "
            "confirm or strike them before you send it: " + "; ".join(bits) + more + ".")


# --------------------------------------------------------------------------- #
# Named claims: proper nouns, acronyms, credentials (the wider gate).
# --------------------------------------------------------------------------- #

# Words that make a phrase a credential claim. A rewrite that says "certified", "licensed" or a
# degree the person never stated is claiming a qualification, whatever it is attached to.
_CRED_WORDS = (r"certified|certification|certifications|certificate|certificates|licensed|license|"
               r"licenses|licensure|accredited|accreditation|chartered|board-certified|credentialed|"
               r"doctorate|doctoral|ph\.?\s?d\.?|master's|masters|bachelor's|bachelors|mba")
_CRED_RE = re.compile(r"(?<![A-Za-z])(?:" + _CRED_WORDS + r")(?![A-Za-z])", re.I)

# Credentials that are usually written as a bare acronym. Any acronym is checked anyway; these
# also count as credential claims, so "licensed CPA" needs a CPA in the person's material.
CREDENTIAL_ACRONYMS = frozenset("""
PHD MBA CPA PMP CFA CFP CMA CIA CISA CISM CISSP CCNA CCNP CCIE OSCP CEH CSM CSPO SAFE PMI-ACP
ITIL LEED PE EIT FE RN BSN MSN NP APRN LPN CNA EMT MD DO JD LLM LLB DDS DMD PHARMD PSYD EDD
MPH MSW LCSW LPC PA-C SHRM-CP SHRM-SCP PHR SPHR ACCA CIMA FRM CAIA EA CDL CPCU CLU ASE
""".split())

# A token that starts with a capital or is an acronym / CamelCase tool name ("AWS", "PyTorch",
# "Node.js", "C++", "SHRM-CP"). Digits alone are figures, handled above.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.+#&'-][A-Za-z0-9+#]+)*[+#]*")

# Capitalised words that are not names: they read capitalised mid-sentence in ordinary resume
# prose (months, days, pronoun I). Everything else capitalised mid-sentence is treated as a
# name, and is fine as long as the person's own material says it somewhere.
_NOT_NAMES = frozenset("""
i january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday
saturday sunday q1 q2 q3 q4
""".split())
_SMALL = frozenset("a an and as at by for from in into of on or the to via with".split())


def _words(text: str) -> set[str]:
    """Lowercased word tokens of ``text``, plus each token's de-pluralised form."""
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(str(text or "")):
        t = m.group(0).lower().strip(".'")
        if not t:
            continue
        out.add(t)
        for part in re.split(r"[.'&-]", t):
            if part:
                out.add(part)
        if len(t) > 3 and t.endswith("s"):
            out.add(t[:-1])
    return out


def _is_acronym(tok: str) -> bool:
    """AWS, PMP, PhD, GraphQL, PyTorch, SHRM-CP: two or more capitals in the token."""
    core = tok.rstrip("+#")
    return sum(1 for ch in core if ch.isupper()) >= 2 and any(ch.isalpha() for ch in core)


def _sentence_starts(text: str) -> set[int]:
    """Character offsets where a sentence (or a clause after a colon) begins."""
    starts = {0}
    for m in re.finditer(r"[.!?:;]\s+|\(\s*|^\s+", text):
        starts.add(m.end())
    return starts


def _credential_spans(text: str) -> list[tuple[int, int, str]]:
    """Each credential mention with the names around it, as (start, end, phrase): "AWS
    Certified Solutions Architect", "certified in Kubernetes", "licensed CPA", "PhD in Physics"."""
    text = str(text or "")
    toks = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(toks):
        tok = toks[i][2]
        is_word = bool(_CRED_RE.fullmatch(tok)) or tok.lower() in ("ph.d", "ph.d.")
        is_acr = tok.upper().strip(".") in CREDENTIAL_ACRONYMS and _is_acronym(tok)
        if not (is_word or is_acr):
            i += 1
            continue
        lo = i
        while (lo > 0 and i - lo < 3 and (out == [] or toks[lo - 1][0] >= out[-1][1])
               and (toks[lo - 1][2][:1].isupper() or _is_acronym(toks[lo - 1][2]))):
            lo -= 1
        hi = i
        j = i + 1
        if j < len(toks) and toks[j][2].lower() in ("in", "as", "for", "of"):
            j += 1
        while j < len(toks) and j - i <= 4 and (toks[j][2][:1].isupper() or _is_acronym(toks[j][2])
                                                or _CRED_RE.fullmatch(toks[j][2])):
            hi = j
            j += 1
        out.append((toks[lo][0], toks[hi][1], " ".join(t[2] for t in toks[lo:hi + 1])))
        i = hi + 1
    return out


def _credential_phrases(text: str) -> list[str]:
    return [p for _s, _e, p in _credential_spans(text)]


# Degree words name the credential itself, so they stay in a claim's core, folded to one
# token per level: "PhD", "Ph.D." and "Doctor of Philosophy" are the same claim, and the profile
# stores degrees by their full names (tailoring/conform.py) while a rewrite may abbreviate them.
_DEGREE_LEVEL = {
    "phd": "@doctorate", "doctorate": "@doctorate", "doctoral": "@doctorate", "doctor": "@doctorate",
    "mba": "@mba", "master": "@master", "masters": "@master", "master's": "@master", "ms": "@master",
    "msc": "@master", "ma": "@master", "meng": "@master",
    "bachelor": "@bachelor", "bachelors": "@bachelor", "bachelor's": "@bachelor", "bs": "@bachelor",
    "bsc": "@bachelor", "ba": "@bachelor", "beng": "@bachelor",
}


def _cred_core(phrase: str) -> set[str]:
    """The names a credential phrase is about, without the generic credential words ("certified",
    "license"). A degree keeps its level ("@doctorate"), since the degree is the claim."""
    text = re.sub(r"ph\.?\s?d\.?", "phd", str(phrase or ""), flags=re.I)
    if re.search(r"master of business administration", text, re.I):
        text += " mba"
    core: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        for part in re.split(r"[.&-]", m.group(0).lower()):
            part = part.strip("'")
            if not part or part in _SMALL or part == "philosophy":
                continue
            if part in _DEGREE_LEVEL:
                core.add(_DEGREE_LEVEL[part])
            elif not _CRED_RE.fullmatch(part):
                core.add(part[:-1] if len(part) > 3 and part.endswith("s") and part[:-1] else part)
    return core


def claims(text: str) -> list[str]:
    """The checkable named claims in ``text``, in order: credential phrases, acronyms and tool
    names, and capitalised names that are not the first word of a sentence."""
    text = str(text or "")
    spans = _credential_spans(text)
    out: list[str] = [p for _s, _e, p in spans]
    starts = _sentence_starts(text)
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if any(a <= m.start() < b for a, b, _p in spans):
            continue                      # already part of a credential phrase
        if _is_acronym(tok):
            out.append(tok)
        elif tok[:1].isupper() and m.start() not in starts and tok.lower() not in _NOT_NAMES:
            out.append(tok)
    return list(dict.fromkeys(out))


def known_material(*sources) -> dict:
    """What the person's own material lets a rewrite name: its words, and its credentials."""
    texts = [s for src in sources for s in _strings(src)]
    # Certifications and degrees the person listed are credentials even when written bare
    # ("AWS Solutions Architect", "BS Computer Science"): each entry counts as one.
    creds = [_cred_core(p) for t in texts for p in _credential_phrases(t)]
    for src in sources:
        if not isinstance(src, dict):
            continue
        certs = src.get("certifications") or []
        if isinstance(certs, str):
            certs = re.split(r"[;\n]", certs)
        for c in certs:
            core = _cred_core(" ".join(_strings(c)))
            if core:
                creds.append(core)
        for ed in src.get("education") or []:
            if isinstance(ed, dict):
                core = _cred_core(" ".join(str(ed.get(k) or "") for k in ("degree", "school")))
                if core:
                    creds.append(core)
    return {"words": set().union(*(_words(t) for t in texts)) if texts else set(),
            "creds": creds}


def new_claims(new: str, *sources, known: dict | None = None) -> list[str]:
    """Claims in ``new`` that none of ``sources`` (profile, brain facts, the source bullet)
    supports. A credential is supported only when the person's material states a credential
    about the same names ("AWS Certified" needs an AWS credential, not just AWS as a skill)."""
    k = known if known is not None else known_material(*sources)
    words, creds = k["words"], k["creds"]
    out: list[str] = []
    for c in claims(new):
        if _CRED_RE.search(c) or any(t.upper().strip(".") in CREDENTIAL_ACRONYMS
                                     for t in c.split() if _is_acronym(t)):
            core = _cred_core(c)
            if not creds or not any(core <= have for have in creds):
                out.append(c)
            continue
        toks = _words(c)
        main = c.lower().strip(".'")
        if main in words or (main.endswith("s") and main[:-1] in words):
            continue
        if toks and toks <= words:
            continue
        out.append(c)
    return out


def gate_rewrite(original: str, new: str, *sources, known: dict | None = None,
                 known_figures: set | None = None) -> list[str]:
    """Everything ``new`` states that the person's material does not: new figures and new named
    claims. Empty means the rewrite may be published; otherwise the caller keeps ``original``.
    ``original`` always counts as the person's material."""
    if known is None:
        known = known_material(original, *sources)
    else:
        extra = known_material(original)
        known = {"words": known["words"] | extra["words"], "creds": known["creds"] + extra["creds"]}
    figs = known_figures if known_figures is not None else source_figures(*sources)
    figs = figs | figures(original)
    bad = sorted(figures(new) - figs, key=lambda s: (len(s), s))
    return bad + new_claims(new, known=known)


def describe_reverted(reverted: list[dict]) -> str:
    """One short line for the chat when the gate kept the person's own wording."""
    if not reverted:
        return ""
    bits = []
    for f in reverted[:3]:
        bits.append(f"{', '.join(f['claims'][:3])} ({f['label']})")
    more = f" and {len(reverted) - 3} more" if len(reverted) > 3 else ""
    n = len(reverted)
    lead = ("I kept your own wording on one bullet" if n == 1
            else f"I kept your own wording on {n} bullets")
    return (lead + ", because the rewrite named something your saved material doesn't show: "
            + "; ".join(bits) + more + ".")
