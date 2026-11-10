"""The job is the unit (2026-11-10): Home, Jobs, Resumes and Tracker open the same job workspace
(the builder with that job's resume session restored). Static checks on the UI code; the API side
is tests/test_builder_sessions.py and the screens are verified in a browser."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
LOCALES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "ui" / "static" / "locales").glob("*.json")}


def _fn(name: str) -> str:
    return JS.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def test_tracker_has_an_optional_in_progress_column_between_saved_and_approved():
    cols = JS.split("const TRK_COLS = [", 1)[1].split("];", 1)[0]
    keys = re.findall(r'key: "(\w+)"', cols)
    assert keys[:3] == ["saved", "in_progress", "approved"]
    line = next(l for l in cols.splitlines() if 'key: "in_progress"' in l)
    assert "optional: true" in line
    assert '"in_progress"' in JS.split("const TRK_STAGES", 1)[1].split(";", 1)[0]


def test_a_tracker_card_with_a_session_shows_its_resume_and_opens_the_workspace():
    card = _fn("trkCardHtml")
    assert "sessScore(sess)" in card and "tracker.chatAgo" in card and "tracker.postingClosed" in card
    assert "data-session" in _fn("trkCardShell")
    board = _fn("renderTrackerBoard")
    assert "openJobWorkspace(card.dataset.sid)" in board and "data-trk-chat" in board


def test_jobs_detail_offers_open_your_resume_and_says_when_you_tailored():
    assert "jobs.openResume" in _fn("fdMainBtn") and "data-fd-open" in _fn("fdMainBtn")
    mine = _fn("fdMineLine")
    assert "jobs.mineLine" in mine and "jobs.notSent" in mine and "jobs.sentOn" in mine
    assert "fd-mine-dot" in mine
    assert "jobs.mineChip" in JS and "fr-mine" in JS                      # the list card's chip


def test_a_resume_row_opens_its_workspace_and_download_is_a_hover_action():
    assert "openSessionWorkspace(rec.session_id)" in _fn("openCvRow")
    assert "openCvRow(card.dataset.id)" in _fn("wireCvManager")
    row = _fn("cvCardHTML")
    assert 'data-act="download"' in row and "cv-chat-ic" in row and "cvs.appliedOn" in row


def test_home_continue_link_sits_beside_see_examples_and_lists_like_it():
    foot = HTML.split('class="home-foot-links"', 1)[1].split("</span>", 1)[0]
    assert 'id="homeExamples"' in foot and 'id="homeContinue"' in foot
    assert 'id="homeContinueList"' in HTML and 'class="home-suggest home-continue"' in HTML
    load = _fn("loadHomeContinue")
    assert "btn.hidden = !n" in load                                     # hidden at 0
    for key in ("home.continue", "home.cInProgress", "home.cRecent", "home.cAll"):
        assert key in load
    assert "home-suggest-l" in load and "home-suggest-row" in load       # the examples list's own style
    keys = _fn("wireHomeContinue")
    for k in ("ArrowDown", "ArrowUp", "Escape", "Enter"):
        assert k in keys
    # The Today strip is not moved: it is still right after the prompt block.
    assert HTML.index('id="homeContinueList"') < HTML.index('id="homeTodayLbl"')


def test_a_closed_jobs_chat_shows_the_note_and_hides_apply():
    note = _fn("renderClosedNote")
    assert "chat.closedTitle" in note and "chat.reuse" in note and "chat.download" in note
    assert "n.closed_at" in note                                         # the date from the job's record
    assert "if (st.expired) renderClosedNote(st)" in JS
    assert '$("#acceptBtn").hidden = Boolean(st.expired)' in JS


def test_every_new_label_is_in_all_five_languages_and_never_uses_resume_as_a_verb():
    keys = ["home.continue", "home.cInProgress", "home.cRecent", "home.cAll", "home.todayNew",
            "tracker.stage.in_progress", "tracker.hintInProgress", "tracker.postingClosed",
            "jobs.openResume", "jobs.mineLine", "jobs.mineChip", "cvs.download", "chat.closedTitle",
            "chat.closedBody", "chat.reuse", "chat.download", "sess.resumePct", "sess.pagePct"]
    assert set(LOCALES) >= {"en", "de", "es", "fr", "pt"}
    for lang in ("en", "de", "es", "fr", "pt"):
        missing = [k for k in keys if k not in LOCALES[lang]]
        assert not missing, (lang, missing)
    en = LOCALES["en"]
    assert en["home.continue"] == "Continue tailoring · {n}"
    assert en["home.cInProgress"] == en["tracker.stage.in_progress"] == "In progress"
    for k in keys:
        assert not re.search(r"\bResume (your|this|the|where|tailoring|editing)\b", en[k]), k
        assert "—" not in en[k] and "–" not in en[k]
