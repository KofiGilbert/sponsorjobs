"""Keep the person's self-identification (EEO) and legal values out of model payloads
(docs/applicant-brain.md, section 9).

Model payloads may carry a form's labels, its options and key names. They never carry a stored
EEO value (a gender, a race, a disability answer) or a legal one (a citizenship, a visa status).
The trouble is that the same words are often the form's own options ("Male", "Asian"), which
the model may see. So the guard works against the form's own text:

* ``sensitive_values(store)``: every current EEO and legal value the brain holds, minus the
  words too common to mean anything on their own ("Yes", "No", anything under 4 characters);
* ``leaks(payload, values, allowed)``: the stored values a payload carries outside the allowed
  texts (the form's labels and options), for tests and checks;
* ``for_model(payload, values, allowed)``: the payload with those values replaced by
  ``[withheld]``, every allowed text left exactly as it is.

Phase 1 ships the guard and a test that scans what the planner hands the model. Phase 4 runs it
on the label mapper's batched call.
"""
from __future__ import annotations

import json
import re

from applicant import keys as K

WITHHELD = "[withheld]"
_GENERIC = frozenset({"yes", "no", "none", "n/a", "true", "false", "decline", "prefer not to say",
                      "i don't wish to answer", "i do not wish to answer"})


def _leaves(value: str) -> list[str]:
    """The strings in a stored value (a JSON value's string leaves, else the value itself)."""
    v = str(value or "").strip()
    if v[:1] in "{[":
        try:
            obj = json.loads(v)
        except ValueError:
            return [v]
        out: list[str] = []

        def walk(o):
            if isinstance(o, dict):
                for x in o.values():
                    walk(x)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
            elif isinstance(o, str):
                out.append(o)
        walk(obj)
        return out
    return [v]


def sensitive_values(store) -> list[str]:
    """Every current EEO and legal value in the brain, longest first."""
    vals: set[str] = set()
    rows = store.q("SELECT value FROM fact WHERE sensitivity IN (?, ?) AND status IN ('active', 'needs_check')",
                   (K.EEO, K.LEGAL))
    for r in rows:
        for s in _leaves(r["value"]):
            s = " ".join(s.split())
            if len(s) >= 4 and s.lower() not in _GENERIC:
                vals.add(s)
    return sorted(vals, key=lambda s: (-len(s), s))


def _rx(value: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.I)


def _strings(payload) -> list[str]:
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [s for v in payload.values() for s in _strings(v)]
    if isinstance(payload, (list, tuple)):
        return [s for v in payload for s in _strings(v)]
    return []


def leaks(payload, values, allowed=()) -> list[str]:
    """The stored values found in the payload outside the allowed texts."""
    ok = {" ".join(str(a or "").split()).lower() for a in allowed}
    found: list[str] = []
    for s in _strings(payload):
        if " ".join(s.split()).lower() in ok:
            continue
        for v in values:
            if v not in found and _rx(v).search(s):
                found.append(v)
    return found


def for_model(payload, values, allowed=()):
    """A copy of the payload with every stored value outside the allowed texts withheld."""
    ok = {" ".join(str(a or "").split()).lower() for a in allowed}

    def fix(o):
        if isinstance(o, str):
            if " ".join(o.split()).lower() in ok:
                return o
            for v in values:
                o = _rx(v).sub(WITHHELD, o)
            return o
        if isinstance(o, dict):
            return {k: fix(v) for k, v in o.items()}
        if isinstance(o, list):
            return [fix(v) for v in o]
        if isinstance(o, tuple):
            return tuple(fix(v) for v in o)
        return o
    return fix(payload)
