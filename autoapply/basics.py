"""The answers every form asks: country of citizenship, desired salary and the five voluntary
self-identification questions, answered once on Your answers ("Answers every form asks").

They live in the answer library (autoapply/answers.py) under their canonical keys, as the
person's own answers, so the planner, the agent's known answers and its asks all find them; the
salary also goes to the prefs store (desired_salary) for the browser extension. This module is
the card's two halves, pure apart from the library it is handed:

* ``read``: what the card shows. Each key's usable answer (answers.unusable() rejects the raw
  replies an older run saved, so those show as empty), as the card's own choice when it is one,
  else the saved words. When two saved salaries disagree, the most recently updated one.
* ``write``: the fields the person changed, saved under the canonical keys. A field they cleared
  removes the answer the card showed for it; a field they saved replaces older wordings of the
  same question. Nothing the card does not show is touched.

Personal data: nothing here leaves the machine (no model, no broker, no Telegram, no log).
"""
from __future__ import annotations

import re

from autoapply import countries, eeo

# (field, canonical key, the question it is saved under). The Hispanic or Latino answer has its
# own key, so it never overwrites a different "Ethnicity" answer (the UK's ethnic group).
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("citizenship", "@citizenship", "Country of citizenship"),
    ("citizenship2", "@citizenship2", "Second country of citizenship"),
    ("salary", "@salary", "Desired annual salary (USD)"),
    ("gender", "@gender", "Gender"),
    ("hispanic", "@hispanic_latino", "Are you Hispanic or Latino?"),
    ("race", "@race", "Race"),
    ("veteran", "@veteran", "Veteran status"),
    ("disability", "@disability", "Disability status"),
)
KEY = {f: k for f, k, _ in FIELDS}
QUESTION = {f: q for f, _, q in FIELDS}
EEO_FIELDS = ("gender", "hispanic", "race", "veteran", "disability")
# What "6 of 7 form answers saved" counts. A second citizenship is optional.
COUNTED = ("citizenship", "salary") + EEO_FIELDS

DECLINE = "Prefer not to say"
# The card's choices (code, the words shown). The words are also what is saved, except where
# SAVED_WORDS says them in full.
CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "gender": (("male", "Male"), ("female", "Female"), ("nonbinary", "Non-binary"), ("decline", DECLINE)),
    "hispanic": (("yes", "Yes"), ("no", "No"), ("decline", DECLINE)),
    "race": (("american_indian", "American Indian or Alaska Native"), ("asian", "Asian"),
             ("black", "Black or African American"), ("pacific", "Native Hawaiian or Other Pacific Islander"),
             ("white", "White"), ("two_or_more", "Two or more races"), ("decline", DECLINE)),
    "veteran": (("not", "I am not a protected veteran"), ("protected", "I am a protected veteran"),
                ("decline", DECLINE)),
    "disability": (("no", "No"), ("yes", "Yes"), ("decline", DECLINE)),
}
# A bare "Yes" or "No" says nothing on its own once it is typed into a box: the Hispanic or
# Latino answer is saved in words that name it.
SAVED_WORDS = {"hispanic": {"yes": "Hispanic or Latino", "no": "Not Hispanic or Latino"}}
MAX_SALARY = 10_000_000
MIN_SALARY = 1_000           # a yearly salary, never an hourly rate or "120" from "120.000"


def parse_salary(text) -> int | None:
    """A yearly amount in whole dollars from what a person types or once saved: "120000",
    "$120,000", "120.000", "120 000", "120k", "1.5M", "120K USD", the first figure of a range.
    None when there is no figure, it is an hourly or monthly rate, or it is too small or too big
    to be a yearly salary."""
    s = str(text or "").lower()
    if re.search(r"(/|\bper\b|\ban?\b)\s*(h|hr|hour|month|mo|week|wk|day)\b|\bhourly\b|\bmonthly\b|\bweekly\b|\bdaily\b", s):
        return None
    s = re.sub(r"[   ']", " ", s).replace("$", " ")
    s = re.sub(r"(?<=\d) (?=\d{3}(?!\d))", "", s)                 # "120 000"
    s = re.sub(r"(?<=\d)[.,](?=\d{3}(?!\d))", "", s)              # "120.000", "120,000", "1.200.000"
    s = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", s)              # "120000,50"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k\b|thousand\b|mm?\b|million\b)?", s)
    if not m:
        return None
    unit = m.group(2) or ""
    mult = 1000 if unit in ("k", "thousand") else 1_000_000 if unit in ("m", "mm", "million") else 1
    v = float(m.group(1)) * mult
    if v < MIN_SALARY or v > MAX_SALARY:
        return None
    return int(round(v))


def _usable(lib, key: str) -> str:
    row = lib.best(key) if lib is not None else None
    return str((row or {}).get("answer") or "").strip()


def _code(field: str, answer: str) -> str:
    """The card's choice code a saved answer makes, "" when it is none of them."""
    key = KEY[field]
    if field == "hispanic":
        return eeo.hispanic_class(answer) or ""
    cls = eeo.value_class(key, answer, saved=True) or ""
    return cls if cls in {c for c, _ in CHOICES[field]} else ""


def _words(field: str, code: str) -> str:
    return SAVED_WORDS.get(field, {}).get(code) or dict(CHOICES[field]).get(code, "")


def _halves(lib) -> dict:
    """The race and Hispanic or Latino answers as the card shows them, and the key each is read
    from: {field: (code, key)}. The question's own answer first; else what another answer says
    (an "Ethnicity" answer "No", or a pick on a combined list, "Asian (Not Hispanic or Latino)")."""
    own_r, own_h, eth = _usable(lib, "@race"), _usable(lib, "@hispanic_latino"), _usable(lib, "@ethnicity")
    race, hisp = eeo.person({"@race": own_r, "@ethnicity": eth, "@hispanic_latino": own_h})
    out: dict = {}
    if own_r:
        out["race"] = (_code("race", own_r), "@race")
    elif race in dict(CHOICES["race"]):
        out["race"] = (race, "@ethnicity")
    if own_h:
        out["hispanic"] = (_code("hispanic", own_h), "@hispanic_latino")
    elif hisp in dict(CHOICES["hispanic"]):
        out["hispanic"] = (hisp, "@ethnicity" if eth and eeo.hispanic_class(eth) else "@race")
    return out


def read(lib, *, eeo_decline: bool = False, salary_fallback="") -> dict:
    """{values, other, answered, total, missing}: ``values`` per field is the card's choice code
    (EEO), the country's name, the salary in whole dollars, or the saved words when they are none
    of these; ``other`` names the fields whose value is saved words outside the card's choices.
    ``salary_fallback`` (the prefs store's desired_salary, what the browser extension fills) is
    shown when the library has no salary."""
    saved = {f: _usable(lib, KEY[f]) for f, _, _ in FIELDS}
    values: dict = {}
    other: list = []
    a = saved["citizenship"]
    values["citizenship"] = countries.canonical(a) or a
    if a and not countries.canonical(a):
        other.append("citizenship")
    values["citizenship2"] = countries.canonical(saved["citizenship2"])     # only a country
    sal = parse_salary(saved["salary"]) if saved["salary"] else None
    if not saved["salary"] and salary_fallback:
        sal = parse_salary(salary_fallback)
    values["salary"] = str(sal) if sal else saved["salary"]
    if saved["salary"] and not sal:
        other.append("salary")
    halves = _halves(lib)
    for f in EEO_FIELDS:
        a = saved[f]
        code = halves[f][0] if f in halves else (_code(f, a) if a else "")
        if code:
            values[f] = code
        elif f == "hispanic":
            values[f] = ""          # words no choice reads are never the Hispanic or Latino answer
        else:
            values[f] = a
            if a:
                other.append(f)
    total = len(COUNTED)
    done = [f for f in COUNTED if values.get(f)]
    if eeo_decline:
        done = sorted(set(done) | set(EEO_FIELDS))
    missing = [f for f in COUNTED if f not in done]
    return {"values": values, "other": other, "answered": total - len(missing), "total": total,
            "missing": missing}


def write(lib, fields: dict) -> dict:
    """Save the fields the person changed ({field: value}; "" clears it). Returns
    {saved: [...], cleared: [...], salary} where salary is what the prefs store gets ("120000",
    "" when cleared, None when untouched), or raises ValueError with a message the person can
    act on. Nothing is written when a field is refused.

    Saving one of these questions is the person's own answer to it: any older row of the same
    question (an "Error: The field ..." wording left under its old key) goes, so the agent and
    Saved answers show one answer. Clearing a race or Hispanic or Latino answer the card showed
    from ANOTHER answer (a pick on a combined list) removes that answer too, keeping the half the
    person did not clear as its own answer, so a cleared answer stays cleared."""
    out = {"saved": [], "cleared": [], "salary": None}
    plan = []
    for f, v in (fields or {}).items():
        if f not in KEY:
            continue
        v = " ".join(str(v or "").split())
        if not v:
            plan.append((f, ""))
            if f == "salary":
                out["salary"] = ""
            continue
        if f in ("citizenship", "citizenship2"):
            name = countries.canonical(v)
            if not name and (f == "citizenship2" or len(v) > 80):
                raise ValueError("Pick a country from the list.")
            plan.append((f, name or v))
        elif f == "salary":
            n = parse_salary(v)
            if not n:
                raise ValueError("Enter your yearly salary as a number, like 120000.")
            plan.append((f, str(n)))
            out["salary"] = str(n)
        else:
            words = _words(f, v)
            if not words:                       # the saved words shown as they are, unchanged
                if len(v) > 120:
                    raise ValueError("Pick one of the choices.")
                words = v
            plan.append((f, words))
    planned = {f for f, _ in plan}
    halves = _halves(lib)
    # Every key this save removes or replaces: the fields' own, and the other answer a cleared
    # race or Hispanic or Latino value was read from.
    gone = {KEY[f] for f, v in plan if not v}
    for f, v in plan:
        if not v and f in halves and halves[f][1] != KEY[f]:
            gone.add(halves[f][1])
    touched = gone | {KEY[f] for f, v in plan if v}
    # The half the person left as it was, read from an answer this save removes or replaces:
    # kept as its own answer.
    for g in ("race", "hispanic"):
        if g not in planned and g in halves and halves[g][0] and halves[g][1] in touched:
            plan.append((g, _words(g, halves[g][0])))
    for k in gone:
        removed = False
        for row in lib.candidates(k):           # every saved answer the card could have shown
            removed = lib.delete(row["key"]) or removed
        f = next((f for f, kk in KEY.items() if kk == k), None)
        if removed and f in planned:
            out["cleared"].append(f)
    for f, v in plan:
        if not v:
            if f not in out["cleared"] and f in halves:
                out["cleared"].append(f)
            continue
        key = KEY[f]
        if lib.save(QUESTION[f], v, "you") is None:
            raise ValueError("That answer can't be saved.")
        for row in lib.candidates(key):         # older wordings of the same question
            if row["key"] != key:
                lib.delete(row["key"])
        out["saved"].append(f)
    return out
