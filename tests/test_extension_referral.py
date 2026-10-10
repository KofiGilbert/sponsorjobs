"""Referrals in the extension must stay privacy-preserving (2026-08).

The whole point of doing referrals differently from the scrape-and-blast tools: we draft the
outreach message LOCALLY from the person's own profile, hand them a LinkedIn people-search
link, and let THEM choose who to contact and send it themselves. We never scrape contacts and
never auto-send.

These are source-presence + invariant checks (the behaviour is exercised deterministically in
test_drafting.py against the app endpoint). They guard against a future edit quietly removing
the wiring or crossing the privacy line the feature was built to hold.
"""

from __future__ import annotations

from pathlib import Path

CONTENT = Path("extension/content.js").read_text(encoding="utf-8")
BACKGROUND = Path("extension/background.js").read_text(encoding="utf-8")


def test_the_background_routes_referral_to_the_local_app_only():
    # It goes to the app's guarded referral endpoint (which requires the extension header),
    # so a web page can't invoke the model on the user's key.
    assert '"/api/profile/referral"' in BACKGROUND
    assert 'msg.type === "referral"' in BACKGROUND

# The in-page referral action was removed on 2026-10-10 (Kofi: not useful). The background
# route above stays so a future app-side feature can reuse it; nothing in the page offers it.
