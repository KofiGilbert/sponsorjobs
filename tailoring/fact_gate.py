"""Numeric fact gate: figures a tailored CV states that the person's material never did.

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
    """One short line for the chat: which figures to confirm, and where."""
    if not flags:
        return ""
    bits = []
    for f in flags[:4]:
        bits.append(f"{', '.join(f['figures'])} ({f['label']})")
    more = f" and {len(flags) - 4} more" if len(flags) > 4 else ""
    return ("A few figures on this draft don't appear in your saved material, so please "
            "confirm or strike them before you send it: " + "; ".join(bits) + more + ".")
