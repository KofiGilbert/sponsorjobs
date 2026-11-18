"""The apply agent's Ready to send notice on Telegram (docs/notify.md, "Ready to send").

When "Assisted apply" leaves an application on its review page, the shell posts the hand-over to
the engine (``/api/autoapply/agent/ready``) with a small notice: how many fields the run filled,
how many lines deserve a look, and those lines in the Review answers list's own words (built by
``readyNotice`` in shell-electron/agent.js, never a self-identification value). This module turns
that into ONE message per run and decides whether it goes now, waits, or not at all:

* no channel (Telegram not linked, no own bot): nothing is sent and nothing is kept;
* the channel is paused (/stop) or "Application updates" is off: dropped, never sent later;
* quiet hours: held, and sent by the notify tick once they end, while the application is still
  waiting for Send and the notice is younger than 36 hours (the job alerts' rule);
* a rate limit or Telegram being down: held for the next tick, the same way;
* the same run twice: sent once.

There is no daily cap: the cap belongs to job-match alerts. There is no Send button: nothing in
notify/ approves an assisted submission from Telegram, so the person reviews and sends in the app.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .alerts import PREFS_DEFAULT, _parse_when, in_quiet_hours
from .channel import ChannelError

HOLD_HOURS = 36
SENT_MAX = 300
LINES_MAX = 12
LINE_MAX = 300
TEXT_MAX = 3800
RETRY_CODES = frozenset({"rate_limited", "telegram_unavailable", "unreachable"})


def notice_key(rid, run) -> str:
    return f"{int(rid)}:{str(run or '')[:40]}"


def _n(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def ready_text(role: str, company: str, notice: dict) -> str:
    """The message (exact template in docs/notify.md)."""
    n = notice if isinstance(notice, dict) else {}
    role, company = " ".join(str(role or "").split()), " ".join(str(company or "").split())
    who = f"{role} at {company}" if role and company else (role or company or "An application")
    filled, flagged = _n(n.get("filled")), _n(n.get("flagged"))
    lines = [" ".join(str(x).split())[:LINE_MAX] for x in (n.get("lines") or []) if str(x).strip()][:LINES_MAX]
    more = max(_n(n.get("more")), flagged - len(lines))
    out = [f"Ready to send: {who}."]
    filled_words = f"I filled {filled} field" + ("" if filled == 1 else "s") + "."
    if flagged:
        out.append(filled_words + f" {flagged} " + ("needs" if flagged == 1 else "need") + " a look:")
        out.extend(lines)
        if more:
            out.append(f"And {more} more in the app.")
    else:
        out.append(filled_words + " Nothing is flagged.")
    out.append("Review it in SponsorJobs, Auto-apply (Review answers), then press Send there. "
               "Nothing is sent until you do.")
    text = "\n".join(out)
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1] + "…"


def _quiet(prefs: dict, now: datetime) -> bool:
    local = now.astimezone() if now.tzinfo else now
    return in_quiet_hours(local.hour, prefs.get("quiet_start", 22), prefs.get("quiet_end", 8))


def _mark(d: dict, key: str) -> None:
    sent = [k for k in (d.get("ready_sent") or []) if k != key]
    sent.append(key)
    d["ready_sent"] = sent[-SENT_MAX:]
    (d.get("ready_held") or {}).pop(key, None)


def deliver(state, channel, key: str, rid: int, text: str, now: datetime,
            paused: bool = False) -> str:
    """Send the notice once. Returns sent / held / dup / none / paused / off / not_linked / failed."""
    if channel is None:
        return "none"
    d = state.load()
    if key in (d.get("ready_sent") or []) or key in (d.get("ready_held") or {}):
        return "dup"
    prefs = {**PREFS_DEFAULT, **(d.get("prefs") or {})}
    if paused or not prefs.get("app_updates", True):
        state.update(lambda x: _mark(x, key))
        return "paused" if paused else "off"
    if _quiet(prefs, now):
        _hold(state, key, rid, text, now)
        return "held"
    return _send(state, channel, key, rid, text, now)


def _hold(state, key, rid, text, now, at=None):
    def fn(d):
        held = d.setdefault("ready_held", {})
        held[key] = {"rid": int(rid), "text": text,
                     "at": at or held.get(key, {}).get("at") or now.astimezone(timezone.utc).isoformat()}
    state.update(fn)


def _send(state, channel, key, rid, text, now, at=None) -> str:
    try:
        channel.send(text, None)
    except ChannelError as exc:
        if exc.code in RETRY_CODES:
            _hold(state, key, rid, text, now, at)
            return "held"
        state.update(lambda x: _mark(x, key))          # paused / not linked: never later
        return exc.code if exc.code in ("paused", "not_linked") else "failed"
    except Exception:   # noqa: BLE001 - a notice is best-effort; the bar shows Ready to send
        state.update(lambda x: _mark(x, key))
        return "failed"
    state.update(lambda x: _mark(x, key))
    return "sent"


def drop(state, rid) -> None:
    """The application left Ready to send (sent, or a new run): a held notice goes with it."""
    def fn(d):
        held = d.get("ready_held") or {}
        for k in [k for k, v in held.items() if int(v.get("rid") or -1) == int(rid)]:
            held.pop(k, None)
    state.update(fn)


def flush(state, channel, now: datetime, still_ready, paused: bool = False) -> int:
    """Send the held notices once quiet hours are over, while each application still waits for
    Send (``still_ready(rid) -> bool``) and the notice is under HOLD_HOURS old. Returns how many
    went out."""
    d = state.load()
    held = dict(d.get("ready_held") or {})
    if not held or channel is None:
        return 0
    prefs = {**PREFS_DEFAULT, **(d.get("prefs") or {})}
    if paused or not prefs.get("app_updates", True):
        state.update(lambda x: [_mark(x, k) for k in held])
        return 0
    if _quiet(prefs, now):
        return 0
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=HOLD_HOURS)
    sent = 0
    for key, h in sorted(held.items(), key=lambda kv: str(kv[1].get("at") or "")):
        when = _parse_when(h.get("at"))
        try:
            ok = (when is None or when >= cutoff) and bool(still_ready(int(h.get("rid"))))
        except Exception:   # noqa: BLE001
            ok = False
        if not ok:
            state.update(lambda x, k=key: _mark(x, k))
            continue
        state.update(lambda x, k=key: (x.get("ready_held") or {}).pop(k, None))
        if _send(state, channel, key, int(h.get("rid")), str(h.get("text") or ""), now, h.get("at")) == "sent":
            sent += 1
        elif key in (state.load().get("ready_held") or {}):
            break                                       # rate limited: the rest wait too
    return sent
