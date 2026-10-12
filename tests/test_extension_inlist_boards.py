"""In-list (search-results) sponsor badging beyond LinkedIn/Indeed.

FrogHire badges search results on many boards; we started with LinkedIn + Indeed (2026-08-03:
ZipRecruiter, verified LIVE with the Playwright browser: 40/40 cards <article id="job-card--...">,
the employer an /co/ link, the title an <h2>).

0.5.0 (2026-10-11): every board is an adapter in extension/boards_core.js, and each adapter
carries its `evidence` string, the honest record of how its selectors were verified. Dice was
read from live server-rendered markup; Indeed, Glassdoor and ZipRecruiter answer curl with a bot
wall, Google with an "enable JavaScript" shell, Handshake with its login page, so those adapters
use documented hooks and say so. The per-adapter DOM tests live in tests/test_extension_boards.py;
this file guards the wiring and the honesty of the evidence.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BOARDS = Path("extension/boards_core.js").read_text(encoding="utf-8")
CONTENT = Path("extension/content.js").read_text(encoding="utf-8")


def _table() -> list[dict]:
    if not shutil.which("node"):
        pytest.skip("needs node")
    r = subprocess.run(["node", "-e",
                        "const B=require(process.argv[1]);console.log(JSON.stringify(B.BOARDS.map(b=>({id:b.id,name:b.name,"
                        "optional:!!b.optional,origins:b.origins,href:b.companyHref,evidence:b.evidence}))))",
                        str(Path("extension/boards_core.js").resolve())], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_board_is_an_adapter_and_linkedin_is_one_among_them():
    ids = [b["id"] for b in _table()]
    assert ids == ["linkedin", "indeed", "glassdoor", "handshake", "dice", "google", "ziprecruiter"]
    # content.js no longer branches on the host for list or posting logic: the adapter decides.
    assert "linkedinCardInfo" not in CONTENT and "linkedinHeader" not in CONTENT
    assert 'HOST.includes("linkedin' not in CONTENT and 'HOST.includes("indeed' not in CONTENT
    assert "BOARD.cards(document)" in CONTENT and "BOARD.cardInfo(card)" in CONTENT
    assert "BOARD.header(document)" in CONTENT and "BOARD.description(document)" in CONTENT


def test_ziprecruiter_cards_use_the_verified_stable_hooks():
    # The <article id> container and the /co/ company link, NOT hashed Tailwind class names.
    assert 'article[id^="job-card"]' in BOARDS
    assert 'a[href*="/co/"]' in BOARDS


def test_dice_uses_the_hooks_read_from_its_live_markup():
    for hook in ('[data-testid="job-card"]', '[data-testid="job-search-job-detail-link"]',
                 '[data-testid="job-card-company-name"]', '/company-profile/', '[data-testid="job-detail-header-card"]'):
        assert hook in BOARDS, hook


def test_each_adapter_says_how_it_was_verified_and_unverified_ones_admit_it():
    """A selector nobody has seen work is a guess; the adapter must say so in `evidence`, so the
    next person knows which boards still need a look in a real browser."""
    by = {b["id"]: b for b in _table()}
    for b in by.values():
        assert len(b["evidence"]) > 60, b["id"]
    assert "live" in by["dice"]["evidence"].lower() and "curl" in by["dice"]["evidence"]
    assert "verified live" in by["ziprecruiter"]["evidence"].lower()
    for gated in ("indeed", "glassdoor"):
        assert re.search(r"403|wall|challenge|captcha", by[gated]["evidence"], re.I), gated
    assert "login" in by["handshake"]["evidence"].lower() and "not verified live" in by["handshake"]["evidence"].lower()
    assert "javascript" in by["google"]["evidence"].lower()


def test_only_linkedin_is_always_on_and_the_rest_are_opt_in_boards():
    by = {b["id"]: b for b in _table()}
    assert by["linkedin"]["optional"] is False
    assert all(b["optional"] for i, b in by.items() if i != "linkedin")
