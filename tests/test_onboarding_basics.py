"""Setup's last step: the answers every form asks (country of citizenship, desired salary, and
the five self-identification questions), right after "A few details to finish your profile".

The step lends the Your answers card (#ansBasicsCard) to the wizard, asks only the rows
/api/answers/basics still reports missing, and never opens when none are. Skip saves nothing and
closes; the "Before I apply" prompt on Auto-apply stays the safety net. These pin the route the
step reads and saves through, the skip-when-complete rule (wizBasicsAsk, run in node), and the
wiring: one primary, Skip never blocks, the card goes home when the wizard closes."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
ALL7 = ["citizenship", "salary", "gender", "hispanic", "race", "veteran", "disability"]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


# --------------------------------------------------------------------------- the route it reads
def _missing(c):
    return c.get("/api/answers/basics").get_json()["missing"]


def test_a_new_install_is_missing_all_seven(app):
    _, c = app
    assert _missing(c) == ALL7


def test_partial_answers_leave_only_the_rest(app):
    _, c = app
    r = c.post("/api/answers/basics", json={"fields": {"citizenship": "Ghana", "gender": "male", "race": "black"}})
    assert r.status_code == 200
    assert r.get_json()["missing"] == ["salary", "hispanic", "veteran", "disability"]
    assert _missing(c) == ["salary", "hispanic", "veteran", "disability"]


def test_all_seven_saved_is_nothing_missing(app):
    _, c = app
    c.post("/api/answers/basics", json={"fields": {
        "citizenship": "Ghana", "salary": "120000", "gender": "female", "hispanic": "no", "race": "black",
        "veteran": "not", "disability": "decline"}})
    assert _missing(c) == []


def test_the_decline_box_answers_the_five(app):
    _, c = app
    r = c.post("/api/answers/basics", json={"fields": {"citizenship": "Ghana", "salary": "140k"},
                                            "eeo_decline": True}).get_json()
    assert r["missing"] == [] and r["values"]["salary"] == "140000"
    assert _missing(c) == []


def test_prefer_not_to_say_counts_as_answered(app):
    _, c = app
    c.post("/api/answers/basics", json={"fields": {f: "decline" for f in ("gender", "hispanic", "race", "veteran",
                                                                           "disability")}})
    assert _missing(c) == ["citizenship", "salary"]


def test_a_salary_only_the_extension_holds_counts(app):
    A, c = app
    A._merge_prefs({"desired_salary": "95000"})
    assert "salary" not in _missing(c)


def test_a_bad_salary_is_refused_with_words_the_step_shows(app):
    _, c = app
    r = c.post("/api/answers/basics", json={"fields": {"salary": "25 an hour"}})
    assert r.status_code == 400 and "yearly salary" in r.get_json()["error"]
    assert "salary" in _missing(c)


# --------------------------------------------------------------------------- the skip rule (JS)
def _ask(d):
    if not shutil.which("node"):
        pytest.skip("needs node")
    i = APP_JS.index("function wizBasicsAsk(d)")
    fn = APP_JS[i:APP_JS.index("\n  }\n", i) + 4]
    out = subprocess.run(["node", "-e", fn + f"\nprocess.stdout.write(JSON.stringify(wizBasicsAsk({json.dumps(d)})));"],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_skip_when_complete():
    assert _ask({"missing": []}) == {"rows": [], "decline": False}
    assert _ask(None) == {"rows": [], "decline": False}


def test_asks_exactly_what_is_missing():
    assert _ask({"missing": ALL7}) == {"rows": ALL7, "decline": True}
    assert _ask({"missing": ["salary"]}) == {"rows": ["salary"], "decline": False}
    assert _ask({"missing": ["veteran", "citizenship"]}) == {"rows": ["citizenship", "veteran"], "decline": True}


def test_open_never_shows_an_empty_step():
    i = APP_JS.index("async function openBasicsStep()")
    body = APP_JS[i:APP_JS.index("\n  }\n", i)]
    assert "wizBasicsAsk(d)" in body and "!ask.rows.length" in body and "closeResumeBox()" in body


# --------------------------------------------------------------------------- the wiring
def _panel(name):
    i = HTML.index(f'data-panel="{name}"')
    return HTML[i:HTML.index('<div class="wiz-panel"', i + 1) if name != "basics" else HTML.index("<!-- Connect your AI", i)]


def test_the_step_follows_the_details_step_with_one_primary_and_a_skip():
    assert HTML.index('data-panel="details"') < HTML.index('data-panel="basics"') < HTML.index('id="aiModal"')
    p = _panel("basics")
    assert p.count("btn-primary") == 1 and 'id="wizBasSave"' in p
    assert 'id="wizBasSkip"' in p and 'data-i18n="wiz.basics.skip">Skip<' in p
    assert 'id="wizBasics"' in p
    # the card itself is not duplicated: one copy, on Your answers
    assert HTML.count('id="ansBasicsCard"') == 1 and HTML.count('id="ansEeo"') == 1


def test_setup_moves_from_details_to_the_step_and_skip_never_blocks():
    assert "setTimeout(wizAfterDetails, 900)" in APP_JS                        # import with no gaps, details done
    assert APP_JS.count("setTimeout(wizAfterDetails, 900)") == 2
    assert "if (b.closest('[data-panel=\"details\"]')) wizAfterDetails();" in APP_JS   # Not now moves on
    assert "function wizAfterDetails() { if (WIZ_SETUP) openBasicsStep(); else closeResumeBox(); }" in APP_JS
    assert '$("#wizBasSkip")?.addEventListener("click", closeResumeBox);' in APP_JS
    # closing the wizard always hands the card back to Your answers
    assert "function closeResumeBox() { wizBasicsReturn();" in APP_JS


def test_the_step_and_the_card_save_the_same_way():
    i = APP_JS.index("async function basSave()")
    save = APP_JS[i:APP_JS.index("\n  }\n", i)]
    assert '/api/answers/basics' in save and "eeo_decline" in save
    assert "await basSave();" in APP_JS[APP_JS.index('$("#ansBasicsSave")?.addEventListener'):]
    assert "await basSave();" in APP_JS[APP_JS.index('$("#wizBasSave")?.addEventListener'):]
    # the "Before I apply" safety net is still there
    assert "await aaBasicsGate(" in APP_JS


def test_every_locale_names_the_step():
    for loc in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{loc}.json").read_text(encoding="utf-8"))
        for k in ("wiz.basics.h", "wiz.basics.p", "wiz.basics.skip", "wiz.basics.save"):
            assert d.get(k), (loc, k)
