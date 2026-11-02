"""The live activity feed: what the agent is doing right now, step by step (OpenHands style).

One event shape for both the apply agent (shell-electron/agent.js builds the same dicts in
JavaScript) and the resume builder (this module):

    {"phase": "fit", "verb": "Fitting the page", "target": "Data Analyst at Acme",
     "summary": "shortening to keep one page", "state": "running", "at": 1793462461.2,
     "detail": "3 compiles"}

``state`` is one of running | done | retry | asking | parked. ``detail`` is optional muted text.
The UI shows the newest event as one live line (a spinner, the verb in the accent colour, the
summary) and every event as a timeline. Nothing here goes to Telegram, and nothing here changes
what a build does: when no run is active, ``step`` is a no-op.

Usage (the resume builder): ``with activity.run("resume", key="builder", target=...):`` around a
build, and ``activity.step(phase, verb, summary=...)`` anywhere below it, as deep as the compile
loop. A step with the same phase as the current one updates it in place (a loop of compiles is one
line, not twenty); a new phase closes the current one as done. Runs are kept per ``key`` (the
interactive builder is "builder", the unattended auto-apply tailoring is "unattended"), so the
builder's live line never shows a background build.

A parallel change (the PDF line check) may add its own steps: call ``step("check", "Checking
every line", ...)`` from wherever the measuring happens, nothing else is needed.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
import uuid

STATES = ("running", "done", "retry", "asking", "parked")
MAX_EVENTS = 60                    # a run longer than this keeps its first and newest steps
_KEEP_RUNS = 8

_lock = threading.Lock()
_runs: dict[str, dict] = {}        # key -> the latest run under that key
_current: contextvars.ContextVar = contextvars.ContextVar("activity_run", default=None)


def _clean(text, limit: int = 140) -> str:
    """One line of plain text, no em or en dashes (the copy rule), capped."""
    s = " ".join(str(text or "").split())
    s = s.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
    return s[:limit].rstrip()


def event(phase: str, verb: str, target: str = "", summary: str = "", state: str = "running",
          detail: str = "", at: float | None = None) -> dict:
    """One event in the shared shape."""
    st = state if state in STATES else "running"
    ev = {"phase": _clean(phase, 40), "verb": _clean(verb, 60), "target": _clean(target, 120),
          "summary": _clean(summary), "state": st, "at": round(at if at is not None else time.time(), 2)}
    d = _clean(detail)
    if d:
        ev["detail"] = d
    return ev


def _new_run(kind: str, key: str, target: str) -> dict:
    return {"id": uuid.uuid4().hex[:12], "kind": kind, "key": key, "target": _clean(target, 120),
            "started": round(time.time(), 2), "ended": None, "ok": None, "events": []}


@contextlib.contextmanager
def run(kind: str, key: str = "builder", target: str = ""):
    """Open a run for the code below. Nested calls join the run already open (a build that calls
    another build is one run). On exit the current step is closed as done; on an error it is
    marked parked and the error is raised as before."""
    cur = _current.get()
    if cur is not None:
        yield cur
        return
    r = _new_run(kind, key, target)
    with _lock:
        _runs[key] = r
        while len(_runs) > _KEEP_RUNS:
            _runs.pop(next(iter(_runs)))
    token = _current.set(r)
    try:
        yield r
    except BaseException:
        _finish(r, ok=False)
        raise
    else:
        _finish(r, ok=True)
    finally:
        _current.reset(token)


def _finish(r: dict, ok: bool) -> None:
    with _lock:
        evs = r["events"]
        if evs and evs[-1]["state"] in ("running", "asking"):
            evs[-1]["state"] = "done" if ok else "parked"
        r["ended"] = round(time.time(), 2)
        r["ok"] = ok


def step(phase: str, verb: str, summary: str = "", state: str = "running", detail: str = "",
         target: str = "") -> dict | None:
    """Record a step on the open run (no-op without one). Same phase as the current running
    step: update it in place. Else the current step closes as done (a retry stays a retry) and
    this one starts."""
    r = _current.get()
    if r is None:
        return None
    ev = event(phase, verb, target or r.get("target", ""), summary, state, detail)
    with _lock:
        evs = r["events"]
        last = evs[-1] if evs else None
        if last is not None and last["phase"] == ev["phase"] and last["state"] in ("running", "retry", "asking"):
            for k in ("verb", "summary", "state"):
                last[k] = ev[k]
            if ev.get("detail"):
                last["detail"] = ev["detail"]
            return dict(last)
        if last is not None and last["state"] in ("running", "asking"):
            last["state"] = "done"
        evs.append(ev)
        if len(evs) > MAX_EVENTS:
            del evs[1:len(evs) - MAX_EVENTS + 1]
        return dict(ev)


def current() -> dict | None:
    """The run open in this context, if any."""
    return _current.get()


def snapshot(key: str = "builder") -> dict | None:
    """A copy of the latest run under ``key`` for the UI: {id, kind, key, target, started,
    ended, ok, events}. None when there has been none."""
    with _lock:
        r = _runs.get(key)
        if r is None:
            return None
        out = dict(r)
        out["events"] = [dict(e) for e in r["events"]]
    return out


def _reset() -> None:
    """Tests only: forget every run."""
    with _lock:
        _runs.clear()
