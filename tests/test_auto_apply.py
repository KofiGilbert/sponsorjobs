"""Auto-apply on verified ground (CLAUDE.md §7).

The AUTO lane is a curated, evidence-based allowlist that ships with the product; nothing
at runtime can add to it. Recruitee is the one verified applicant-usable ATS. The Recruitee
driver submits via the keyless Careers Site API and aborts to assisted on any challenge —
never evades. Autonomous submission is opt-in and OFF by default.
"""

from __future__ import annotations

import ui.app as app
from submit import allowlist, drivers
from submit.drivers import recruitee_submit


# --- allowlist: shipped, evidence-based, maintainer-only --------------------- #

def test_only_applicant_usable_and_enabled_are_auto():
    hosts = allowlist.auto_hosts()
    assert "recruitee.com" in hosts
    # Every auto host must be BOTH applicant_usable AND enabled — never one without the other.
    for e in allowlist.REGISTRY:
        if any(h in hosts for h in e.hosts):
            assert e.applicant_usable and e.auto_enabled


def test_employer_key_ats_are_present_but_not_auto():
    # They're documented as evidence (why they're assisted), but never in the auto set.
    hosts = allowlist.auto_hosts()
    for name in ("Greenhouse", "Lever", "Ashby", "Workable", "SmartRecruiters"):
        e = next(x for x in allowlist.REGISTRY if x.ats == name)
        assert e.applicant_usable is False and e.auto_eligible is False
        assert not any(h in hosts for h in e.hosts)
        assert e.doc_url and e.reason      # evidence recorded


def test_every_registry_entry_has_evidence():
    for e in allowlist.REGISTRY:
        assert e.endpoint and e.doc_url and e.reason and e.verified_on


def test_evidence_for_matches_subdomains():
    assert allowlist.evidence_for("acme.recruitee.com").ats == "Recruitee"
    assert allowlist.evidence_for("boards.greenhouse.io").ats == "Greenhouse"
    assert allowlist.evidence_for("unknown.example.com") is None


def test_a_maintainer_cannot_enable_a_non_usable_site():
    # auto_eligible requires BOTH flags — enabling a non-applicant-usable entry does nothing.
    from submit.allowlist import SubmissionEvidence
    fake = SubmissionEvidence("X", ("x.com",), "POST /x", "http://d", "employer_api_key",
                              applicant_usable=False, reason="needs key", verified_on="2026-07-14",
                              auto_enabled=True)
    assert fake.auto_eligible is False


# --- Recruitee driver: keyless submit, honest, never evades ------------------ #

def _record():
    return {"profile": {"identity": {"name": "Sam Rivera", "email": "sam@x.com", "phone": "123"}},
            "cover_letter": "Hello", "screening": [{"question": "Q", "answer": "A"}]}


def test_recruitee_endpoint_derivation():
    from submit.drivers import _to_candidates_endpoint
    assert _to_candidates_endpoint("https://acme.recruitee.com/o/senior-eng") == \
        "https://acme.recruitee.com/api/offers/senior-eng/candidates"
    assert _to_candidates_endpoint("https://x.com/o/y") == ""      # not recruitee


def test_recruitee_submit_posts_honest_candidate():
    sent = {}
    def http(url, payload):
        sent["url"] = url
        sent["payload"] = payload
        return 201, '{"candidate":{"id":1}}'
    r = recruitee_submit("https://acme.recruitee.com/o/eng", _record(), http=http)
    assert r["ok"] is True and r["needs_assist"] is False
    assert sent["url"] == "https://acme.recruitee.com/api/offers/eng/candidates"
    assert sent["payload"]["candidate"]["email"] == "sam@x.com"    # honest identity
    assert sent["payload"]["candidate"]["name"] == "Sam Rivera"


def test_recruitee_submit_missing_identity_defers_to_assisted():
    r = recruitee_submit("https://acme.recruitee.com/o/eng", {"cover_letter": "hi"},
                         http=lambda u, p: (201, "{}"))
    assert r["ok"] is False and r["needs_assist"] is True


def test_recruitee_submit_aborts_on_captcha():
    r = recruitee_submit("https://acme.recruitee.com/o/eng", _record(),
                         http=lambda u, p: (200, "Please complete the reCAPTCHA challenge"))
    assert r["ok"] is False and r["needs_assist"] is True


def test_recruitee_submit_aborts_on_auth_wall():
    r = recruitee_submit("https://acme.recruitee.com/o/eng", _record(),
                         http=lambda u, p: (403, "forbidden"))
    assert r["ok"] is False and r["needs_assist"] is True


def test_driver_registry_only_serves_verified_hosts():
    assert drivers.driver_for("acme.recruitee.com") is recruitee_submit
    assert drivers.driver_for("boards.greenhouse.io") is None       # employer-key ATS: no driver
    assert drivers.driver_for("linkedin.com") is None


# --- autonomous opt-in gate (OFF by default) --------------------------------- #

def test_autonomous_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    assert app._autonomous_on() is False
    # With autonomous off, even a verified auto host gets NO driver -> nothing auto-submits.
    assert app._submit_driver_for("https://acme.recruitee.com/o/eng") is None


def test_autonomous_toggle_wires_the_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    app._set_autonomous(True)
    assert app._autonomous_on() is True
    assert app._submit_driver_for("https://acme.recruitee.com/o/eng") is recruitee_submit
    # A prohibited/employer-key host still has no driver even with autonomous on.
    assert app._submit_driver_for("https://linkedin.com/jobs/1") is None
    assert app._submit_driver_for("https://boards.greenhouse.io/a/jobs/1") is None


def test_settings_endpoint_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    c = app.app.test_client()
    assert c.get("/api/submit/settings").get_json()["autonomous"] is False
    assert c.post("/api/submit/settings", json={"autonomous": True}).get_json()["autonomous"] is True
    assert c.get("/api/submit/settings").get_json()["autonomous"] is True
