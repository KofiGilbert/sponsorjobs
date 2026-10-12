"""The job tracker board (Huntr / Teal style, local-first): one stage per job the person saved,
tailored, or applied to, on the SAME sourced_job rows the Jobs page uses (sourcing/board.py), the
/api/tracker/* routes, and the funnel math. Clocks are pinned everywhere a date is asserted."""

from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
from datetime import date

import pytest

from sourcing.board import (FOLLOW_UP_DAYS, STAGES, TAILORED_SOURCE, company_key, export_csv,
                            export_rows, funnel_metrics, suggest_stage, week_start)
from sourcing.watchlist import Watchlist

TODAY = date(2026, 10, 8)                     # a Thursday; its week starts Mon 2026-10-05
NOW = "2026-10-08T10:00:00+00:00"
JOB = {"source_id": "freehire::abc", "source": "freehire", "company": "Acme Robotics, Inc.",
       "title": "Data Analyst", "location": "NYC", "remote": "", "url": "http://x",
       "posted_at": "2026-10-01", "salary": "$120k/yr", "jd_text": ""}
JOB2 = {**JOB, "source_id": "freehire::def", "company": "Beta", "title": "Engineer"}


def _w() -> Watchlist:
    return Watchlist(tempfile.mktemp(suffix=".db"))


# --- store / migration -------------------------------------------------------------------------

def test_migration_adds_board_columns_and_tables_to_an_older_db():
    path = tempfile.mktemp(suffix=".db")
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE sourced_job (
        source_id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT,
        remote TEXT, url TEXT, jd_text TEXT, posted_at TEXT, first_seen TEXT, is_new INTEGER,
        dismissed INTEGER DEFAULT 0, last_seen TEXT)""")
    raw.execute("INSERT INTO sourced_job(source_id, company, title) VALUES('old::1', 'Old', 'Role')")
    raw.commit(); raw.close()
    w = Watchlist(path)
    cols = {r[1] for r in w._conn.execute("PRAGMA table_info(sourced_job)")}
    assert {"stage", "stage_at", "applied_at", "follow_up_at", "notes"} <= cols
    tables = {r[0] for r in w._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tracker_activity", "tracker_contact"} <= tables
    assert w.stage_of("old::1") is None            # an old row is not on the board until touched
    assert w.board_jobs() == []
    w.close()
    Watchlist(path).close()                        # idempotent on a second open


def test_track_move_and_activity_log():
    w = _w()
    w.upsert_jobs([JOB])
    w.track_job(JOB["source_id"], "saved", note="first", now=NOW)
    assert w.stage_of(JOB["source_id"]) == "saved"
    w.track_job(JOB["source_id"], "applied", now=NOW)     # already on the board: left where it is
    assert w.stage_of(JOB["source_id"]) == "saved"

    row = w.move_stage(JOB["source_id"], "applied", note="sent it", now=NOW)
    assert row["stage"] == "applied" and row["applied_at"] == NOW
    assert row["follow_up_at"] == "2026-10-14"            # NOW + FOLLOW_UP_DAYS, armed on Applied
    assert FOLLOW_UP_DAYS == 6

    same = w.move_stage(JOB["source_id"], "applied", now=NOW)   # no-op, no activity row
    assert same["stage"] == "applied"
    row = w.move_stage(JOB["source_id"], "interviewing", now="2026-10-12T09:00:00+00:00")
    assert row["applied_at"] == NOW                       # the applied date survives progress

    acts = w.activity(JOB["source_id"])
    assert [(a["from_stage"], a["to_stage"]) for a in acts] == \
        [("applied", "interviewing"), ("saved", "applied"), (None, "saved")]
    assert acts[1]["note"] == "sent it" and acts[-1]["note"] == "first"

    with pytest.raises(ValueError):
        w.move_stage(JOB["source_id"], "nope")
    with pytest.raises(KeyError):
        w.move_stage("missing::x", "applied")
    w.close()


def test_back_to_saved_means_not_applied_and_closed_stages_drop_the_nudge():
    w = _w()
    w.upsert_jobs([JOB, JOB2])
    w.track_job(JOB["source_id"], "applied", now=NOW)
    row = w.move_stage(JOB["source_id"], "saved", now=NOW)
    assert row["applied_at"] is None and row["follow_up_at"] is None
    w.track_job(JOB2["source_id"], "applied", now=NOW)
    row = w.move_stage(JOB2["source_id"], "rejected", now=NOW)
    assert row["applied_at"] == NOW and row["follow_up_at"] is None
    w.close()


def test_interviewing_straight_from_saved_stamps_an_applied_date():
    w = _w()
    w.upsert_jobs([JOB])
    w.track_job(JOB["source_id"], "saved", now=NOW)
    row = w.move_stage(JOB["source_id"], "interviewing", now=NOW)
    assert row["applied_at"] == NOW                        # it was applied to, just not logged
    w.close()


def test_bookmark_and_board_stay_in_step():
    w = _w()
    w.set_saved(JOB["source_id"], True, JOB)                # saving puts it in the Saved column
    assert w.stage_of(JOB["source_id"]) == "saved"
    w.set_saved(JOB["source_id"], False)                    # un-saving a merely Saved job removes it
    assert w.stage_of(JOB["source_id"]) is None
    assert w.activity(JOB["source_id"])[0]["to_stage"] == ""   # the exit is logged honestly

    w.set_saved(JOB["source_id"], True)
    w.move_stage(JOB["source_id"], "applied", now=NOW)
    w.set_saved(JOB["source_id"], False)                    # past Saved: the board keeps it
    assert w.stage_of(JOB["source_id"]) == "applied"
    w.close()


def test_tracked_rows_survive_pruning_and_tailored_rows_stay_off_the_feed():
    w = _w()
    w.upsert_jobs([JOB, JOB2, {**JOB, "source_id": f"{TAILORED_SOURCE}:7", "source": TAILORED_SOURCE}])
    w.track_job(JOB["source_id"], "applied", now=NOW)
    w._conn.execute("UPDATE sourced_job SET last_seen=datetime('now', '-90 days')")
    w._conn.commit()
    assert w.prune_stale(days=21) == 2                      # the untracked feed rows go
    assert w.get_job(JOB["source_id"]) is not None
    assert w.stage_of(JOB["source_id"]) == "applied"
    w.upsert_jobs([JOB2, {**JOB, "source_id": f"{TAILORED_SOURCE}:7", "source": TAILORED_SOURCE}])
    assert {j["source_id"] for j in w.list_jobs()} == {JOB["source_id"], JOB2["source_id"]}
    w.close()


def test_notes_follow_up_contacts_and_due_list():
    w = _w()
    w.upsert_jobs([JOB, JOB2])
    w.track_job(JOB["source_id"], "applied", now=NOW)
    w.set_notes(JOB["source_id"], "  spoke to Ann  ")
    assert w.board_job(JOB["source_id"])["notes"] == "  spoke to Ann  "
    assert w.set_follow_up(JOB["source_id"], "2026-10-08") == "2026-10-08"
    with pytest.raises(ValueError):
        w.set_follow_up(JOB["source_id"], "next tuesday")
    assert [d["source_id"] for d in w.due_followups(TODAY)] == [JOB["source_id"]]
    assert w.due_followups(date(2026, 10, 7)) == []        # not yet
    w.move_stage(JOB["source_id"], "offer", now=NOW)
    assert w.due_followups(TODAY) == []                     # an offer no longer wants a nudge
    assert w.set_follow_up(JOB["source_id"], "") is None

    cid = w.add_contact(JOB["source_id"], " Ann Lee ", "Recruiter", "ann@acme.com", "https://linkedin.com/in/ann")
    with pytest.raises(ValueError):
        w.add_contact(JOB["source_id"], "   ")
    cs = w.contacts(JOB["source_id"])
    assert [c["name"] for c in cs] == ["Ann Lee"] and cs[0]["id"] == cid
    assert w.remove_contact(cid) and w.contacts(JOB["source_id"]) == []
    w.close()


# --- funnel math (pinned clock) ----------------------------------------------------------------

def _rows():
    return [
        {"source_id": "a", "stage": "applied", "applied_at": "2026-10-06", "interviewed_at": None, "offered_at": None, "rejected_at": None},
        {"source_id": "b", "stage": "interviewing", "applied_at": "2026-10-07T09:00:00", "interviewed_at": "2026-10-08T12:00:00", "offered_at": None, "rejected_at": None},
        {"source_id": "c", "stage": "offer", "applied_at": "2026-09-01", "interviewed_at": "2026-09-11", "offered_at": "2026-09-20", "rejected_at": None},
        {"source_id": "d", "stage": "rejected", "applied_at": "2026-09-28", "interviewed_at": "2026-10-01", "offered_at": None, "rejected_at": "2026-10-03"},
        {"source_id": "e", "stage": "saved", "applied_at": None, "interviewed_at": None, "offered_at": None, "rejected_at": None},
        {"source_id": "f", "stage": "rejected", "applied_at": "2026-08-30", "interviewed_at": None, "offered_at": None, "rejected_at": "2026-09-02"},
        {"source_id": "g", "stage": "applied", "applied_at": "2026-10-04", "interviewed_at": None, "offered_at": None, "rejected_at": None},  # last Sunday: last week, not this one
    ]


def test_week_start_is_monday():
    assert week_start(TODAY) == date(2026, 10, 5)
    assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)
    assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)


def test_funnel_metrics_with_fixed_dates():
    m = funnel_metrics(_rows(), TODAY, weekly_goal=4)
    assert m["week_start"] == "2026-10-05"
    assert m["applied_total"] == 6                          # every row with an applied date
    assert m["applied_week"] == 2                           # a (Oct 6) and b (Oct 7); g was last week
    assert m["interviews"] == 3                             # b, c, d (d was rejected AFTER an interview)
    assert m["offers"] == 1
    assert m["rejected"] == 2
    assert m["pct_interview"] == 50.0                       # 3 / 6
    assert m["pct_offer"] == 33.3                           # 1 / 3
    assert m["median_days_to_interview"] == 3               # gaps 1 (b), 10 (c), 3 (d) -> 3
    assert m["goal"] == {"target": 4, "done": 2, "pct": 50.0}
    assert len(m["weeks"]) == 12
    assert m["weeks"][-1] == {"week": "2026-10-05", "count": 2}
    assert m["weeks"][-2] == {"week": "2026-09-28", "count": 2}      # d (Sep 28) and g (Sun Oct 4)
    assert m["weeks"][0]["week"] == "2026-07-20"
    assert sum(w["count"] for w in m["weeks"]) == 6                  # every applied date is in the window
    assert next(w for w in m["weeks"] if w["week"] == "2026-08-24")["count"] == 1   # f (Sun Aug 30)
    assert next(w for w in m["weeks"] if w["week"] == "2026-08-31")["count"] == 1   # c (Sep 1)


def test_funnel_metrics_empty_and_even_median():
    m = funnel_metrics([], TODAY)
    assert m["applied_total"] == 0 and m["pct_interview"] == 0.0 and m["median_days_to_interview"] is None
    assert m["goal"] == {"target": 0, "done": 0, "pct": 0}
    rows = [{"source_id": "a", "stage": "interviewing", "applied_at": "2026-10-01", "interviewed_at": "2026-10-03"},
            {"source_id": "b", "stage": "interviewing", "applied_at": "2026-10-01", "interviewed_at": "2026-10-08"}]
    assert funnel_metrics(rows, TODAY)["median_days_to_interview"] == 4.5
    assert funnel_metrics(rows, TODAY, weekly_goal=1)["goal"] == {"target": 1, "done": 0, "pct": 0.0}  # last week
    this_week = [{"source_id": "a", "stage": "applied", "applied_at": "2026-10-05"},
                 {"source_id": "b", "stage": "applied", "applied_at": "2026-10-08"}]
    assert funnel_metrics(this_week, TODAY, weekly_goal=1)["goal"] == {"target": 1, "done": 2, "pct": 100}  # capped


def test_export_rows_only_applied_jobs_oldest_first_and_csv_shape():
    jobs = [
        {"source_id": "b", "stage": "interviewing", "applied_at": "2026-10-07T09:00:00", "company": "Beta",
         "title": "Engineer", "location": "Remote", "url": "http://b", "notes": "two\nlines  here"},
        {"source_id": "a", "stage": "rejected", "applied_at": "2026-09-01", "company": "Acme",
         "title": "Analyst", "location": "NYC", "url": "http://a", "notes": ""},
        {"source_id": "s", "stage": "saved", "applied_at": None, "company": "Nope", "title": "x"},
    ]
    rows = export_rows(jobs)
    assert [r["Employer"] for r in rows] == ["Acme", "Beta"]
    assert rows[0]["Result"] == "Not selected" and rows[1]["Result"] == "Interview"
    assert rows[1]["Notes"] == "two lines here" and rows[1]["Date applied"] == "2026-10-07"
    csv_text = export_csv(jobs)
    lines = csv_text.split("\r\n")
    assert lines[0].startswith("Date applied,Employer,Position,Location,Contact method")
    assert lines[1] == "2026-09-01,Acme,Analyst,NYC,Online application,http://a,Not selected,"
    assert "—" not in csv_text and "–" not in csv_text


def test_inbox_suggestion_matches_by_company_and_never_forces():
    jobs = [{"source_id": "freehire::abc", "company": "Acme Robotics, Inc.", "title": "Data Analyst", "stage": "applied"},
            {"source_id": "freehire::zzz", "company": "Beta", "title": "Engineer", "stage": "interviewing"}]
    assert company_key("Acme Robotics, Inc.") == "acme robotics"
    s = suggest_stage({"from": "jobs@acme-robotics.com", "subject": "Next steps", "snippet": "", "status": "advanced"}, jobs)
    assert s == {"source_id": "freehire::abc", "company": "Acme Robotics, Inc.", "title": "Data Analyst",
                 "from_stage": "applied", "stage": "interviewing"}
    s = suggest_stage({"from": "no-reply@acmerobotics.com", "subject": "Your application", "snippet": "", "status": "rejected"}, jobs)
    assert s and s["stage"] == "rejected"
    # Already interviewing at Beta: an "advanced" mail suggests nothing new.
    assert suggest_stage({"from": "hr@beta.com", "subject": "Beta interview", "snippet": "", "status": "advanced"}, jobs) is None
    assert suggest_stage({"from": "friend@gmail.com", "subject": "lunch?", "snippet": "", "status": ""}, jobs) is None


# --- endpoints ---------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as A
    importlib.reload(A)
    return A


def _save(c, job=JOB):
    r = c.post("/api/jobs/save", json={"source_id": job["source_id"], "saved": True, "job": job})
    assert r.status_code == 200


def test_board_lists_by_stage_and_move_logs(app):
    c = app.app.test_client()
    _save(c)
    b = c.get("/api/tracker/board").get_json()
    assert b["stages"] == list(STAGES)
    assert [x["company"] for x in b["columns"]["saved"]] == [JOB["company"]]
    assert b["total"] == 1 and b["goal"] == 10                   # the default weekly goal

    r = c.post("/api/tracker/move", json={"source_id": JOB["source_id"], "stage": "applied", "note": "dragged"})
    assert r.status_code == 200
    card = r.get_json()["job"]
    assert card["stage"] == "applied" and card["applied_at"] and card["follow_up_at"]
    only = c.get("/api/tracker/board?stage=applied").get_json()
    assert list(only["columns"]) == ["applied"] and len(only["columns"]["applied"]) == 1
    assert c.get("/api/tracker/board?stage=bogus").status_code == 400

    acts = c.get(f"/api/tracker/activity?source_id={JOB['source_id']}").get_json()["activity"]
    assert acts[0]["to_stage"] == "applied" and acts[0]["note"] == "dragged"
    assert c.post("/api/tracker/move", json={"source_id": JOB["source_id"], "stage": "nope"}).status_code == 400
    assert c.post("/api/tracker/move", json={"stage": "applied"}).status_code == 400


def test_move_from_the_feed_needs_a_snapshot_for_an_unknown_job(app):
    c = app.app.test_client()
    assert c.post("/api/tracker/move", json={"source_id": JOB2["source_id"], "stage": "saved"}).status_code == 404
    r = c.post("/api/tracker/move", json={"source_id": JOB2["source_id"], "stage": "saved", "job": JOB2})
    assert r.status_code == 200 and r.get_json()["job"]["title"] == "Engineer"
    assert c.get("/api/tracker/activity?source_id=missing::x").status_code == 404


def test_side_panel_notes_follow_up_and_contacts(app):
    c = app.app.test_client()
    _save(c)
    sid = JOB["source_id"]
    assert c.post("/api/tracker/note", json={"source_id": "missing::x", "notes": "x"}).status_code == 404
    assert c.post("/api/tracker/note", json={"source_id": sid, "notes": "call back Monday"}).get_json()["notes"] == "call back Monday"
    assert c.post("/api/tracker/followup", json={"source_id": sid, "date": "soon"}).status_code == 400
    r = c.post("/api/tracker/followup", json={"source_id": sid, "date": "2020-01-01"}).get_json()
    assert r["follow_up_at"] == "2020-01-01" and r["due_count"] == 0     # Saved jobs are not nudged
    c.post("/api/tracker/move", json={"source_id": sid, "stage": "applied"})
    assert c.get("/api/tracker/metrics").get_json()["due_count"] == 1    # applied + a past date = due
    r = c.post("/api/tracker/followup", json={"source_id": sid, "date": ""}).get_json()
    assert r["follow_up_at"] is None and r["due_count"] == 0

    assert c.post("/api/tracker/contact", json={"source_id": sid, "name": " "}).status_code == 400
    r = c.post("/api/tracker/contact", json={"source_id": sid, "name": "Ann", "role": "Recruiter",
                                              "email": "ann@acme.com", "linkedin": "https://linkedin.com/in/ann"}).get_json()
    cid = r["id"]
    assert [x["name"] for x in r["contacts"]] == ["Ann"]
    panel = c.get(f"/api/tracker/job?source_id={sid}").get_json()
    assert panel["notes"] == "call back Monday" and panel["contacts"][0]["email"] == "ann@acme.com"
    assert panel["stage"] == "applied" and panel["records"] == [] and panel["activity"]
    assert c.post("/api/tracker/contact/remove", json={"source_id": sid, "id": 999}).status_code == 404
    assert c.post("/api/tracker/contact/remove", json={"source_id": sid, "id": cid}).get_json()["contacts"] == []
    assert c.get("/api/tracker/job?source_id=missing::x").status_code == 404


def test_goal_setting_and_export_csv(app):
    c = app.app.test_client()
    assert c.post("/api/tracker/goal", json={"target": "many"}).status_code == 400
    assert c.post("/api/tracker/goal", json={"target": 9000}).get_json()["target"] == 500   # clamped
    assert c.post("/api/tracker/goal", json={"target": 7}).get_json()["target"] == 7
    assert c.get("/api/tracker/metrics").get_json()["goal"]["target"] == 7
    _save(c)
    c.post("/api/tracker/move", json={"source_id": JOB["source_id"], "stage": "applied"})
    r = c.get("/api/tracker/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment; filename=\"applications-" in r.headers["Content-Disposition"]
    import csv
    import io
    rows = list(csv.reader(io.StringIO(r.data.decode("utf-8"))))
    assert rows[0][:3] == ["Date applied", "Employer", "Position"]
    assert rows[1][1:3] == ["Acme Robotics, Inc.", "Data Analyst"]


def test_tailored_records_land_on_the_board_and_the_review_queue_moves_it(app):
    from ui.records import CVRecords
    c = app.app.test_client()
    _save(c)
    recs = CVRecords(app.DB_PATH)
    # A resume tailored for the saved posting, then applied through the review queue.
    rid = recs.add("Data Analyst", JOB["company"], 70, "", data={
        "status": "applied", "applied_at": "2026-10-06T10:00:00+00:00",
        "source_job": {"source_id": JOB["source_id"], "source": "freehire", "url": JOB["url"]}})
    # A resume tailored from a PASTED JD: no sourced posting, so it gets a board-only row.
    rid2 = recs.add("Platform Engineer", "Gamma", 60, "", jd_label="Platform Engineer · Gamma",
                    data={"status": "ready"})
    recs.close()
    b = c.get("/api/tracker/board").get_json()
    applied = b["columns"]["applied"]
    assert [x["company"] for x in applied] == [JOB["company"]]      # pulled forward from Saved
    assert applied[0]["applied_at"] == "2026-10-06" and applied[0]["record"]["id"] == rid
    saved = b["columns"]["saved"]
    assert [x["source_id"] for x in saved] == [f"{TAILORED_SOURCE}:{rid2}"]
    assert saved[0]["title"] == "Platform Engineer" and saved[0]["company"] == "Gamma"
    # ...and that board-only row never shows on the Jobs feed.
    assert all(j["source"] != TAILORED_SOURCE for j in c.get("/api/jobs?saved=1").get_json()["jobs"])

    # The review queue's own stage picker drives the board, both ways.
    assert c.post(f"/api/record/{rid}/stage", json={"stage": "interviewing"}).status_code == 200
    assert c.get("/api/tracker/board").get_json()["columns"]["interviewing"][0]["source_id"] == JOB["source_id"]
    assert c.post(f"/api/record/{rid}/status", json={"status": "ready"}).status_code == 200
    assert [x["source_id"] for x in c.get("/api/tracker/board").get_json()["columns"]["saved"]] \
        == [JOB["source_id"], f"{TAILORED_SOURCE}:{rid2}"] or \
        set(x["source_id"] for x in c.get("/api/tracker/board").get_json()["columns"]["saved"]) \
        == {JOB["source_id"], f"{TAILORED_SOURCE}:{rid2}"}

    # A board move patches the record so the apply to-do agrees with the board.
    c.post("/api/tracker/move", json={"source_id": JOB["source_id"], "stage": "offer"})
    rec = next(x for x in c.get("/api/records").get_json()["records"] if x["id"] == rid)
    assert rec["stage"] == "offer"
    panel = c.get(f"/api/tracker/job?source_id={JOB['source_id']}").get_json()
    assert [r["id"] for r in panel["records"]] == [rid]


def test_inbox_reply_only_suggests_a_move(app):
    c = app.app.test_client()
    _save(c)
    c.post("/api/tracker/move", json={"source_id": JOB["source_id"], "stage": "applied"})
    entries = [{"from": "talent@acmerobotics.com", "sender": "Acme Talent", "subject": "Next steps",
                "snippet": "We'd love to schedule a call", "status": "advanced"},
               {"from": "x@nowhere.com", "sender": "", "subject": "hi", "snippet": "", "status": ""}]
    app._board_suggestions(entries)
    assert entries[0]["suggest"]["stage"] == "interviewing"
    assert entries[0]["suggest"]["source_id"] == JOB["source_id"]
    assert entries[1]["suggest"] is None
    # Nothing moved on its own.
    assert c.get("/api/tracker/board").get_json()["columns"]["interviewing"] == []
