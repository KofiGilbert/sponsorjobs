"""Per-site submission policy + routing (CLAUDE.md §7).

Two lanes: AUTO only for sites verified in the shipped evidence-based allowlist; ASSISTED
everywhere else and by default. Conservative — a wrong auto-submit is worse than an extra
click. The real submission is a pluggable driver, so routing is fully testable offline.
"""

from __future__ import annotations

from submit import classify_record, submit_record
from submit.policy import submission_policy


def test_prohibiting_sites_are_always_assisted():
    for url in ("https://www.linkedin.com/jobs/view/123",
                "https://indeed.com/viewjob?jk=1", "https://glassdoor.com/job/x",
                "https://careers.ziprecruiter.com/x", "https://monster.com/j/1",
                "https://dice.com/job/1"):
        assert submission_policy(url) == "assisted"


def test_verified_recruitee_is_auto():
    assert submission_policy("https://acme.recruitee.com/o/senior-engineer") == "auto"
    assert submission_policy("https://recruitee.com/o/x") == "auto"


def test_employer_key_ats_are_assisted_not_auto():
    # Greenhouse/Lever/Ashby/Workable/SmartRecruiters have official APIs but they need the
    # EMPLOYER's key -> not applicant-usable -> assisted.
    for url in ("https://boards.greenhouse.io/acme/jobs/1",
                "https://jobs.lever.co/acme/1", "https://jobs.ashbyhq.com/acme/1",
                "https://acme.workable.com/j/ABC", "https://jobs.smartrecruiters.com/acme/1"):
        assert submission_policy(url) == "assisted"


def test_unknown_and_empty_default_to_assisted():
    assert submission_policy("https://some-random-portal.example.com/apply") == "assisted"
    assert submission_policy("") == "assisted"
    assert submission_policy("not a url") == "assisted"


def test_no_runtime_path_can_add_to_auto():
    # There is intentionally no config/inference hook — auto comes ONLY from the shipped
    # allowlist. submit.policy exposes no _config_auto_hosts / _POLICY_FILE to override.
    import submit.policy as pol
    assert not hasattr(pol, "_config_auto_hosts")
    assert not hasattr(pol, "_POLICY_FILE")


def test_classify_carries_evidence():
    gh = classify_record({"source_job": {"url": "https://boards.greenhouse.io/a/jobs/1"}})
    assert gh["tier"] == "assisted" and gh["can_auto"] is False
    assert gh["evidence"]["ats"] == "Greenhouse"
    assert gh["evidence"]["applicant_usable"] is False and gh["evidence"]["doc_url"]
    rc = classify_record({"source_job": {"url": "https://acme.recruitee.com/o/x"}})
    assert rc["tier"] == "auto" and rc["can_auto"] is True
    assert rc["evidence"]["ats"] == "Recruitee" and rc["evidence"]["applicant_usable"] is True
    assert classify_record({})["tier"] == "assisted"      # no url -> assisted


def test_submit_assisted_path():
    a = submit_record({"source_job": {"url": "https://linkedin.com/jobs/1"}})
    assert a["ok"] is True and a["status"] == "assisted"


def test_submit_auto_needs_a_driver_then_uses_it():
    rec = {"source_job": {"url": "https://acme.recruitee.com/o/eng"}}

    # Eligible but no driver wired -> honest "pending", never a fake success.
    pending = submit_record(rec)
    assert pending["ok"] is False and pending["status"] == "auto_pending"

    seen = {}
    def ok_driver(url, data):
        seen["url"] = url
        return {"ok": True, "detail": "Submitted to Recruitee."}
    r = submit_record(rec, driver=ok_driver, now="2026-07-14T10:00")
    assert r["ok"] is True and r["status"] == "auto_submitted" and r["submitted_at"]
    assert seen["url"] == "https://acme.recruitee.com/o/eng"


def test_submit_driver_raise_never_crashes():
    rec = {"source_job": {"url": "https://acme.recruitee.com/o/eng"}}
    def boom(url, data):
        raise RuntimeError("network down")
    bad = submit_record(rec, driver=boom)
    assert bad["ok"] is False and bad["status"] == "auto_failed" and "network down" in bad["message"]


def test_driver_needs_assist_drops_to_assisted():
    # A captcha/auth wall -> the driver signals needs_assist -> we drop to assisted, never evade.
    rec = {"source_job": {"url": "https://acme.recruitee.com/o/eng"}}
    def walled(url, data):
        return {"ok": False, "needs_assist": True, "detail": "captcha wall"}
    r = submit_record(rec, driver=walled)
    assert r["tier"] == "assisted" and r["status"] == "assisted"


def test_submit_endpoint_surfaces_lane_and_records_outcome(tmp_path, monkeypatch):
    import types

    import ui.app as app
    from ui.records import CVRecords
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    monkeypatch.setattr(app, "_sponsors", lambda: types.SimpleNamespace(lookup=lambda c: None))
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "",
                   data={"status": "ready", "source_job": {"url": "https://linkedin.com/jobs/1"}})
    recs.close()
    client = app.app.test_client()

    assert client.get(f"/api/record/{rid}").get_json()["submission"]["tier"] == "assisted"
    r = client.post(f"/api/record/{rid}/submit", json={}).get_json()
    assert r["ok"] is True and r["tier"] == "assisted" and r["status"] == "assisted"
    saved = client.get(f"/api/record/{rid}").get_json()
    assert saved["submission"]["result"]["status"] == "assisted"


def _applied_record(tmp_path, monkeypatch):
    import ui.app as app
    from ui.records import CVRecords
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={
        "status": "applied", "source_job": {"url": "https://x.recruitee.com/o/1"}})
    recs.close()
    return app, rid


def test_submit_endpoint_is_idempotent_for_an_applied_record(tmp_path, monkeypatch):
    """A retry/double-click on an already-applied record must NOT fire a second live
    submission or burn another cap slot."""
    app, rid = _applied_record(tmp_path, monkeypatch)
    r = app.app.test_client().post(f"/api/record/{rid}/submit", json={}).get_json()
    assert r["ok"] is True and r["status"] == "already_applied"


def test_batch_approve_is_idempotent_for_an_applied_record(tmp_path, monkeypatch):
    app, rid = _applied_record(tmp_path, monkeypatch)
    out = app.app.test_client().post("/api/review/approve", json={"approve": [rid]}).get_json()
    assert "already submitted" in out["approved"][0]["message"].lower()
