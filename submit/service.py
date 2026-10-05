"""Route a finished application to submission by the per-site policy (CLAUDE.md §7).

Two lanes only: AUTO (submit unattended, for sites verified in ``submit.allowlist``) and
ASSISTED (pre-fill, the person clicks submit) for everything else and by default.

`classify_record` says what would happen (and carries the evidence for why); `submit_record`
does it. The unattended submission is a pluggable **driver** so routing is fully testable
offline. With no driver wired, an AUTO record honestly reports it's eligible-but-waiting
rather than pretending to submit. If a driver hits a captcha / auth wall it signals
``needs_assist`` and we drop to ASSISTED — we never evade bot detection.
"""

from __future__ import annotations

from submit.allowlist import evidence_for
from submit.policy import _host, submission_policy

LANE_LABEL = {
    "auto": "Auto-submit (verified official submission API)",
    "assisted": "Assisted (you review and click submit)",
}


def _job_url(record_data: dict) -> str:
    src = record_data.get("source_job") or {}
    return str(src.get("url") or "").strip()


def classify_record(record_data: dict) -> dict:
    """Given a record's persisted data bag, return how it would be submitted:
    {tier, label, url, can_auto, evidence}. No side effects.

    ``evidence`` (when present) explains the decision from the shipped allowlist — e.g.
    for an assisted ATS, that an official API exists but needs the employer's key."""
    url = _job_url(record_data)
    tier = submission_policy(url)
    ev = evidence_for(_host(url)) if url else None
    return {
        "tier": tier,
        "label": LANE_LABEL[tier],
        "url": url,
        "can_auto": tier == "auto" and bool(url),
        "evidence": None if ev is None else {
            "ats": ev.ats, "doc_url": ev.doc_url, "auth_model": ev.auth_model,
            "applicant_usable": ev.applicant_usable, "reason": ev.reason,
        },
    }


def submit_record(record_data: dict, driver=None, now: str = "") -> dict:
    """Attempt submission per policy and return a result dict describing what happened
    (and how the record's status should be updated). Never raises for a normal outcome.

    `driver(url, record_data) -> {"ok": bool, "detail": str, "needs_assist": bool}` performs
    the real unattended submission for an AUTO site; injected so this stays testable offline.
    A driver that returns ``needs_assist`` (captcha/auth wall) drops the record to assisted.
    """
    plan = classify_record(record_data)
    tier, url = plan["tier"], plan["url"]

    if tier == "assisted":
        return _assisted("This site isn't a verified auto-submit site. Fill it with the "
                         "extension and click submit yourself.")

    # tier == "auto"
    if not url:
        return {"ok": False, "tier": tier, "status": "auto_no_url",
                "message": "Auto-submit eligible, but this application has no source URL to "
                           "submit to. Build it from a sourced job.", "submitted_at": None}
    if driver is None:
        return {"ok": False, "tier": tier, "status": "auto_pending",
                "message": "Auto-submit eligible. Turn on autonomous submission (or the "
                           "submit driver isn't wired here) so Tailor can submit it for you.",
                "submitted_at": None}
    try:
        res = driver(url, record_data) or {}
    except Exception as exc:   # a driver failure must never crash the queue
        return {"ok": False, "tier": tier, "status": "auto_failed", "posted": True,
                "message": f"Auto-submit tried but failed: {exc}", "submitted_at": None}
    # `posted`: did the driver actually hit the site's API? The caller advances the min-gap
    # spacing clock on any posted attempt (even a failure), so failures can't hammer the API.
    posted = bool(res.get("posted"))
    if res.get("needs_assist"):
        # A captcha / auth wall — the site is telling us it doesn't want a bot. Drop to assisted.
        out = _assisted(res.get("detail") or "Site needs a human step, so fill it and click submit.")
        out["posted"] = posted
        return out
    if res.get("ok"):
        return {"ok": True, "tier": tier, "status": "auto_submitted", "posted": posted,
                "message": res.get("detail") or "Submitted automatically.",
                "submitted_at": now or None}
    return {"ok": False, "tier": tier, "status": "auto_failed", "posted": posted,
            "message": res.get("detail") or "Auto-submit did not complete.",
            "submitted_at": None}


def _assisted(message: str) -> dict:
    return {"ok": True, "tier": "assisted", "status": "assisted",
            "message": message, "submitted_at": None}
