"""In-list (search-results) sponsor badging beyond LinkedIn/Indeed (2026-08-03).

FrogHire badges search results on many boards; we started with LinkedIn + Indeed. This adds
ZipRecruiter, whose card structure was verified LIVE with the Playwright browser on a real
results page (www.ziprecruiter.com/jobs-search): 40/40 cards are <article id="job-card--...">,
the employer is an /co/ link, the title an <h2>, and a simulated badge row rendered in the
correct spot (between title and location) on the live page.

Glassdoor and Handshake are deliberately NOT added here: Glassdoor is Cloudflare-walled to
automated browsers (couldn't verify selectors) and Handshake needs a .edu login. Their
POSTINGS still badge via JSON-LD (manifest coverage), only their in-list search results wait
for on-site verification. Source-presence guards so the verified wiring can't silently regress.
"""

from __future__ import annotations

from pathlib import Path

CONTENT = Path("extension/content.js").read_text(encoding="utf-8")


def test_ziprecruiter_is_scanned_as_a_list_page():
    assert "ziprecruiter" in CONTENT
    # It participates in the in-list scan (isList gate), like LinkedIn and Indeed.
    assert "linkedin" in CONTENT and "indeed" in CONTENT


def test_ziprecruiter_cards_use_the_verified_stable_hooks():
    # The <article id> container and the /co/ company link, NOT hashed Tailwind class names.
    assert 'article[id^="job-card"]' in CONTENT
    assert 'a[href*="/co/"]' in CONTENT


def test_unverified_boards_are_not_claimed_for_in_list():
    # We only wire in-list for boards we could actually verify. If someone later adds Glassdoor
    # or Handshake to the in-list scan, they must verify selectors on-site first (update this).
    assert "glassdoor" not in CONTENT.lower()
    assert "handshake" not in CONTENT.lower()
