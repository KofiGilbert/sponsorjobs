"""What Auto-apply learns from the roles the person skips, and their company lists.

Competitor pattern (AIApply's "decline with a reason", plus exclude / target company lists): when
the person skips a role they may say why in one tap. A few plain rules come out of those reasons,
each one visible on the Auto-apply page as a line in "What I've learned" and each one undoable:

* "Wrong role" twice on similar titles  -> fewer roles like that title (moved to the back)
* "Too senior" / "Too junior"            -> fewer roles at that level and above / below
* "Location"                             -> fewer roles in that place
* "Pay too low" on a role with known pay -> a pay floor: roles whose known pay is at or under it
                                            are left out (unknown pay is never guessed)
* "No sponsorship"                       -> that employer is left out while you need sponsorship
* "Company"                              -> the page asks once "Skip all roles at X?"; yes adds X
                                            to "Never apply to"

Down-weighting only moves a role to the back of the run's order, so the picker scores it last
(and usually not at all). Nothing is hidden on Jobs except what the person asked for. Undoing a
rule clears the reason on the skips that made it, so the rules are always re-derived from the
skips and nothing drifts.

Pure: the state is a plain dict (the app keeps it in a local JSON file), so this runs offline.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache

from sourcing.filters import salary_value
from sourcing.sponsors import normalize_employer

REASONS = ("wrong_role", "too_senior", "too_junior", "location", "pay_low", "company",
           "no_sponsorship", "other")
MAX_SKIPS = 500
MAX_COMPANIES = 200
ROLE_REPEAT = 2           # "Wrong role" on this many similar titles before it becomes a rule
ROLE_SIMILAR = 0.6        # Jaccard over title words (seniority words removed)
CLOSED_STANCES = ("not_offered", "restricted")   # the ad says no, or citizenship / clearance

_WORD = re.compile(r"[a-z0-9+#]+")
# Seniority words, kept out of a title's "kind of role" so "Senior Data Analyst" and "Data
# Analyst" read as the same kind of role.
_LEVEL_WORDS = frozenset("senior sr junior jr lead principal staff intern internship entry level "
                         "associate i ii iii iv new grad graduate mid head of chief".split())

# Levels, low to high. A title with no level word is mid-level.
LEVELS = ("intern", "entry", "mid", "senior", "lead", "manager")
_LEVEL_RE = [
    (5, re.compile(r"\b(manager|director|head\s+of|vp|vice\s+president|chief)\b", re.I)),
    (4, re.compile(r"\b(staff|principal|lead|distinguished|architect)\b", re.I)),
    (3, re.compile(r"\b(senior|sr\.?|iii|iv)\b", re.I)),
    (0, re.compile(r"\b(intern|internship|co-?op)\b", re.I)),
    (1, re.compile(r"\b(junior|jr\.?|entry[\s-]*level|new\s*grad(uate)?|associate|"
                   r"early\s*career|graduate|trainee|apprentice|i)\b", re.I)),
]


def level_of(title: str) -> int:
    """0 intern .. 5 manager, from the title alone. No level word reads as mid (2)."""
    t = title or ""
    for lvl, rx in _LEVEL_RE:
        if rx.search(t):
            return lvl
    return 2


def role_words(title: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall((title or "").lower())
                     if w not in _LEVEL_WORDS and len(w) > 1)


def _similar(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= ROLE_SIMILAR


@lru_cache(maxsize=65536)
def company_key(name: str) -> str:
    """Cached: a run walks tens of thousands of rows but only a few thousand employers."""
    return normalize_employer(name or "") or (name or "").strip().lower()


@lru_cache(maxsize=65536)
def _clearance_heavy(company: str) -> bool:
    from sourcing.quality import employer_is_clearance_heavy
    return employer_is_clearance_heavy(company)


def place_key(location: str) -> str:
    """"Austin, TX, United States" -> "austin tx"; anything mentioning remote -> "remote"."""
    s = (location or "").lower()
    if "remote" in s:
        return "remote"
    s = re.sub(r",?\s*(united states( of america)?|usa|us)\s*$", "", s.strip())
    return " ".join(_WORD.findall(s))


def _is_remote(job: dict) -> bool:
    return (str(job.get("remote") or "").lower() == "remote"
            or "remote" in (job.get("location") or "").lower())


# --------------------------------------------------------------------------- state
def empty_state() -> dict:
    return {"skips": [], "exclude": [], "prioritize": []}


def clean_state(raw) -> dict:
    st = empty_state()
    if not isinstance(raw, dict):
        return st
    for k in ("skips", "exclude", "prioritize"):
        v = raw.get(k)
        if isinstance(v, list):
            st[k] = [x for x in v if isinstance(x, dict)]
    return st


def record_skip(state: dict, job: dict, reason: str = "", note: str = "",
                now: float | None = None) -> dict:
    """Add (or update) one skip. ``job`` carries key (a source_id or "rec:<id>"), title,
    company, location and salary. A second call for the same key replaces its reason, so the
    reason can be given a moment after the one-click skip."""
    reason = reason if reason in REASONS else ""
    key = str(job.get("key") or job.get("source_id") or "").strip()[:300]
    entry = {"key": key, "title": str(job.get("title") or "")[:200],
             "company": str(job.get("company") or "")[:200],
             "location": str(job.get("location") or "")[:200],
             "salary": str(job.get("salary") or "")[:60],
             "reason": reason, "note": str(note or "").strip()[:200],
             "at": int(now if now is not None else time.time())}
    skips = [s for s in state.get("skips", []) if not key or s.get("key") != key]
    skips.append(entry)
    state["skips"] = skips[-MAX_SKIPS:]
    return entry


def set_company(state: dict, which: str, name: str, on: bool = True, why: str = "you") -> bool:
    """Add or remove a company on "exclude" (Never apply to) or "prioritize". A company is
    only ever on one list. Returns False for an empty name or unknown list."""
    if which not in ("exclude", "prioritize"):
        return False
    name = str(name or "").strip()[:120]
    key = company_key(name)
    if not key:
        return False
    for lst in ("exclude", "prioritize"):
        state[lst] = [c for c in state.get(lst, []) if company_key(c.get("name")) != key]
    if on:
        state[which].append({"name": name, "why": why if why in ("you", "skip") else "you",
                             "at": int(time.time())})
        state[which] = state[which][-MAX_COMPANIES:]
    return True


# --------------------------------------------------------------------------- rules
def learned(state: dict) -> list[dict]:
    """The rules the skips add up to, each {id, kind, value, count}. ``value`` is what the UI
    shows: a title, a level name, a place, a pay figure, or a company."""
    skips = [s for s in state.get("skips", []) if s.get("reason")]
    rules: list[dict] = []

    # Wrong role: groups of similar titles, ROLE_REPEAT or more.
    groups: list[list[dict]] = []
    for s in (x for x in skips if x["reason"] == "wrong_role"):
        w = role_words(s.get("title"))
        if not w:
            continue
        for g in groups:
            if _similar(role_words(g[0]["title"]), w):
                g.append(s)
                break
        else:
            groups.append([s])
    for g in groups:
        if len(g) >= ROLE_REPEAT:
            words = " ".join(sorted(role_words(g[0]["title"])))
            rules.append({"id": "role:" + words, "kind": "role", "value": g[-1]["title"],
                          "count": len(g), "words": sorted(role_words(g[0]["title"]))})

    senior = [level_of(s.get("title")) for s in skips if s["reason"] == "too_senior"]
    if senior:
        lvl = min(senior)
        rules.append({"id": "senior", "kind": "senior", "value": LEVELS[lvl], "level": lvl,
                      "count": len(senior)})
    junior = [level_of(s.get("title")) for s in skips if s["reason"] == "too_junior"]
    if junior:
        lvl = max(junior)
        rules.append({"id": "junior", "kind": "junior", "value": LEVELS[lvl], "level": lvl,
                      "count": len(junior)})

    places: dict[str, list[dict]] = {}
    for s in (x for x in skips if x["reason"] == "location"):
        k = place_key(s.get("location"))
        if k:
            places.setdefault(k, []).append(s)
    for k, ss in places.items():
        rules.append({"id": "loc:" + k, "kind": "location", "value": ss[-1]["location"],
                      "key": k, "count": len(ss)})

    pays = [salary_value(s.get("salary")) for s in skips if s["reason"] == "pay_low"]
    pays = [p for p in pays if p > 0]
    if pays:
        rules.append({"id": "pay", "kind": "pay", "value": max(pays), "count": len(pays)})

    nospon: dict[str, list[dict]] = {}
    for s in (x for x in skips if x["reason"] == "no_sponsorship"):
        k = company_key(s.get("company"))
        if k:
            nospon.setdefault(k, []).append(s)
    for k, ss in nospon.items():
        rules.append({"id": "nospon:" + k, "kind": "nospon", "value": ss[-1]["company"],
                      "key": k, "count": len(ss)})
    return rules


def forget(state: dict, rule_id: str) -> int:
    """Undo one learned rule: clear the reason on every skip that made it (the skips stay, so
    the role is still passed over, it just stops teaching). Returns how many skips changed."""
    rule = next((r for r in learned(state) if r["id"] == rule_id), None)
    if not rule:
        return 0
    kind = rule["kind"]

    def made_it(s: dict) -> bool:
        r = s.get("reason")
        if kind == "role":
            return r == "wrong_role" and _similar(role_words(s.get("title")),
                                                  frozenset(rule["words"]))
        if kind == "senior":
            return r == "too_senior"
        if kind == "junior":
            return r == "too_junior"
        if kind == "location":
            return r == "location" and place_key(s.get("location")) == rule["key"]
        if kind == "pay":
            return r == "pay_low"
        if kind == "nospon":
            return r == "no_sponsorship" and company_key(s.get("company")) == rule["key"]
        return False

    n = 0
    for s in state.get("skips", []):
        if made_it(s):
            s["reason"], s["forgot"] = "", True
            n += 1
    return n


# --------------------------------------------------------------------------- sponsorship
def needs_sponsorship(answers: dict | None) -> bool:
    """Whether the person needs visa sponsorship, through ONE adapter so the work-authorization
    profile (autoapply/workauth.py, being added separately) can take over without touching the
    callers. Until then it reads the saved application answer: "No" means they do not need it;
    "Yes", "Future" and unanswered all mean they do (SponsorJobs' audience, and the feed already
    leaves these roles out for everyone, so an unanswered profile keeps today's behaviour)."""
    answers = answers or {}
    try:
        from autoapply.workauth import needs_sponsorship as _workauth   # type: ignore
    except ImportError:
        _workauth = None
    if _workauth is not None:
        try:
            v = _workauth(answers)
            if v is not None:
                return bool(v)
        except Exception:   # noqa: BLE001 - fall back to the saved answer
            pass
    return str(answers.get("needs_sponsorship") or "").strip().lower() != "no"


def closed_to_visa(job: dict) -> bool:
    """The ad requires US citizenship, a green card or a clearance, or says it will not sponsor
    now or in the future; or the employer is a clearance-heavy defense prime. The same rules the
    feed build uses for its 'ad_says_no' and 'clearance_employer' funnel steps."""
    if job.get("ad_stance") in CLOSED_STANCES:
        return True
    return _clearance_heavy(job.get("company") or "")


# --------------------------------------------------------------------------- shaping a run
def shape(rows: list[dict], state: dict | None, *, needs_sponsor: bool = True) -> list[dict]:
    """Apply the company lists and what was learned to the run's ordered candidates.

    Left out: companies on "Never apply to", employers learned not to sponsor (while the person
    needs sponsorship), roles closed to a visa holder (same condition), and roles whose known pay
    is at or under the learned floor. Saved roles keep their place at the front; among the rest,
    prioritized companies come first and down-weighted roles (similar to a "Wrong role", at a
    skipped level, in a skipped place) go last (prioritized ones first among them), each group
    otherwise in its original order. Each kept row
    gets ``priority`` / ``down`` flags so the page can say why."""
    state = state or empty_state()
    rules = learned(state)
    excl = {company_key(c.get("name")) for c in state.get("exclude", [])}
    prio = {company_key(c.get("name")) for c in state.get("prioritize", [])}
    if needs_sponsor:
        excl |= {r["key"] for r in rules if r["kind"] == "nospon"}
    excl.discard("")
    prio.discard("")
    floor = next((r["value"] for r in rules if r["kind"] == "pay"), 0)
    role_sets = [frozenset(r["words"]) for r in rules if r["kind"] == "role"]
    max_lvl = next((r["level"] for r in rules if r["kind"] == "senior"), None)
    min_lvl = next((r["level"] for r in rules if r["kind"] == "junior"), None)
    places = {r["key"] for r in rules if r["kind"] == "location"}

    def down(j: dict) -> bool:
        if role_sets and any(_similar(role_words(j.get("title")), s) for s in role_sets):
            return True
        if max_lvl is not None or min_lvl is not None:
            lvl = level_of(j.get("title"))
            if (max_lvl is not None and lvl >= max_lvl) or (min_lvl is not None and lvl <= min_lvl):
                return True
        if places:
            k = "remote" if _is_remote(j) else place_key(j.get("location"))
            if k in places:
                return True
        return False

    saved, top, mid, back = [], [], [], []
    for j in rows or []:
        ck = company_key(j.get("company"))
        if ck and ck in excl:
            continue
        if needs_sponsor and closed_to_visa(j):
            continue
        if floor:
            pay = salary_value(j.get("salary"))
            if pay and pay <= floor:
                continue
        j = dict(j)
        if ck in prio:
            j["priority"] = True
        if j.get("saved"):
            saved.append(j)
        elif down(j):
            j["down"] = True
            back.append(j)
        elif j.get("priority"):
            top.append(j)
        else:
            mid.append(j)
    back.sort(key=lambda j: not j.get("priority"))     # stable: priority first among the rest
    return saved + top + mid + back


def mark_board(rows: list[dict], state: dict | None) -> list[dict]:
    """For the Jobs page: a copy of each row with ``excluded`` (a "Never apply to" company: shown
    with a quiet marker, never hidden, so a search for that company still finds it) and
    ``priority`` set."""
    state = state or empty_state()
    excl = {company_key(c.get("name")) for c in state.get("exclude", [])} - {""}
    prio = {company_key(c.get("name")) for c in state.get("prioritize", [])} - {""}
    out = []
    for j in rows or []:
        ck = company_key(j.get("company"))
        j = dict(j)
        if ck in excl:
            j["excluded"] = True
        elif ck in prio:
            j["priority"] = True
        out.append(j)
    return out
