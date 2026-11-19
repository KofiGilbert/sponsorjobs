"""The three apply modes (docs/apply-agent.md section 26; CLAUDE.md section 7).

One saved choice (ui/app.py ``_apply_mode``, its own small file), made in Apply setup ("How to apply") and
on the Auto-apply page:

    auto      "I'm away".   Works through the approved queue in the background, nothing shown.
                            Sends by itself ONLY on the verified allowlist (submit/allowlist.py);
                            everywhere else it fills the form and leaves it at Ready to send.
                            Never asks during the day. One notice per send, one daily summary.
    assisted  "I'm here".   Visible in the Browser tab; the person presses Send. No Telegram asks.
    hybrid    "I'm around". Like assisted, and the end-of-form questions also go to Telegram.

Pure logic, no Flask or files: ui/app.py stores the choice and reads these back, so the rule
"who may press Submit" lives in one tested place. Nothing here can add a site to the AUTO lane:
``may_send_alone`` only ever narrows what the allowlist and the person's opt-in already allow.
"""
from __future__ import annotations

AUTO, ASSISTED, HYBRID = "auto", "assisted", "hybrid"
MODES = (AUTO, ASSISTED, HYBRID)


def migrate(saved, autonomous_was_on: bool) -> str:
    """The mode for a prefs file: the saved one, else the old Autonomous submit switch
    (on -> auto, off -> assisted)."""
    if saved in MODES:
        return saved
    return AUTO if autonomous_was_on else ASSISTED


def clamp_threshold(value, default: int = 0) -> int:
    """"Only send by itself when the match is at least X%". 0 means off (every allowlisted
    site); otherwise 1 to 100."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, n))


def may_send_alone(mode: str, lane: str, match, threshold=0, *, drafted: bool = False) -> bool:
    """May the agent press the site's Submit with nobody there? Only in Auto, only on a site the
    verified allowlist put in the auto lane, and, with a threshold set, only when the match is
    known and at least that high. Everything else waits at Ready to send for the person.

    (2026-11-18) ``drafted``: the application holds a written answer the agent drafted for an
    open-ended question (autoapply/drafts.py). It is the person's to read first: never sent
    alone, on any site."""
    if drafted or mode != AUTO or lane != "auto":
        return False
    t = clamp_threshold(threshold)
    if not t:
        return True
    try:
        return match is not None and float(match) >= t
    except (TypeError, ValueError):
        return False


def telegram_asks(mode: str) -> bool:
    """Do the apply agent's questions also go to Telegram? Only in Hybrid."""
    return mode == HYBRID


def hidden(mode: str) -> bool:
    """Does the background queue run out of sight? Only in Auto."""
    return mode == AUTO


def auto_sites() -> list[str]:
    """The names of the systems the agent can send on by itself today, from the verified
    allowlist. Shown as is: never a made-up number."""
    from submit.allowlist import REGISTRY
    return sorted({e.ats for e in REGISTRY if e.auto_eligible})
