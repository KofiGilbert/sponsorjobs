"""Terms a site requires to apply (live failure, Lam Research, 2026-10-30).

A "Data Privacy Agreement" dialog with Cancel and I Acknowledge opened during the run (Phenom
opens it when the resume is attached) and stayed: nothing handled it and the run stalled. The
owner's decision: the agent may accept a privacy notice or terms a site REQUIRES to apply, with
the person's consent, recorded. So:

* the page side finds such a dialog or required box (tests/test_agent_view.js, on the real Lam
  markup in tests/fixtures/forms/phenom_lam.html), never a marketing opt-in or a cookie banner;
* a preference accept_required_terms ("ask" by default, or "always") in data/app_prefs.json;
* on "ask" the person is asked on the bar and Telegram with three choices; "Accept for all
  applications" sets "always"; "Stop" stops the run and nothing is accepted; a typed reply is
  read strictly, never by a model;
* every acceptance is recorded on the application and named in the Ready to send summary;
* the brain never clicks a policy link, never accepts terms itself, and reports needs-consent.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply import brain
from autoapply.brain import SYSTEM_PROMPT, parse_reply

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
LAM_URL = "https://careers.lamresearch.com/careers/apply?pid=1099556233587&domain=lamresearch.com"
QUESTION = "Lam Research needs you to accept its Data Privacy Agreement to apply. Accept it?"


# --------------------------------------------------------------------------- the brain
def test_brain_prompt_leaves_terms_to_the_person_and_never_opens_a_policy_link():
    low = SYSTEM_PROMPT.lower()
    assert "never click a link to a privacy policy" in low
    assert "never tick a box that accepts terms" in low
    assert "i acknowledge" in low and "needs-consent" in low
    assert "marketing box" in low and "leave it unticked" in low
    # the old exception (the model ticks "I agree to the terms" itself) is gone
    assert "tick it (the person turned account creation on)" not in low
    assert len(SYSTEM_PROMPT.splitlines()) < 60


def test_needs_consent_is_a_blocked_reason_the_shell_understands():
    assert "needs-consent" in brain.BLOCKED_REASONS
    r = parse_reply('{"memory": "", "evaluation": "", "actions": [], "done": {"outcome": "blocked", "reason": "needs-consent"}}',
                    {1})
    assert r["done"] == {"outcome": "blocked", "reason": "needs-consent"}


# --------------------------------------------------------------------------- the words
def test_the_question_and_the_three_choices():
    assert AK.terms_question("Lam Research", "Data Privacy Agreement") == QUESTION
    assert AK.terms_question("", "Terms of Use") == "This site needs you to accept its Terms of Use to apply. Accept it?"
    assert AK.TERMS_CHOICES == ("Accept for all applications", "Just this one", "Stop")
    assert all(len(c) <= AK.LABEL_MAX for c in AK.TERMS_CHOICES)          # Telegram button labels
    assert AK.terms_decision("Accept for all applications") == ("accept", "all")
    assert AK.terms_decision("Just this one") == ("accept", "once")
    assert AK.terms_decision("Stop") == ("stop", "")
    text, buttons = AK.telegram_ask("abc", [{"question": QUESTION, "options": list(AK.TERMS_CHOICES)}])
    assert QUESTION in text and [b[0][0] for b in buttons] == list(AK.TERMS_CHOICES)


@pytest.mark.parametrize("reply,want", [
    ("Accept for all applications", "Accept for all applications"), ("1", "Accept for all applications"),
    ("always", "Accept for all applications"), ("Just this one", "Just this one"), ("2", "Just this one"),
    ("yes", "Just this one"), ("Yes.", "Just this one"), ("stop", "Stop"), ("3", "Stop"), ("No", "Stop"),
    ("maybe later", None), ("what does it say?", None), ("yes but only if it is not marketing", None), ("", None),
])
def test_a_typed_reply_is_read_strictly(reply, want):
    assert AK.read_terms_reply(reply) == want


def test_the_shell_says_the_same_words():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const A = require(%r);
const out = [A.termsQuestion('Lam Research', 'Data Privacy Agreement'), A.TERMS_CHOICES,
  A.acceptedLine('Lam Research', ['Data Privacy Agreement']), A.acceptedLine('Lam Research', []),
  A.acceptedLine('', ['Privacy notice', 'Terms of use', 'Privacy notice']),
  A.blockedMessage('needs-consent'), A.blockedMessage('terms-declined')];
console.log(JSON.stringify(out));
""" % str(ROOT / "shell-electron" / "agent.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    q, choices, one, none, two, consent, declined = json.loads(out.stdout)
    assert q == QUESTION and choices == list(AK.TERMS_CHOICES)
    assert one == "Accepted Lam Research's Data Privacy Agreement for you."
    assert none == ""
    assert two == "Accepted the site's Privacy notice and Terms of use for you."
    assert "accept its terms" in consent and "did not apply" in declined


def test_the_shell_handles_terms_with_consent_and_never_on_stop():
    """The shell's handleTerms: asks (or takes "always"), records, then clicks; on Stop or no
    answer it stops without clicking. The Ready to send summary names what was accepted."""
    src = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    body = src[src.index("async function handleTerms("):src.index("  return { run, send, stop")]
    decide = body.index('"/api/autoapply/agent/terms"')
    click = body.index("realClick(wc, it.index)")
    record = body.index('"/api/autoapply/agent/terms/accepted"')
    assert decide < click < record
    stop_at = body.index('r.decision !== "accept"')
    assert stop_at < click and "terms-declined" in body[stop_at:click]
    assert 'reason: "needs-consent"' in body[:click]            # no answer: stop, nothing accepted
    assert "acceptedLine(opts.company" in src and "terms.accepted" in src
    run = src[src.index("async function run("):src.index("// ---- Submit: the shell's decision")]
    assert run.count("handleTerms(wc, terms") == 3                  # before the fill, after it, and on needs-consent


# --------------------------------------------------------------------------- the routes
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    sent = []

    class Chan:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Chan())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Business Operations Analyst 2", "Lam Research", 70, "", data={"status": "approved"})
    recs.close()
    return mod, sent, rid


def _post(mod, path, body):
    return mod.app.test_client().post(path, json=body, headers=H).get_json()


def _terms_in_thread(mod, body):
    out = {}

    def run():
        out["r"] = _post(mod, "/api/autoapply/agent/terms", body)
    th = threading.Thread(target=run)
    th.start()
    for _ in range(50):
        with mod._ASKS_LOCK:
            if any(a.get("kind") == "terms" and a.get("open", True) for a in mod._ASKS.values()):
                break
        time.sleep(0.1)
    return th, out


def _open_terms_ask(mod):
    with mod._ASKS_LOCK:
        return next(a for a in mod._ASKS.values() if a.get("kind") == "terms" and a.get("open", True))


def test_the_preference_defaults_to_ask_and_check_only_returns_the_question(A):
    mod, sent, rid = A
    assert mod._terms_pref() == "ask"
    r = _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                                  "check_only": True})
    assert r == {"ok": True, "decision": "ask", "question": QUESTION, "options": list(AK.TERMS_CHOICES)}
    assert sent == []


def test_always_accepts_without_asking(A):
    mod, sent, rid = A
    mod._merge_prefs({"accept_required_terms": "always"})
    r = _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL})
    assert r == {"ok": True, "decision": "accept", "via": "always"}
    assert sent == [] and not mod._ASKS


def test_accept_for_all_applications_on_telegram_sets_always(A):
    mod, sent, rid = A
    th, out = _terms_in_thread(mod, {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                     "id": "t1", "timeout_s": 10})
    ask = _open_terms_ask(mod)
    assert sent and sent[0][0].startswith(QUESTION) and [b[0][0] for b in sent[0][1]] == list(AK.TERMS_CHOICES)
    assert mod._agent_ask_event("button", f"ask:{ask['id']}:0") == "Got it: Accept for all applications."
    th.join(15)
    assert out["r"] == {"ok": True, "id": "t1", "decision": "accept", "via": "all"}
    assert mod._terms_pref() == "always"
    assert mod._load_prefs()["accept_required_terms"] == "always"


def test_just_this_one_on_the_bar_accepts_once_and_keeps_asking_next_time(A):
    mod, _sent, rid = A
    th, out = _terms_in_thread(mod, {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                     "id": "t2", "timeout_s": 10})
    # the bar only takes one of the three choices
    bad = mod.app.test_client().post("/api/autoapply/agent/answer", json={"id": "t2", "answer": "Sure", "exact": True,
                                                                         "index": 0}, headers=H)
    assert bad.status_code == 400
    ok = _post(mod, "/api/autoapply/agent/answer", {"id": "t2", "answer": "Just this one", "exact": True, "index": 0})
    assert ok["ok"] and ok["done"]
    th.join(15)
    assert out["r"]["decision"] == "accept" and out["r"]["via"] == "once"
    assert mod._terms_pref() == "ask"


def test_stop_declines_and_a_typed_reply_is_never_guessed(A):
    mod, _sent, rid = A
    th, out = _terms_in_thread(mod, {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                     "id": "t3", "timeout_s": 10})
    # an unclear typed reply is not consent: the person is told to pick one, the question stays open
    msg, done = mod._ask_take_reply("t3", "hmm what is in it")
    assert not done and "Accept for all applications" in msg
    assert _open_terms_ask(mod)["items"][0]["answer"] is None
    msg, done = mod._ask_take_reply("t3", "no")
    assert done and msg == "Got it: Stop."
    th.join(15)
    assert out["r"]["decision"] == "stop" and out["r"]["ok"] is True
    assert mod._terms_pref() == "ask"


def test_no_answer_is_never_an_acceptance(A):
    mod, _sent, rid = A
    r = _post(mod, "/api/autoapply/agent/terms", {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                                  "timeout_s": 1})
    assert r["ok"] is False and r["reason"] == "timeout" and "decision" not in r


def test_every_acceptance_is_recorded_on_the_application(A):
    mod, _sent, rid = A
    body = {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL, "kind": "modal", "via": "once"}
    r = _post(mod, "/api/autoapply/agent/terms/accepted", body)
    assert r["ok"] is True
    _post(mod, "/api/autoapply/agent/terms/accepted", body)                       # the same one again: one entry
    _post(mod, "/api/autoapply/agent/terms/accepted", dict(body, title="E-Verify acknowledgement", kind="checkbox"))
    got = mod._record_data(mod._get_record(rid))["terms_accepted"]
    assert [(x["company"], x["title"], x["host"], x["kind"]) for x in got] == [
        ("Lam Research", "Data Privacy Agreement", "careers.lamresearch.com", "modal"),
        ("Lam Research", "E-Verify acknowledgement", "careers.lamresearch.com", "checkbox")]
    assert all(x["url"] == LAM_URL and x["at"] for x in got)
    assert _post(mod, "/api/autoapply/agent/terms/accepted", {"title": "x"}) == {"ok": False, "reason": "no-record"}


def test_the_preference_lives_in_app_prefs_and_survives_other_saves(A):
    mod, _sent, _rid = A
    c = mod.app.test_client()
    d = c.get("/api/answers/basics", headers=H).get_json()
    assert d["accept_required_terms"] == "ask"
    d = c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "always"}, headers=H).get_json()
    assert d["accept_required_terms"] == "always"
    assert json.loads((mod._DATA / "app_prefs.json").read_text())["accept_required_terms"] == "always"
    # the browser extension's whole-store save does not carry it, and does not reset it
    mod._save_prefs({"work_auth_us": "Yes"})
    assert mod._terms_pref() == "always"
    c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "ask"}, headers=H)
    assert mod._terms_pref() == "ask"
    c.post("/api/answers/basics", json={"fields": {}, "accept_required_terms": "sometimes"}, headers=H)
    assert mod._terms_pref() == "ask"


def test_an_open_terms_ask_takes_the_reply_before_any_late_answer(A):
    """A late answer (an ask that stopped waiting within the day) never steals a reply meant for
    an open terms ask; a terms ask is never kept for a late reply; once it closes, late answers
    work as before."""
    mod, _sent, rid = A
    mod._recent_ask_add("old1", [{"question": "Gender", "options": ["Male", "Female"], "all_options": ["Male", "Female"],
                                  "partial": False}], record_id=rid, company="Lam Research", timeout=1)
    mod._recent_ask_end("old1", [])
    th, out = _terms_in_thread(mod, {"record_id": rid, "title": "Data Privacy Agreement", "url": LAM_URL,
                                     "id": "t9", "timeout_s": 10})
    assert mod._agent_ask_event("text", "1") == "Got it: Accept for all applications."
    th.join(15)
    assert out["r"]["decision"] == "accept" and out["r"]["via"] == "all"
    rows = mod._recent_asks_load()
    assert [r["id"] for r in rows] == ["old1"] and rows[0]["items"][0]["question"] == "Gender"
    # the terms ask is closed now: a tap on its button is never consent, and never a late answer
    assert mod._agent_ask_event("button", "ask:t9:1") in ("That question was already answered.", "That question is no longer open.")
    # the late answer for the old ask still works
    got = mod._agent_ask_event("text", "1. Male")
    assert got and got.startswith("Saved for next time: Gender")
