"""Assisted-apply autofill endpoint (GREEN lane, CLAUDE.md §7).

The browser extension fetches /api/profile/autofill to PRE-FILL an application form; the
person reviews and submits themselves. These tests cover the name/address normalization,
the response shape, and the LOCKDOWN — these endpoints carry personal data / spend the
user's API credits, so they require the extension's header and expose no CORS.
"""

from __future__ import annotations

import ui.app as app
from ui.app import _parse_address, _split_name

# The extension sends this header via its background worker; a web page cannot set it
# cross-origin without a preflight the server refuses. Tests simulate the extension.
EXT = {"X-Tailor-Extension": "1"}


def test_split_name():
    assert _split_name("Kofi Gilbert") == ("Kofi", "Gilbert")
    assert _split_name("Ada B. Lovelace") == ("Ada", "B. Lovelace")
    assert _split_name("Cher") == ("Cher", "")
    assert _split_name("  ") == ("", "")


def test_parse_address_full():
    a = _parse_address("123 Lakeshore Ave, Chicago, IL 60615")
    assert a == {"street": "123 Lakeshore Ave", "city": "Chicago",
                 "state": "IL", "zip": "60615"}


def test_parse_address_partial_is_conservative():
    # No state/zip pattern -> those stay blank rather than being guessed.
    a = _parse_address("221B Baker Street")
    assert a["street"] == "221B Baker Street"
    assert a["state"] == "" and a["zip"] == ""
    assert _parse_address("") == {"street": "", "city": "", "state": "", "zip": ""}


def test_autofill_endpoint_normalizes_and_is_locked_down(monkeypatch):
    monkeypatch.setattr(app, "_saved_identity", lambda: {
        "name": "Kofi Gilbert",
        "email": "kofi@example.com",
        "phone": "(312) 555-0134",
        "address": "123 Lakeshore Ave, Chicago, IL 60615",
        "linkedin": "https://linkedin.com/in/kofi",
        "github": "https://github.com/KofiGilbert",
    })
    client = app.app.test_client()
    r = client.get("/api/profile/autofill", headers=EXT)
    assert r.status_code == 200
    # NOT CORS-open: a web page must never be able to read this PII cross-origin.
    assert r.headers.get("Access-Control-Allow-Origin") is None
    body = r.get_json()
    assert body["ok"] is True and body["loaded"] is True
    f = body["fields"]
    assert f["first_name"] == "Kofi" and f["last_name"] == "Gilbert"
    assert f["city"] == "Chicago" and f["state"] == "IL" and f["zip"] == "60615"
    assert f["email"] == "kofi@example.com"
    assert f["github"] == "https://github.com/KofiGilbert"
    # Empty fields are omitted, not sent as "".
    assert "website" not in f and "" not in f.values()


def test_autofill_includes_reusable_answers(monkeypatch):
    """Answer once, fill everywhere: the payload carries the extra saved answers so the
    extension fills the whole form, not just contact info."""
    monkeypatch.setattr(app, "_saved_identity", lambda: {"name": "Kofi", "email": "k@e.com"})
    monkeypatch.setattr(app, "_load_prefs", lambda: {
        "work_authorized": "Yes", "needs_sponsorship": "No", "willing_to_relocate": "Yes",
        "over_18": "Yes", "years_experience": "3", "desired_salary": "120000",
        "earliest_start": "2 weeks", "hear_about_us": "LinkedIn"})
    r = app.app.test_client().get("/api/profile/autofill", headers=EXT).get_json()
    assert r["screening"]["willing_to_relocate"] == "Yes"       # Yes/No screening answers
    assert r["screening"]["work_authorized"] == "Yes" and r["screening"]["over_18"] == "Yes"
    assert r["answers"] == {"years_experience": "3", "desired_salary": "120000",
                            "earliest_start": "2 weeks", "hear_about_us": "LinkedIn"}


def test_save_prefs_stores_and_sanitizes_new_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_PREFS_PATH", tmp_path / "prefs.json")
    saved = app._save_prefs({
        "willing_to_relocate": "Yes", "years_experience": "5",
        "desired_salary": "x" * 100, "earliest_start": "immediately",
        "work_authorized": "maybe"})                             # invalid Yes/No
    assert saved["willing_to_relocate"] == "Yes" and saved["years_experience"] == "5"
    assert len(saved["desired_salary"]) == 40                    # length-capped
    assert saved["work_authorized"] == ""                       # invalid dropped, not stored


def test_extension_endpoints_reject_requests_without_the_header(monkeypatch):
    """A website the user visits (no extension header) gets 403 — can't read PII, can't
    spend API credits, can't overwrite saved answers."""
    monkeypatch.setattr(app, "_saved_identity", lambda: {"name": "Kofi", "email": "k@e.com"})
    c = app.app.test_client()
    assert c.get("/api/profile/autofill").status_code == 403
    assert c.get("/api/profile/prefs").status_code == 403
    assert c.post("/api/profile/prefs", json={"eeo_decline": True}).status_code == 403
    assert c.post("/api/profile/draft", json={"jd": "x", "questions": ["y"]}).status_code == 403
    assert c.get("/api/sponsors/lookup?company=Google").status_code == 403


def test_autofill_endpoint_empty_profile(monkeypatch):
    monkeypatch.setattr(app, "_saved_identity", lambda: {})
    monkeypatch.setattr(app, "_load_prefs", lambda: {})
    r = app.app.test_client().get("/api/profile/autofill", headers=EXT)
    body = r.get_json()
    assert body["ok"] is True and body["loaded"] is False and body["fields"] == {}
    # Screening/EEO blocks are always present so the extension can rely on their shape.
    assert body["screening"] == {} and body["eeo"] == {"decline": False}


def test_prefs_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_PREFS_PATH", tmp_path / "app_prefs.json")
    client = app.app.test_client()
    # Save valid + one invalid value -> invalid is coerced to "" (never a bad screening answer).
    r = client.post("/api/profile/prefs", headers=EXT, json={
        "work_authorized": "Yes", "needs_sponsorship": "Maybe", "eeo_decline": True})
    saved = r.get_json()["prefs"]
    assert saved["work_authorized"] == "Yes"
    assert saved["needs_sponsorship"] == ""              # invalid "Maybe" coerced to ""
    assert saved["eeo_decline"] is True
    # The reusable answers added alongside default to empty when not supplied.
    assert saved["willing_to_relocate"] == "" and saved["years_experience"] == ""
    # Persisted and read back.
    assert client.get("/api/profile/prefs", headers=EXT).get_json()["prefs"] == saved
    assert r.headers.get("Access-Control-Allow-Origin") is None


def test_autofill_surfaces_saved_screening(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_saved_identity", lambda: {"name": "Kofi Gilbert",
                                                         "email": "k@example.com"})
    monkeypatch.setattr(app, "_load_prefs", lambda: {
        "work_authorized": "Yes", "needs_sponsorship": "Yes", "eeo_decline": True})
    body = app.app.test_client().get("/api/profile/autofill", headers=EXT).get_json()
    assert body["screening"] == {"work_authorized": "Yes", "needs_sponsorship": "Yes"}
    assert body["eeo"] == {"decline": True}
