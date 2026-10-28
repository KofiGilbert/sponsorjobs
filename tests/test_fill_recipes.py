"""The fill-recipe table (autoapply/recipes.json) and the app endpoints around it.

The table describes each application system as DATA for the assisted-apply engine: URL patterns,
selectors per profile field, the continue/submit buttons it HIGHLIGHTS, and the success markers.
These tests pin the contract: every entry is well-formed, none of them can click anything (there
is no action vocabulary at all, only selectors to find and outline), the embedded fallback copy in
extension/autofill_core.js matches the table, and the Flask routes that serve it, learn from the
person's corrections, and mark a job applied behave. No network, no browser.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECIPES = ROOT / "autoapply" / "recipes.json"
CORE = ROOT / "extension" / "autofill_core.js"
EXT = {"X-Tailor-Extension": "1"}

REQUIRED_SYSTEMS = {
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "workday", "icims",
    "bamboohr", "jobvite", "taleo", "successfactors", "oracle_recruiting", "eightfold", "phenom",
    "avature", "adp",
}
PROFILE_KEYS = {"first_name", "last_name", "name", "email", "phone", "address", "street", "city",
                "state", "zip", "country", "location", "linkedin", "github", "website", "resume"}
# The only keys an entry may carry: anything that smells like an ACTION is refused by name.
ALLOWED_KEYS = {"id", "name", "url_patterns", "detect_markers", "js_rendered", "verified", "frame",
                "form", "fields", "continue_buttons", "submit_buttons", "success_markers", "multipage",
                "handoff", "notes"}
FORBIDDEN_WORDS = re.compile(r"\b(click|press|submit_action|auto_submit|autosubmit|actions?)\b", re.I)


def _recipes() -> dict:
    return json.loads(RECIPES.read_text(encoding="utf-8"))


def test_every_required_system_is_described():
    ids = {s["id"] for s in _recipes()["systems"]}
    assert REQUIRED_SYSTEMS <= ids, f"missing: {sorted(REQUIRED_SYSTEMS - ids)}"
    assert len(ids) == len(_recipes()["systems"]), "duplicate system ids"


def test_every_entry_is_well_formed():
    for s in _recipes()["systems"]:
        sid = s.get("id")
        assert set(s) <= ALLOWED_KEYS, f"{sid}: unknown keys {sorted(set(s) - ALLOWED_KEYS)}"
        for k in ("id", "name", "url_patterns", "fields", "continue_buttons", "submit_buttons",
                  "success_markers", "multipage", "js_rendered", "verified", "notes"):
            assert k in s, f"{sid}: missing {k}"
        assert s["url_patterns"] and all(p.startswith("https://") for p in s["url_patterns"]), sid
        assert isinstance(s["multipage"], bool) and isinstance(s["js_rendered"], bool), sid
        assert set(s["fields"]) <= PROFILE_KEYS, f"{sid}: unknown profile keys {set(s['fields']) - PROFILE_KEYS}"
        for key, sels in s["fields"].items():
            assert isinstance(sels, list) and sels and all(isinstance(x, str) and x.strip() for x in sels), (sid, key)
        assert isinstance(s["continue_buttons"], list) and isinstance(s["submit_buttons"], list), sid
        assert s["submit_buttons"], f"{sid}: a recipe must know the submit button to OUTLINE it"
        assert s["success_markers"], f"{sid}: a recipe must know how the site says 'received'"
        for m in s["success_markers"]:
            assert isinstance(m, dict) and len(m) == 1 and set(m) <= {"text", "selector", "url_re"}, (sid, m)
        v = s["verified"]
        assert {"date", "method", "urls"} <= set(v) and isinstance(v["urls"], list), sid
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", v["date"]), sid


def test_no_recipe_can_click_anything():
    """The table is selectors and markers only. There is no action vocabulary, so the engine has
    nothing it COULD click: submit_buttons exist to be outlined for the person (CLAUDE.md 7)."""
    for s in _recipes()["systems"]:
        for k in s:
            assert not FORBIDDEN_WORDS.search(k), (s["id"], k)
        for sels in list(s["fields"].values()) + [s["continue_buttons"], s["submit_buttons"]]:
            for sel in sels:
                assert not re.search(r"\bclick\b", sel, re.I), (s["id"], sel)
    core = CORE.read_text(encoding="utf-8")
    # The engine never calls .click() or .submit() on anything.
    assert not re.search(r"\.click\(\)", core), "the engine must not click"
    assert not re.search(r"\.submit\(\)", core), "the engine must not submit a form"
    assert not re.search(r"requestSubmit\(", core)


def test_question_patterns_cover_work_auth_and_sponsorship():
    q = _recipes()["question_patterns"]
    assert set(q) >= {"work_auth_us", "needs_sponsorship", "eeo_decline"}
    wa, sp = re.compile(q["work_auth_us"]["label_re"], re.I), re.compile(q["needs_sponsorship"]["label_re"], re.I)
    assert wa.search("Are you legally authorized to work in the United States?")
    assert sp.search("Will you now or in the future require sponsorship for employment visa status?")
    assert re.compile(q["needs_sponsorship"]["future_re"], re.I).search("now or in the future require")
    assert q["needs_sponsorship"]["answers"] == ["Yes", "No", "Future"]


def test_embedded_fallback_copy_matches_the_table():
    """scripts/embed_recipes.py keeps the engine's offline copy identical to recipes.json."""
    from scripts.embed_recipes import compact, embedded_text
    assert embedded_text(CORE.read_text(encoding="utf-8")) == compact(_recipes())


def test_site_specific_entries_come_before_the_marker_detected_generic_ones():
    """Phenom and Avature live on the employer's own domain (careers.*, jobs.*), so they carry
    detect_markers and must come AFTER every host-specific system or they would shadow Lever
    (jobs.lever.co) and friends."""
    systems = _recipes()["systems"]
    first_generic = next(i for i, s in enumerate(systems) if s.get("detect_markers"))
    for s in systems[first_generic:]:
        if not s.get("detect_markers"):
            assert not any("*/" in p.split("//", 1)[1][:12] for p in s["url_patterns"]), s["id"]
    assert all(not s.get("detect_markers") for s in systems[:first_generic])
    assert {s["id"] for s in systems if s.get("detect_markers")} == {"phenom", "avature"}


# ------------------------------------------------------------------ Flask routes
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import ui.app as app
    from intake.memory import ConversationMemory
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_PREFS_PATH", tmp_path / "app_prefs.json")
    app._SESSION.pop("s", None)
    ident = {"name": "Kofi Gilbert", "email": "kofi@example.com", "phone": "555-100-2000",
             "address": "1 Main St, Chicago, IL 60601"}
    m = ConversationMemory(db)
    m.save("default", {"identity": dict(ident), "experience": [{"org": "Acme", "roles": []}]},
           {"identity": dict(ident), "education": [], "experience": []}, [])
    m.close()
    return app, app.app.test_client()


def test_recipes_endpoint_serves_the_table(client):
    app, c = client
    d = c.get("/api/autofill/recipes").get_json()
    assert d["version"] == _recipes()["version"]
    assert {s["id"] for s in d["systems"]} == {s["id"] for s in _recipes()["systems"]}


def test_autofill_payload_carries_recipes_and_first_class_answers(client):
    app, c = client
    c.post("/api/profile/answers", json={"work_auth_us": "Yes", "needs_sponsorship": "Future",
                                         "eeo_decline": True})
    d = c.get("/api/profile/autofill", headers=EXT).get_json()
    assert d["recipes"]["systems"] and d["recipes"]["version"] == _recipes()["version"]
    assert d["screening"]["work_auth_us"] == "Yes"
    assert d["screening"]["work_authorized"] == "Yes"          # older spelling kept in step
    assert d["screening"]["needs_sponsorship"] == "Future"
    assert d["eeo"] == {"decline": True}
    assert d["fields"]["first_name"] == "Kofi" and d["fields"]["city"] == "Chicago"


def test_profile_view_and_answers_roundtrip(client):
    app, c = client
    assert c.get("/api/profile").get_json()["answers"] == {"work_auth_us": "", "needs_sponsorship": "",
                                                           "eeo_decline": False}
    r = c.post("/api/profile/answers", json={"work_auth_us": "No", "needs_sponsorship": "Yes"})
    assert r.status_code == 200 and r.get_json()["answers"]["work_auth_us"] == "No"
    assert c.get("/api/profile").get_json()["answers"]["needs_sponsorship"] == "Yes"
    # Partial update keeps the rest; invalid values are refused, never stored.
    c.post("/api/profile/answers", json={"eeo_decline": True})
    a = c.get("/api/profile").get_json()["answers"]
    assert a == {"work_auth_us": "No", "needs_sponsorship": "Yes", "eeo_decline": True}
    assert c.post("/api/profile/answers", json={"needs_sponsorship": "Maybe"}).status_code == 400
    assert c.post("/api/profile/answers", json={"work_auth_us": "Future"}).status_code == 400
    # The extension popup reads the same store under its own key names.
    p = c.get("/api/profile/prefs", headers=EXT).get_json()["prefs"]
    assert p["work_authorized"] == "No" and p["needs_sponsorship"] == "Yes" and p["eeo_decline"] is True


def test_learn_from_corrections_updates_identity_and_answers(client):
    app, c = client
    r = c.post("/api/profile/autofill", headers=EXT, json={
        "fields": {"phone": "555-999-0000", "last_name": "Gilbert-Mensah", "linkedin": "https://linkedin.com/in/kofi"},
        "screening": {"needs_sponsorship": "No"},
    }).get_json()
    assert r["ok"] is True and set(r["learned"]) >= {"phone", "name", "linkedin", "needs_sponsorship"}
    d = c.get("/api/profile/autofill", headers=EXT).get_json()
    assert d["fields"]["phone"] == "5559990000"      # the autofill payload carries digits (Workday, 2026-10-28)
    assert d["fields"]["last_name"] == "Gilbert-Mensah" and d["fields"]["first_name"] == "Kofi"
    assert d["fields"]["linkedin"] == "https://linkedin.com/in/kofi"
    assert d["screening"]["needs_sponsorship"] == "No"
    # Address parts are re-joined into the one saved address line.
    c.post("/api/profile/autofill", headers=EXT, json={"fields": {"city": "Evanston", "zip": "60201"}})
    d = c.get("/api/profile/autofill", headers=EXT).get_json()
    assert d["fields"]["city"] == "Evanston" and d["fields"]["zip"] == "60201" and d["fields"]["state"] == "IL"
    # Unknown keys and empty values are ignored, nothing is learned from them.
    r = c.post("/api/profile/autofill", headers=EXT, json={"fields": {"password": "x", "email": ""}}).get_json()
    assert r["learned"] == []
    assert c.post("/api/profile/autofill", json={"fields": {"phone": "1"}}).status_code == 403


def test_applied_marks_the_matching_record(client):
    app, c = client
    from ui.records import CVRecords
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={"source_job": {"url": "https://jobs.lever.co/acme/123"}})
    other = recs.add("Analyst", "Beta", 60, "", data={"source_job": {"url": "https://jobs.lever.co/beta/9"}})
    recs.close()
    # Matched by the job URL the package was built from (query/fragment ignored).
    r = c.post("/api/autofill/applied", headers=EXT,
               json={"url": "https://jobs.lever.co/acme/123/apply?src=x", "page_url": "https://jobs.lever.co/acme/123/thanks"}).get_json()
    assert r["ok"] is True and r["matched"] is False      # the apply/thanks pages are not the job page
    r = c.post("/api/autofill/applied", headers=EXT,
               json={"url": "https://jobs.lever.co/acme/123?utm=1"}).get_json()
    assert r["matched"] is True and r["record_id"] == rid and r["stage"] == "applied" and r["follow_up_due"]
    recs = CVRecords(app.DB_PATH)
    try:
        assert app._record_data(recs.get(rid))["status"] == "applied"
        assert app._record_data(recs.get(other)).get("status") != "applied"
    finally:
        recs.close()
    # Explicit record id wins; an unknown page with no record marks nothing.
    r = c.post("/api/autofill/applied", headers=EXT, json={"record_id": other, "url": "https://x.example/"}).get_json()
    assert r["matched"] is True and r["record_id"] == other
    r = c.post("/api/autofill/applied", headers=EXT, json={"url": "https://nowhere.example/apply"}).get_json()
    assert r["matched"] is False and r["record_id"] is None
    assert c.post("/api/autofill/applied", json={"url": "https://x/"}).status_code == 403


def test_prefs_route_dispatches_extension_events(client):
    """The extension's background worker only knows the prefs route, so learn/applied ride on it
    as events, and an event must never wipe the saved answers."""
    app, c = client
    c.post("/api/profile/prefs", headers=EXT, json={"work_authorized": "Yes", "eeo_decline": True})
    r = c.post("/api/profile/prefs", headers=EXT,
               json={"event": {"type": "learn", "fields": {"phone": "555-000-1111"}}}).get_json()
    assert r["ok"] is True and "phone" in r["learned"]
    p = c.get("/api/profile/prefs", headers=EXT).get_json()["prefs"]
    assert p["work_authorized"] == "Yes" and p["eeo_decline"] is True      # untouched by the event
    r = c.post("/api/profile/prefs", headers=EXT,
               json={"event": {"type": "applied", "url": "https://nowhere.example/"}}).get_json()
    assert r["ok"] is True and r["matched"] is False
    assert c.post("/api/profile/prefs", headers=EXT, json={"event": {"type": "nope"}}).status_code == 400
