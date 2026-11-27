"""The Orb's pure rules: switches, nudge pacing and the chat limits.

Every function takes ``now`` (and the data) as arguments, so a test passes its own clock.

Owner rules (2026-11-26):
* Nudges come only from real data, one concrete thing each (a follow-up due, a resume ready, an
  interview booked, new matches on a saved search, a saved fact that went stale).
* At most 3 unasked bubbles a day and one per 30 minutes; never while the person types or while
  Autofill runs; none in quiet hours.
* Dismissing the same kind twice turns that kind off for 7 days.
* The AI suggests, the person clicks: a nudge only ever carries a button, never an action.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

MAX_PER_DAY = 3                 # unasked bubbles a day
MIN_GAP = timedelta(minutes=30)
SNOOZE_AFTER = 2                # dismissals of one kind in a row
SNOOZE_FOR = timedelta(days=7)

# kind -> priority (higher first), repeat window (None: never again for the same key), lifetime.
# (2026-11-27, QA) Every nudge is ONE concrete thing with ONE action ("Follow up with Acme
# (applied Nov 23)" [Draft email]), never a total ("40 things wait on you"): the totals were
# vague and repeated the lines above them, so the "daily" summary kind is gone.
KINDS: dict[str, dict] = {
    "followup": {"priority": 80, "repeat": timedelta(days=3), "ttl": timedelta(days=14)},
    "practice": {"priority": 78, "repeat": timedelta(days=3), "ttl": timedelta(days=14)},
    "ready":    {"priority": 75, "repeat": timedelta(days=2), "ttl": timedelta(days=10)},
    # (2026-11-26) the daily sweep: a fill over a day old with no sign it was sent, asked once
    "sent":     {"priority": 72, "repeat": None, "ttl": timedelta(days=2)},
    "facts":    {"priority": 65, "repeat": timedelta(days=7), "ttl": timedelta(days=30)},
    "matches":  {"priority": 50, "repeat": None, "ttl": timedelta(days=3)},
}
WAITING_TOP = 3                 # the chat's opening lists this many, then "and N more"

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
    return iso(now + KINDS.get(kind, {"ttl": timedelta(days=1)})["ttl"])


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


def rank(rows: list[dict]) -> list[dict]:
    """What waits, most important first, ONE KIND AT A TIME: the best of each kind by the kinds'
    priority, then the second of each, and so on. Fifteen follow-ups never push the interview or
    the ready resume out of the top three. Within a kind the rows keep their order (the data's
    own: the oldest follow-up first, the best fit first)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get("type") or ""), []).append(r)
    kinds = sorted(groups, key=lambda k: -int((KINDS.get(k) or {}).get("priority")
                                               or max([int(x.get("priority") or 0) for x in groups[k]] or [0])))
    out: list[dict] = []
    depth = 0
    while len(out) < len(rows):
        for k in kinds:
            if depth < len(groups[k]):
                out.append(groups[k][depth])
        depth += 1
    return out


def pick(rows: list[dict], *, snoozed: set[str], shown_kinds: dict | None = None) -> dict | None:
    """The pending nudge to show: a kind not shown yet today first (so one busy kind never takes
    all three bubbles), then the highest priority, then the data's own order."""
    live = [r for r in rows if r.get("status") == "pending" and r.get("type") not in snoozed]
    if not live:
        return None
    seen = shown_kinds or {}
    live.sort(key=lambda r: (int(seen.get(r.get("type"), 0)), -int(r.get("priority") or 0),
                             str(r.get("created_at") or ""), int(r.get("id") or 0)))
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


# (2026-11-27, QA) The Orb's own questions, typed or pressed, answered from the person's data on
# this computer with no AI call: what waits, which jobs to apply to, interview practice, and how
# the assistant works. Anything else goes to the assistant as before.
_Q = (
    ("today", re.compile(r"\b(which|what) (jobs?|roles?|applications?)\b.*\bapply\b|\bapply (to|for) today\b|"
                         r"\bwhere should i apply\b|\bwhat should i apply\b")),
    ("practice", re.compile(r"\bpractic[es]\b.*\binterview|\binterview (practice|prep)\b|\bmock interview\b|"
                            r"\bprepare for (my|the|an) (next )?interview\b")),
    ("waiting", re.compile(r"\bwhat('?s| is| are)? (waiting|left|pending)\b|\bwhat waits\b|\bwaiting (on|for) me\b|"
                           r"\bwhat (do|should) i (need to )?do (next|today|now)\b|\bmy to ?do\b")),
    ("how", re.compile(r"\bhow do(es)? (you|this|the assistant) work\b|\bwhat can you do\b|\bwho are you\b|"
                       r"\bwhat are you\b|\bhow do i use you\b")),
)


def orb_question(text: str) -> str:
    """"waiting" | "today" | "practice" | "how" for one of the Orb's own questions, else ""."""
    t = " ".join(str(text or "").lower().replace("\u2019", "'").split()).strip(" ?.!")
    if not t or len(t) > 120:
        return ""
    for kind, rx in _Q:
        if rx.search(t):
            return kind
    return ""


HOW_TEXT = ("I am your job search helper. I work from what is in SponsorJobs on this computer: your "
            "resumes, your Tracker and your profile. When you press Autofill, I fill the form and point "
            "at anything I am not sure of. I never press Submit, you do. I nudge you at most 3 times a "
            "day, and only about real things. Right click me to mute me or turn me off.")
