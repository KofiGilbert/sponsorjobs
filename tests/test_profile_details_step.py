"""The details step at onboarding: a resume import that leaves a template-critical gap (a
project's dates) hands the wizard the gate's own questions as slots, the step saves them
through /api/profile/details into the saved profile (both bags), and the gap is gone for the
next build and for Auto-apply's pre-flight. A complete import asks nothing. Offline: the
model is a stub that returns a fixed profile."""
from __future__ import annotations

from pathlib import Path

from docx import Document

import ui.app as app
from ui.session import _flatten_profile, _missing_skeleton

ROOT = Path(__file__).resolve().parents[1]

IMPORTED = {
    "identity": {"name": "Sam Rivera", "email": "sam@example.com"},
    "education": [{"school": "State University", "degree": "B.S. in Computer Science",
                   "date": "2019"}],
    "skills": {"Languages": "Python, SQL"},
    "projects": [{"title": "Gate Check", "bullets": ["Checks the gate."]},
                 {"title": "Ledger", "bullets": ["Double-entry CLI."]}],
    "experience": [{"org": "Acme Corp", "location": "Remote",
                    "roles": [{"title": "Software Engineer", "dates": "2020 - 2024",
                               "bullets": ["Shipped features."]}]}],
}


class _Stub:
    def __init__(self, profile):
        self.profile = profile

    def extract_profile(self, text):
        return {k: (dict(v) if isinstance(v, dict) else [dict(x) for x in v] if isinstance(v, list) else v)
                for k, v in self.profile.items()}


def _isolate(tmp_path, monkeypatch, profile=IMPORTED):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app, "_make_llm", lambda: _Stub(profile))
    app._SESSION.pop("s", None)
    doc = Document()
    doc.add_paragraph("Sam Rivera")
    cv = tmp_path / "resume.docx"
    doc.save(str(cv))
    return app.app.test_client(), cv


def _import(client, cv):
    with open(cv, "rb") as f:
        r = client.post("/api/profile/from_cv", data={"file": (f, "resume.docx")},
                        content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _gaps():
    saved = app._memory().load("default") or {}
    return _missing_skeleton(*_flatten_profile(saved["profile"]), critical_only=True)


def test_import_with_undated_projects_hands_the_wizard_the_step(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    d = _import(client, cv)
    qs = [s["q"] for s in d["missing"]]
    assert qs == ['the dates for your project "Gate Check"', 'the dates for your project "Ledger"']
    assert all(s["sec"] == "projects" and s["key"] == "dates" and s["soft"] and not s["crit"]
               for s in d["missing"])                 # SOFT: optional, with a Skip
    assert [s["decline"] for s in d["missing"]] == ["project_dates:gate check", "project_dates:ledger"]
    assert d["missing"][0]["match"] == {"name": "Gate Check"}
    # The same slots on demand, for "Add the details" on Auto-apply.
    assert client.get("/api/profile/missing").get_json()["missing"] == d["missing"]
    assert _gaps() == [] and app._aa_missing_details() == []   # soft: nothing is blocked


def test_the_save_lands_in_both_bags_and_clears_the_gate(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    slots = _import(client, cv)["missing"]
    r = client.post("/api/profile/details", json={
        "answers": [{"sec": s["sec"], "key": s["key"], "match": s["match"], "value": v}
                    for s, v in zip(slots, ["Jan 2024 - Mar 2024", "2023"])]}).get_json()
    assert r["ok"] is True and r["missing"] == []
    saved = app._memory().load("default")
    by = {p["title"]: p for p in saved["profile"]["projects"]}
    assert by["Gate Check"]["dates"] == "Jan 2024 - Mar 2024" and by["Ledger"]["dates"] == "2023"
    # The essentials bag carries no projects of its own after an import (it reads the
    # profile's), so the profile bag above is the one copy a session loads.
    assert "projects" not in saved["essentials"]
    assert _gaps() == [] and app._aa_missing_details() == []   # the next build and the run pass
    assert client.get("/api/profile/missing").get_json()["missing"] == []


def test_a_partial_save_asks_only_for_the_rest(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    slots = _import(client, cv)["missing"]
    r = client.post("/api/profile/details", json={
        "answers": [{**{k: slots[0][k] for k in ("sec", "key", "match")}, "value": "2024"}]}).get_json()
    assert [s["q"] for s in r["missing"]] == ['the dates for your project "Ledger"']


def test_a_skip_is_remembered_and_never_asked_again(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    slots = _import(client, cv)["missing"]
    r = client.post("/api/profile/details", json={
        "answers": [{**{k: slots[1][k] for k in ("sec", "key", "match")}, "value": "2023"}],
        "declines": [{"sec": "projects", "match": slots[0]["match"],
                      "decline": slots[0]["decline"]}]}).get_json()
    assert r["missing"] == []
    saved = app._memory().load("default")
    gate = next(p for p in saved["profile"]["projects"] if p["title"] == "Gate Check")
    assert not gate.get("dates") and not gate.get("personal")          # kept as it is, undated
    assert "project_dates:gate check" in saved["essentials"]["declined"]
    assert client.get("/api/profile/missing").get_json()["missing"] == []
    # Saving the profile editor (which shows only section skips) keeps the per-project skip.
    p = client.get("/api/profile").get_json()
    client.post("/api/profile/sections", json={k: p[k] for k in (
        "education", "projects", "extracurricular", "summary", "interests", "skills_input", "declined")}
        | {"experience": [{"org": "Acme Corp", "title": "Software Engineer", "dates": "2020 - 2024",
                           "location": "Remote"}]})
    assert "project_dates:gate check" in app._memory().load("default")["essentials"]["declined"]


def test_a_skip_on_a_hard_detail_is_ignored(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    _import(client, cv)
    client.post("/api/profile/details", json={"declines": [{"sec": "identity", "decline": "name"}]})
    assert "name" not in (app._memory().load("default")["essentials"].get("declined") or [])


def test_a_template_without_projects_never_asks_for_project_dates(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_DEFAULT_TEMPLATE", "accounting")       # no Projects section
    assert _import(client, cv)["missing"] == []
    assert client.get("/api/profile/missing").get_json()["missing"] == []


def test_identity_and_role_answers_land_where_they_belong(tmp_path, monkeypatch):
    gappy = {**IMPORTED, "identity": {"name": ""}, "projects": [],
             "experience": [{"org": "Acme Corp", "roles": [{"title": "Software Engineer",
                                                           "bullets": ["Shipped."]}]}]}
    client, cv = _isolate(tmp_path, monkeypatch, gappy)
    slots = _import(client, cv)["missing"]
    qs = [s["q"] for s in slots]
    assert qs == ["your full name", "an email or phone number",
                  "the employment dates for your role at Acme Corp",
                  "the location (city, state) for your role at Acme Corp"]
    assert all(s["decline"] is None for s in slots)   # the template needs each of these
    vals = ["Sam Rivera", "sam@example.com", "2020 - 2024", "Austin, TX"]
    r = client.post("/api/profile/details", json={
        "answers": [{**{k: s[k] for k in ("sec", "key", "match")}, "value": v}
                    for s, v in zip(slots, vals)]}).get_json()
    assert r["missing"] == []
    saved = app._memory().load("default")
    assert saved["profile"]["identity"]["name"] == "Sam Rivera"
    assert saved["profile"]["identity"]["email"] == "sam@example.com"   # "contact" filed as email
    role = saved["profile"]["experience"][0]
    assert role["location"] == "Austin, TX" and role["roles"][0]["dates"] == "2020 - 2024"
    flat = saved["essentials"]["experience"][0]
    assert flat["location"] == "Austin, TX" and flat["dates"] == "2020 - 2024"
    assert _gaps() == []


def test_a_complete_import_skips_the_step(tmp_path, monkeypatch):
    complete = {**IMPORTED, "projects": [{"title": "Gate Check", "dates": "2024",
                                          "bullets": ["Checks the gate."]}]}
    client, cv = _isolate(tmp_path, monkeypatch, complete)
    assert _import(client, cv)["missing"] == []
    assert client.get("/api/profile/missing").get_json()["missing"] == []


def test_the_save_is_refused_while_a_resume_session_is_open(tmp_path, monkeypatch):
    client, cv = _isolate(tmp_path, monkeypatch)
    _import(client, cv)
    monkeypatch.setitem(app._SESSION, "s", object())
    assert client.post("/api/profile/details", json={"answers": []}).status_code == 409


def test_the_wizard_has_the_step_and_auto_apply_opens_it():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-panel="details"' in html and "A few details to finish your profile" in html
    assert 'T("wiz.details.skip", "Skip")' in js and 'T("wiz.details.optional", "Optional")' in js
    assert 'if ((r.missing || []).length) openProfileDetails(r.missing);' in js   # after an import
    assert '() => openProfileDetails()]' in js                                    # Add the details
    assert 'api("/api/profile/details", { answers, declines })' in js
