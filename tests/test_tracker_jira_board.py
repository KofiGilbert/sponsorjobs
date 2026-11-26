"""The Tracker as a Jira-style board of five states (2026-11-25, assisted apply).

* a board card carries what the panel's "Apply" needs: the package to fill (``apply_id``), when
  it was tailored, whether it was never opened (``new``) and whether to ask "Did you submit it?"
  (``ask_submitted``), from the same helpers the Applications list uses;
* the board says how many jobs closed before they were applied to (``closed_notice``), the same
  once-per-batch line as Applications;
* five columns of STATES (To apply, Applied, Interviewing, Offer, Closed) over the eight stage
  keys; a card has no button or link on its face, one lozenge per state; every action is in the
  side panel (Apply runs the Applications row's Autofill: aaSilentApprove, then aaFill); a drop
  maps to the right stage (tests/js/test_tracker_board.js, under node);
* the board ends above the fixed "Local." line, so a column's last card is never under it.

Every company here is made up. No test reaches the network (tests/conftest.py)."""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
H = {"X-Tailor-Extension": "1"}


def _fn(name: str) -> str:
    return JS.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.delenv("SJ_APPLY_AGENT", raising=False)
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    return mod


def _add(A, role, company="Brightwater Freight", **data):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, company, 70, "", jd_label=role,
                        data={"status": "ready",
                              "source_job": {"url": f"https://boards.greenhouse.io/bw/jobs/{role}",
                                             "source_id": f"gh:{role}"},
                              **data})
    finally:
        recs.close()


def _card(c, title):
    cols = c.get("/api/tracker/board").get_json()["columns"]
    return next(x for col in cols.values() for x in col if x["title"] == title)


# --------------------------------------------------------------------------- the API

def test_ready_cards_carry_apply_new_and_ask_submitted(A, monkeypatch):
    monkeypatch.setattr(A, "ASK_SUBMITTED_AFTER_S", 0)
    new = _add(A, "Analyst")
    opened = _add(A, "Engineer", status="approved")
    c = A.app.test_client()
    a, e = _card(c, "Analyst"), _card(c, "Engineer")
    assert a["stage"] == e["stage"] == "approved"
    assert (a["apply_id"], a["new"], a["ask_submitted"]) == (new, True, False)
    assert (e["apply_id"], e["new"], e["ask_submitted"]) == (opened, False, False)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", a["tailored_at"])
    # After an Autofill the card asks, the same as the Applications row; Not yet stops it.
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": opened, "page": 1, "filled": 5, "left": []})
    assert _card(c, "Engineer")["ask_submitted"] is True
    row = next(x for x in c.get("/api/autoapply/board").get_json()["ready"] if x["id"] == opened)
    assert row["ask_submitted"] is True
    c.post("/api/autoapply/not-yet", json={"record_id": opened})
    assert _card(c, "Engineer")["ask_submitted"] is False
    # Yes: marked applied, the card moves to Applied and no longer offers Apply.
    c.post(f"/api/record/{new}/status", json={"status": "applied"})
    a = _card(c, "Analyst")
    assert a["stage"] == "applied" and a["apply_id"] is None and a["ask_submitted"] is False


def test_a_card_without_a_package_has_nothing_to_apply(A):
    c = A.app.test_client()
    snap = {"source_id": "gh:hand", "company": "Larkspur Labs", "title": "Writer", "url": "https://example.com/j"}
    assert c.post("/api/tracker/move", json={"source_id": "gh:hand", "stage": "approved", "job": snap}).status_code == 200
    card = _card(c, "Writer")
    assert card["apply_id"] is None and card["new"] is False and card["ask_submitted"] is False


def test_the_board_carries_the_closed_jobs_line_once_per_batch(A):
    closed = {"status": "approved", "submission": {"status": "failed", "reason": "posting_closed"},
              "job_closed": {"at": 1000.0}}
    _add(A, "One", **closed)
    c = A.app.test_client()
    assert c.get("/api/tracker/board").get_json()["closed_notice"] == {"n": 1, "at": 1000.0}
    assert c.get("/api/autoapply/board").get_json()["closed_notice"] == {"n": 1, "at": 1000.0}
    c.post("/api/autoapply/closed/seen", json={"at": 1000.0})
    assert c.get("/api/tracker/board").get_json()["closed_notice"] is None
    assert c.get("/api/autoapply/board").get_json()["closed_notice"] is None


# --------------------------------------------------------------------------- the page

def test_the_card_and_page_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_tracker_board.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert "ok tracker board" in out.stdout


def test_a_card_has_no_action_and_a_click_is_always_the_panel():
    for gone in ("trkApplyBtn", "trkAskHtml", "trkCardMeta", "data-trk-apply"):
        assert gone not in JS, gone                   # the card's old buttons are gone, not hidden
    card = _fn("trkCardHtml")
    assert "<button" not in card and "<a " not in card and "trk-new" not in card
    assert card.index("trk-card-t") < card.index("trk-card-loz") < card.index("trk-card-foot")
    board = _fn("renderTrackerBoard")
    assert 'card.addEventListener("click", () => openTrackerPanel(sid));' in board
    assert "trkApply(" not in board and "openJobWorkspace" not in board
    assert "TRK_COLS.map(col =>" in board             # always all five: no optional column
    assert "trkDropOn(e.dataTransfer.getData(\"text/plain\"), col.dataset.col)" in board
    table = _fn("renderTrackerTable")
    assert "<select" not in table and "<button" not in table and 'trkIcon("chevron")' in table


def test_apply_runs_the_applications_autofill_from_the_panel():
    run = _fn("trkApply")
    assert "/api/autoapply/item/${id}" in run
    assert run.index("aaSilentApprove(id, item)") < run.index("aaFill(id, { item")
    assert "closeTrackerPanel()" in run               # the in-app browser never opens under the panel
    assert "const it = item || aaFind(id);" in _fn("aaSilentApprove")
    panel = _fn("openTrackerPanel")
    assert 'if (act === "apply") trkApply(sid, e.currentTarget);' in panel
    assert 'else if (act === "continue") trkContinue(sid);' in panel and 'else if (act === "tailor") trkTailor(sid);' in panel
    assert "else trkTailor(sid);" not in panel        # "Add job link" never starts a new resume
    assert "await tailorJob(sid);" in _fn("trkTailor")          # the Jobs page's own tailoring flow
    assert "openJobWorkspace(sid)" in _fn("trkContinue")        # the job's resume chat


def test_the_side_panel_is_the_one_place_that_acts():
    acts = _fn("trkPanelActions")
    for bit in ("trk-p-main", "tracker.apply", "tracker.continueTailoring", "tracker.tailorResume",
                "tracker.resume", "tracker.coverLetter", "tracker.chat", "tracker.posting"):
        assert bit in acts, bit
    panel = _fn("openTrackerPanel")
    order = [panel.index(x) for x in ('class="trk-p-head"', 'class="trk-p-loz"', "${trkPanelAsk(j)}", "${trkPanelActions(j)}",
                                      'class="trk-p-fields"', '"tracker.notes"', '"tracker.contacts"', "${trkMailSectionHtml(j)}",
                                      '"tracker.files"', '"tracker.timeline"')]
    assert order == sorted(order)                     # header, lozenges, the question, ONE action row, then the sections
    assert "trkStatusSelect(j)" in panel and "[data-trk-yes]" in panel and "[data-trk-notyet]" in panel
    assert 'e.key === "Escape" && TRK_OPEN' in JS      # Escape closes it
    assert 'if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrackerPanel(sid); }' in _fn("renderTrackerBoard")


def test_one_toolbar_and_one_quiet_numbers_line():
    view = HTML[HTML.index('id="view-tracker"'):HTML.index('id="trkScrim"')]
    assert view.index('id="trkMetrics"') < view.index('class="trk-toolbar"') < view.index('id="trkBoard"')
    bar = view[view.index('class="trk-toolbar"'):view.index('id="trkBoard"')]
    for bit in ('id="trkSearch"', 'id="trkDueChip"', 'id="trkReadyChip"', 'id="trkViewBoard"', 'id="trkViewTable"', 'id="trkExport"'):
        assert bit in bar, bit
    assert 'id="trkDueChip" type="button" aria-pressed="false" hidden' in bar
    assert 'id="trkReadyChip" type="button" aria-pressed="false" hidden' in bar
    assert 'id="trkClosed"' in view
    metrics = _fn("renderTrackerMetrics")
    assert "trk-spark" not in metrics and "trk-st " not in metrics and 'id="trkGoal"' in metrics
    strip = _fn("renderInboxStrip")
    assert "if (!INBOX_SUGG.length) { host.hidden = true;" in strip
    # the inbox notice is ONE line: later messages wait behind "N more"
    assert "data-sugg-more" in strip and "#view-tracker .trk-inbox:not(.is-open) .trk-inbox-row:not(:first-child) { display: none; }" in CSS


def test_five_columns_fit_and_the_last_card_clears_the_fixed_footer():
    block = CSS[CSS.index("/* ============ Tracker (2026-11-25): five states"):]
    # five equal columns in a grid: no sideways scroll from 1280px up (5 x 168 + 4 x 12 = 888)
    assert "#view-tracker > .trk-board { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: repeat(5, minmax(168px, 1fr)); gap: 12px;" in block
    assert ".trk-col.is-empty { flex" not in block     # an empty column keeps its width
    # The page itself never scrolls and stops ABOVE the fixed "Local." line, whose height the
    # view reserves as bottom padding; each column scrolls on its own, with clearance under its
    # last card.
    view = block[block.index("#view-tracker {"):block.index("}", block.index("#view-tracker {"))]
    foot = int(re.search(r"--trk-foot: (\d+)px;", view).group(1))
    assert "overflow: hidden" in view and "padding: 24px 32px var(--trk-foot);" in view
    line = CSS[CSS.index(".privacy-line {"):CSS.index("}", CSS.index(".privacy-line {"))]
    size = float(re.search(r"font-size: ([\d.]+)px", line).group(1))
    top, bottom = re.search(r"padding: (\d+)px \d+px (\d+)px;", line).groups()
    assert "position: absolute" in line and "bottom: 0" in line
    assert foot >= int(top) + int(bottom) + size * 1.5  # padding plus one line of its text
    cards = re.search(r"\.trk-col-cards \{[^}]*\}", block).group(0)
    assert "min-height: 0; overflow-y: auto;" in cards
    assert int(re.search(r"padding: \d+px \d+px (\d+)px;", cards).group(1)) >= 8
    assert "-webkit-line-clamp: 2" in block              # titles stop at two lines
    # raised cards on a sunken column, a hover state, lifted while dragged, a focus ring; both themes
    for bit in ("--trk-col-bg:", "--trk-card-shadow:", "--trk-card-shadow-hover:", "--trk-card-shadow-lift:",
                ".trk-card:hover {", ".trk-card:focus-visible {", ".trk-card.is-lifted {", ".trk-card.is-dragging {",
                ':root:not([data-theme="light"]) #view-tracker {', ".trk-loz-success {", ".trk-loz-info {", ".trk-loz-warn {"):
        assert bit in block, bit


def test_the_new_column_name_is_used_where_the_old_one_was():
    import agent.home as home
    import notify.alerts as alerts
    assert "Ready to apply" not in JS
    for text in (home.result_text(3, 0, assisted=True), home.status_text({"ready": 2}, [], assisted=True)):
        assert "Ready to apply" not in text and "apply" in text
    assert "To apply on the Tracker" in home.result_text(3, 0, assisted=True)
    assert "the Tracker" in alerts.digest_text({"tailored": 2, "waiting": 2}, assisted=True)
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert not [k for k, v in en.items() if "Ready to apply" in v]
    assert en["tracker.col.todo"] == "To apply" and en["tracker.stage.approved"] == "Resume ready"


def test_every_new_string_has_every_locale():
    keys = ("tracker.searchPh", "tracker.dueChip", "tracker.apply", "tracker.applyTip", "tracker.tailoredOn",
            "tracker.appliedOnDay", "tracker.followUpOn", "tracker.savedOn", "tracker.sinceOn", "tracker.closedOn",
            "tracker.noMatch", "tracker.markedApplied", "tracker.dateCol", "tracker.close",
            "tracker.col.todo", "tracker.hintTodo", "tracker.loz.ready", "tracker.loz.tailoring", "tracker.loz.none",
            "tracker.loz.due", "tracker.loz.ask", "tracker.loz.jobClosed", "tracker.tailorResume",
            "tracker.continueTailoring", "tracker.statusCol", "tracker.readyChip", "tracker.readyChipTip",
            "inbox.strip.more", "inbox.strip.less")
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[\u2014\u2013]", d[k]), (code, k)
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert en["tracker.applyTip"] == "Opens the application and fills it from your profile. You check it and submit."
    assert not [k for k in keys if re.search(r"\bCV\b|r\u00e9sum\u00e9", en[k])]
    for k in keys:                                       # every key is really asked for by the page
        assert k in JS or k in HTML, k
