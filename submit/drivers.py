"""Submission drivers for AUTO-lane sites (CLAUDE.md §7).

A driver performs the real, unattended submission for a verified auto-eligible site, using
the person's OWN reviewed material, identifying honestly, and respecting the site's rate
limits. Drivers exist ONLY for sites in ``submit.allowlist`` with ``auto_eligible`` True.

Hard safety rule: a driver NEVER evades bot detection. If it meets a captcha, a login
wall, or any anti-bot challenge, it aborts and signals ``needs_assist`` so the caller
drops the application to ASSISTED — the challenge is the site telling us it doesn't want a
bot here. The HTTP callable is injected so drivers are fully testable offline.

A driver returns ``{"ok": bool, "detail": str, "needs_assist": bool}``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _post_json(url: str, payload: dict) -> tuple[int, str]:  # pragma: no cover - network
    """Default transport: POST JSON, return (status, body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(getattr(resp, "status", 0) or resp.getcode()), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")


def _applicant_fields(record_data: dict) -> dict:
    """Pull the person's honest contact identity + materials from the record/profile."""
    ident = (record_data.get("profile") or {}).get("identity") or record_data.get("identity") or {}
    return {
        "name": ident.get("name") or "",
        "email": ident.get("email") or "",
        "phone": ident.get("phone") or "",
        "cover_letter": record_data.get("cover_letter") or "",
        "answers": [a.get("answer", "") for a in (record_data.get("screening") or [])],
    }


def recruitee_submit(url: str, record_data: dict, http=_post_json) -> dict:
    """Submit an application to a Recruitee offer via its keyless Careers Site API.

    Endpoint: ``POST https://{company}.recruitee.com/api/offers/{offer_slug}/candidates``
    (verified keyless, candidate-facing — see submit.allowlist). Builds the candidate from
    the person's honest identity. Aborts to assisted if required fields are missing or the
    site returns an auth/challenge response, rather than guessing or evading.
    """
    # ``posted`` tells the caller whether a real request hit the site's API — it advances the
    # min-gap spacing clock even on failure, so a run of failing items can't hammer the API.
    f = _applicant_fields(record_data)
    if not (f["name"] and f["email"]):
        return {"ok": False, "needs_assist": True, "posted": False,
                "detail": "Missing name/email to submit honestly, so handing to assisted."}

    api = _to_candidates_endpoint(url)
    if not api:
        return {"ok": False, "needs_assist": True, "posted": False,
                "detail": "Couldn't derive the Recruitee offer endpoint from the URL, so assisted."}

    candidate = {"candidate": {"name": f["name"], "email": f["email"], "phone": f["phone"],
                               "cover_letter": f["cover_letter"]}}
    if f["answers"]:
        candidate["candidate"]["open_question_answers"] = f["answers"]

    try:
        status, body = http(api, candidate)
    except Exception as exc:
        # A connection was attempted (the request left the machine), so space the next one.
        return {"ok": False, "needs_assist": True, "posted": True,
                "detail": f"Network error, handing to assisted: {exc}"}

    if status in (401, 403) or _looks_like_challenge(body):
        # An auth wall / captcha means the site doesn't want a bot here — never evade.
        return {"ok": False, "needs_assist": True, "posted": True,
                "detail": "Recruitee returned an auth/anti-bot challenge, so dropping to assisted."}
    if 200 <= status < 300:
        return {"ok": True, "needs_assist": False, "posted": True, "detail": "Submitted to Recruitee."}
    return {"ok": False, "needs_assist": True, "posted": True,
            "detail": f"Recruitee submission returned HTTP {status}, handing to assisted."}


def _to_candidates_endpoint(url: str) -> str:
    """Turn a Recruitee offer/careers URL into its Careers Site candidates endpoint.

    Recruitee careers URLs look like ``https://{company}.recruitee.com/o/{offer_slug}``;
    the sanctioned submit endpoint is ``/api/offers/{offer_slug}/candidates`` on the same host.
    """
    try:
        p = urlparse(url if "//" in (url or "") else "//" + (url or ""))
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    # Dot-boundary match (same rule as policy._matches / driver_for) so a lookalike like
    # "evilrecruitee.com" can never derive an endpoint — defense-in-depth, since policy
    # already gates this driver to genuine recruitee hosts before it's ever reached.
    if not (host == "recruitee.com" or host.endswith(".recruitee.com")):
        return ""
    # Offer slug is the last non-empty path segment (…/o/{slug} or …/{slug}).
    segs = [s for s in (p.path or "").split("/") if s and s not in ("o", "offers")]
    if not segs:
        return ""
    return f"https://{host}/api/offers/{segs[-1]}/candidates"


def _looks_like_challenge(body: str) -> bool:
    low = (body or "").lower()
    return any(w in low for w in ("captcha", "recaptcha", "hcaptcha", "are you human",
                                  "verify you are", "challenge", "cloudflare"))


# Per-host driver registry — a host gets a driver ONLY if it's auto-eligible in the
# allowlist. This dict never grows at runtime by inference; adding one is a code change.
_DRIVERS = {
    "recruitee.com": recruitee_submit,
}


def driver_for(host: str):
    """The submission driver for a host (exact or sub-domain), or None if none is wired."""
    host = (host or "").lower()
    for suffix, drv in _DRIVERS.items():
        if host == suffix or host.endswith("." + suffix):
            return drv
    return None
