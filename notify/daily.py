"""What the person hears from Auto mode (docs/notify.md, "Auto mode"; docs/apply-agent.md 26).

In Auto ("I'm away") the apply agent works out of sight and never asks. The person hears from
it in exactly two ways, both built here:

1. **Per application it sent by itself** (only on the verified allowlist): one short notice,
   ``Sent: {Role} at {Company}``. Telegram through the active channel when one is linked,
   else an in-app notification. Once per application. Quiet hours hold it (the notify tick sends
   it when they end); "Application updates" off or a paused channel keeps it in the app only.
2. **ONE daily summary**: ``{n} sent for you, {m} ready for your Send, {k} need an answer``.
   Built once per local day, after SUMMARY_HOUR and outside quiet hours, and only when at least
   one of the three is above zero. The app shows it with **Send all**; the Telegram copy has no
   button, because nothing in notify/ approves an assisted submission from Telegram (the same
   rule as the Ready to send notice): the person's Send stays in the app.

Only the role, the company and counts go out. Never an answer, never a self-identification
value, never a resume line.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .alerts import PREFS_DEFAULT, _parse_when, in_quiet_hours
from .channel import ChannelError

SUMMARY_HOUR = 18           # local hour from which the day's summary may go out
HOLD_HOURS = 36
SENT_MAX = 400
INAPP_MAX = 40
RETRY_CODES = frozenset({"rate_limited", "telegram_unavailable", "unreachable"})


def _who(role: str, company: str) -> str:
    role, company = " ".join(str(role or "").split())[:120], " ".join(str(company or "").split())[:80]
    return f"{role} at {company}" if role and company else (role or company or "An application")


def sent_text(role: str, company: str) -> str:
    return f"Sent: {_who(role, company)}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_text(sent: int, ready: int, need: int, telegram: bool = False, check: int = 0, could_not: int = 0) -> str:
    """The daily summary's words. ``telegram``: the copy that goes to the phone, which says
    where Send all is instead of offering it. (2026-11-19) ``check``: answers Auto gave by best
    guess on the applications waiting for Send; ``could_not``: forms the agent could not do (a
    required field it never guesses)."""
    sent, ready, need = max(0, int(sent)), max(0, int(ready)), max(0, int(need))
    check, could_not = max(0, int(check or 0)), max(0, int(could_not or 0))
    line = (f"Today: {sent} sent for you, {ready} ready for your Send, "
            f"{_plural(need, 'needs', 'need')} an answer.")
    if check:
        line += f" {_plural(check, 'answer needs', 'answers need')} your check before you send."
    if could_not:
        line += f" {_plural(could_not, 'form', 'forms')} I could not do."
    if not telegram:
        return line
    tail = []
    if ready:
        tail.append("Open SponsorJobs, Auto-apply, and press Send all. Nothing more is sent until you do.")
    if need:
        tail.append("Answer the open questions in Auto-apply, Needs you.")
    return line + ("\n" + " ".join(tail) if tail else "")


def _quiet(prefs: dict, now: datetime) -> bool:
    local = now.astimezone() if now.tzinfo else now
    return in_quiet_hours(local.hour, prefs.get("quiet_start", 22), prefs.get("quiet_end", 8))


def _prefs(d: dict) -> dict:
    return {**PREFS_DEFAULT, **(d.get("prefs") or {})}


def _inapp(d: dict, kind: str, text: str, now: datetime, **extra) -> dict:
    item = {"id": f"{kind}:{extra.get('rid', '')}:{now.astimezone(timezone.utc).isoformat()}", "kind": kind,
            "text": text, "at": now.astimezone(timezone.utc).isoformat(), "seen": False, **extra}
    rows = [r for r in (d.get("auto_inapp") or []) if isinstance(r, dict)]
    rows.append(item)
    d["auto_inapp"] = rows[-INAPP_MAX:]
    return item


def _try_send(channel, text: str) -> str:
    """sent / retry / off (paused or not linked: never later) / failed."""
    try:
        channel.send(text, None)
        return "sent"
    except ChannelError as exc:
        if exc.code in RETRY_CODES:
            return "retry"
        return "off" if exc.code in ("paused", "not_linked") else "failed"
    except Exception:   # noqa: BLE001 - a notice is best-effort; the app shows it anyway
        return "failed"


def notify_sent(state, channel, rid: int, role: str, company: str, now: datetime,
                paused: bool = False) -> str:
    """The notice for ONE application the agent sent by itself. Once per application.
    Returns telegram / held / inapp / dup."""
    rid = int(rid)
    text = sent_text(role, company)
    d = state.load()
    if rid in (d.get("auto_sent") or []) or str(rid) in (d.get("auto_sent_held") or {}):
        return "dup"
    prefs = _prefs(d)

    def mark(x, inapp: bool):
        ids = [i for i in (x.get("auto_sent") or []) if i != rid]
        ids.append(rid)
        x["auto_sent"] = ids[-SENT_MAX:]
        (x.get("auto_sent_held") or {}).pop(str(rid), None)
        if inapp:
            _inapp(x, "sent", text, now, rid=rid)

    if channel is None or paused or not prefs.get("app_updates", True):
        state.update(lambda x: mark(x, True))
        return "inapp"
    if _quiet(prefs, now):
        state.update(lambda x: x.setdefault("auto_sent_held", {}).__setitem__(
            str(rid), {"text": text, "at": now.astimezone(timezone.utc).isoformat()}))
        return "held"
    got = _try_send(channel, text)
    if got == "sent":
        state.update(lambda x: mark(x, False))
        return "telegram"
    if got == "retry":
        state.update(lambda x: x.setdefault("auto_sent_held", {}).__setitem__(
            str(rid), {"text": text, "at": now.astimezone(timezone.utc).isoformat()}))
        return "held"
    state.update(lambda x: mark(x, True))
    return "inapp"


def flush_sent(state, channel, now: datetime, paused: bool = False) -> int:
    """Held "Sent" notices go out once quiet hours end. A notice older than HOLD_HOURS, or one
    whose channel went away, lands in the app instead. Returns how many went to Telegram."""
    d = state.load()
    held = dict(d.get("auto_sent_held") or {})
    if not held:
        return 0
    prefs = _prefs(d)
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=HOLD_HOURS)

    def settle(x, key, text, inapp):
        (x.get("auto_sent_held") or {}).pop(key, None)
        ids = [i for i in (x.get("auto_sent") or []) if i != int(key)]
        ids.append(int(key))
        x["auto_sent"] = ids[-SENT_MAX:]
        if inapp:
            _inapp(x, "sent", text, now, rid=int(key))

    off = channel is None or paused or not prefs.get("app_updates", True)
    if not off and _quiet(prefs, now):
        return 0
    out = 0
    for key, h in sorted(held.items(), key=lambda kv: str(kv[1].get("at") or "")):
        text = str(h.get("text") or "")
        when = _parse_when(h.get("at"))
        if off or (when is not None and when < cutoff):
            state.update(lambda x, k=key, t=text: settle(x, k, t, True))
            continue
        got = _try_send(channel, text)
        if got == "sent":
            state.update(lambda x, k=key, t=text: settle(x, k, t, False))
            out += 1
        elif got == "retry":
            break                                   # rate limited: the rest wait too
        else:
            state.update(lambda x, k=key, t=text: settle(x, k, t, True))
    return out


def summary_due(state, now: datetime) -> bool:
    """Is today's summary still to build? Once per local day, from SUMMARY_HOUR, never in
    quiet hours."""
    local = now.astimezone() if now.tzinfo else now
    d = state.load()
    if (d.get("auto_summary") or {}).get("date") == local.date().isoformat():
        return False
    return local.hour >= SUMMARY_HOUR and not _quiet(_prefs(d), now)


def daily_summary(state, channel, counts: dict, now: datetime, paused: bool = False) -> dict | None:
    """Build today's summary once. ``counts`` = {sent, ready, need} from the records. Returns
    the summary ({date, sent, ready, need, text, telegram}) or None when it is not due. A day
    with nothing to say is marked done and says nothing."""
    if not summary_due(state, now):
        return None
    local = now.astimezone() if now.tzinfo else now
    sent, ready, need = (max(0, int(counts.get(k) or 0)) for k in ("sent", "ready", "need"))
    check, could_not = max(0, int(counts.get("check") or 0)), max(0, int(counts.get("could_not") or 0))
    summary = {"date": local.date().isoformat(), "sent": sent, "ready": ready, "need": need,
               "check": check, "could_not": could_not, "text": "", "telegram": False, "seen": False}
    if not (sent or ready or need or could_not):
        state.update(lambda x: x.__setitem__("auto_summary", summary))
        return summary
    summary["text"] = summary_text(sent, ready, need, check=check, could_not=could_not)
    prefs = _prefs(state.load())
    if channel is not None and not paused and prefs.get("app_updates", True):
        summary["telegram"] = _try_send(channel, summary_text(sent, ready, need, telegram=True, check=check,
                                                              could_not=could_not)) == "sent"
    state.update(lambda x: x.__setitem__("auto_summary", summary))
    return summary


def inbox(state) -> dict:
    """What the app shows: the unseen in-app notices and today's (or the last) summary."""
    d = state.load()
    return {"notices": [r for r in (d.get("auto_inapp") or []) if isinstance(r, dict) and not r.get("seen")],
            "summary": d.get("auto_summary") if isinstance(d.get("auto_summary"), dict) else None}


def mark_seen(state, ids=None, summary: bool = False) -> None:
    def fn(d):
        want = set(ids or [])
        for r in d.get("auto_inapp") or []:
            if isinstance(r, dict) and r.get("id") in want:
                r["seen"] = True
        if summary and isinstance(d.get("auto_summary"), dict):
            d["auto_summary"]["seen"] = True
    state.update(fn)
