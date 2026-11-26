"""The Tracker's lifecycle (2026-11-26, approved by the owner).

* Every Closed card carries a reason: Job closed, Not selected, No response, Passed. Existing
  cards are given one once (archived -> Passed, a closed posting -> Job closed, rejected -> Not
  selected), nothing else on them changes.
* An expired To apply posting moves to Closed with "Job closed" by itself (the free posting check).
* "Still interested?" on a To apply card untouched for 30 days, only once its posting was checked
  open (or when it has no link to check); "No response?" on an Applied card quiet for 30 days;
  "Keep" starts the 30 days again.
* An accepted offer stays in Offer, "Accepted".
* "Remove from tracker" takes a card off for good (the board catching up never brings it back),
  keeps its resumes and records, and Undo puts it back as it was.
* "Add job link" saves an http(s) link; Apply then works.
* After an Autofill, the model reads the changed page; a page that confirms the application moves
  the card to Applied with today's date. "Did you submit?" is asked once per application.

Every company here is made up. No test reaches the network (tests/conftest.py)."""

from __future__ import annotations

import importlib
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from sourcing.watchlist import Watchlist
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}


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
    # The background posting check is run by hand in the tests that need it.
    monkeypatch.setattr(mod, "_todo_liveness_kick", lambda jobs=None: False)
    return mod


def _job(c, sid, title, stage="saved", url="https://jobs.example.com/1", **more):
    snap = {"source_id": sid, "company": "Larkspur Labs", "title": title, "url": url, **more}
    assert c.post("/api/tracker/move", json={"source_id": sid, "stage": stage, "job": snap}).status_code == 200


def _card(c, sid):
    cols = c.get("/api/tracker/board").get_json()["columns"]
    return next((x for col in cols.values() for x in col if x["source_id"] == sid), None)


def _sql(A, q, args=()):
    w = A._watchlist()
    try:
        w._conn.execute(q, args)
        w._conn.commit()
    finally:
        w.close()


def _ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat() + "T09:00:00"


def _add_record(A, role, sid, url="https://boards.greenhouse.io/lark/jobs/1", **data):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, "Larkspur Labs", 70, "", jd_label=role,
                        data={"status": "ready", "source_job": {"url": url, "source_id": sid}, **data})
    finally:
        recs.close()


# --------------------------------------------------------------------------- existing data

def test_existing_closed_cards_get_a_reason_once_and_keep_everything_else():
    path = tempfile.mktemp(suffix=".db")
    w = Watchlist(path)
    rows = [{"source_id": f"x::{i}", "source": "x", "company": "Old Co", "title": t, "location": "", "remote": "",
             "url": "", "jd_text": "", "posted_at": "", "salary": ""} for i, t in enumerate(("A", "B", "C", "D"))]
    w.upsert_jobs(rows)
    w._conn.execute("UPDATE sourced_job SET stage='archived', stage_at='2026-10-01', notes='kept' WHERE source_id='x::0'")
    w._conn.execute("UPDATE sourced_job SET stage='archived', stage_at='2026-10-02', closed_at='2026-10-02', "
                    "closed_reason='posting closed' WHERE source_id='x::1'")
    w._conn.execute("UPDATE sourced_job SET stage='rejected', stage_at='2026-10-03' WHERE source_id='x::2'")
    w._conn.execute("UPDATE sourced_job SET stage='applied', stage_at='2026-10-04' WHERE source_id='x::3'")
    # an older database: no reason on any of them yet
    w._conn.execute("UPDATE sourced_job SET close_reason=NULL")
    w._conn.commit()
    w.close()
    w = Watchlist(path)                              # the migration runs on open
    got = {j["source_id"]: j for j in w.board_jobs()}
    assert got["x::0"]["close_reason"] == "passed" and got["x::0"]["notes"] == "kept"
    assert got["x::1"]["close_reason"] == "job_closed" and got["x::1"]["closed_at"] == "2026-10-02"
    assert got["x::2"]["close_reason"] == "not_selected"
    assert got["x::3"]["close_reason"] is None and got["x::3"]["stage"] == "applied"
    assert [j["stage_at"] for j in sorted(got.values(), key=lambda j: j["source_id"])] == \
        ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    w._conn.execute("UPDATE sourced_job SET close_reason='no_response' WHERE source_id='x::2'")
    w._conn.commit()
    w.close()
    w = Watchlist(path)                              # a reason already there is never rewritten
    assert w.board_job("x::2")["close_reason"] == "no_response"
    w.close()


# --------------------------------------------------------------------------- closing with a reason

def test_close_with_a_reason_change_it_and_undo(A):
    c = A.app.test_client()
    _job(c, "lc:1", "Analyst", stage="applied")
    r = c.post("/api/tracker/move", json={"source_id": "lc:1", "stage": "archived", "reason": "no_response"})
    assert r.status_code == 200 and r.get_json()["job"]["stage"] == "archived"
    card = _card(c, "lc:1")
    assert card["close_reason"] == "no_response" and card["closed_on"] == date.today().isoformat()
    # Not selected is the Rejected stage (the funnel's rejection count)
    c.post("/api/tracker/move", json={"source_id": "lc:1", "stage": "archived", "reason": "not_selected"})
    card = _card(c, "lc:1")
    assert (card["stage"], card["close_reason"]) == ("rejected", "not_selected")
    c.post("/api/tracker/undo", json={"source_id": "lc:1"})
    card = _card(c, "lc:1")
    assert (card["stage"], card["close_reason"]) == ("archived", "no_response")
    # dropping a To apply card on Closed: Passed; an applied one: Not selected
    _job(c, "lc:2", "Writer")
    c.post("/api/tracker/move", json={"source_id": "lc:2", "stage": "archived"})
    assert _card(c, "lc:2")["close_reason"] == "passed"
    c.post("/api/tracker/move", json={"source_id": "lc:2", "stage": "saved"})
    assert _card(c, "lc:2")["close_reason"] == ""
    assert c.post("/api/tracker/move", json={"source_id": "lc:2", "stage": "archived", "reason": "bored"}).status_code == 400


def test_an_accepted_offer_stays_in_offer(A):
    c = A.app.test_client()
    _job(c, "lc:3", "Engineer", stage="offer")
    out = c.post("/api/tracker/accept", json={"source_id": "lc:3", "accepted": True}).get_json()
    assert out["job"]["stage"] == "offer" and out["job"]["accepted"] is True
    assert _card(c, "lc:3")["accepted"] is True
    c.post("/api/tracker/accept", json={"source_id": "lc:3", "accepted": False})
    assert _card(c, "lc:3")["accepted"] is False
    c.post("/api/tracker/accept", json={"source_id": "lc:3", "accepted": True})
    c.post("/api/tracker/move", json={"source_id": "lc:3", "stage": "interviewing"})
    assert _card(c, "lc:3")["accepted"] is False         # leaving Offer drops it
    assert c.post("/api/tracker/accept", json={"source_id": "lc:3"}).status_code == 400


# --------------------------------------------------------------------------- remove from tracker

def test_remove_keeps_the_job_off_for_good_and_undo_puts_it_back(A):
    c = A.app.test_client()
    rid = _add_record(A, "Data Analyst", "gh:lark1")
    assert _card(c, "gh:lark1")["stage"] == "approved"
    out = c.post("/api/tracker/remove", json={"source_id": "gh:lark1"})
    assert out.status_code == 200 and out.get_json()["job"] is None
    for _ in range(2):                                   # the board catching up never re-adds it
        assert _card(c, "gh:lark1") is None
    assert c.get("/api/tracker/job?source_id=gh:lark1").status_code == 404
    recs = CVRecords(A.DB_PATH)
    try:
        assert recs.get(rid) is not None                 # the resume and its record are kept
    finally:
        recs.close()
    c.post("/api/tracker/undo", json={"source_id": "gh:lark1"})
    assert _card(c, "gh:lark1")["stage"] == "approved"
    # a saved job: removed with its bookmark, and Undo brings the bookmark back
    c.post("/api/jobs/save", json={"source_id": "gh:lark1", "saved": True})
    _job(c, "lc:4", "Designer")
    _sql(A, "UPDATE sourced_job SET saved=1 WHERE source_id='lc:4'")
    c.post("/api/tracker/remove", json={"source_id": "lc:4"})
    w = A._watchlist()
    try:
        assert w.board_job("lc:4")["saved"] is False and w.stage_of("lc:4") is None
    finally:
        w.close()
    c.post("/api/tracker/undo", json={"source_id": "lc:4"})
    w = A._watchlist()
    try:
        assert w.board_job("lc:4")["saved"] is True and w.stage_of("lc:4") == "saved"
    finally:
        w.close()
    # the person's own action (a drag, a save) puts a removed job back
    c.post("/api/tracker/remove", json={"source_id": "lc:4"})
    c.post("/api/tracker/move", json={"source_id": "lc:4", "stage": "saved"})
    assert _card(c, "lc:4")["stage"] == "saved"
    assert c.post("/api/tracker/remove", json={"source_id": "nope"}).status_code == 404


# --------------------------------------------------------------------------- the 30 day questions

def test_still_interested_waits_for_the_posting_check(A):
    c = A.app.test_client()
    _job(c, "lc:5", "Planner")
    _sql(A, "UPDATE sourced_job SET stage_at=? WHERE source_id='lc:5'", (_ago(31),))
    card = _card(c, "lc:5")
    assert card["still_interested"] is False             # not checked since it went quiet: check first
    _sql(A, "UPDATE sourced_job SET live_status='open', live_at=? WHERE source_id='lc:5'", (_ago(0),))
    card = _card(c, "lc:5")
    assert card["still_interested"] is True and card["needs_link"] is False
    c.post("/api/tracker/keep", json={"source_id": "lc:5"})
    assert _card(c, "lc:5")["still_interested"] is False  # Keep: the 30 days start again
    # a job with no link (a pasted description) cannot be checked: ask, with Add job link
    _job(c, "lc:6", "Clerk", url="")
    _sql(A, "UPDATE sourced_job SET stage_at=? WHERE source_id='lc:6'", (_ago(40),))
    card = _card(c, "lc:6")
    assert card["still_interested"] is True and card["needs_link"] is True and card["has_link"] is False
    # recent cards ask nothing
    _job(c, "lc:7", "Fresh")
    assert _card(c, "lc:7")["still_interested"] is False


def test_no_response_after_30_quiet_days(A):
    c = A.app.test_client()
    _job(c, "lc:8", "Analyst", stage="applied")
    assert _card(c, "lc:8")["no_response"] is False
    _sql(A, "UPDATE sourced_job SET stage_at=?, applied_at=? WHERE source_id='lc:8'", (_ago(35), _ago(35)))
    assert _card(c, "lc:8")["no_response"] is True
    c.post("/api/tracker/keep", json={"source_id": "lc:8"})
    assert _card(c, "lc:8")["no_response"] is False
    _sql(A, "UPDATE sourced_job SET kept_at=? WHERE source_id='lc:8'", (_ago(31),))
    w = A._watchlist()
    try:
        w.record_mail("lc:8", "m-1", subject="Re: your application", now=_ago(3))
    finally:
        w.close()
    assert _card(c, "lc:8")["no_response"] is False      # mail on it is movement


def test_an_expired_to_apply_posting_moves_to_closed(A, monkeypatch):
    from sourcing import liveness
    c = A.app.test_client()
    _job(c, "lc:9", "Operator", url="https://jobs.example.com/9")
    _job(c, "lc:10", "Mechanic", url="https://jobs.example.com/10")
    _job(c, "lc:11", "Pasted", url="")
    monkeypatch.setattr(liveness, "check_many", lambda items, **k: {
        sid: {"status": "closed" if sid == "lc:9" else "open", "why": "404"} for sid, _u, _s in items})
    out = A._todo_liveness_recheck()
    assert out == {"checked": 2, "closed": 1}            # the one without a link is not asked about
    gone = _card(c, "lc:9")
    assert (gone["stage"], gone["close_reason"]) == ("archived", "job_closed") and gone["closed_at"]
    assert _card(c, "lc:10")["live_status"] == "open" and _card(c, "lc:10")["stage"] == "saved"
    assert A._todo_liveness_recheck() == {"checked": 0, "closed": 0}   # checked again only later
    # dragged back to To apply: the person says it is open
    c.post("/api/tracker/move", json={"source_id": "lc:9", "stage": "saved"})
    back = _card(c, "lc:9")
    assert back["closed_at"] is None and back["close_reason"] == ""
    c.post("/api/tracker/undo", json={"source_id": "lc:9"})
    again = _card(c, "lc:9")
    assert again["close_reason"] == "job_closed" and again["closed_at"]


# --------------------------------------------------------------------------- Add job link

def test_add_job_link_makes_apply_possible(A):
    c = A.app.test_client()
    rid = _add_record(A, "Writer", "pasted:abc", url="")
    card = _card(c, "pasted:abc")
    assert card["apply_id"] == rid and card["has_link"] is False
    for bad in ("", "jobs.example.com/1", "ftp://x.example.com/1", "javascript:alert(1)", "https://nodot"):
        assert c.post("/api/tracker/link", json={"source_id": "pasted:abc", "url": bad}).status_code == 400, bad
    out = c.post("/api/tracker/link", json={"source_id": "pasted:abc", "url": "https://jobs.example.com/w"}).get_json()
    assert out["job"]["has_link"] is True
    item = c.get(f"/api/autoapply/item/{rid}").get_json()
    assert (item.get("item") or item).get("apply_url", "").startswith("https://jobs.example.com/w")
    assert c.post("/api/tracker/link", json={"source_id": "nope", "url": "https://a.example.com/"}).status_code == 404


# --------------------------------------------------------------------------- applied, automatically

def test_the_model_reads_the_confirmation_page(A, monkeypatch):
    calls = []

    def model(system, user):
        calls.append(user)
        return '{"submitted": "yes"}' if "received your application" in user else '{"submitted": "no"}'

    monkeypatch.setattr(A, "_confirm_model", model)
    rid = _add_record(A, "Data Analyst", "gh:lark2", url="https://boards.greenhouse.io/lark/jobs/2")
    c = A.app.test_client()
    assert _card(c, "gh:lark2")["stage"] == "approved"
    form = {"url": "https://boards.greenhouse.io/lark/jobs/2", "page_url": "https://boards.greenhouse.io/lark/jobs/2#app",
            "record_id": rid, "title": "Apply", "text": "First name Last name Email Resume Submit application", "fields": 9}
    assert c.post("/api/autofill/confirm-check", headers=H, json=form).get_json()["verdict"] == "no"
    assert c.post("/api/autofill/confirm-check", headers=H, json=form).get_json()["verdict"] == "no"
    assert len(calls) == 1                               # each page is asked about once
    assert _card(c, "gh:lark2")["stage"] == "approved"
    done = {**form, "page_url": "https://boards.greenhouse.io/lark/jobs/2/confirmation", "fields": 0,
            "text": "Thank you! We have received your application and will be in touch."}
    out = c.post("/api/autofill/confirm-check", headers=H, json=done).get_json()
    assert out["verdict"] == "yes" and out["matched"] is True and out["record_id"] == rid
    card = _card(c, "gh:lark2")
    assert card["stage"] == "applied" and card["applied_at"] == date.today().isoformat()
    # it is the shell's call only: a web page cannot reach it (no custom header)
    assert A.app.test_client().post("/api/autofill/confirm-check", json=done).status_code == 403


def test_no_ai_means_unsure_never_a_guess(A, monkeypatch):
    def boom(system, user):
        raise RuntimeError("offline")
    monkeypatch.setattr(A, "_confirm_model", boom)
    c = A.app.test_client()
    body = {"url": "https://a.example.com/j", "page_url": "https://a.example.com/done", "text": "Something happened on this page."}
    assert c.post("/api/autofill/confirm-check", headers=H, json=body).get_json()["verdict"] == "unsure"
    assert c.post("/api/autofill/confirm-check", headers=H, json={**body, "text": "short"}).get_json()["verdict"] == "unsure"


def test_did_you_submit_is_asked_once(A, monkeypatch):
    monkeypatch.setattr(A, "ASK_SUBMITTED_AFTER_S", 0)
    rid = _add_record(A, "Engineer", "gh:lark3", status="approved")
    c = A.app.test_client()
    c.post("/api/autoapply/fill-report", headers=H, json={"record_id": rid, "page": 1, "filled": 5, "left": []})
    assert _card(c, "gh:lark3")["ask_submitted"] is True
    c.post("/api/autoapply/not-yet", json={"record_id": rid})            # No
    assert _card(c, "gh:lark3")["ask_submitted"] is False
    assert _card(c, "gh:lark3")["stage"] == "approved"                   # it stays in To apply
    recs = CVRecords(A.DB_PATH)
    try:
        recs.merge_data(rid, {"filled_at": "2030-01-01T00:00:00+00:00"})  # another Autofill later
    finally:
        recs.close()
    monkeypatch.setattr(A.time, "time", lambda: 1_900_000_000.0)
    assert _card(c, "gh:lark3")["ask_submitted"] is False                # not asked again


# --------------------------------------------------------------------------- the pane's watch, under node

def test_the_page_watch_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_applied_watch.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert "ok applied watch" in out.stdout


def test_the_watch_is_wired_into_the_success_watch():
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    arm = main[main.index("function armSuccessWatch("):main.index("function disarmSuccessWatch(")]
    assert "appliedWatch.createModelWatch(" in arm and '"/api/autofill/confirm-check"' in arm
    assert arm.index("detectSuccess()") < arm.index("model.tick()")   # the recipe's markers first
    assert "model.navigated()" in arm
    assert "appliedWatch.announceSubmitted(" in arm          # the one shared submitted event
    assert "30 * 60 * 1000" in arm


def test_no_old_package_wording_is_left():
    import json
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        assert "Tracker" in d["aa.noLink"] or code != "en"
    js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    assert "Open the package" not in js
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert not [k for k, v in en.items() if "Open the package" in v]


def test_every_new_string_has_every_locale():
    import json
    import re
    keys = ("tracker.closedOne", "tracker.closedN", "tracker.loz.still", "tracker.loz.accepted", "tracker.loz.noResponseQ",
            "tracker.reason.noResponse", "tracker.reason.passed", "tracker.closedOn", "tracker.showOlder", "tracker.addLink",
            "tracker.linkLabel", "tracker.linkSave", "tracker.linkBad", "tracker.linkSaved", "tracker.noLongerOpen",
            "tracker.remove", "tracker.askNo", "tracker.keep", "tracker.closePassed", "tracker.closeNoResponse",
            "tracker.offerAccepted", "tracker.offerQ", "tracker.markAccepted", "tracker.removeFromTracker",
            "tracker.removed.toast", "tracker.kept", "aa.noLink")
    js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[—–]", d[k]), (code, k)
            if code != "en":
                assert d[k] != en[k] or len(en[k]) <= 3, (code, k)   # really translated
    for k in keys:
        assert f'"{k}"' in js, k
    assert not [k for k in keys if re.search(r"\bCV\b|résumé", en[k])]


def test_sqlite_columns_exist_on_a_fresh_db():
    path = tempfile.mktemp(suffix=".db")
    Watchlist(path).close()
    raw = sqlite3.connect(path)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(sourced_job)")}
    raw.close()
    assert {"close_reason", "accepted_at", "kept_at", "untracked_at", "live_status", "live_at"} <= cols


def test_a_card_never_shows_blank(A):
    """A resume chat started from a pasted description before its role was known left a board
    row with no title or company; the card takes the resume's role and the board's employer."""
    rid = _add_record(A, "Senior Analyst", "pasted:blank1", url="https://jobs.lever.co/acme-consulting/abc-123")
    w = A._watchlist()
    try:
        w.upsert_jobs([{"source_id": "pasted:blank1", "source": "pasted", "company": "", "title": "", "location": "",
                        "remote": "", "url": "", "jd_text": "", "posted_at": "", "salary": ""}])
        w.track_job("pasted:blank1", "applied")
    finally:
        w.close()
    recs = CVRecords(A.DB_PATH)
    try:
        recs._conn.execute("UPDATE cv_record SET company='' WHERE id=?", (rid,))
        recs._conn.commit()
    finally:
        recs.close()
    card = _card(A.app.test_client(), "pasted:blank1")
    assert (card["title"], card["company"], card["has_link"]) == ("Senior Analyst", "Acme Consulting", True)
    assert A._company_from_link("https://www.example.com/careers/123") == ""
