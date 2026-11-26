"""Immigration and background questions, read by meaning (2026-11-26, the owner).

On Axon the app left "Are you an alien who has been admitted to the United States under a
nonimmigrant visa? (i.e. H-1B, TN, F1)" for the person although their saved work authorization
says F-1 OPT. Word lists miss such wordings, so when the engine's own rules leave a choice question
open, the model reads it:

* work authorization and immigration (visa type, authorized now, sponsorship now or later,
  citizenship, permanent residence, lawful status): answered from the saved work authorization,
  the model mapping the question and its options to it; asked only when the profile lacks the fact;
* background (convictions, court orders, fugitive, controlled substances, firearms eligibility,
  mental-health adjudication, discharge from the forces...): NEVER invented. Answered only from the
  person's own earlier answer to a question with the same meaning (saved under "Background: ..." in
  their answers, from a form they submitted), unless they asked to be asked every time;
* anything else: left as it is.

``pick`` returns (option, topic) or (None, topic). One short model call per question, options and
saved facts, cached on this computer (legal_options.json). ``topic`` alone (for saving a submitted
answer) is the same call with nothing saved to pick from.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

BG_PREFIX = "Background: "
TOPICS = ("work_authorization", "background", "other")

_SYSTEM = (
    "You read one question from a job application form and its numbered options. First say its topic: "
    "work_authorization (immigration, visa type or status, authorization to work, sponsorship now or later, "
    "citizenship, permanent residence, lawful presence), background (criminal convictions, charges, court orders, "
    "fugitive status, controlled substances, firearms eligibility, mental-health adjudication, military discharge, "
    "renounced citizenship and other legal-background facts), or other. Then pick the option that the person's "
    "saved facts answer, by meaning, in any wording or language. For work_authorization use only the saved work "
    "authorization. For background use only a saved background answer to a question with the SAME meaning; never "
    "infer one background fact from another and never assume. If the facts do not clearly answer it, pick 0. "
    "Reply with JSON only: {\"topic\": \"...\", \"pick\": <number>}")

_LOCK = threading.Lock()
_STATE: dict = {"path": None, "model": None}


def configure(path=None, model=None) -> None:
    _STATE["path"] = Path(path) if path else None
    _STATE["model"] = model


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


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
    while len(d) > 2000:
        d.pop(next(iter(d)))
    p.write_text(json.dumps(d), encoding="utf-8")


def prompt(question: str, options: list, work_auth: str, background: list) -> tuple[str, str]:
    lines = [f"Question: {' '.join(str(question or '').split())[:800]}", "Options:"]
    lines += [f"{i}. {' '.join(str(o).split())[:200]}" for i, o in enumerate(options, 1)]
    lines.append("Saved work authorization: " + (" ".join(str(work_auth or "").split())[:800] or "(none saved)"))
    if background:
        lines.append("Saved background answers (question: answer):")
        lines += [f"- {' '.join(str(q).split())[:200]}: {' '.join(str(a).split())[:80]}" for q, a in background[:30]]
    else:
        lines.append("Saved background answers: (none)")
    return _SYSTEM, "\n".join(lines)


def parse(raw: str, options: list) -> tuple[str | None, str]:
    m = re.search(r"\{.*?\}", str(raw or ""), re.S)
    if not m:
        return None, "other"
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None, "other"
    topic = str(d.get("topic") or "other")
    topic = topic if topic in TOPICS else "other"
    try:
        n = int(d.get("pick") or 0)
    except (TypeError, ValueError):
        n = 0
    pick = str(options[n - 1]) if 1 <= n <= len(options) and topic != "other" else None
    return pick, topic


def pick(question: str, options, work_auth: str = "", background=None) -> tuple[str | None, str]:
    """(the option the saved facts answer, the question's topic); (None, topic) when they do not."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    bg = [(str(q), str(a)) for q, a in (background or []) if str(q).strip() and str(a).strip()]
    if not opts or len(opts) > 40:
        return None, "other"
    raw_key = "|".join([_norm(question)[:800], "\x1f".join(sorted(_norm(o) for o in opts)), _norm(work_auth),
                        "\x1f".join(sorted(_norm(q) + "=" + _norm(a) for q, a in bg))])
    sig = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]
    with _LOCK:
        hit = _load().get(sig)
    if isinstance(hit, dict) and "topic" in hit:
        p = hit.get("pick")
        return (p if p in opts else None), hit["topic"]
    model = _STATE["model"]
    if model is None:
        return None, "other"
    try:
        raw = model(*prompt(question, opts, work_auth, bg))
    except Exception:                                    # noqa: BLE001 - no model: left for the person
        return None, "other"
    p, topic = parse(raw, opts)
    if topic == "background" and not bg:
        p = None                                         # never a background answer from nothing
    with _LOCK:
        d = _load()
        d[sig] = {"pick": p, "topic": topic}
        _save(d)
    return p, topic


def topic(question: str, options) -> str:
    """The question's topic alone (nothing saved to pick from)."""
    return pick(question, options, "", [])[1]


def saved_background(rows) -> list[tuple[str, str]]:
    """The person's saved background answers from their answer library rows."""
    out = []
    for r in rows or []:
        q = str(r.get("question") or "")
        if q.startswith(BG_PREFIX) and str(r.get("answer") or "").strip():
            out.append((q[len(BG_PREFIX):], str(r["answer"]).strip()))
    return out
