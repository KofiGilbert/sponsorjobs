"""Self-identification on any form's wording (owner decision 2026-11-26).

Forms word the same self-identification choice in endless ways: "I am not a protected veteran", a
plain "No" under the Title 38 notice, "Choose not to disclose", "Male / Man", any language. Word
lists break on the next site. So when autoapply/eeo.py's instant word rules are not sure, the
model is shown the question (with its notice text), the site's options and the person's saved
answer for that question, like any other profile fact, and picks the option that says the same
thing, or says that none does.

* ``choose``: the pick, from the local cache when this question, these options and this saved
  answer were seen before (no call), else one short model call, kept in the cache.
* ``learn``: when nothing fit and the person was asked, what they picked is kept for that
  question, those options and that saved answer, so the same wording never asks again.
* A standing "Decline to self-identify" always wins: the caller picks the page's decline choice
  (``decline``), with the model only finding which option that is when the words are unusual.

The cache lives in the app's data folder on this computer (selfid_options.json).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

DECLINE_ANSWER = "I decline to self-identify (prefer not to say)"

_SYSTEM = ("You fill one voluntary self-identification question on a job application for the person. You get the "
           "question (with any notice text), the form's numbered options and the person's own saved answer. Pick the "
           "one option that means the same as the saved answer, in any wording or language. Read the question with its "
           "notice: under a protected veteran notice (Title 38, section 4212, VEVRAA) a plain \"No\" means not a "
           "protected veteran and a plain \"Yes\" means a protected veteran. A saved answer that is a protected veteran "
           "classification fits the generic protected veteran option. Never pick an option that says more or something "
           "else than the saved answer; if none fits, pick 0. Reply with JSON only: {\"pick\": <number>}")

_LOCK = threading.Lock()
_STATE: dict = {"path": None, "model": None}


def configure(path=None, model=None) -> None:
    """Where the cache lives, and ``model(system, user) -> str`` (the app's cheaper model call)."""
    _STATE["path"] = Path(path) if path else None
    _STATE["model"] = model


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def _sig(key: str, question: str, options: list, answer: str) -> str:
    raw = "|".join([key, _norm(question)[:600], "\x1f".join(sorted(_norm(o) for o in options)), _norm(answer)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load() -> dict:
    p = _STATE["path"]
    if not p:
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = _STATE["path"]
    if not p:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    while len(d) > 2000:                                   # oldest first
        d.pop(next(iter(d)))
    p.write_text(json.dumps(d), encoding="utf-8")


def prompt(question: str, options: list, answer: str) -> tuple[str, str]:
    """(system, user) for one pick."""
    lines = [f"Question: {' '.join(str(question or '').split())[:800]}", "Options:"]
    lines += [f"{i}. {' '.join(str(o).split())[:200]}" for i, o in enumerate(options, 1)]
    lines.append(f"The person's saved answer: {' '.join(str(answer).split())[:200]}")
    return _SYSTEM, "\n".join(lines)


def parse(raw: str, options: list) -> str | None:
    """The option the reply picks; None for 0, an unknown number or no JSON."""
    m = re.search(r"\{.*?\}", str(raw or ""), re.S)
    if not m:
        return None
    try:
        n = int(json.loads(m.group(0)).get("pick"))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(options[n - 1]) if 1 <= n <= len(options) else None


def choose(key: str, question: str, options: list, answer: str) -> str | None:
    """The option for the saved ``answer``: the person's own earlier pick on this wording, else
    the cached pick, else one model call (cached, a "none fits" included)."""
    opts = [str(o) for o in options or [] if str(o).strip()]
    a = str(answer or "").strip()
    if not opts or not a:
        return None
    sig = _sig(key, question, opts, a)
    with _LOCK:
        hit = _load().get(sig)
    if isinstance(hit, dict) and "pick" in hit:
        return hit["pick"] if hit["pick"] in opts else None
    model = _STATE["model"]
    if model is None:
        return None
    try:
        raw = model(*prompt(question, opts, a))
    except Exception:                                    # noqa: BLE001 - no model: the person is asked
        return None                                      # (not cached: the next run may have one)
    pick = parse(raw, opts)
    with _LOCK:
        d = _load()
        d[sig] = {"key": key, "pick": pick, "by": "model"}
        _save(d)
    return pick


def decline(key: str, question: str, options: list) -> str | None:
    """The page's decline choice in words eeo.py does not know (the standing decline is on)."""
    return choose(key, question, options, DECLINE_ANSWER)


def learn(key: str, question: str, options: list, answer: str, picked: str) -> bool:
    """The person picked ``picked`` when asked: kept for this question, these options and this
    saved answer, so the same wording is answered without asking next time."""
    opts = [str(o) for o in options or [] if str(o).strip()]
    if not str(answer or "").strip() or str(picked) not in opts:
        return False
    with _LOCK:
        d = _load()
        d[_sig(key, question, opts, answer)] = {"key": key, "pick": str(picked), "by": "person"}
        _save(d)
    return True
