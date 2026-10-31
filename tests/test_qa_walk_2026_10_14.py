"""Fixes from the 2026-10-14 whole-app QA walk (brand-new and returning user, 1893 and 1440 wide,
light and dark). Each test pins one bug that walk found. Offline."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
LOCALES = {p.stem: json.loads(p.read_text(encoding="utf-8"))
           for p in (ROOT / "ui" / "static" / "locales").glob("*.json")}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


# --- US detection: Workday's "City, ST,IN, India" read as Indiana ----------------------------

@pytest.mark.parametrize("loc", ["Mumbai, MH,IN, India", "Noida, UP,IN, India",
                                 "Noida, IN-UP, India", "Hyderabad, TS,IN, India",
                                 "Bengaluru, KA,IN, India", "Remote - India", "Toronto, ON, Canada"])
def test_a_place_ending_in_a_foreign_country_is_not_us(loc):
    from sourcing.sponsors import looks_us
    assert not looks_us(loc)


@pytest.mark.parametrize("loc", ["Indianapolis, IN", "Redmond, WA,US, US", "Cambridge, MA",
                                 "Paris, TX", "Pune, India; Austin, TX",
                                 "New York, New York, US; Remote - Ohio, USA"])
def test_us_places_still_read_as_us(loc):
    from sourcing.sponsors import looks_us
    assert looks_us(loc)


# --- dates: SQLite stamps are UTC, the person lives in local time ----------------------------

def test_local_day_converts_a_utc_stamp_to_the_local_calendar_day(client):
    A, _ = client
    stamp = "2026-10-15 02:40:00"                       # an evening in the Americas, stored in UTC
    want = datetime(2026, 10, 15, 2, 40, tzinfo=timezone.utc).astimezone().date().isoformat()
    assert A._local_day(stamp) == want
    assert A._local_stamp(stamp).startswith(want)
    assert A._local_day("") == "" and A._local_day(None) == ""
    assert A._local_day("not a date") == "not a date"[:10]


# --- interview setup: a role saved from the feed brings its description ----------------------

def test_prefill_reads_a_feed_roles_description_from_the_feed(client, monkeypatch):
    A, c = client
    w = A._watchlist()
    try:
        w.track_job("freehire::x", "applied", now="2999-01-01T09:00:00+00:00",
                    job={"source_id": "freehire::x", "source": "freehire", "company": "Lucid",
                         "title": "Mobile Service Technician"})          # slim row: no jd_text
    finally:
        w.close()
    monkeypatch.setattr(A, "_feed_serving", lambda: True)
    monkeypatch.setattr(A, "_feed_job_with_jd",
                        lambda sid: {"source_id": sid, "jd_text": "Repair vehicles in the field."})
    d = c.get("/api/preps/prefill").get_json()
    assert d["role"] == "Mobile Service Technician"
    assert d["jd"] == "Repair vehicles in the field."


# --- copy: resume (never CV) in English, no developer words, back links say where they go -----

def test_no_cv_or_developer_words_in_english_ui():
    for text in (HTML, APP_JS):
        assert "CV-worthy" not in text and "on your CV" not in text
    assert "git-ignored" not in HTML
    assert "picture of your CV" not in HTML and "picture of your CV" not in LOCALES["en"]["notify.contactDesc"]


def test_back_links_name_the_page_the_rail_names():
    # The rail labels the first page Home; the back links said "Dashboard" / "Saved Resumes".
    assert '<span id="pkgBackLbl">Dashboard</span>' not in HTML
    assert "Saved Resumes" not in APP_JS
    assert '? "Dashboard" : "Resumes"' not in APP_JS
    # (Phase 6) Your answers is part of the memory page (My profile), so a door from the page
    # itself shows no back link; the other doors name where they came from.
    assert 'origin: origin === "profile" ? "" : (origin || "autoapply")' in APP_JS
    assert 'autoapply: ["nav.autoapply", "Auto-apply"], package: ["ans.backApp", "Application"]' in APP_JS
    assert all("ans.backProfile" not in loc for loc in LOCALES.values())


def test_chat_avatar_is_the_product_initial():
    assert '<div class="msg-avatar">${role === "agent" ? "S" : "Y"}</div>' in APP_JS
    assert '<div class="msg-avatar">T</div>' not in APP_JS


# --- states and numbers that were wrong ------------------------------------------------------

def test_empty_saved_list_says_nothing_is_saved_not_no_filter_match():
    assert "No saved jobs yet. Press Save on a role to keep it here." in APP_JS
    assert "function jfNarrowed()" in APP_JS


def test_up_next_counts_roles_not_matches_and_never_says_lower_the_lowest_match():
    for lang, loc in LOCALES.items():
        assert "aa.nextNoneMin" in loc, lang
    assert LOCALES["en"]["aa.nextOf"] == "{n} of {m} roles"
    assert "aaMinFitFloor()" in APP_JS


def test_unknown_package_count_is_not_shown_as_zero():
    assert "known = onPass || s.packages_left != null" in APP_JS
    assert 'String(s.packages_left ?? 0));\n    }' not in APP_JS


def test_bare_united_states_location_is_named_not_blank():
    # usLocation (shared with Up next) names a bare US location instead of returning blank;
    # tests/test_us_location.py runs it on real spellings.
    assert 'if (!t) return "United States";' in APP_JS


def test_package_visa_badges_use_the_readable_labels():
    assert "${esc(VISA_LABELS[v.code] || v.code)}</span>`).join(\"\");" in APP_JS


# --- layout + navigation ----------------------------------------------------------------------

def test_saved_star_keeps_clear_of_the_freshness_label():
    assert ".feed-row.is-saved .fr-top { padding-right: 14px; }" in CSS


def test_profile_summary_box_is_full_width():
    assert ".pe-card > .pe-bullets { display: block; width: 100%;" in CSS


def test_my_profile_unlights_the_nav_and_visa_change_opens_your_answers():
    assert '$("#menuProfile")?.addEventListener("click", () => { closeAcctMenu(); openProfilePage(); });' in APP_JS
    assert "function openProfilePage() { return openMemory({}); }" in APP_JS   # the one door the menu goes through
    i = APP_JS.index("async function openMemory(")
    assert 'classList.remove("is-active")' in APP_JS[i:i + 1200]
    assert '$("#aaPfVisaChange")?.addEventListener("click", () => openAnswers("autoapply"));' in APP_JS
