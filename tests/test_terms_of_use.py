"""Applying on your behalf is covered by the Terms of Use (Kofi, 2026-11-01: "people don't have
time to be clicking around; put it in the terms and conditions of use").

* The person agrees where they already click: finishing sign-in (email code or Google) records
  the Terms version, the time and "signin"; a person who agreed to an older version (or never
  signed in) gets one quiet line on Auto-apply, and showing it records "notice".
* Agreeing turns on what the Terms cover (accepting the notices a site requires, creating a site
  account) unless the person decided an item themselves: a switch they turned off stays off.
* Before that, nothing is on, and creating a portal account is refused.
* Nothing asks before a run: the bar's up-front terms question and setup's terms box are gone;
  the memory page's "What I may do for you" card holds the switches.
"""

import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
TERMS = (ROOT / "site" / "terms.html").read_text(encoding="utf-8")
H = {"X-Tailor-Extension": "1"}


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_saved_identity", lambda: {"name": "Test Person", "email": "test@example.com"})
    return mod, mod.app.test_client()


def _consent(c):
    return c.get("/api/autoapply/consent", headers=H).get_json()


def _status(c):
    return c.get("/api/terms/status", headers=H).get_json()


def _sign_in(mod, c, monkeypatch, ok=True):
    monkeypatch.setattr(mod, "_broker_post", lambda path, body: (200, {"token": "tok-1", "email": "test@example.com"})
                        if ok else (400, {"reason": "bad"}))
    return c.post("/api/account/email/verify", json={"email": "test@example.com", "code": "123456"}, headers=H)


# --------------------------------------------------------------------------- before agreeing
def test_nothing_is_on_before_the_person_agreed(A):
    mod, c = A
    st = _status(c)
    assert st["accepted"] is False and st["version"] == mod.TERMS_VERSION and st["accepted_version"] == ""
    d = _consent(c)
    assert d["terms"]["on"] is False and d["accounts"]["on"] is False
    r = c.post("/api/autoapply/agent/portal-account", json={"host": "acme.wd5.myworkdayjobs.com", "create": True},
               headers=H).get_json()
    assert r["ok"] is False and r["reason"] == "portal_accounts_off"


def test_the_old_portal_default_is_not_an_agreement(A):
    mod, c = A
    mod._PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod._PREFS_FILE.write_text(json.dumps({"portal_accounts": True}), encoding="utf-8")
    assert _consent(c)["accounts"]["on"] is False


# --------------------------------------------------------------------------- agreeing at sign-in
def test_sign_in_records_the_version_and_turns_on_what_the_terms_cover(A, monkeypatch):
    mod, c = A
    assert _sign_in(mod, c, monkeypatch).get_json()["ok"] is True
    st = _status(c)
    assert st["accepted"] is True and st["accepted_version"] == mod.TERMS_VERSION and st["via"] == "signin"
    assert st["at"] > 0
    d = _consent(c)
    assert d["terms"] == {"decided": True, "on": True} and d["accounts"] == {"decided": True, "on": True}
    b = c.get("/api/answers/basics", headers=H).get_json()
    assert b["accept_required_terms"] == "always" and b["terms_decided"] is True
    r = c.post("/api/autoapply/agent/portal-account", json={"host": "acme.wd5.myworkdayjobs.com", "create": True},
               headers=H).get_json()
    assert r["ok"] is True


def test_google_sign_in_records_it_too(A, monkeypatch):
    mod, c = A

    class R:
        status_code = 200
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert c.post("/api/account/adopt", json={"token": "tok-g"}, headers=H).get_json()["ok"] is True
    assert _status(c)["via"] == "signin" and _consent(c)["terms"]["on"] is True


def test_a_failed_sign_in_records_nothing(A, monkeypatch):
    mod, c = A
    assert _sign_in(mod, c, monkeypatch, ok=False).status_code == 400
    assert _status(c)["accepted"] is False and _consent(c)["terms"]["on"] is False


def test_a_switch_the_person_turned_off_stays_off(A, monkeypatch):
    mod, c = A
    # "No, ask me first" from the old bar question, and accounts switched off on the memory page
    c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "ask"}, headers=H)
    c.post("/api/autoapply/consent", json={"accounts": False}, headers=H)
    _sign_in(mod, c, monkeypatch)
    d = _consent(c)
    assert _status(c)["accepted"] is True
    assert d["terms"] == {"decided": True, "on": False} and d["accounts"] == {"decided": True, "on": False}


def test_turning_off_after_agreeing_is_kept_and_signing_in_again_changes_nothing(A, monkeypatch):
    mod, c = A
    _sign_in(mod, c, monkeypatch)
    at = _status(c)["at"]
    d = c.post("/api/autoapply/consent", json={"terms": False}, headers=H).get_json()
    assert d["terms"]["on"] is False and mod._terms_pref() == "ask"
    _sign_in(mod, c, monkeypatch)                         # same version: recorded once
    assert _status(c)["at"] == at and _consent(c)["terms"]["on"] is False


def test_a_new_terms_version_is_agreed_again_but_never_overrides_the_person(A, monkeypatch):
    mod, c = A
    _sign_in(mod, c, monkeypatch)
    c.post("/api/autoapply/consent", json={"accounts": False}, headers=H)
    monkeypatch.setattr(mod, "TERMS_VERSION", "2099-01-01")
    assert _status(c)["accepted"] is False
    c.post("/api/terms/notice", json={}, headers=H)
    st = _status(c)
    assert st["accepted"] is True and st["accepted_version"] == "2099-01-01" and st["via"] == "notice"
    assert _consent(c)["accounts"]["on"] is False


# --------------------------------------------------------------------------- the notice line
def test_the_auto_apply_notice_records_the_version_and_counts(A):
    mod, c = A
    d = c.post("/api/terms/notice", json={}, headers=H).get_json()
    assert d["accepted"] is True and d["via"] == "notice"
    assert _consent(c)["terms"]["on"] is True


def test_the_switches_take_only_true_or_false(A):
    mod, c = A
    assert c.post("/api/autoapply/consent", json={}, headers=H).status_code == 400
    assert c.post("/api/autoapply/consent", json={"terms": "yes", "accounts": 1}, headers=H).status_code == 400
    assert _consent(c)["terms"]["on"] is False


def test_the_memory_page_says_the_terms_choice_once(A):
    mod, c = A
    mod._merge_prefs({"accept_required_terms": "always"})
    page = c.get("/api/memory/page", headers=H).get_json()
    assert not any(r.get("key") == "accept_required_terms" for r in page.get("answers") or [])


# --------------------------------------------------------------------------- the wiring
def _fn(name):
    i = APP_JS.index(name)
    return APP_JS[i:APP_JS.index("\n  }\n", i)]


def test_no_question_before_a_run():
    assert "aaTermsGate" not in APP_JS and "terms-gate" not in APP_JS and "aaConsentGate" not in APP_JS
    assert 'id="aaConsent"' not in INDEX
    assert 'id="ansTerms"' not in INDEX and "#ansTerms" not in APP_JS
    assert "terms_decided" not in _fn("async function openBasicsStep(")
    fill = _fn("async function aaFill(")
    assert "consent" not in fill.lower() and "terms" not in fill.lower()


def test_the_sign_in_line_sits_under_the_buttons_and_links_the_terms():
    i = INDEX.index('id="signinStart"')
    step = INDEX[i:INDEX.index('id="signinCode"', i)]
    assert step.index('id="signinEmailGo"') < step.index('id="signinTerms"')
    text = re.sub(r"<[^>]+>", "", step[step.index('id="signinTerms"'):]).split("</p>")[0]
    text = " ".join(text.split())
    assert "By continuing, you agree to the Terms of Use, including letting SponsorJobs accept the notices job sites require when it applies for you." in text
    assert 'href="https://sponsorjobs.ai/terms.html#applying"' in step


def test_the_notice_is_one_line_on_auto_apply_and_never_a_modal():
    i = INDEX.index('id="aaTou"')
    line = INDEX[i - 40:INDEX.index("</p>", i)]
    assert "<p" in line and "dialog" not in line and "Our Terms now cover accepting site notices for you." in line
    assert 'id="aaTouOff"' in line and "Turn off" in line
    assert "aaTouNotice();" in _fn("async function loadAutoApply(")
    notice = _fn("async function aaTouNotice(")
    assert '"/api/terms/notice"' in notice and "Panel" not in notice
    assert 'openMemory({ origin: "autoapply", focus: "may" })' in APP_JS


def test_the_memory_page_card_holds_the_switches():
    i = INDEX.index('id="memMay"')
    card = INDEX[i:INDEX.index("</section>", i)]
    assert "What I may do for you" in card and 'id="memTerms"' in card and 'id="memAccounts"' in card
    assert 'id="aaMayBtn"' in INDEX


def test_the_terms_page_says_it_in_plain_words():
    i = TERMS.index('<h2 id="applying">Applying on your behalf</h2>')
    sec = TERMS[i:TERMS.index("<h2", i + 10)]
    for words in ("Accept the privacy notices and terms a job site requires", "Create an account on a job site",
                  "with your SponsorJobs email", "Fill the site's forms with the answers you saved",
                  "never accepts marketing or cookie choices", "never solves a captcha",
                  "without your Send", "Autonomous submit", "listed on the application before you send",
                  "turn each of these off in the app"):
        assert words in sec, words
    assert '<a href="#applying">Applying on your behalf</a>' in TERMS
    assert not re.search(r"[—–]", sec)


def test_new_copy_has_every_locale_and_no_dashes():
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    keys = [k for k in en if k.startswith(("consent.", "tou.", "signin.terms"))]
    assert len(keys) >= 16
    for code in ("de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        assert all(d.get(k) for k in keys), code
        assert not any(ch in d[k] for k in keys for ch in "—–")
    assert not any(ch in en[k] for k in keys for ch in "—–")
    assert not any(re.search(r"\bCV\b", en[k]) for k in keys)
