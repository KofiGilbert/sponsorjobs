"""Where one application stands, in words that are true (CLAUDE.md §7).

A tailored application moves through four stages, and every screen (Review, Ready to apply,
the Tracker) reads them from here so they never disagree:

    queued     tailored, waiting for the person to review it
    approved   reviewed, not submitted yet: either waiting for the person's click on the site,
               or queued for an auto-submit on a verified auto site
    submitted  sent, with HOW: by the auto lane ("auto") or by the person ("you")
    failed     an auto-submit attempt did not go through. It goes back to the person as
               "Needs you", with a reason category they can act on

plus ``skipped`` (the person passed on it). Pure logic, no Flask or DB: ui/app.py stores the
patches this module builds on the record's data bag, and reads ``app_stage`` back.
"""

from __future__ import annotations

STAGES = ("queued", "approved", "submitted", "failed", "skipped")

# Failure reason categories. Each says what the person has to do next, not what broke inside.
REASONS = {
    "posting_closed": "The posting is closed",
    "form_blocker": "The form asks for something only you can give",
    "needs_signin": "The site wants a sign-in or verification",
    "site_error": "The site had an error",
}
# What to do next, per reason. A closed posting has nothing left to do.
NEXT = {
    "posting_closed": "",
    "form_blocker": " Open it to finish.",
    "needs_signin": " Open it to finish.",
    "site_error": " Open it to submit it yourself.",
}

STAGE_LABEL = {
    "queued": "To review",
    "approved": "Approved",
    "submitted": "Submitted",
    "failed": "Needs you",
    "skipped": "Skipped",
}


def _sub(data: dict) -> dict:
    s = (data or {}).get("submission")
    return s if isinstance(s, dict) else {}


def app_stage(data: dict | None) -> dict:
    """The stage of one record and a one-line, true description of it.

    Returns {stage, label, detail, how, reason}. ``how`` is "auto" / "you" for a submitted
    application (else ""), ``reason`` a REASONS key for a failed one (else "")."""
    data = data or {}
    status = data.get("status") or "ready"
    sub = _sub(data)
    sst = sub.get("status") or ""
    if status == "applied" or data.get("stage") in ("applied", "interviewing", "offer", "rejected"):
        how = sub.get("by") or ("auto" if sst == "auto_submitted" else "you")
        detail = "Submitted for you" if how == "auto" else "You submitted it"
        return _out("submitted", detail, how=how)
    if status == "skipped":
        return _out("skipped", "You skipped it")
    if status == "approved" and sst in ("failed", "auto_failed"):
        reason = sub.get("reason") if sub.get("reason") in REASONS else "site_error"
        return _out("failed", f"{REASONS[reason]}.{NEXT[reason]}", reason=reason)
    if status == "approved":
        if data.get("queued") or sst == "queued":
            return _out("approved", "Queued to submit for you")
        return _out("approved", "Waiting for you to submit it on the site")
    return _out("queued", "Waiting for your review")


def _out(stage: str, detail: str, how: str = "", reason: str = "") -> dict:
    return {"stage": stage, "label": STAGE_LABEL[stage], "detail": detail,
            "how": how, "reason": reason}


def is_waiting_to_submit(data: dict | None) -> bool:
    """Approved but not submitted (including a failed auto attempt handed back): the
    Ready to apply list."""
    return app_stage(data)["stage"] in ("approved", "failed")


def approved_patch() -> dict:
    """An assisted-lane approval. Nothing was filled or sent: the person submits on the site."""
    return {"status": "approved", "queued": False,
            "submission": {"tier": "assisted", "status": "approved",
                           "message": "Approved. Open the application and submit it."}}


def queued_patch(reason: str) -> dict:
    """An auto-lane approval held back by the daily cap or spacing."""
    return {"status": "approved", "queued": True,
            "submission": {"tier": "auto", "status": "queued",
                           "message": f"Approved. Queued to submit for you ({reason})."}}


def failed_patch(result: dict) -> dict:
    """An auto-submit attempt that did not go through: back to the person, with why."""
    reason = result.get("reason") if result.get("reason") in REASONS else "site_error"
    return {"status": "approved", "queued": False,
            "submission": {"tier": "auto", "status": "failed", "reason": reason,
                           "message": result.get("message") or REASONS[reason]}}


def submitted_by(how: str, tier: str = "", message: str = "", at: str | None = None) -> dict:
    """The ``submission`` part of a record once it was sent, and by whom."""
    msg = message or ("Submitted for you." if how == "auto" else "You submitted it.")
    return {"submission": {"tier": tier or ("auto" if how == "auto" else "assisted"),
                           "status": "auto_submitted" if how == "auto" else "submitted",
                           "by": how, "message": msg, "at": at}}


# --------------------------------------------------------------------------- Auto-apply tiles
# The Auto-apply page groups every application into four tiles. Each is a stage above, named
# for what the person does next. Skipped applications are on no tile.
TILES = ("review", "waiting", "submitted", "needs")
TILE_OF_STAGE = {"queued": "review", "approved": "waiting", "submitted": "submitted",
                 "failed": "needs"}

# Short reason words for the "Needs you" tile ("1 sign-in, 1 form question").
REASON_SHORT = {
    "needs_signin": ("sign-in", "sign-ins"),
    "form_blocker": ("form question", "form questions"),
    "site_error": ("site error", "site errors"),
    "posting_closed": ("closed posting", "closed postings"),
    "paused": ("paused application", "paused applications"),   # the apply agent parked it (ui/app.py)
}

# The one row action per tile (and per reason on "Needs you"). The page turns the code into
# the button label; the server decides it so the row and the drawer never disagree.
ROW_ACTION = {"review": "review", "waiting": "fill", "submitted": "view"}
NEEDS_ACTION = {"needs_signin": "signin", "form_blocker": "finish", "site_error": "finish",
                "posting_closed": "view"}


def tile_of(data: dict | None) -> str | None:
    """Which Auto-apply tile one record sits on, or None for a skipped one."""
    return TILE_OF_STAGE.get(app_stage(data)["stage"])


def row_action(tile: str, reason: str = "") -> str:
    if tile == "needs":
        return NEEDS_ACTION.get(reason, "finish")
    return ROW_ACTION.get(tile, "view")


def needs_summary(reasons: list[str]) -> str:
    """'1 sign-in, 1 form question' from the reasons of the Needs you items, most first."""
    counts: dict[str, int] = {}
    for r in reasons:
        key = r if r in REASON_SHORT else "site_error"
        counts[key] = counts.get(key, 0) + 1
    order = sorted(counts, key=lambda k: (-counts[k], list(REASON_SHORT).index(k)))
    return ", ".join(f"{counts[k]} {REASON_SHORT[k][0 if counts[k] == 1 else 1]}" for k in order)


def default_tile(counts: dict) -> str:
    """The tile the page opens on: the first one with something to do (Waiting on you, then To
    review, then Needs you), else Submitted when there is history, else To review."""
    for t in ("waiting", "review", "needs", "submitted"):
        if counts.get(t):
            return t
    return "review"
