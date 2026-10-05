"""Application tracker (feature #3): the honest funnel (saved -> applied -> interviewing -> offer /
rejected) and the in-app follow-up nudge. The pure logic lives in submit/tracker.py (no DB / Flask,
so it is exact and testable); the endpoints wire it into the records store."""
from __future__ import annotations

import json
import types
from datetime import date, timedelta

from submit.tracker import (FOLLOW_UP_DAYS, STAGES, effective_stage, follow_up,
                            stage_patch)
from ui.records import CVRecords

NOW = "2026-08-17T09:00:00+00:00"
TODAY = date(2026, 8, 17)


# --- pure logic --------------------------------------------------------------------------------

def test_applied_arms_a_follow_up_six_days_out():
    patch = stage_patch({}, "applied", NOW)
    assert patch["stage"] == "applied" and patch["status"] == "applied"
    assert patch["applied_at"] == NOW
    assert patch["follow_up_due"] == (TODAY + timedelta(days=FOLLOW_UP_DAYS)).isoformat()


def test_progressing_past_applied_clears_the_nudge():
    # Once you are interviewing (or have an offer, or were rejected), a "did they see it?" nudge
    # no longer helps -> follow-up cleared, but it stays out of the apply to-do (status applied).
    for stage in ("interviewing", "offer", "rejected"):
        patch = stage_patch({"applied_at": NOW}, stage, NOW)
        assert patch["stage"] == stage
        assert patch["status"] == "applied"
        assert patch["follow_up_due"] is None


def test_back_to_saved_returns_it_to_the_todo():
    patch = stage_patch({"applied_at": NOW, "follow_up_due": "2026-08-23"}, "saved", NOW)
    assert patch["status"] == "ready"          # back on the apply to-do
    assert patch["applied_at"] is None and patch["follow_up_due"] is None


def test_re_applying_keeps_the_original_applied_date():
    patch = stage_patch({"applied_at": "2026-08-01T00:00:00+00:00"}, "applied", NOW)
    assert patch["applied_at"] == "2026-08-01T00:00:00+00:00"
    assert patch["follow_up_due"] == (date(2026, 8, 1) + timedelta(days=FOLLOW_UP_DAYS)).isoformat()


def test_unknown_stage_is_rejected():
    try:
        stage_patch({}, "ghosted", NOW)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_effective_stage_defaults_from_legacy_status():
    assert effective_stage({}) == "saved"
    assert effective_stage({"status": "applied"}) == "applied"   # older rows, no 'stage' field
    assert effective_stage({"stage": "offer", "status": "applied"}) == "offer"


def test_follow_up_is_due_only_when_the_date_has_arrived():
    applied = {"stage": "applied", "follow_up_due": "2026-08-17"}
    assert follow_up(applied, TODAY)["is_due"] is True
    assert follow_up(applied, date(2026, 8, 16))["is_due"] is False
    # not applied, or no due date -> no nudge
    assert follow_up({"stage": "interviewing", "follow_up_due": "2026-08-17"}, TODAY) is None
    assert follow_up({"stage": "applied"}, TODAY) is None


# --- endpoints ---------------------------------------------------------------------------------

def _app(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    return app


def test_stage_endpoint_moves_the_funnel_and_lists_follow_up(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={"status": "ready"})
    recs.close()
    c = app.app.test_client()

    r = c.post(f"/api/record/{rid}/stage", json={"stage": "applied"}).get_json()
    assert r["stage"] == "applied" and r["follow_up_due"]

    # It now appears in /api/records with the funnel + follow-up fields.
    rec = next(x for x in c.get("/api/records").get_json()["records"] if x["id"] == rid)
    assert rec["stage"] == "applied" and rec["follow_up_due"] == r["follow_up_due"]

    # Moving to interviewing clears the nudge.
    r2 = c.post(f"/api/record/{rid}/stage", json={"stage": "interviewing"}).get_json()
    assert r2["stage"] == "interviewing" and r2["follow_up_due"] is None


def test_stage_endpoint_validates_and_404s(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = app.app.test_client()
    assert c.post("/api/record/1/stage", json={"stage": "nope"}).status_code == 400
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Eng", "Acme", 50, "", data={})
    recs.close()
    assert c.post(f"/api/record/{rid}/stage", json={"stage": "applied"}).status_code == 200
    assert c.post("/api/record/999999/stage", json={"stage": "applied"}).status_code == 404


def test_mark_applied_via_status_endpoint_also_arms_the_nudge(tmp_path, monkeypatch):
    # The existing "mark applied" button now sets a follow-up date too (shared code path).
    app = _app(tmp_path, monkeypatch)
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Eng", "Acme", 60, "", data={"status": "ready"})
    recs.close()
    r = app.app.test_client().post(f"/api/record/{rid}/status", json={"status": "applied"}).get_json()
    assert r["status"] == "applied" and r["follow_up_due"]


def test_followups_endpoint_splits_due_from_upcoming(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    recs = CVRecords(app.DB_PATH)
    past = (date.today() - timedelta(days=1)).isoformat()
    soon = (date.today() + timedelta(days=3)).isoformat()
    recs.add("Due role", "A", 70, "", data={"stage": "applied", "follow_up_due": past})
    recs.add("Soon role", "B", 70, "", data={"stage": "applied", "follow_up_due": soon})
    recs.add("Interviewing", "C", 70, "", data={"stage": "interviewing", "follow_up_due": past})
    recs.close()
    out = app.app.test_client().get("/api/followups").get_json()
    assert out["due_count"] == 1
    assert [x["role"] for x in out["due"]] == ["Due role"]
    assert [x["role"] for x in out["upcoming"]] == ["Soon role"]  # interviewing one is excluded
