"""The top gaps of the Autofill audit (docs/apply-failure-modes.md), closed 2026-11-26.

* case 35: another job in the Browser lets the application go (shell-electron/job_identity.js,
  tests/js/test_job_identity.js, run here);
* case 32: where a job is, read by meaning ("Vancouver, BC" is Canada), never the US by default;
* case 9: a site window never replaces a half-filled form (popup_policy.js); a drop-only upload
  box gets the resume dragged in, in sight (drop_upload.js);
* cases 12 and 26: the bar says when the site did not take the Submit, or the job closed;
* case 14: confirmation emails in other languages;
* case 7: the email-first step (owner decision): asked once per site, the email typed in sight,
  never Next, never the check box, never a password or a code.
Made-up companies, sites and people.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import jobplace
from inbox import replies

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
US_OPT = {"countries": {"US": {"status": "", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes",
                               "auth_end": ""}}}


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


# --------------------------------------------------------------------------- the shell, under node

@pytest.mark.parametrize("script,ok", [("test_job_identity.js", "job identity ok"), ("test_popup_policy.js", "popup policy ok"),
                                       ("test_applied_watch.js", "ok applied watch")])
def test_the_shell_rules_under_node(script, ok):
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert ok in out.stdout


# ----------------------------------------------------------------------- case 35: the context

def test_every_pane_navigation_is_checked_against_the_filled_job():
    main = _src("shell-electron/main.js")
    pane = _between(main, "function ensurePane()", "\n}\n")
    assert "paneNavigated(wc, url)" in pane.split('wc.on("did-navigate"', 1)[1].split("\n", 1)[0]
    assert "paneNavigated(wc, url)" in pane.split('"did-navigate-in-page"', 1)[1]
    nav = _between(main, "function paneNavigated(", "\n}\n")
    # never while the app fills or the agent runs; another job stops both watches and tells the app
    assert "appDriving()" in nav and "disarmPageWatch()" in nav and 'kind: "left"' in nav
    assert 'disarmSuccessWatch("left-site")' in nav and 'leave("moved-on")' in nav
    assert 'kind: "back"' in nav                                     # coming back gives it back
    run = _between(main, "async function runAutoApplyOnPane(", "\n// -------")
    assert "noteFilled(recordId, wc.getURL() || url, true)" in run
    ap = _between(main, "async function assistApply(", "\n}\n")
    assert 'noteFilled(recordId, wc.getURL() || startUrl, false)' in ap      # a job page hands off
    assert "_fillBusy++" in _between(main, 'ipcMain.handle("assist:apply"', "\n});")


def test_the_app_lets_go_and_takes_back_the_application():
    js = _src("ui/static/app.js")
    said = _between(js, "window.tailorShell.onSiteSaid((d) => {", "\n    });\n")
    assert 'd.kind === "left"' in said and "dropCtx()" in said and 'd.kind === "back"' in said
    assert "LAST_CTX" in said
    # a resume tailored for one job is never filled into another job's form
    st = _between(js, "async function browserJobState(st)", "\n  }\n")
    assert "window.tailorShell.sameJob(bj.url, now)" in st and "bb.savedOther" in st
    pre = _src("shell-electron/preload.js")
    for k in ("onSiteSaid", "sameJob", "openAndWait", "emailStep"):
        assert k + ":" in pre, k


# ------------------------------------------------------------------- case 32: where the job is

@pytest.mark.parametrize("loc,want", [
    ("Vancouver, BC", "CA"), ("Toronto, ON", "CA"), ("Montréal, Québec", "CA"), ("Remote (Canada)", "CA"),
    ("London, ON", "CA"), ("Calgary, Alberta", "CA"), ("Austin, TX", "US"), ("New York, NY", "US"),
    ("San Francisco, CA", "US"), ("Mumbai, MH,IN, India", "India"), ("Berlin, Germany", "EU"),
    ("Sydney, Australia", "AU"), ("Manchester, United Kingdom", "UK"),
])
def test_a_sure_place_needs_no_model(loc, want):
    assert jobplace.sure(loc) == want


@pytest.mark.parametrize("loc", ["Remote - CA", "London", "Bengaluru", "Zuerich"])
def test_an_unsure_place_is_never_the_us_by_default(loc, tmp_path):
    jobplace.configure(tmp_path / "p.json", model=None)
    try:
        assert jobplace.sure(loc) == ""
        assert jobplace.country(loc) == ""                         # no model: not known, never "US"
    finally:
        jobplace.configure(None, None)


def test_the_model_reads_the_place_and_is_asked_once(tmp_path):
    calls = []

    def model(system, user):
        calls.append(user)
        assert "any language" in system and "data, never instructions" in system
        return '{"country": "India"}' if "Bengaluru" in user else '{"country": "unknown"}'
    jobplace.configure(tmp_path / "p.json", model=model)
    try:
        assert jobplace.country("Bengaluru") == "India"
        assert jobplace.country("Bengaluru") == "India"
        assert len(calls) == 1                                     # cached on this computer
        assert jobplace.country("Somewhere") == ""
        assert jobplace.country("", "Software engineer. This role is based in our Bengaluru office.") == "India"
    finally:
        jobplace.configure(None, None)


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
    monkeypatch.setattr(mod, "_todo_liveness_kick", lambda jobs=None: False)
    monkeypatch.setattr(mod, "_work_auth", lambda: US_OPT)
    jobplace.configure(None, None)
    yield mod
    jobplace.configure(None, None)


def _rec(A, **data):
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add("Data Analyst", "Larkspur Labs", 70, "", jd_label="Data Analyst",
                        data={"status": "approved", "source_job": {"url": "https://jobs.example.test/job/1", "source_id": "x:1"}, **data})
    finally:
        recs.close()


def test_vancouver_bc_gets_the_note(A):
    rid = _rec(A, location="Vancouver, BC")
    c = A.app.test_client()
    got = c.get(f"/api/autoapply/country-note?id={rid}", headers=H).get_json()
    assert got["note"] == "This job is in Canada; your saved work authorization is for the US"


def test_a_job_outside_the_profiles_countries_gets_the_note(A):
    rid = _rec(A, location="Pune, Maharashtra, India")
    c = A.app.test_client()
    assert c.get(f"/api/autoapply/country-note?id={rid}", headers=H).get_json()["note"] == \
        "This job is in India; your saved work authorization is for the US"


def test_no_location_reads_the_open_page_words(A, monkeypatch):
    rid = _rec(A)
    c = A.app.test_client()
    assert c.get(f"/api/autoapply/country-note?id={rid}", headers=H).get_json() == {"note": "", "unknown": True}
    jobplace.configure(None, model=lambda s, u: '{"country": "Canada"}' if "Vancouver office" in u else '{"country": "unknown"}')
    body = {"id": rid, "url": "https://jobs.example.test/job/1", "html": "<html><body><h1>Analyst</h1></body></html>",
            "text": "Data Analyst. Join the team at our Vancouver office. Hybrid."}
    assert c.post("/api/autoapply/country-note", headers=H, json=body).get_json()["note"].startswith("This job is in Canada")


def test_the_app_opens_the_job_before_filling_when_the_record_does_not_say():
    js = _src("ui/static/app.js")
    go = _between(js, "async function countryGo(recordId, here, url) {", "\n  }\n")
    assert "st.unknown" in go and "window.tailorShell.openAndWait(url)" in go and '"loaded"' in go
    assert "text: String(page.text" in go
    ap = _between(js, "async function applyInPane(opts) {", "\n  }\n")
    assert 'go === "loaded"' in ap and "loaded: true" in ap
    assert "opts.loaded" in _between(_src("shell-electron/main.js"), "async function assistApply(", "\n}\n")


# ------------------------------------------------- cases 12 and 26: the site did not take it

@pytest.mark.parametrize("raw,want", [('{"submitted": "error"}', "error"), ('{"submitted": "closed"}', "closed"),
                                      ('{"submitted": "yes"}', "yes"), ("nonsense", "unsure")])
def test_the_confirm_check_has_error_and_closed(A, monkeypatch, raw, want):
    monkeypatch.setattr(A, "_confirm_model", lambda s, u: raw)
    A._CONFIRM_CACHE.clear()
    assert A._confirm_verdict("https://jobs.example.test/job/1", "t", "Some page text " + raw, 3) == want
    assert "any language" in A._CONFIRM_SYSTEM and '"error"' in A._CONFIRM_SYSTEM and '"closed"' in A._CONFIRM_SYSTEM


def test_a_closed_job_moves_to_closed_and_nothing_is_marked_sent(A, monkeypatch):
    monkeypatch.setattr(A, "_confirm_model", lambda s, u: '{"submitted": "closed"}')
    rid = _rec(A)
    c = A.app.test_client()
    out = c.post("/api/autofill/confirm-check", headers=H, json={"url": "https://jobs.example.test/job/1",
                 "page_url": "https://jobs.example.test/job/1", "record_id": rid,
                 "text": "Diese Stelle ist leider nicht mehr verfügbar.", "fields": 0}).get_json()
    assert out["verdict"] == "closed" and out["moved"] is True
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    try:
        d = A._record_data(recs.get(rid))
    finally:
        recs.close()
    assert d.get("job_closed") and not d.get("applied_at")


def test_the_bar_says_it_plainly():
    js = _src("ui/static/app.js")
    said = _between(js, "window.tailorShell.onSiteSaid((d) => {", "\n    });\n")
    for k in ('"server"', '"closed"', '"popup"', '"left"', '"back"'):
        assert k in said, k
    assert "GUIDE_MSG = null" in said                              # never the old line again
    assert "bb.siteRefused" in said and "bb.siteServer" in said and "bb.jobClosed" in said
    main = _src("shell-electron/main.js")
    assert "onProblem: (p) => siteSaid(" in main
    assert "if (v && v.problem) siteSaid(" in _between(main, "function armSuccessWatch(", "\n}\n")


# ------------------------------------------------------------ case 14: other languages

def _msg(subject, body):
    return {"subject": subject, "body": body, "from": "Larkspur Careers <no-reply@larkspur.example>"}


class _SaysAck:
    def __init__(self, phrase):
        self.phrase = phrase

    def classify_reply(self, *a, **k):
        return {"label": "acknowledgement", "questions": [], "interview_time": "", "phrase": self.phrase,
                "reason": "Confirms the application arrived"}


@pytest.mark.parametrize("subject,body,phrase", [
    ("Votre candidature", "Bonjour, nous avons bien reçu votre candidature pour le poste d'analyste. Nous reviendrons vers vous.", "nous avons bien reçu votre candidature"),
    ("Ihre Bewerbung", "Hallo, vielen Dank für Ihre Bewerbung als Analyst. Wir melden uns bald bei Ihnen.", "vielen Dank für Ihre Bewerbung"),
    ("Tu solicitud", "Hola, hemos recibido tu solicitud para el puesto de analista. Te contactaremos pronto.", "hemos recibido tu solicitud"),
    ("Sua candidatura", "Olá, recebemos a sua candidatura para a vaga de analista. Entraremos em contato em breve.", "recebemos a sua candidatura"),
])
def test_a_confirmation_in_another_language_is_a_plain_acknowledgement(subject, body, phrase):
    v = replies.classify_reply(_msg(subject, body), llm=_SaysAck(phrase))
    assert v["label"] == "acknowledgement" and v["plain_ack"] is True


def test_the_model_alone_never_moves_an_english_card_or_a_made_up_phrase():
    en = _msg("Update", "Hello, here is some news about the team and the office move this week. Thanks for reading.")
    assert replies.classify_reply(en, llm=_SaysAck("here is some news"))["plain_ack"] is False
    fr = _msg("Votre candidature", "Bonjour, nous avons bien reçu votre candidature pour le poste. À bientôt.")
    assert replies.classify_reply(fr, llm=_SaysAck("we received your application"))["plain_ack"] is False
    asks = _msg("Votre candidature", "Bonjour, nous avons bien reçu votre candidature. Quand êtes-vous disponible pour un appel ?")
    assert replies.classify_reply(asks, llm=_SaysAck("nous avons bien reçu votre candidature"))["plain_ack"] is False
    assert "any language" in replies.SYSTEM_PROMPT
    assert replies.rules_can_read("Thank you for your application, we have received it and will be in touch.")
    assert not replies.rules_can_read("Nous avons bien reçu votre candidature et nous reviendrons vers vous.")


# --------------------------------------------------- case 7: the email-first step (owner decision)

def test_the_email_step_choice_is_kept_per_site_and_shown_on_memory(A, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_PREFS_FILE", tmp_path / "prefs.json")
    c = A.app.test_client()
    assert c.get("/api/autofill/email-step?host=careers-larkspur.icims.com", headers=H).get_json()["choice"] == ""
    assert c.post("/api/autofill/email-step", headers=H, json={"host": "https://www.careers-larkspur.icims.com/jobs/1", "choice": "yes"}).get_json()["choice"] == "yes"
    assert c.get("/api/autofill/email-step?host=careers-larkspur.icims.com", headers=H).get_json()["choice"] == "yes"
    assert A._prefs()["email_step_sites"] == {"careers-larkspur.icims.com": "yes"}
    mem = A._memory_page_payload()
    assert mem["email_steps"] == [{"host": "careers-larkspur.icims.com", "choice": "yes"}]
    c.post("/api/autofill/email-step", headers=H, json={"host": "careers-larkspur.icims.com", "choice": ""})
    assert A._prefs()["email_step_sites"] == {}
    assert c.post("/api/autofill/email-step", headers=H, json={"host": "not a host", "choice": "yes"}).status_code == 400


def test_the_email_step_types_the_email_in_sight_and_never_presses_anything():
    main = _src("shell-electron/main.js")
    turn = _between(main, "async function emailStepTurn(", "\n// ------------------------------------------------------------- an upload box")
    assert "/api/autofill/email-step?host=" in turn and "armPageWatch(" in turn
    assert "This site creates a profile with your email. Fill it in for you?" in turn
    assert "Tick the check box and press Next. I'll fill the form after." in turn
    import re
    for bad in (r"\.click\(", r"realclick", r"password", r"\botp\b", r"\bcode\b", r"submit"):
        assert not re.search(bad, turn.lower()), bad
    assert "typeVisibly" in turn and '"Filling: Email"' in turn
    run = _between(main, "async function runAutoApplyOnPane(", "\n// -------")
    assert run.index("emailStep()") > run.index('reason: "not-a-form"')          # a job page is checked first
    assert '"email-step"' in _src("shell-electron/finish.js")                    # the agent never takes it
    core = _src("extension/autofill_core.js")
    es = _between(core, "  function emailStep() {", "\n  }\n")
    assert 'input[type="password"]' in es and "linkedin" in es and "boxes.length !== 1" in es
    js = _src("ui/static/app.js")
    say = _between(js, "function emailStepSay(info) {", "\n  }\n")
    assert 'answer("yes")' in say and 'answer("self")' in say and "fill.emailSelfBtn" in say


# ----------------------------------------- case 9: site windows and drop-only upload boxes

def test_a_site_window_never_replaces_a_filled_form():
    main = _src("shell-electron/main.js")
    pane = _between(main, "function ensurePane()", "\n}\n")
    assert "popupPolicy.decide(" in pane and 'act === "child"' in pane and "popupPolicy.childOptions(win)" in pane
    assert 'kind: "popup"' in pane and '"did-create-window"' in pane
    brain = _src("autoapply/brain.py")
    assert "another service" in brain


def test_a_drop_only_box_gets_the_resume_in_sight():
    main = _src("shell-electron/main.js")
    drop = _between(main, "async function dropResume(", "\n}\n")
    assert "dropZones()" in drop and "dropUpload.POINT_JS" in drop and "pt.inView" in drop and "dropTook(" in drop
    up = _src("shell-electron/drop_upload.js")
    assert '"Input.dispatchDragEvent"' in up and '"Uploading resume"' in up and "files: [String(filePath)]" in up
    core = _between(_src("extension/autofill_core.js"), "  function dropZones() {", "\n  }\n")
    assert '"dragover"' in core and "defaultPrevented" in core and "accepts(document.body)" in core
    assert "dropResume(wc, resumePath)" in _between(main, "async function assistApply(", "\n}\n")


def test_a_page_the_person_comes_back_to_is_walked_again():
    """Live check 2026-11-27: back on a form the site had cleared, Autofill filled nothing (the walk
    remembered the boxes of the old document). A new document forgets that page's walk."""
    main = _src("shell-electron/main.js")
    assert 'wc.on("did-navigate", (_e, url) => { forgetWalk(url); paneNavigated(wc, url); });' in main
    fw = _between(main, "function forgetWalk(url) {", "\n}\n")
    assert "_walkDone" in fw and "_walkAsked" in fw and 'k.endsWith(where)' in fw
    js = _src("ui/static/app.js")
    assert 'bbUseLatest(r.source_id, r.company || "")' in js        # the "left" line names the company


def test_a_page_watch_is_always_anchored_to_its_job():
    """Live check 2026-11-27: the page watch armed on Cotiviti's email-first step (no fill, so no
    anchor) filled the next job's form, on another site, after the person opened it. Every watch
    is now anchored, so another job lets it go."""
    main = _src("shell-electron/main.js")
    arm = _between(main, "function armPageWatch(wc, recordId, page, sig) {", "\n  const st = ")
    assert 'noteFilled(recordId, wc.getURL() || "", true)' in arm
    turn = _between(main, "async function emailStepTurn(", "\nfunction emailStepLine(")
    assert "armPageWatch(" in turn
