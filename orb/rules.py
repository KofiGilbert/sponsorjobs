"""The Orb's pure rules: switches, nudge pacing and the chat limits.

Every function takes ``now`` (and the data) as arguments, so a test passes its own clock.

Owner rules (2026-11-26):
* Nudges come only from real data (a follow-up due, a resume ready, an interview booked, new
  matches on a saved search, a saved fact that went stale, the day's "things wait on you").
* At most 3 unasked bubbles a day and one per 30 minutes; never while the person types or while
  Autofill runs; none in quiet hours.
* Dismissing the same kind twice turns that kind off for 7 days.
* The AI suggests, the person clicks: a nudge only ever carries a button, never an action.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

MAX_PER_DAY = 3                 # unasked bubbles a day
MIN_GAP = timedelta(minutes=30)
SNOOZE_AFTER = 2                # dismissals of one kind in a row
SNOOZE_FOR = timedelta(days=7)

# kind -> priority (higher first), repeat window (None: never again for the same key), lifetime
KINDS: dict[str, dict] = {
    "ready":    {"priority": 80, "repeat": timedelta(days=2), "ttl": timedelta(days=10)},
    # (2026-11-26) the daily sweep: a fill over a day old with no sign it was sent, asked once
    "sent":     {"priority": 75, "repeat": None, "ttl": timedelta(days=2)},
    "followup": {"priority": 70, "repeat": timedelta(days=3), "ttl": timedelta(days=14)},
    "facts":    {"priority": 65, "repeat": timedelta(days=7), "ttl": timedelta(days=30)},
    "practice": {"priority": 60, "repeat": timedelta(days=3), "ttl": timedelta(days=14)},
    "matches":  {"priority": 50, "repeat": None, "ttl": timedelta(days=3)},
    "daily":    {"priority": 40, "repeat": None, "ttl": timedelta(days=1)},
}

# The chat: a per-hour message cap, then a daily spend cap on the model turns; past either the
# assistant still answers, from rules only (no model call).
CHAT_PER_HOUR = int(os.environ.get("SJ_ORB_CHAT_PER_HOUR") or 30)
CHAT_DAILY_CENTS = float(os.environ.get("SJ_ORB_CHAT_DAILY_CENTS") or 25)
CHAT_TURN_CENTS = 0.2           # one intent parse on the small model, rounded up

EVENT_KEEP = timedelta(days=180)
MEMORY_KEEP = 20                # chat summaries kept
CHAT_KEEP = 40                  # raw turns kept for the open chat


def flag(name: str, env=None, default: bool = True) -> bool:
    """An Orb switch from the environment: "0" turns it off, "1" on, anything else the default.
    SJ_ORB is the kill switch for the whole Orb; SJ_ORB_NUDGES for the unasked bubbles only."""
    raw = str((os.environ if env is None else env).get(name) or "").strip()
    if raw == "0":
        return False
    if raw == "1":
        return True
    return default


def enabled(*, env_on: bool, setting_on: bool, remote_on: bool) -> tuple[bool, str]:
    """Is the Orb on, and if not, why ("env" | "setting" | "remote")."""
    if not env_on:
        return False, "env"
    if not remote_on:
        return False, "remote"
    if not setting_on:
        return False, "setting"
    return True, ""


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    """The same quiet hours as the notifications (notify/alerts.py): start == end means none."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat()


def parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s)) if s else None
    except ValueError:
        return None


def expires_at(kind: str, now: datetime) -> str:
    return iso(now + KINDS.get(kind, KINDS["daily"])["ttl"])


def may_repeat(kind: str, shown_at, now: datetime) -> bool:
    """A nudge already shown comes back only after its kind's repeat window."""
    rep = KINDS.get(kind, {}).get("repeat")
    t = parse(shown_at)
    return bool(rep and t and now - t >= rep)


def may_show(*, now: datetime, shown_today: int, last_shown, typing: bool, autofill: bool,
             quiet: bool) -> tuple[bool, str]:
    """May ONE unasked bubble show now? (ok, why not)."""
    if typing:
        return False, "typing"
    if autofill:
        return False, "autofill"
    if quiet:
        return False, "quiet"
    if shown_today >= MAX_PER_DAY:
        return False, "cap"
    t = parse(last_shown)
    if t and now - t < MIN_GAP:
        return False, "gap"
    return True, ""


def pick(rows: list[dict], *, snoozed: set[str]) -> dict | None:
    """The pending nudge to show: highest priority first, then the oldest."""
    live = [r for r in rows if r.get("status") == "pending" and r.get("type") not in snoozed]
    if not live:
        return None
    live.sort(key=lambda r: (-int(r.get("priority") or 0), str(r.get("created_at") or ""), int(r.get("id") or 0)))
    return live[0]


def chat_mode(*, last_hour: int, spent_cents: float) -> str:
    """"ai" while under both caps, else "rules" (answers from the app's own data only)."""
    if last_hour >= CHAT_PER_HOUR or spent_cents >= CHAT_DAILY_CENTS:
        return "rules"
    return "ai"


def mute_command(text: str) -> str:
    """"mute" / "unmute" typed in the chat: handled on this computer, no AI call."""
    t = " ".join(str(text or "").lower().split()).strip(" .!")
    if t in ("mute", "mute sound", "mute sounds", "be quiet", "sound off"):
        return "mute"
    if t in ("unmute", "unmute sound", "unmute sounds", "sound on"):
        return "unmute"
    return ""
