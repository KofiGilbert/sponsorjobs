"""The Tracker as a Jira-style board (2026-11-24, assisted apply).

* a board card carries what its "Apply" needs: the package to fill (``apply_id``), when it was
  tailored, whether it was never opened (``new``) and whether to ask "Did you submit it?"
  (``ask_submitted``), from the same helpers the Applications list uses;
* the board says how many jobs closed before they were applied to (``closed_notice``), the same
  once-per-batch line as Applications;
* a card is compact and has no Resume / Chat / Details links; Ready to apply has one Apply that
  runs the Applications row's Autofill (aaSilentApprove, then aaFill); the search box and the
  inbox strip behave (tests/js/test_tracker_board.js, under node).

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


def test_apply_runs_the_applications_autofill():
    btn = _fn("trkApplyBtn")
    assert 'c.stage !== "approved" || !c.apply_id' in btn and "data-trk-apply" in btn and "trkApplyTip()" in btn
    run = _fn("trkApply")
    assert "/api/autoapply/item/${id}" in run
    assert run.index("aaSilentApprove(id, item)") < run.index("aaFill(id, { item")
    assert "closeTrackerPanel()" in run               # the in-app browser never opens under the panel
    assert "const it = item || aaFind(id);" in _fn("aaSilentApprove")
    board = _fn("renderTrackerBoard")
    assert "trkApply(sid" in board and "openTrackerPanel(sid)" in board
    assert "openJobWorkspace" not in board            # a click is always the side panel
    assert "[data-trk-apply]" in _fn("renderTrackerTable")


def test_the_side_panel_is_the_one_place_for_files_chat_and_apply():
    acts = _fn("trkPanelActions")
    for bit in ('"btn-primary btn-sm trk-p-apply"', "tracker.resume", "tracker.coverLetter", "data-trk-chat", "tracker.posting"):
        assert bit in acts, bit
    panel = _fn("openTrackerPanel")
    assert "trkPanelActions(j)" in panel and "trkAskHtml(j)" in panel and 'id="trkPanelStage"' in panel
    assert "openJobWorkspace(sid)" in panel
    assert 'e.key === "Escape" && TRK_OPEN' in JS      # Escape closes it


def test_one_toolbar_and_one_quiet_numbers_line():
    view = HTML[HTML.index('id="view-tracker"'):HTML.index('id="trkScrim"')]
    assert view.index('id="trkMetrics"') < view.index('class="trk-toolbar"') < view.index('id="trkBoard"')
    bar = view[view.index('class="trk-toolbar"'):view.index('id="trkBoard"')]
    for bit in ('id="trkSearch"', 'id="trkDueChip"', 'id="trkViewBoard"', 'id="trkViewTable"', 'id="trkExport"'):
        assert bit in bar, bit
    assert 'id="trkDueChip" type="button" aria-pressed="false" hidden' in bar
    assert 'id="trkClosed"' in view
    metrics = _fn("renderTrackerMetrics")
    assert "trk-spark" not in metrics and "trk-st " not in metrics and 'id="trkGoal"' in metrics
    strip = _fn("renderInboxStrip")
    assert "if (!INBOX_SUGG.length) { host.hidden = true;" in strip


def test_columns_fill_the_height_scroll_on_their_own_and_empty_ones_stay_narrow():
    css = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
    block = css[css.index("/* ============ Tracker (2026-11-24): a Jira-style board"):]
    assert "#view-tracker > .trk-board { flex: 1 1 auto;" in block and "overflow-x: auto" in block
    assert ".trk-col-cards { flex: 1 1 auto; min-height: 0; overflow-y: auto;" in block
    assert ".trk-col.is-empty { flex: 0.6 1 0;" in block
    assert "-webkit-line-clamp: 2" in block              # titles stop at two lines


def test_every_new_string_has_every_locale():
    keys = ("tracker.searchPh", "tracker.dueChip", "tracker.apply", "tracker.applyTip", "tracker.tailoredOn",
            "tracker.appliedOnDay", "tracker.followUpOn", "tracker.savedOn", "tracker.sinceOn", "tracker.archivedOn",
            "tracker.updatedAgo", "tracker.noMatch", "tracker.markedApplied", "tracker.dateCol", "tracker.close",
            "tracker.moveTo")
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[—–]", d[k]), (code, k)
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert en["tracker.applyTip"] == "Opens the application and fills it from your profile. You check it and submit."
    assert not [k for k in keys if re.search(r"\bCV\b|résumé", en[k])]
