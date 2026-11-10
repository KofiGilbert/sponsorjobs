"""Home's "Today" strip, ranked by fit to the person before anything else.

The strip called itself "fresh best-fit roles" but ranked only on sponsorship, a US location and
newness, so a BI and analytics person (titles: AI Senior Business Intelligence Manager, Senior
Business Analyst) was shown MongoDB Site Reliability Engineer roles first (Kofi, 2026-11-10).

The ranking is cheap and runs on the slim feed rows (no description, no model): Home asks on every
visit, and reading descriptions for a thousand rows costs seconds (notify/alerts.py makes the same
trade with its title prescore before it reads any JD). Three signals, all from the saved profile:

* the role's TITLE against the person's own role titles: a specific word ("intelligence",
  "business") counts fully, a word every title has ("manager", "analyst") a quarter, a two-word
  phrase from their titles ("business analyst") a bonus, and their skills ("Power BI", "SQL") in
  the title a little (the words of notify.alerts.profile_targets);
* the role's FUNCTION (its feed tags, sourcing/jobtags.py) against the functions the person's own
  titles name, the most recent role weighing most;
* reachability (a known sponsor, a US location, new today) only as a tie-breaker.

No profile: no ranking, and the strip says what it is ("new sponsor roles"), never "best-fit".
"""
from __future__ import annotations

import re

# Words nearly every title carries: matching one says little about fit.
GENERIC = frozenset("""manager analyst associate assistant specialist coordinator engineer developer
director representative operations research product technician consultant officer administrator
executive team member head vp president intern staff lead principal senior sr jr junior ii iii iv
global group services service support general""".split())
POOL = 3000          # the freshest roles considered (the feed is newest first)
PER_COMPANY = 2      # at most this many from one employer in the strip's first screen


def _words(text: str) -> list[str]:
    from notify.alerts import _tokens
    return _tokens(text)


def _role_titles(profile: dict) -> list[str]:
    """The person's own role titles, most recent first, then any target titles. Not project
    names: a project called "Gate Check" says nothing about the jobs they want."""
    out = []
    for org in (profile or {}).get("experience") or []:
        for role in (org or {}).get("roles") or [org]:
            t = str((role or {}).get("title") or "").strip()
            if t:
                out.append(t)
    out += [str(t).strip() for t in (profile or {}).get("target_titles") or [] if str(t).strip()]
    return list(dict.fromkeys(out))


def fit_targets(profile: dict) -> dict | None:
    """What a role is ranked against, or None when the profile names no role titles."""
    from notify.alerts import profile_targets
    from sourcing.jobtags import job_tags
    titles = _role_titles(profile)
    if not titles:
        return None
    title_words: dict[str, float] = {}
    bigrams: set[tuple[str, str]] = set()
    areas: dict[str, float] = {}
    for i, t in enumerate(titles):
        ws = _words(t)
        for w in ws:
            # A two-letter word ("AI", "BI") is a modifier more than a job: half weight.
            weight = 0.25 if w in GENERIC else 0.5 if len(w) <= 2 else 1.0
            title_words[w] = max(title_words.get(w, 0.0), weight)
        bigrams.update(zip(ws, ws[1:]))
        recency = max(0.4, 1.0 - 0.2 * i)
        for k, area in enumerate(job_tags(t, "")[:2]):
            areas[area] = max(areas.get(area, 0.0), recency * (1.0 if k == 0 else 0.6))
    skills = {w for w in profile_targets(profile).get("skill_words") or set()
              if w not in GENERIC and w not in title_words and len(w) > 1}
    return {"title_words": title_words, "bigrams": bigrams, "skills": skills, "areas": areas,
            "titles": titles}


_US = re.compile(r"\b(USA?|United States|Remote|[A-Z]{2})\b")


def score(row: dict, t: dict) -> float:
    """0 to about 100: how well one feed row fits the person (see the module doc)."""
    ws = _words(row.get("title") or "")
    hit = 0.0
    for w in set(ws):
        if w in t["title_words"]:
            hit += t["title_words"][w]
        elif w in t["skills"]:
            hit += 0.6
    hit += 0.6 * sum(1 for b in zip(ws, ws[1:]) if b in t["bigrams"])
    title = min(1.0, hit / 1.6)
    tags = (row.get("tags") or [])[:3]
    area = max([t["areas"].get(x, 0.0) * (1.0 if k == 0 else 0.6) for k, x in enumerate(tags)] or [0.0])
    s = 60.0 * title + 30.0 * area
    if row.get("visa"):
        s += 3.0
    if _US.search(row.get("location") or ""):
        s += 1.5
    if row.get("is_new"):
        s += 1.0
    return round(s, 2)


def rank(rows: list[dict], targets: dict | None, pool: int = POOL) -> list[dict]:
    """The rows best fit first. The freshest ``pool`` are ranked; the rest keep their order after
    them. One employer fills at most PER_COMPANY of the first screen, and a role listed twice (same
    company and title) shows once there."""
    if not targets:
        return rows
    head, tail = list(rows[:pool]), list(rows[pool:])
    scored = sorted(enumerate(head), key=lambda p: (-score(p[1], targets), p[0]))
    first, later, per, seen = [], [], {}, set()
    for _, r in scored:
        co = str(r.get("company") or "").strip().lower()
        key = (co, str(r.get("title") or "").strip().lower())
        if len(first) < 30 and per.get(co, 0) < PER_COMPANY and key not in seen:
            first.append(r)
            per[co] = per.get(co, 0) + 1
            seen.add(key)
        else:
            later.append(r)
    return first + later + tail
