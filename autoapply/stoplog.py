"""Where Autofill and the agent stop (2026-11-26): a small event each time, so the brain is fixed in
general instead of one site at a time.

An event says WHERE and WHY, never WHO: the time, the hiring system and host, the kind of page, the
event type (``TYPES``), the public question and option texts involved (shortened), the agent's own
one-line reason, the step count, the cost so far and the app version. Never the person's answers,
name, email, phone, resume text or anything typed: ``build`` keeps only the fields above and
``scrub`` takes out every saved profile value, email address, long digit run and link from the
texts, whatever the caller sent.

Events are kept on this computer (data/stoplog.jsonl) and sent to the broker in small batches by a
background thread (``flush``): fire-and-forget, never in a run's way, and nothing is lost offline
(the next batch takes it).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

TYPES = ("asked_person", "stuck_loop", "cost_cap", "captcha", "sign_in_wall", "required_left_empty",
         "site_error", "closed_posting", "other")
PAGE_KINDS = ("job_page", "form", "review", "sign_in", "other")
MAX_QUESTION, MAX_OPTION, MAX_OPTIONS, MAX_REASON = 200, 60, 8, 160
KEEP_LINES = 2000
BATCH = 50

# The hiring systems by host (the family names the admin page groups by).
_ATS = (("greenhouse", ("greenhouse.io",)), ("lever", ("lever.co",)), ("ashby", ("ashbyhq.com",)),
        ("workday", ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com")), ("icims", ("icims.com",)),
        ("smartrecruiters", ("smartrecruiters.com",)), ("workable", ("workable.com",)), ("recruitee", ("recruitee.com",)),
        ("taleo", ("taleo.net",)), ("successfactors", ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu")),
        ("eightfold", ("eightfold.ai",)), ("phenom", ("phenompeople.com",)), ("jobvite", ("jobvite.com",)),
        ("bamboohr", ("bamboohr.com",)), ("oracle", ("oraclecloud.com",)), ("adp", ("adp.com",)),
        ("ukg", ("ultipro.com", "ukg.net")), ("paylocity", ("paylocity.com",)), ("avature", ("avature.net",)),
        ("jibe", ("jibeapply.com",)), ("dayforce", ("dayforcehcm.com",)))

_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(\.[\w-]+)+")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_DIGITS = re.compile(r"\+?\(?\d[\d\s().-]{5,}\d")        # a phone, an ID, a card: 7 or more digits with separators


def host_of(url: str) -> str:
    """The page's host without "www." ("" for no URL)."""
    try:
        h = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def ats_of(host: str) -> str:
    """The hiring system's family for a host, else "careersite" (a company's own site)."""
    h = str(host or "").lower()
    for name, suffixes in _ATS:
        if any(h == s or h.endswith("." + s) for s in suffixes):
            return name
    return "careersite" if h else ""


def scrub(text, values=None, limit: int = MAX_REASON) -> str:
    """Public words only: every saved profile value (``values``: {key: value}), email address, link
    and long digit run replaced, whitespace folded, cut to ``limit``."""
    s = " ".join(str(text or "").split())
    vals = []
    for v in (values or {}).values():
        if isinstance(v, (list, tuple)):
            vals += [str(x) for x in v]
        elif v is not None:
            vals.append(str(v))
    for v in sorted({" ".join(v.split()) for v in vals if len(" ".join(v.split())) >= 3}, key=len, reverse=True):
        s = re.sub(re.escape(v), "[x]", s, flags=re.I)
    s = _EMAIL.sub("[x]", s)
    s = _URL.sub("[link]", s)
    s = _DIGITS.sub("[x]", s)
    return s[:limit].rstrip()


def build(raw: dict, values=None, version: str = "") -> dict | None:
    """The event as it may be kept and sent, or None when it is not one. Only the listed fields
    survive; every text is scrubbed (``scrub``)."""
    r = raw if isinstance(raw, dict) else {}
    kind = str(r.get("type") or "")
    if kind not in TYPES:
        return None
    host = host_of(r.get("url")) or str(r.get("host") or "").lower()[:120]
    page = str(r.get("page_kind") or "other")
    opts = [scrub(o, values, MAX_OPTION) for o in (r.get("options") or [])[:MAX_OPTIONS] if str(o or "").strip()]
    try:
        steps = max(0, min(999, int(r.get("steps") or 0)))
    except (TypeError, ValueError):
        steps = 0
    try:
        cost = round(max(0.0, min(99.0, float(r.get("cost") or 0))), 4)
    except (TypeError, ValueError):
        cost = 0.0
    return {"at": int(r.get("at") or time.time()), "host": scrub(host, values, 120), "ats": ats_of(host),
            "page_kind": page if page in PAGE_KINDS else "other", "type": kind,
            "question": scrub(r.get("question"), values, MAX_QUESTION), "options": opts,
            "reason": scrub(r.get("reason"), values, MAX_REASON), "steps": steps, "cost": cost,
            "version": scrub(r.get("version") or version, None, 24)}


# --------------------------------------------------------------------------- the local log
_LOCK = threading.Lock()
_STATE: dict = {"path": None, "send": None, "busy": False}


def configure(path=None, send=None) -> None:
    """Where the log lives, and ``send(events) -> bool`` (the broker POST; True when it took them)."""
    _STATE["path"] = Path(path) if path else None
    _STATE["send"] = send


def _files():
    p = _STATE["path"]
    return (p, p.with_suffix(".sent")) if p else (None, None)


def record(events: list) -> int:
    """Append built events to the local log (the oldest go past KEEP_LINES); returns how many."""
    p, _ = _files()
    evs = [e for e in events or [] if isinstance(e, dict)]
    if not p or not evs:
        return 0
    with _LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        lines += [json.dumps(e, ensure_ascii=False) for e in evs]
        drop = max(0, len(lines) - KEEP_LINES)
        p.write_text("\n".join(lines[drop:]) + "\n", encoding="utf-8")
        if drop:
            _set_sent(max(0, _sent() - drop))
    return len(evs)


def _sent() -> int:
    _, s = _files()
    try:
        return int(s.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError, AttributeError):
        return 0


def _set_sent(n: int) -> None:
    _, s = _files()
    if s:
        s.write_text(str(max(0, n)), encoding="utf-8")


def pending() -> list:
    """The events not sent yet, oldest first."""
    p, _ = _files()
    if not p:
        return []
    with _LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        start = min(_sent(), len(lines))
    out = []
    for ln in lines[start:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            out.append(None)
    return out


def flush_once() -> int:
    """Send one batch; returns how many the broker took (0 offline or refused: kept for later)."""
    send = _STATE["send"]
    todo = pending()[:BATCH]
    if not send or not todo:
        return 0
    good = [e for e in todo if isinstance(e, dict)]
    try:
        ok = bool(send(good)) if good else True
    except Exception:                                    # noqa: BLE001 - offline: the next one takes it
        ok = False
    if not ok:
        return 0
    with _LOCK:
        _set_sent(_sent() + len(todo))
    return len(good)


def flush() -> None:
    """Send what is waiting in the background, a batch at a time, never twice at once."""
    with _LOCK:
        if _STATE["busy"]:
            return
        _STATE["busy"] = True

    def work():
        try:
            for _ in range(20):
                if not flush_once():
                    break
        finally:
            _STATE["busy"] = False
    threading.Thread(target=work, name="stoplog-flush", daemon=True).start()
