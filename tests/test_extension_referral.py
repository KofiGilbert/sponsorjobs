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


def test_the_content_script_offers_the_action_and_drafts_from_the_profile():
    assert "Ask for a referral" in CONTENT
    assert 'type: "referral"' in CONTENT
    # It sends the role + company so the message names the specific job, not a generic blast.
    assert "role: jobTitle()" in CONTENT and "company" in CONTENT


def test_it_points_the_person_to_people_but_does_not_scrape_or_send():
    # A people-search link (the person searches, picks, and messages themselves)...
    assert "linkedin.com/search/results/people" in CONTENT
    assert "Find people at" in CONTENT
    # ...and it says plainly that nothing is auto-sent.
    assert "Nothing is auto-sent" in CONTENT
    # Hard line: the extension must not itself send messages or read the person's contacts.
    for forbidden in ("/messaging/", "sendMessage(", "auto-send", "connections?"):
        # (sendMessage here would mean LinkedIn's messaging API; our own chrome runtime call
        # is chrome.runtime.sendMessage, which is different and allowed.)
        assert f"linkedin.com{forbidden}" not in CONTENT
