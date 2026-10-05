"""Templates are chosen by TARGET, not by looks (2026-07).

Research behind this: every ATS-safe CV converges on the same single-column, one-page,
standard-header shape (Amazon parses through Workday and wants no graphics; multi-column
layouts get scrambled; banking is one page, near-absolute). So the differences between "a
Google CV" and "a JPMorgan CV" are NOT the layout. They are section order, section set,
bullet rhetoric, and font.

That means asking someone to pick a template on appearance asks them to judge a
difference that isn't there. The category carries the real one, and "help me pick" reads
the JD they already pasted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ui.app as app

TDIR = Path("config/templates")


@pytest.fixture
def empty_profile(tmp_path, monkeypatch):
    """Isolate the saved-profile source so `_candidate_seniority()` reads NO ambient data
    (matching the `_SPONSORS_DB` isolation from #129). These suggestion tests assert the layout
    from the JD text alone; a populated dev machine's real seniority would otherwise add the
    experienced-professional score boost and leak into the result."""
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "empty.db"))


def test_every_template_declares_who_it_is_for():
    """A template without a category can't be grouped, and the picker would have to guess
    one from its filename. The manifest is where the answer belongs."""
    for p in sorted(TDIR.glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        assert m.get("category"), f"{p.name} declares no category"
        assert m.get("best_for"), f"{p.name} doesn't say who it's for"


def test_no_dashes_in_template_names_the_person_reads():
    """The user's standing rule: a dash in the copy reads as AI-written. Display names and
    the 'best for' line are both shown in the picker."""
    for p in sorted(TDIR.glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        for field in ("display_name", "category", "best_for"):
            text = str(m.get(field) or "")
            assert "—" not in text and "–" not in text, f"{p.name}: dash in {field}: {text!r}"


def test_the_api_exposes_category_and_best_for():
    for t in app._templates():
        assert t["category"], f"{t['name']} has no category"
        assert "best_for" in t


def test_a_senior_jd_picks_the_experienced_template(empty_profile):
    s = app._suggest_template(
        "Senior Staff Engineer. 8+ years of experience. You will lead architecture.")
    assert s["name"] == "summary"
    assert s["reason"], "picked a template without saying why"
    assert s["matched"], "picked a template without showing what it matched"


def test_a_graduate_jd_picks_the_student_template(empty_profile):
    s = app._suggest_template(
        "Graduate Analyst Program. Campus hire. Internship experience welcome, entry level.")
    assert s["name"] == "shetty"
    assert s["matched"]


def test_an_unreadable_jd_admits_it_rather_than_guessing(empty_profile):
    """"I couldn't tell" is a real answer. A confident guess is how the sticky-template
    bug felt: a choice made for the person with nothing on screen explaining it.

    Uses the `empty_profile` fixture so a populated dev machine's real seniority can't add the
    experienced-professional boost and turn this "no match" into a confident pick (#129 smell)."""
    s = app._suggest_template("We are looking for a nice person to join our lovely team.")
    assert s["name"] == app._DEFAULT_TEMPLATE
    assert s["reason"] == ""
    assert s["matched"] == []


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    app._SESSION.pop("s", None)


def test_starting_a_cv_picks_the_template_from_the_jd_without_asking(tmp_path, monkeypatch):
    """Choosing is work. Someone who just pasted a job wants a CV, not a decision about
    layout, and we have already read the JD so we already know the answer."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    client.post("/api/session/start", json={
        "jd": "Senior Staff Engineer. 8+ years of experience leading architecture."})
    assert app._SESSION["s"].template_name == "summary"


def test_an_explicit_choice_still_wins(tmp_path, monkeypatch):
    """Auto-picking must never overrule someone who went to the picker and chose."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    client.post("/api/session/start", json={
        "jd": "Senior Staff Engineer. 8+ years of experience.", "template": "shetty"})
    assert app._SESSION["s"].template_name == "shetty"


def test_the_session_says_which_template_it_used(tmp_path, monkeypatch):
    """A template picked FOR the person is only acceptable if they can see which, and
    change it. An unnamed default is the silent sticky state this replaced."""
    _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    client.post("/api/session/start", json={"jd": "Graduate Analyst Program, campus hire."})
    st = client.get("/api/session/state").get_json()
    assert st["template"] == "shetty"
    assert st["template_label"] == "Classic"


def test_suggest_endpoint_needs_no_model_and_no_key(tmp_path, monkeypatch):
    """Deterministic keyword overlap, not an LLM call: the person is one click from the
    picker with every preview right there, so a slow, costly, non-reproducible answer
    would buy nothing. It must work with no API key configured."""
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    r = app.app.test_client().post("/api/templates/suggest",
                                   json={"jd": "Senior Principal Engineer, 10 years."})
    assert r.status_code == 200
    assert r.get_json()["name"] == "summary"


def test_an_ops_jd_still_picks_the_experienced_template(tmp_path, monkeypatch):
    """The tie-break must not over-correct: a JD that hits `experienced`'s distinguishing
    keywords (operations / supervisor / area manager) still resolves to `experienced`, not the
    generic `summary`."""
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    r = app.app.test_client().post("/api/templates/suggest",
                                   json={"jd": "Area Manager, operations, 8 years."})
    assert r.status_code == 200
    assert r.get_json()["name"] == "experienced"
