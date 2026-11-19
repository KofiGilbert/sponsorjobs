"""The person's work history and education, shaped for an application form's repeating sections.

Workday's "Apply Manually" path (and other portals with "Work Experience 1, 2, ..." sections) asks
for each job's title, company, location and From / To month, and each school's name, degree and
field of study. The resume-parse path fills those from the uploaded resume; this builds the same
values from the person's saved profile (or the tailored resume's profile for this application), so
the fill engine (extension/autofill_core.js fillHistory) can put them in without asking the person
for what their own material already says (2026-11-16, Uline on Workday: six questions asked).

Values stay on the machine: they go to the page, never to the model.
"""

from __future__ import annotations

import re

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_NOW = re.compile(r"\b(present|current|now|today|ongoing)\b", re.I)
_DATE = re.compile(r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+|(?P<mm>\d{1,2})\s*[/.-]\s*)?(?P<y>(?:19|20)\d{2})")


def _point(text: str) -> dict | None:
    """'March 2026' / 'Sept 2024' / '03/2026' / '2024' -> {"m": 3, "y": 2026} (m 0 when unknown)."""
    m = _DATE.search(str(text or ""))
    if not m:
        return None
    mon = 0
    if m.group("mon"):
        mon = _MONTHS.get(m.group("mon").lower()[:3], 0)
    elif m.group("mm"):
        mon = int(m.group("mm")) if 1 <= int(m.group("mm")) <= 12 else 0
    return {"m": mon, "y": int(m.group("y"))}


def parse_span(text: str) -> dict:
    """'March 2026 - Present' -> {"start": {...}, "end": None, "current": True}."""
    s = str(text or "").strip()
    parts = re.split(r"\s+(?:-|–|—|to|until)\s+|\s*[–—]\s*", s, maxsplit=1)
    start = _point(parts[0]) if parts else None
    end_text = parts[1] if len(parts) > 1 else ""
    current = bool(_NOW.search(end_text)) or (not end_text and bool(_NOW.search(s)))
    end = None if current else (_point(end_text) if end_text else None)
    return {"start": start, "end": end, "current": current}


_LEVELS = [
    ("doctorate", re.compile(r"\b(ph\.?\s?d|doctor(ate)?|d\.?phil|ed\.?d)\b", re.I)),
    ("masters", re.compile(r"\b(master'?s?|m\.?\s?s\.?c?|m\.?\s?a\.?|mba|m\.?eng|mph|mfa|m\.?ed)\b", re.I)),
    ("bachelors", re.compile(r"\b(bachelor'?s?|b\.?\s?s\.?c?|b\.?\s?a\.?|b\.?eng|b\.?tech|undergraduate)\b", re.I)),
    ("associates", re.compile(r"\b(associate'?s?|a\.?\s?a\.?s?)\b", re.I)),
    ("high school", re.compile(r"\b(high school|secondary|ged)\b", re.I)),
]


def degree_level(degree: str) -> str:
    """The level a degree's name says ("Master of Science in ..." -> "masters"), else ""."""
    for level, rx in _LEVELS:
        if rx.search(str(degree or "")):
            return level
    return ""


def field_of_study(degree: str) -> str:
    """The subject a degree's name gives: 'Master of Science in Business Analytics' and
    'MBA - Business Analytics (STEM), Some School' -> 'Business Analytics' (else '')."""
    d = re.sub(r"\s+", " ", str(degree or "")).strip()
    m = re.search(r"\bin\s+([A-Z][^(),;]{2,80})", d)            # the last "in <Subject>"
    if m:
        for m in re.finditer(r"\bin\s+([A-Z][^(),;]{2,80})", d):
            pass
    else:
        m = re.search(r"^[^-–—]{2,40}\s[-–—]\s([A-Z][^(),;]{2,80})", d)   # "MBA - <Subject> (...)"
    if not m:
        return ""
    return m.group(1).strip(" ,.-")


def _bullets(role: dict) -> str:
    out = []
    for b in role.get("bullets") or []:
        t = b.get("text") if isinstance(b, dict) else b
        t = re.sub(r"\s+", " ", str(t or "")).strip()
        if t:
            out.append(t)
    return "\n".join(out)[:2000]


def form_history(profile: dict, max_jobs: int = 5, max_schools: int = 3) -> dict:
    """{"history": [...], "education": [...]} for the fill engine, newest first, from a profile
    shaped like the saved profile (experience[].org/location/roles[].title/dates/bullets,
    education[].school/degree/date/location). Empty lists when the profile has none."""
    history = []
    for org in (profile or {}).get("experience") or []:
        if not isinstance(org, dict):
            continue
        company = str(org.get("org") or org.get("company") or "").strip()
        for role in org.get("roles") or [org]:
            if not isinstance(role, dict):
                continue
            title = str(role.get("title") or "").strip()
            if not (title and company):
                continue
            span = parse_span(role.get("dates") or org.get("dates") or "")
            history.append({"title": title, "company": company,
                            "location": str(role.get("location") or org.get("location") or "").strip(),
                            "start": span["start"], "end": span["end"], "current": span["current"],
                            "description": _bullets(role)})
            if len(history) >= max_jobs:
                break
        if len(history) >= max_jobs:
            break
    education = []
    for ed in (profile or {}).get("education") or []:
        if not isinstance(ed, dict):
            continue
        school = str(ed.get("school") or ed.get("institution") or "").strip()
        if not school:
            continue
        degree = str(ed.get("degree") or "").strip()
        span = parse_span(ed.get("date") or ed.get("dates") or "")
        education.append({"school": school, "degree": degree, "level": degree_level(degree),
                          "field": str(ed.get("field") or "").strip() or field_of_study(degree),
                          "start": span["start"], "end": span["end"], "current": span["current"]})
        if len(education) >= max_schools:
            break
    return {"history": history, "education": education, "skills": form_skills(profile)}


def form_skills(profile: dict, limit: int = 12) -> list:
    """The person's own skill names, short ones first in their saved order (for a form's
    "Type to Add Skills" list: only a choice the list itself offers is ever added)."""
    raw = (profile or {}).get("skills") or {}
    groups = raw.values() if isinstance(raw, dict) else [raw]
    out = []
    for g in groups:
        items = g if isinstance(g, list) else re.split(r"[,;\n]", str(g or ""))
        for it in items:
            name = str((it.get("name") if isinstance(it, dict) else it) or "").strip(" .")
            if 1 < len(name) <= 40 and name.lower() not in {x.lower() for x in out}:
                out.append(name)
            if len(out) >= limit:
                return out
    return out


# (2026-11-19, Home Depot on Workday) A form's Languages rows: the person's own languages with the
# level they saved, never one they did not ("English (Native)", "Spanish - conversational",
# {"name": "French", "level": "Fluent"}).
_LEVEL = re.compile(r"\b(native|bilingual|mother tongue|first language|fluent|full professional|"
                    r"professional working|limited working|proficient|advanced|upper intermediate|"
                    r"intermediate|conversational|elementary|beginner|basic|working)\b", re.I)


def form_languages(profile: dict, limit: int = 4) -> list:
    """[{"name", "level"}] from the profile's ``languages`` (a list, a comma line, or {name,
    level} rows); "level" is "" when the profile names none. Empty when it has none."""
    raw = (profile or {}).get("languages") or []
    items = raw if isinstance(raw, list) else re.split(r"[,;\n]", str(raw))
    out = []
    for it in items:
        if isinstance(it, dict):
            name = str(it.get("name") or it.get("language") or "").strip()
            level = str(it.get("level") or it.get("proficiency") or "").strip()
        else:
            text = " ".join(str(it or "").split())
            m = re.match(r"^(.*?)\s*(?:\((.*?)\)|[-:\u2013\u2014]\s*(.+))?$", text)
            name, level = (m.group(1) or "").strip(), (m.group(2) or m.group(3) or "").strip()
            if not level:
                lm = _LEVEL.search(name)
                if lm:
                    level, name = lm.group(0), (name[:lm.start()] + name[lm.end():]).strip(" ,-")
        name = name.strip(" .,")
        if not name or len(name) > 40 or name.lower() in {x["name"].lower() for x in out}:
            continue
        out.append({"name": name, "level": level[:40]})
        if len(out) >= limit:
            break
    return out
