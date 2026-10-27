"""The Auto-apply page (tailor-ux 2026-10): stage tiles with real counts, the tile it opens on,
the drawer's data, Up next skips, and the Review page folded in (no Review nav, no dead front
end, every link that pointed at Review lands on Auto-apply). Every HTTP call is local: no test
reaches a real employer's endpoint."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from submit.stages import default_tile, needs_summary, row_action, tile_of
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
GH = "https://boards.greenhouse.io/acme/jobs/1"


# --- pure tile logic ---------------------------------------------------------------------------

def test_tile_of_every_stage():
    assert tile_of({}) == "review"
    assert tile_of({"status": "approved"}) == "waiting"
    assert tile_of({"status": "approved", "queued": True}) == "waiting"
    assert tile_of({"status": "approved", "submission": {"status": "failed",
                                                         "reason": "needs_signin"}}) == "needs"
    assert tile_of({"status": "applied"}) == "submitted"
    assert tile_of({"status": "skipped"}) is None


def test_row_action_matches_the_stage_and_reason():
    assert row_action("review") == "review"
    assert row_action("waiting") == "fill"
    assert row_action("submitted") == "view"
    assert row_action("needs", "needs_signin") == "signin"
    assert row_action("needs", "form_blocker") == "finish"
    assert row_action("needs", "posting_closed") == "view"


def test_needs_summary_counts_reasons_most_first():
    assert needs_summary([]) == ""
    assert needs_summary(["needs_signin", "form_blocker"]) == "1 sign-in, 1 form question"
    assert needs_summary(["form_blocker", "needs_signin", "form_blocker"]) == \
        "2 form questions, 1 sign-in"
    assert needs_summary(["nonsense"]) == "1 site error"


def test_default_tile_is_the_first_with_work():
    assert default_tile({"review": 2, "waiting": 6, "needs": 1}) == "waiting"
    assert default_tile({"review": 2, "waiting": 0, "needs": 1}) == "review"
    assert default_tile({"needs": 1}) == "needs"
    assert default_tile({"submitted": 4}) == "submitted"
    assert default_tile({}) == "review"


# --- the app -----------------------------------------------------------------------------------

@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    return mod


def _add(A, role="Data Engineer", company="Acme", data=None, url=GH):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, company, 70, "", jd_label=role,
                        data={"status": "ready",
                              "source_job": {"url": url, "source_id": f"gh:{role}",
                                             "location": "New York, NY"},
                              "coverage": {"ratio": 82, "present": ["SQL", "dbt"],
                                           "missing": ["Kafka"], "missing_supported": []},
                              "cover_letter": "Dear team, I build data pipelines.",
                              "screening": [{"question": "Earliest start?", "answer": "November"}],
                              **(data or {})})
    finally:
        recs.close()


def test_board_counts_each_tile_from_the_records(A):
    _add(A, "Analyst", data={"fit": 82})                            # to review
    _add(A, "Engineer", data={"status": "approved"})                # waiting on you
    _add(A, "Scientist", data={"status": "approved",
                               "submission": {"status": "failed", "reason": "needs_signin"}})
    _add(A, "Lead", data={"status": "applied", "applied_at": "2099-01-01T09:00:00",
                          "submission": {"by": "auto", "status": "auto_submitted"}})
    _add(A, "Skipped", data={"status": "skipped"})
    b = A.app.test_client().get("/api/autoapply/board").get_json()
    assert b["counts"] == {"review": 1, "waiting": 1, "submitted": 1, "needs": 1}
    assert b["default"] == "waiting"
    assert b["needs_summary"] == "1 sign-in"
    assert b["week"] == {"sent": 1, "for_you": 1}
    assert [x["role"] for x in b["recent"]] == ["Lead"]
    row = b["tiles"]["needs"][0]
    assert row["action"] == "signin" and row["reason"] == "needs_signin"
    assert b["tiles"]["waiting"][0]["action"] == "fill"
    assert b["tiles"]["review"][0]["match"] == 82 and b["tiles"]["review"][0]["site"] == "Greenhouse"
    assert b["today"]["cap"] >= 1 and "autonomous" in b["today"]


def test_board_opens_on_review_when_nothing_is_approved(A):
    _add(A, "Analyst")
    assert A.app.test_client().get("/api/autoapply/board").get_json()["default"] == "review"


def test_approving_records_when_and_moves_tiles(A):
    c = A.app.test_client()
    rid = _add(A, "Analyst")
    c.post("/api/review/approve", json={"approve": [rid]})
    b = c.get("/api/autoapply/board").get_json()
    assert b["counts"]["review"] == 0 and b["counts"]["waiting"] == 1
    assert b["tiles"]["waiting"][0]["approved_at"]
    c.post("/api/review/approve", json={"skip": [rid]})
    assert sum(c.get("/api/autoapply/board").get_json()["counts"].values()) == 0


def test_item_has_what_will_be_sent_and_no_guesses(A):
    rid = _add(A, "Analyst", data={"fit": 77})
    d = A.app.test_client().get(f"/api/autoapply/item/{rid}").get_json()
    assert d["match"] == 77                       # the run's fit wins over keyword coverage
    assert d["resume"]["terms"] == ["SQL", "dbt"] and d["resume"]["terms_total"] == 3
    assert d["resume"]["exists"] is False and d["resume"]["pages"] is None
    assert d["letter"]["words"] == 6
    assert d["screening"] == [{"q": "Earliest start?", "a": "November", "knockout": False}]
    assert d["pay"] == "" and d["type"] == ""     # the stored job holds neither: left empty
    assert A.app.test_client().get("/api/autoapply/item/99999").status_code == 404


def test_skip_takes_a_role_out_of_the_next_run(A):
    c = A.app.test_client()
    assert c.post("/api/autoapply/skip", json={}).status_code == 400
    c.post("/api/autoapply/skip", json={"source_id": "gh:1"})
    assert A._prefs()["aa_skip"] == ["gh:1"]
    c.post("/api/autoapply/skip", json={"source_id": "gh:1", "undo": True})
    assert A._prefs()["aa_skip"] == []


def test_skipped_roles_leave_the_candidates(A, monkeypatch):
    rows = [{"source_id": "gh:1", "title": "Data Engineer", "company": "A", "location": "NY"},
            {"source_id": "gh:2", "title": "Data Engineer", "company": "B", "location": "NY"}]
    monkeypatch.setattr(A, "_board_rows", lambda: rows)
    A._set_prefs({"aa_skip": ["gh:1"]})
    got, _meta = A._aa_candidates({})
    assert [j["source_id"] for j in got] == ["gh:2"]


# --- Review folded into Auto-apply -------------------------------------------------------------

def test_review_nav_and_page_are_gone():
    assert 'data-nav="review"' not in HTML
    assert 'id="view-review"' not in HTML
    assert 'data-nav-to="review"' not in HTML
    assert "Review &amp; submit" not in HTML and "Review & submit" not in APP_JS
    assert 'showView("review")' not in APP_JS
    for gone in ("rvwList", "rvwApprove", "rvwSelectAll", "loadReview(", "aaReadyList"):
        assert gone not in APP_JS, gone


def test_old_review_links_land_on_auto_apply():
    # A stale "review" destination (an old link or notification) opens Auto-apply's To review.
    assert re.search(r'review:\s*\["autoapply",\s*"review"\]', APP_JS)
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert "nav.review" not in en


def test_review_endpoints_stay():
    import ui.app as mod
    rules = {r.rule for r in mod.app.url_map.iter_rules()}
    for path in ("/api/review/queue", "/api/review/approve", "/api/review/telegram/send"):
        assert path in rules


def test_page_has_the_four_tiles_and_one_primary():
    view = HTML[HTML.index('id="view-autoapply"'):]
    view = view[:view.index("</section>\n\n")]
    for t in ("review", "waiting", "submitted", "needs"):
        assert f'data-aa-tile="{t}"' in view
    assert view.count("btn-primary") == 1


def test_match_is_the_fit_never_the_keyword_share(A):
    """One "match" number everywhere: the person's fit for the job (Jobs, Auto-apply), never the
    share of the ad's phrases on the resume (that is "Keywords" in the editor). With no fit to
    show, the row shows no number rather than the keyword share under the same name."""
    _add(A, "Analyst")                                   # keyword share 82, no fit, no ad text
    b = A.app.test_client().get("/api/autoapply/board").get_json()
    assert b["tiles"]["review"][0]["match"] is None
    rows = A.app.test_client().get("/api/records").get_json()["records"]
    assert rows[0]["match"] is None and rows[0]["coverage"] == 70
