"""A resume's FACTS are the person's own words and never the tailor's.

Tailoring rewords bullets. It never touches a job title, an employer, a role's dates or
location, a school, a degree, or a project's name: those state facts about the person, and a
page that says "AI Senior Data Manager" for a saved "Senior AI Data Manager" is a page that is
wrong about them (seen on the owner's real resume, 2026-11-24). notify/cvdoc.py already asks
for an explicit confirm before a chat edit changes one of these; the automatic build is at least
as strict.

``restore_facts(page, anchor)`` is the guard: every fact on ``page`` that differs from the
person's value for the same entry in ``anchor`` is put back, in place, and each fix is logged and
returned. The anchor is the person's saved material plus their own explicit edits in this chat
(``apply_edits``); the caller decides what that is (ui/session.py ``_fact_anchor``).

Entries are matched, not compared by position: the same employer and title, the same employer
and the same words in another order, the same employer and dates, or the same title and dates. A value the anchor leaves empty is not protected (a date the
brain filled in, a location added later); only what the person gave is.
"""

from __future__ import annotations

import copy
import logging
import re

_log = logging.getLogger(__name__)

# The fact fields of each section. Bullets, tools, links and skills are not facts here.
FIELDS = {
    "experience": ("title", "org", "dates", "location"),
    "education": ("school", "degree", "date", "location"),
    "projects": ("org", "name", "title"),
}


def _n(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _w(s) -> tuple:
    return tuple(sorted(_n(s).split()))


def _ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


class _Unit:
    """One entry's facts: an experience role (with its employer), a school, or a project."""

    __slots__ = ("section", "entry", "role")

    def __init__(self, section: str, entry: dict, role: dict | None = None):
        self.section, self.entry, self.role = section, entry, role if role is not None else entry

    def _home(self, f: str) -> tuple[dict, str]:
        """(the dict that holds field ``f``, its key)."""
        e, r = self.entry, self.role
        if self.section != "experience":
            return e, f
        if f == "org":
            return e, ("company" if not e.get("org") and e.get("company") else "org")
        if f == "location":
            return (r, f) if (r is not e and r.get("location") and not e.get("location")) else (e, f)
        if f == "dates":
            return (e, f) if (r is not e and not r.get("dates") and e.get("dates")) else (r, f)
        return r, f

    def get(self, f: str) -> str:
        d, k = self._home(f)
        return _ws(d.get(k))

    def set(self, f: str, value: str) -> None:
        d, k = self._home(f)
        d[k] = value
        if f == "title" and self.section in ("experience", "projects"):
            # A restored or confirmed title is the person's own: no suggestion is pending.
            if d.get("title_suggested"):
                d["title_suggested"] = False

    def facts(self) -> dict:
        return {f: self.get(f) for f in FIELDS[self.section]}

    def name(self) -> str:
        """A project's printed name (renderer: ``org``, else ``name``, else ``title``)."""
        return self.get("org") or self.get("name") or self.get("title")


def units(profile: dict, section: str) -> list[_Unit]:
    """Every entry's facts in ``section``, both profile shapes (nested roles and flat)."""
    out: list[_Unit] = []
    for e in (profile or {}).get(section) or []:
        if not isinstance(e, dict):
            continue
        if section == "experience":
            roles = e.get("roles") if isinstance(e.get("roles"), list) and e.get("roles") else [e]
            for r in roles:
                if isinstance(r, dict):
                    out.append(_Unit(section, e, r))
        else:
            out.append(_Unit(section, e))
    return out


def _tiers(section: str) -> list:
    """Match keys, strongest first; a key with an empty part never matches."""
    if section == "experience":
        return [lambda u: (_n(u.get("org")), _n(u.get("title")), _n(u.get("dates"))),
                lambda u: (_n(u.get("org")), _n(u.get("title"))),
                lambda u: (_n(u.get("org")), _w(u.get("title"))),
                lambda u: (_n(u.get("org")), _n(u.get("dates"))),
                lambda u: (_n(u.get("title")), _n(u.get("dates"))),
                lambda u: (_w(u.get("title")), _n(u.get("dates")))]
    if section == "education":
        return [lambda u: (_n(u.get("school")),),
                lambda u: (_w(u.get("school")),),
                lambda u: (_n(u.get("degree")),),
                lambda u: (_w(u.get("degree")),)]
    return [lambda u: (_n(u.name()),),
            lambda u: (_w(u.name()),),
            lambda u: (_n(u.entry.get("link") or u.entry.get("url") or u.entry.get("repo")),)]


def match(page: list, anchor: list, section: str) -> list[tuple]:
    """Pairs (page unit, anchor unit), each side used once, strongest key first."""
    pairs: list[tuple] = []
    free_p, free_a = list(range(len(page))), list(range(len(anchor)))
    for key in _tiers(section):
        by: dict = {}
        for i in free_a:
            k = key(anchor[i])
            if all(k):
                by.setdefault(k, []).append(i)
        for j in list(free_p):
            k = key(page[j])
            if not all(k) or not by.get(k):
                continue
            i = by[k].pop(0)
            free_a.remove(i)
            free_p.remove(j)
            pairs.append((page[j], anchor[i]))
    # Last, a near-identical label ("Tidewatch AI Dashboard" for "Tidewatch Dashboard"): the one
    # clearly closest entry, never a guess between two.
    from difflib import SequenceMatcher
    for j in list(free_p):
        lp = _label(page[j])
        scored = sorted(((SequenceMatcher(None, lp, _label(anchor[i])).ratio(), i) for i in free_a
                         if lp and _label(anchor[i])), reverse=True)
        if scored and scored[0][0] >= FUZZY and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.1):
            i = scored[0][1]
            free_a.remove(i)
            free_p.remove(j)
            pairs.append((page[j], anchor[i]))
    return pairs


FUZZY = 0.8


def _label(u: _Unit) -> str:
    if u.section == "experience":
        return _n(f"{u.get('org')} {u.get('title')}")
    if u.section == "education":
        return _n(f"{u.get('school')} {u.get('degree')}")
    return _n(u.name())


def _fmt(section: str, f: str, v: str) -> str:
    """The template's own formatting of a fact, so a format difference is never a change."""
    from . import conform as C
    try:
        if f in ("dates", "date"):
            return _ws(C.format_dates(v)) or _ws(v)
        if f == "title":
            return _ws(C.clean_title(v))
        if f == "location":
            return _ws(C.format_location(v))
        if f in ("school",) or (f == "org" and section != "projects"):
            return _ws(C.format_school(v))
        if f == "degree":
            return _ws(C.format_degree(v))
    except Exception:          # noqa: BLE001 - compare the plain text then
        pass
    return _ws(v)


def _same(u: _Unit, f: str, got: str, want: str) -> bool:
    if _fmt(u.section, f, got) == _fmt(u.section, f, want):
        return True
    if f in ("dates", "date"):
        from .conform import date_lacks_month, dummy_months
        holder = u.role if u.section == "experience" else u.entry
        # A year-only date shown with placeholder months (flagged red for the person to fix) is
        # the same fact, as is any date the page marks as a placeholder.
        if holder.get("dates_placeholder") or u.entry.get("dates_placeholder"):
            return True
        try:
            if date_lacks_month(want) and _fmt(u.section, f, dummy_months(want)) == _fmt(u.section, f, got):
                return True
        except Exception:      # noqa: BLE001
            pass
    return False


def restore_facts(page: dict, anchor: dict | None, *, where: str = "") -> list[dict]:
    """Put back, in ``page`` (in place), every fact that differs from the person's value for the
    same entry in ``anchor``. Returns one record per fix: {section, field, entry, page, saved}."""
    if not isinstance(page, dict) or not isinstance(anchor, dict):
        return []
    fixes: list[dict] = []
    for section, fields in FIELDS.items():
        for p, a in match(units(page, section), units(anchor, section), section):
            for f in fields:
                want, got = a.get(f), p.get(f)
                if not want or _same(p, f, got, want):
                    continue
                p.set(f, want)
                fixes.append({"section": section, "field": f,
                              "entry": a.get("org") or a.get("school") or a.name(),
                              "page": got, "saved": want})
    for fx in fixes:
        _log.warning("resume fact restored%s: %s %s %r -> %r (the person's own value)",
                     f" ({where})" if where else "", fx["section"], fx["field"],
                     fx["page"], fx["saved"])
    return fixes


def snapshot(profile: dict) -> dict:
    """The fact fields of every entry, positionally ({section: [facts, ...]}), for ``diff``."""
    return {s: [u.facts() for u in units(profile, s)] for s in FIELDS}


def diff(before: dict, after: dict) -> list[dict]:
    """The facts an in-place edit changed, as edit records for ``apply_edits``: the entry's facts
    before, the field and its new value. ``before`` and ``after`` are ``snapshot``s of the same
    structure (an edit that changed only values); a different shape records nothing."""
    out: list[dict] = []
    for s in FIELDS:
        b, a = before.get(s) or [], after.get(s) or []
        if len(b) != len(a):
            continue
        for fb, fa in zip(b, a):
            for f in FIELDS[s]:
                if fb.get(f, "") != fa.get(f, ""):
                    out.append({"section": s, "before": dict(fb), "field": f, "value": fa.get(f, "")})
    return out


def apply_edits(anchor: dict, edits: list) -> dict:
    """``anchor`` with the person's explicit edits laid over it (in place; also returned). An edit
    whose entry the anchor does not hold adds nothing: a new entry has no saved value to guard."""
    for ed in edits or []:
        s = ed.get("section")
        if s not in FIELDS or not isinstance(ed.get("before"), dict):
            continue
        probe = _Unit(s, {k: v for k, v in ed["before"].items()})
        hit = match([probe], units(anchor, s), s)
        if hit:
            hit[0][1].set(ed["field"], str(ed.get("value") or ""))
    return anchor


def add_missing(anchor: dict, more: dict) -> dict:
    """Entries of ``more`` (the facts this chat knows, e.g. its essentials) that ``anchor`` does
    not hold, appended to it, so a new entry the person gave in this chat is guarded too."""
    for s in FIELDS:
        mine = units(more, s)
        have = units(anchor, s)
        matched = {id(p) for p, _a in match(mine, have, s)}
        for u in mine:
            if id(u) in matched:
                continue
            row = {k: v for k, v in u.facts().items() if v}
            if row:
                anchor.setdefault(s, []).append(copy.deepcopy(row))
    return anchor


def changes(before: dict, after: dict) -> list[dict]:
    """The facts ``after`` states differently from ``before`` for the same entry (entries are
    matched, so a reshaped list is fine): {section, field, old, new, before, unit}. A fact
    ``before`` left empty is not a change (filling it in adds the person's own answer)."""
    out: list[dict] = []
    for s, fields in FIELDS.items():
        for a, b in match(units(after, s), units(before, s), s):
            for f in fields:
                old, new = b.get(f), a.get(f)
                if old and not _same(a, f, new, old):
                    out.append({"section": s, "field": f, "old": old, "new": new,
                                "before": b.facts(), "unit": a})
    return out


def typed(value: str, message: str) -> bool:
    """Did the person type ``value`` in ``message`` (case, spacing and punctuation aside)? A
    model that re-emits a title in another word order never passes this; "change my title to
    Lead Analyst" does."""
    v, m = _n(value), _n(message)
    return bool(v) and f" {v} " in f" {m} "
