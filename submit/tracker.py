"""The application funnel + follow-up nudge (feature #3).

Pure, deterministic logic for tracking an application through the stages a job seeker actually
moves through -- saved -> applied -> interviewing -> offer / rejected -- and for reminding the
person to follow up a few days after they applied. There is no DB or Flask here, so it is exact and
unit-testable; ui/app.py wires these helpers into the records store and the dashboard.

The follow-up is honest and in-app only: it never contacts an employer, it just nudges YOU. It is
set when an application is freshly applied, and cleared once you are interviewing, have an offer,
or were rejected -- points where a "did they see it?" nudge no longer helps.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# The honest funnel. "saved" = tailored but not sent; the rest track what happened after.
STAGES = ("saved", "applied", "interviewing", "offer", "rejected")

# A polite, effective follow-up window: long enough not to pester, short enough to still matter.
FOLLOW_UP_DAYS = 6


def _date_of(iso: str | None) -> date | None:
    """Parse a stored ISO timestamp or date to a date; None if missing or unparseable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        try:
            return date.fromisoformat(str(iso)[:10])
        except ValueError:
            return None


def effective_stage(data: dict | None) -> str:
    """The funnel stage for a record. Older rows predate the tracker, so fall back to the legacy
    submit ``status`` (applied -> "applied", otherwise "saved") -- nothing needs a migration."""
    data = data or {}
    st = data.get("stage")
    if st in STAGES:
        return st
    return "applied" if data.get("status") == "applied" else "saved"


def stage_patch(data: dict | None, stage: str, now_iso: str) -> dict:
    """The data-bag patch to move an application to ``stage``.

    Keeps the submit-machinery ``status`` consistent (saved -> "ready" so it returns to the apply
    to-do; every later stage -> "applied" so it leaves the to-do) and manages the follow-up date:
    set it when freshly APPLIED, clear it once the person has progressed or the role is closed.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    data = data or {}
    patch: dict = {"stage": stage}
    if stage == "saved":
        patch["status"] = "ready"
        patch["applied_at"] = None
        patch["follow_up_due"] = None
    elif stage == "applied":
        patch["status"] = "applied"
        applied_at = data.get("applied_at") or now_iso
        patch["applied_at"] = applied_at
        d = _date_of(applied_at) or _date_of(now_iso)
        patch["follow_up_due"] = (d + timedelta(days=FOLLOW_UP_DAYS)).isoformat() if d else None
    else:  # interviewing / offer / rejected -- past the point a follow-up nudge helps
        patch["status"] = "applied"
        if not data.get("applied_at"):
            patch["applied_at"] = now_iso
        patch["follow_up_due"] = None
    return patch


def follow_up(data: dict | None, today: date) -> dict | None:
    """The follow-up read for one record: its due date and whether it is due yet. None when the
    application is not in a state that wants a nudge (not currently 'applied', or has no due date)."""
    data = data or {}
    if effective_stage(data) != "applied":
        return None
    d = _date_of(data.get("follow_up_due"))
    if not d:
        return None
    return {"due": d.isoformat(), "is_due": d <= today}
