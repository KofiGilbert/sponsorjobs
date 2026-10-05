"""An HONEST pre-send review of a tailored resume -- the last check before the person exports or
applies. It is DELIBERATELY deterministic (no model call): fast, free, and testable, matching the
same philosophy as the jobs-board fit score. Its headline job is catching the tailor's OWN drift --
any JD skill the finished resume now claims that the person's profile cannot actually back, which is
exactly the claim that unravels in an interview. Around that it surfaces easy wins (skills they have
but did not show), honest gaps (skills the job wants that they lack, and must NOT invent), and
whether the page fits. The output is a verdict plus a categorized checklist the UI renders.
"""
from __future__ import annotations

from tailoring.keywords import profile_text, term_present


def review_resume(profile: dict, coverage: dict, status: str = "clean") -> dict:
    """Return an honest pre-send review of the tailored resume.

    ``coverage`` is the session's coverage dict: ``present`` (terms shown on the resume),
    ``missing_supported`` (in the profile but not surfaced) and ``missing`` (wanted by the JD,
    absent from the profile). ``status`` is the assembler's page verdict ("clean"/"overflow"/
    "underfull"). Deterministic, so it is unit-testable and never changes wording run-to-run.
    """
    prof = profile_text(profile or {})
    present = list(coverage.get("present") or [])
    missing_supported = list(coverage.get("missing_supported") or [])
    missing_unsupported = list(coverage.get("missing") or coverage.get("missing_unsupported") or [])

    # THE headline check. A term is in ``present`` because it appears on the resume; if the profile
    # cannot back it, the tailor introduced a claim the person cannot defend. Whole-token match, so
    # this is conservative -- it fires rarely, and when it does it is worth a look before sending.
    unverified = [t for t in present if not term_present(t, prof)]

    checks: list[dict] = []
    checks.append({
        "id": "honesty",
        "level": "flag" if unverified else "pass",
        "label": ("A claim on your resume that your profile does not back" if unverified
                  else "Every skill shown is backed by your own profile"),
        "items": unverified[:12],
    })
    if missing_supported:
        checks.append({
            "id": "opportunities",
            "level": "info",
            "label": "You already have these, worth surfacing before you send",
            "items": missing_supported[:12],
        })
    if missing_unsupported:
        checks.append({
            "id": "gaps",
            "level": "info",
            "label": "The job asks for these and your profile lacks them, do not invent them",
            "items": missing_unsupported[:12],
        })
    page_ok = status == "clean"
    checks.append({
        "id": "length",
        "level": "pass" if page_ok else "warn",
        "label": ("Fits one clean page" if page_ok else
                  "Runs long, trim it to one page" if status == "overflow" else
                  "Looks sparse, there is room to add more"),
        "items": [],
    })

    # Verdict, honesty first: an unverifiable claim is the ONLY thing that holds you back (it is the
    # one that costs an interview). Layout is advisory; opportunities and gaps are just guidance.
    if unverified:
        verdict = "check"
        headline = "One thing to fix first: a claim your profile does not back."
    elif not page_ok:
        verdict = "review"
        headline = "Reads honest. One layout tweak is worth a look."
    else:
        verdict = "ready"
        headline = "Honest and tight. Ready to send."

    return {
        "verdict": verdict,
        "headline": headline,
        "checks": checks,
        "unverified_count": len(unverified),
    }
