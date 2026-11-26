"""Applications as ONE list, "Ready to apply" (2026-11-24, assisted apply only).

* every open, unsent, not-skipped job with a tailored resume is on it (new, opened, or handed
  back by an old agent run for any reason but a closed posting), one row per job; there is no
  separate approve step;
* a closed posting is never on it: one quiet line says how many left, once per batch, and its
  Tracker card moves to Closed (Archived) with "Job closed", existing records included;
* the list and the Tracker's "Ready to apply" column count the same jobs, and "N applied" is the
  Tracker's own applied count;
* an ad that names too few skills shows no percent ("Ad too short to score"), a fit
  stored under the old rule is worked out again, and the picker never takes a job on such a
  score;
* "Did you submit it?" after an Autofill, until Yes or Not yet;
* the closed-job check also runs from the notify tick.

Every company and person here is made up. No test reaches the network (tests/conftest.py)."""

from __future__ import annotations

import importlib
import json
import re
import time
from pathlib import Path

import pytest

from notify.alerts import MIN_SCORED_SKILLS, digest_text, match_percent, too_thin_to_score
from submit.stages import is_closed, is_ready_to_apply
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
H = {"X-Tailor-Extension": "1"}
CLOSED = {"status": "approved", "submission": {"status": "failed", "reason": "posting_closed"},
          "job_closed": {"at": 1000.0}}


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


def _add(A, role, company="Northwind", sid=None, **data):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, company, 70, "", jd_label=role,
                        data={"status": "ready",
                              "source_job": {"url": f"https://boards.greenhouse.io/nw/jobs/{role}",
                                             "source_id": sid or f"gh:{role}"},
                              **data})
    finally:
        recs.close()


def _data(A, rid):
    recs = CVRecords(A.DB_PATH)
    try:
        return json.loads(recs.get(rid)["data"])
    finally:
        recs.close()


# --------------------------------------------------------------------------- who is on the list

def test_ready_to_apply_is_every_open_unsent_package():
    assert is_ready_to_apply({})                                          # new
    assert is_ready_to_apply({"status": "approved"})                      # opened
    assert is_ready_to_apply({"status": "approved",
                              "submission": {"status": "failed", "reason": "needs_signin"}})
    assert not is_ready_to_apply(CLOSED) and is_closed(CLOSED)
    assert not is_ready_to_apply({"status": "applied"})
    assert not is_ready_to_apply({"status": "skipped"})
    # moved back to Saved on the Tracker: not on either list
    assert not is_ready_to_apply({"status": "ready", "stage": "saved"})


def test_the_board_lists_one_row_per_ready_job_and_never_a_closed_one(A):
    new = _add(A, "Analyst")
    opened = _add(A, "Engineer", status="approved")
    back = _add(A, "Scientist", status="approved",
                submission={"status": "failed", "reason": "form_blocker"})
    _add(A, "Closed role", **CLOSED)
    _add(A, "Sent", status="applied", applied_at="2099-01-01T09:00:00")
    _add(A, "Passed", status="skipped")
    old = _add(A, "Twice", sid="gh:twice")
    newer = _add(A, "Twice", sid="gh:twice")
    b = A.app.test_client().get("/api/autoapply/board").get_json()
    ids = [x["id"] for x in b["ready"]]
    assert set(ids) == {new, opened, back, newer} and old not in ids
    assert b["ready_count"] == 4
    row = next(x for x in b["ready"] if x["id"] == new)
    assert row["new"] is True and row["match_thin"] is False
    assert next(x for x in b["ready"] if x["id"] == opened)["new"] is False


def test_the_tracker_ready_to_apply_column_counts_the_same_jobs(A):
    _add(A, "Analyst")
    _add(A, "Engineer", status="approved")
    _add(A, "Closed role", **CLOSED)
    _add(A, "Sent", status="applied", applied_at="2099-01-01T09:00:00")
    c = A.app.test_client()
    b = c.get("/api/autoapply/board").get_json()
    t = c.get("/api/tracker/board").get_json()
    assert len(t["columns"]["approved"]) == b["ready_count"] == 2
    arch = t["columns"]["archived"]
    assert [x["title"] for x in arch] == ["Closed role"] and arch[0]["closed_at"]
    assert b["applied_count"] == 1
    # The Tracker card of a closed job never reads "Needs you"; it says "Job closed <date>".
    assert "tracker.jobClosedOn" in APP_JS
    card = APP_JS.split("function trkCardHtml(", 1)[1].split("\n  }\n", 1)[0]
    assert "tracker.needsYou" not in card


def test_an_applied_job_stays_applied_when_its_posting_closes(A):
    _add(A, "Analyst", sid="gh:a", status="applied", applied_at="2099-01-01T09:00:00")
    _add(A, "Analyst", sid="gh:a", **CLOSED)
    t = A.app.test_client().get("/api/tracker/board").get_json()
    assert [x["title"] for x in t["columns"]["applied"]] == ["Analyst"]
    assert not t["columns"]["archived"]


def test_closing_a_record_moves_its_tracker_card_to_closed(A):
    rid = _add(A, "Analyst")
    c = A.app.test_client()
    assert len(c.get("/api/tracker/board").get_json()["columns"]["approved"]) == 1
    A._mark_record_closed(A._get_record(rid), "https://boards.greenhouse.io/nw/jobs/Analyst", "no longer open")
    w = A._watchlist()
    try:
        assert w.stage_of("gh:Analyst") == "archived"
        assert w.board_job("gh:Analyst")["closed_at"]
    finally:
        w.close()
    assert c.get("/api/autoapply/board").get_json()["ready"] == []


def test_the_closed_line_shows_once_per_batch(A):
    _add(A, "One", **CLOSED)
    _add(A, "Two", **{**CLOSED, "job_closed": {"at": 2000.0}})
    c = A.app.test_client()
    b = c.get("/api/autoapply/board").get_json()
    assert b["closed_notice"] == {"n": 2, "at": 2000.0}
    c.post("/api/autoapply/closed/seen", json={"at": 2000.0})
    assert c.get("/api/autoapply/board").get_json()["closed_notice"] is None
    _add(A, "Three", **{**CLOSED, "job_closed": {"at": 3000.0}})
    assert c.get("/api/autoapply/board").get_json()["closed_notice"] == {"n": 1, "at": 3000.0}


def test_the_notify_tick_runs_the_closed_job_check(A, monkeypatch):
    ran = []
    monkeypatch.setattr(A, "_liveness_kick", lambda rows: ran.append(len(rows)) or False)
    _add(A, "Analyst")
    A._notify_tick()
    assert ran == [1]


# --------------------------------------------------------------------------- the match number

def test_an_ad_naming_too_few_skills_gets_no_percent():
    one = {"total": 1, "ratio": 1.0, "required_total": 1, "required_covered": 1, "title_fit": 1.0}
    assert match_percent(one) is None and too_thin_to_score(one)
    four = dict(one, total=MIN_SCORED_SKILLS - 1)
    assert match_percent(four) is None
    five = dict(one, total=MIN_SCORED_SKILLS, ratio=0.6, required_total=0, required_covered=0)
    assert match_percent(five) is not None and not too_thin_to_score(five)
    assert not too_thin_to_score({"total": 0})


def test_one_required_skill_no_longer_sinks_a_rich_ad():
    # 14 skills, 3 covered; the ad's required part named one of them, not covered: that one miss
    # used to take 60% of the score (5%). Now the required weight needs 3 required skills.
    m = {"total": 14, "ratio": 3 / 14, "required_total": 1, "required_covered": 0, "title_fit": 0.5}
    assert match_percent(m) == round(100 * (3 / 14) * 0.9)
    rich = {"total": 14, "ratio": 0.5, "required_total": 6, "required_covered": 6, "title_fit": 1.0}
    assert match_percent(rich) == round(100 * (0.6 + 0.2))


def test_a_stored_fit_from_a_thin_ad_is_worked_out_again(A, monkeypatch):
    monkeypatch.setattr(A, "_job_match", lambda jd, title="": {
        "total": 1, "ratio": 1.0, "required_total": 1, "required_covered": 1, "title_fit": 1.0})
    rid = _add(A, "Assistant Manager", fit=100, jd_text="We want Forecasting.")
    row = next(x for x in A.app.test_client().get("/api/autoapply/board").get_json()["ready"] if x["id"] == rid)
    assert row["match"] is None and row["match_thin"] is True
    d = _data(A, rid)
    assert d["fit_thin"] is True and d["fit"] is None and d["fit_rule"]
    # A fit kept under the current rule is read as it is.
    rid2 = _add(A, "Analyst", fit=72, fit_rule=d["fit_rule"], jd_text="x")
    assert A._record_fit_info(A._get_record(rid2), _data(A, rid2)) == (72, False)


def test_the_picker_never_takes_a_thin_ad_on_its_score():
    from autoapply.picker import rank
    rows = [{"source_id": "gh:1", "title": "Assistant Manager", "company": "Contoso"}]
    res = rank(rows, records=[], score_fn=lambda j: match_percent(
        {"total": 1, "ratio": 1.0, "required_total": 1, "required_covered": 1, "title_fit": 1.0}),
        count=5, min_fit=0)
    assert res["jobs"] == [] and res["unscored"] == 1



def test_junk_tokens_are_not_skills():
    from tailoring.keywords import skill_terms
    terms = skill_terms("Candidates who do NOT have a G.E.D may apply. #LI-AF1. Use SQL and Python daily.")
    assert "NOT" not in terms and "LI-AF1" not in terms and "G.E.D" not in terms
    assert "SQL" in terms


def test_the_page_shows_the_thin_note_not_a_number():
    assert '"aa.matchThin", "Ad too short to score"' in APP_JS
    assert 'm.verdict === "thin"' in APP_JS          # the Jobs detail says it too


# --------------------------------------------------------------------------- the label

def test_a_company_above_a_long_title_is_read_the_right_way_round():
    from ui.session import _jd_label
    jd = ("Fabrikam Capital\nVice President / Associate, Analytics and Business Intelligence\n\n"
          "Fabrikam Capital, Chicago. We are hiring.")
    assert _jd_label(jd) == ("Vice President / Associate", "Fabrikam Capital")


def test_an_old_swapped_record_shows_role_and_company(A):
    jd = ("Fabrikam Capital\nVice President / Associate, Analytics and Business Intelligence\n\n"
          "Fabrikam Capital, Chicago. We are hiring.")
    recs = CVRecords(A.DB_PATH)
    try:
        rid = recs.add("Fabrikam Capital", "", 70, "", jd_label="Fabrikam Capital",
                       data={"status": "ready", "jd_text": jd, "job_id": "pasted:x1", "source_job": {}})
    finally:
        recs.close()
    c = A.app.test_client()
    row = next(x for x in c.get("/api/autoapply/board").get_json()["ready"] if x["id"] == rid)
    assert (row["company"], row["role"]) == ("Fabrikam Capital", "Vice President / Associate")
    card = c.get("/api/tracker/board").get_json()["columns"]["approved"][0]
    assert (card["company"], card["title"]) == ("Fabrikam Capital", "Vice President / Associate")


# --------------------------------------------------------------------------- did you submit it?

def test_the_question_waits_for_the_success_watch_and_a_real_fill(A):
    # Right after a fill the person is still on the form (the shell's 30-minute watch may yet mark
    # it applied), so nothing is asked; a run that filled nothing (a job page) asks nothing later.
    rid = _add(A, "Analyst", status="approved")
    c = A.app.test_client()
    row = lambda: next(x for x in c.get("/api/autoapply/board").get_json()["ready"] if x["id"] == rid)  # noqa: E731
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 0, "left": []})
    assert not _data(A, rid).get("filled_at") and row()["ask_submitted"] is False
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 6, "left": []})
    assert _data(A, rid)["filled_at"] and row()["ask_submitted"] is False
    at = _data(A, rid)["filled_at"]
    from datetime import datetime
    t = datetime.fromisoformat(at).timestamp()
    assert A._ask_submitted({"filled_at": at}, "approved", now=t + A.ASK_SUBMITTED_AFTER_S) is True
    assert A._ask_submitted({"filled_at": at}, "approved", now=t + 60) is False


def test_after_an_autofill_the_row_asks_once_until_yes_or_no(A, monkeypatch):
    monkeypatch.setattr(A, "ASK_SUBMITTED_AFTER_S", 0)
    rid = _add(A, "Analyst", status="approved")
    c = A.app.test_client()
    row = lambda: next(x for x in c.get("/api/autoapply/board").get_json()["ready"] if x["id"] == rid)  # noqa: E731
    assert row()["ask_submitted"] is False
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 6, "left": []})
    assert _data(A, rid)["filled_at"] and row()["ask_submitted"] is True
    c.post("/api/autoapply/not-yet", json={"record_id": rid})
    assert row()["ask_submitted"] is False
    time.sleep(1.1)                                   # the next fill has a new stamp
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 6, "left": []})
    assert row()["ask_submitted"] is False            # asked ONCE per application (2026-11-26)
    c.post(f"/api/record/{rid}/status", json={"status": "applied"})
    assert all(x["id"] != rid for x in c.get("/api/autoapply/board").get_json()["ready"])


# --------------------------------------------------------------------------- the page and words

def test_the_page_is_one_list_with_assisted_apply():
    view = INDEX[INDEX.index('id="view-autoapply"'):]
    assert 'aria-label="Application stages" data-agent-only' in view
    assert 'data-i18n="aa.sub">Jobs with a tailored resume, ready for you to apply.</p>' in view
    assert 'id="aaClosed" role="status" hidden data-assisted-only' in view
    assert 'id="aaApplied" hidden data-assisted-only' in view
    assert "if (!APPLY_AGENT) { aaRenderReady(); aaRenderToday(); return; }" in APP_JS
    assert 'data-act="fill"' in APP_JS and 'aaSilentApprove(id).then(() => aaFill(id, { btn }))' in APP_JS
    assert ">Autofill this page</button>" in INDEX
    assert not re.search(r"Your best-fit roles", INDEX)


def test_every_new_string_has_every_locale():
    keys = ("aa.ready", "aa.new", "aa.matchThin", "aa.matchThinTip", "aa.act.fillTip", "aa.appliedOne",
            "aa.appliedN", "aa.seeTracker", "aa.closedOne", "aa.closedN", "aa.askSubmitted", "aa.notYet",
            "aa.markedApplied", "aa.empty.ready", "aa.queuedReady", "aa.hint.ready", "tracker.jobClosedOn",
            "fill.againCaptcha", "fill.againSignin", "fill.againJob", "fill.againNoForm",
            "fill.againLogin", "aa.act.fill", "bb.fillHere", "pkg.fill", "pkg.fillAgain")
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[—–]", d[k]), (code, k)
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert en["aa.act.fill"] == "Autofill application" and en["bb.fillHere"] == "Autofill this page"
    assert en["tracker.stage.approved"] == "Resume ready" and en["aa.ready"] == "To apply"
    assert not [k for k in keys if "CV" in en[k] or "résumé" in en[k]]


def test_telegram_names_the_page_applications():
    from notify.assistant import WHERE_ANSWERS
    t = digest_text({"tailored": 3, "waiting": 2})
    assert "2 ready for you to apply" in t and "Applications" in t and "Auto-apply" not in t
    assert WHERE_ANSWERS == "Applications, Your answers"
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert not re.findall(r'"[^"\n]*SponsorJobs, Auto-apply[^"\n]*"', src)


def test_closed_column_says_which_ending_and_see_all_goes_to_tracker():
    # A rejected card in the Tracker's Closed column says "Not selected"; a closed job says
    # "Job closed". The Today panel's link goes to the Tracker (one applied count, not two).
    assert '"tracker.notSelected", "Not selected"' in APP_JS
    assert '"aa.seeAllTracker", "See all in Tracker"' in APP_JS
    assert 'if (!APPLY_AGENT) { navTo("tracker"); return; }' in APP_JS
