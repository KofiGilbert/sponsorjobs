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
  removes that key's saved answer; nothing else is ever removed.

Personal data: nothing here leaves the machine (no model, no broker, no Telegram, no log).
"""
from __future__ import annotations

import re

from autoapply import countries, eeo

# (field, canonical key, the question it is saved under)
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("citizenship", "@citizenship", "Country of citizenship"),
    ("citizenship2", "@citizenship2", "Second country of citizenship"),
    ("salary", "@salary", "Desired annual salary (USD)"),
    ("gender", "@gender", "Gender"),
    ("hispanic", "@ethnicity", "Are you Hispanic or Latino?"),
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
# The card's choices (code, the words saved and shown). The words are also what forms read best.
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
MAX_SALARY = 10_000_000


def parse_salary(text) -> int | None:
    """A yearly amount in whole dollars from what a person types or once saved: "120000",
    "$120,000", "120k", "120K USD", the first figure of a range. None when there is no figure."""
    s = str(text or "").lower().replace(",", "").replace("$", " ").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k\b|thousand\b)?", s)
    if not m:
        return None
    v = float(m.group(1)) * (1000 if m.group(2) else 1)
    if v <= 0 or v > MAX_SALARY:
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


def read(lib, *, eeo_decline: bool = False) -> dict:
    """{values, other, answered, total, missing}: ``values`` per field is the card's choice code
    (EEO), the country's name, the salary in whole dollars, or the saved words when they are none
    of these; ``other`` names the fields whose value is saved words outside the card's choices."""
    saved = {f: _usable(lib, KEY[f]) for f, _, _ in FIELDS}
    values: dict = {}
    other: list = []
    for f in ("citizenship", "citizenship2"):
        a = saved[f]
        values[f] = countries.canonical(a) or a
        if a and not countries.canonical(a):
            other.append(f)
    sal = parse_salary(saved["salary"]) if saved["salary"] else None
    values["salary"] = str(sal) if sal else saved["salary"]
    if saved["salary"] and not sal:
        other.append("salary")
    # An answer picked on a combined race list says both halves ("Asian (Not Hispanic or
    # Latino)"): each fills in what the other key leaves open, as the planner reads them.
    race, hisp = eeo.person({"@race": saved["race"], "@ethnicity": saved["hispanic"]})
    for f in EEO_FIELDS:
        a = saved[f]
        code = _code(f, a) if a else ""
        if not a and f == "race" and race in dict(CHOICES["race"]):           # only when nothing
            code = race                                                        # of its own is saved
        if not a and f == "hispanic" and hisp in dict(CHOICES["hispanic"]):
            code = hisp
        if code:
            values[f] = code
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
    act on. Nothing is written when a field is refused."""
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
            name = countries.canonical(v) or v
            if len(name) > 80:
                raise ValueError("Pick a country from the list.")
            plan.append((f, name))
        elif f == "salary":
            n = parse_salary(v)
            if not n:
                raise ValueError("Enter your yearly salary as a number, like 120000.")
            plan.append((f, str(n)))
            out["salary"] = str(n)
        else:
            words = dict(CHOICES[f]).get(v)
            if words is None:                   # the saved words shown as they are, unchanged
                if len(v) > 120:
                    raise ValueError("Pick one of the choices.")
                words = v
            plan.append((f, words))
    # A race or a Hispanic or Latino answer the card showed from the OTHER key's words (a pick
    # on a combined list, "Asian (Not Hispanic or Latino)") is kept as its own answer when that
    # other key is overwritten or cleared: the person saw it and left it as it was.
    now = {f: _usable(lib, KEY[f]) for f in ("race", "hispanic")}
    race0, hisp0 = eeo.person({"@race": now["race"], "@ethnicity": now["hispanic"]})
    planned = {f for f, _ in plan}
    if "hispanic" in planned and "race" not in planned and not now["race"] and race0 in dict(CHOICES["race"]):
        plan.append(("race", dict(CHOICES["race"])[race0]))
    if "race" in planned and "hispanic" not in planned and not now["hispanic"] and hisp0 in dict(CHOICES["hispanic"]):
        plan.append(("hispanic", dict(CHOICES["hispanic"])[hisp0]))
    for f, v in plan:
        key = KEY[f]
        if v:
            if lib.save(QUESTION[f], v, "you") is None:
                raise ValueError("That answer can't be saved.")
            out["saved"].append(f)
        else:
            gone = False
            for row in lib.candidates(key):     # every saved answer the card could have shown
                gone = lib.delete(row["key"]) or gone
            if gone:
                out["cleared"].append(f)
    return out
