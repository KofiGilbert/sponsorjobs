"""The apply agent's brain (autoapply/brain.py, docs/apply-agent.md section 5) and its routes.

Pins the data boundary (no profile value in any prompt, placeholders resolved after the reply),
the hard validation of the model's reply (unknown index dropped, five actions at most, done alone),
the submit guard (the model never presses Submit), the loop nudge, the FakeLLM scripted run from a
job page to ready_to_submit, the portal-account routes (the password never comes out), and the host
fence. Deterministic FakeLLM, no network.
"""
from __future__ import annotations

import importlib
import json
import re

import pytest

from autoapply import brain
from autoapply.brain import (SYSTEM_PROMPT, build_user_message, loop_nudges, parse_reply, redact,
                             resolve_actions, step, view_lines)
from llm.base import FakeLLM
from submit.policy import run_allowed_hosts

APPLICANT = {"first_name": "Kofi", "last_name": "Gilbert", "full_name": "Kofi Gilbert",
             "email": "kofi@example.com", "phone": "555-100-2000", "city": "Austin"}
PORTAL = {"host": "acme.wd5.myworkdayjobs.com", "email": "kofi@example.com", "password": "Zq8!kL2m9Xp#4Rt7"}
PLACEHOLDERS = ["first_name", "last_name", "email", "phone", "city", "portal_email", "portal_password"]

JOB_PAGE = """URL: https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123
Title: Analyst
[Start of page]
Business Intelligence Analyst
[1]<button>Apply</button>
[2]<a>Save</a>
[3]<input type=search label="Search jobs" value="" search=true>
[End of page]"""

SIGNIN_PAGE = """URL: https://acme.wd5.myworkdayjobs.com/en-US/External/login
Title: Sign In
Sign In
[3]<input type=email label="Email Address" value="" required>
[4]<input type=password label="Password" value="">
[5]<checkbox label="Remember me" checked=false>
[6]<button>Sign In</button>
[7]<a>Create Account</a>
[End of page]"""

FORM_PAGE = """URL: https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123/apply/page2
Title: Apply - Analyst
My Experience
[10]<input type=text label="Job Title" value="(filled)">
[11]<combobox label="School or University" value="">
[12]<select label="Veteran status" options=3 first="Yes|No|Decline">
[13]<file label="Resume/CV">
[14]<button>Next</button>
[End of page]"""

REVIEW_PAGE = """URL: https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123/apply/review
Title: Review
Review your application
Contact: {{email}} shown back
[20]<a>Edit</a>
[21]<button>Submit</button>
[End of page]"""


def _req(view, step_n=1, history=None, **kw):
    return {"step": step_n, "max_steps": 40, "url": "https://acme.wd5.myworkdayjobs.com/en-US/x",
            "lane": "assisted", "view": view, "history": history or [],
            "job": {"role": "Analyst", "company": "Acme"}, **kw}


# --- the prompt ---------------------------------------------------------------------------------

def test_system_prompt_is_short_dash_free_and_forbids_submit():
    assert len(SYSTEM_PROMPT.splitlines()) < 60
    assert not re.search(r"[—–]", SYSTEM_PROMPT)
    low = SYSTEM_PROMPT.lower()
    assert "never click submit" in low and "ready_to_submit" in low
    assert "never press enter" in low and "search=true" in low


def test_user_message_ends_with_step_info_and_carries_no_profile_value():
    view = ("[1]<button>Apply</button>\nWelcome back Kofi Gilbert, kofi@example.com\n"
            "We will call you on (555) 100-2000")
    history = [{"step": 1, "evaluation": "typed kofi@example.com into email",
                "actions": [{"op": "type", "index": 3, "text": "kofi@example.com"}], "results": ["ok"]}]
    msg = build_user_message(_req(view, 2, history, left=["Veteran status"]), PLACEHOLDERS, [],
                             values={**APPLICANT, "portal_password": PORTAL["password"]})
    assert msg.rstrip().endswith("</step_info>")
    assert msg.index("<view>") < msg.index("<step_info>")
    for v in ("kofi@example.com", "Kofi Gilbert", "555-100-2000", "5551002000", PORTAL["password"]):
        assert v not in msg
    assert "{{email}}" in msg and "{{full_name}}" in msg and "{{phone}}" in msg
    assert "Veteran status" in msg and "step 2 of 40" in msg and "{{portal_password}}" in msg


def test_redact_longest_first_case_insensitive_short_values_left():
    out = redact("KOFI GILBERT and kofi and TX", {"full_name": "Kofi Gilbert", "first_name": "Kofi", "state": "TX"})
    assert out == "{{full_name}} and {{first_name}} and TX"


# --- the reply ----------------------------------------------------------------------------------

def test_parse_reply_drops_unknown_index_and_caps_five():
    acts = [{"op": "click", "index": 99}] + [{"op": "scroll", "direction": "down"}] * 7
    r = parse_reply(json.dumps({"memory": "m", "evaluation": "e", "actions": acts, "done": None}), {1, 2})
    assert r is not None and all(a["op"] == "scroll" for a in r["actions"]) and len(r["actions"]) == 5


def test_parse_reply_done_alone_wins_and_garbage_is_none():
    raw = "```json\n" + json.dumps({"actions": [{"op": "click", "index": 1}],
                                     "done": {"outcome": "blocked", "reason": "captcha"}}) + "\n```"
    r = parse_reply(raw, {1})
    assert r["actions"] == [] and r["done"] == {"outcome": "blocked", "reason": "captcha"}
    assert parse_reply("", {1}) is None
    assert parse_reply("I would click Apply.", {1}) is None
    assert parse_reply(json.dumps({"memory": "nothing", "actions": [], "done": None}), {1}) is None
    bad = parse_reply(json.dumps({"done": {"outcome": "blocked", "reason": "made up"}}), {1})
    assert bad["done"]["reason"] == "needs-person"        # an unknown reason -> needs a person


def test_resolve_actions_placeholders_fact_gate_and_digit_runs():
    labels = {3: "Email Address", 11: "School or University", 12: "Veteran status", 15: "Search jobs by keyword"}
    notes = []
    out = resolve_actions([
        {"op": "type", "index": 3, "text": "{{email}}"},
        {"op": "type", "index": 4, "text": "{{portal_password}}"},
        {"op": "type", "index": 12, "text": "No"},             # a fact question: dropped
        {"op": "type", "index": 15, "text": "call 5551002000"},   # a digit run: dropped
        {"op": "type", "index": 15, "text": "analyst"},        # free text on a search box: fine
        {"op": "type", "index": 15, "text": "{{ghost}}"},      # no such value: dropped
        {"op": "click", "index": 14},
    ], APPLICANT, PORTAL, labels, notes)
    assert out[0] == {"op": "type", "index": 3, "text": "kofi@example.com", "sensitive": True, "placeholder": "email"}
    assert out[1]["text"] == PORTAL["password"] and out[1]["sensitive"] is True
    assert out[2] == {"op": "type", "index": 15, "text": "analyst", "sensitive": False}
    assert out[3] == {"op": "click", "index": 14} and len(out) == 4
    assert len(notes) == 3


# --- step: guards -------------------------------------------------------------------------------

class _Scripted:
    """A brain that answers with the given raw texts, in order."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def agent_step(self, system, user):
        self.calls.append(user)
        return self.replies.pop(0) if self.replies else ""


def test_submit_guard_turns_a_submit_click_into_ready_to_submit():
    llm = _Scripted(json.dumps({"memory": "review", "evaluation": "ok",
                                "actions": [{"op": "click", "index": 21}], "done": None}))
    out = step(_req(REVIEW_PAGE, 9), llm, APPLICANT, PORTAL, placeholders=PLACEHOLDERS)
    assert out["actions"] == [] and out["done"]["outcome"] == "ready_to_submit"
    assert "kofi@example.com" not in llm.calls[0] and "{{email}}" in llm.calls[0]


def test_empty_reply_retries_once_then_blocks():
    llm = _Scripted("", "")
    out = step(_req(JOB_PAGE), llm, APPLICANT, None, placeholders=PLACEHOLDERS)
    assert len(llm.calls) == 2 and out["done"] == {"outcome": "blocked", "reason": "needs-person"}


def test_loop_nudge_after_three_repeats_and_done_only_at_the_limit():
    hist = [{"step": i, "evaluation": "no change", "actions": [{"op": "click", "index": 1}], "results": ["ok"]}
            for i in range(1, 4)]
    nudges = loop_nudges(hist)
    assert len(nudges) == 1 and "click 1" in nudges[0] and "3 times" in nudges[0]
    assert loop_nudges(hist[:2]) == []
    llm = _Scripted(json.dumps({"actions": [{"op": "click", "index": 1}], "done": None}))
    out = step(_req(JOB_PAGE, 40, hist), llm, APPLICANT, None, placeholders=PLACEHOLDERS)
    assert "<nudges>" in llm.calls[0] and "3 times" in llm.calls[0] and "last step" in llm.calls[0]
    assert out["actions"] == [] and out["done"]["outcome"] == "blocked"   # no done at the limit -> blocked


def test_view_lines_parse_index_tag_text_and_label():
    lines = view_lines(SIGNIN_PAGE + "\n*[8]<option>Harvard University</option>")
    assert lines[3]["tag"] == "input" and lines[3]["label"] == "Email Address"
    assert lines[6]["text"] == "Sign In" and lines[8]["text"] == "Harvard University"


# --- the FakeLLM scripted run -------------------------------------------------------------------

class _Recording(FakeLLM):
    """The FakeLLM, keeping every prompt it was shown so the data boundary can be checked."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def agent_step(self, system, user):
        self.seen.append(user)
        return super().agent_step(system, user)


def test_fake_llm_runs_from_the_job_page_to_ready_to_submit():
    llm = _Recording()
    history = []
    seq = []
    for n, view in enumerate([JOB_PAGE, SIGNIN_PAGE, FORM_PAGE, REVIEW_PAGE], start=1):
        out = step(_req(view, n, history), llm, APPLICANT, PORTAL, placeholders=PLACEHOLDERS,
                   resume_path="/tmp/Kofi Gilbert Resume.pdf")
        seq.append(out)
        history.append({"step": n, "evaluation": out["evaluation"], "memory": out["memory"],
                        "actions": out["actions"], "results": ["ok"] * len(out["actions"])})
    assert seq[0]["actions"] == [{"op": "click", "index": 1}]
    a = seq[1]["actions"]
    assert [x["op"] for x in a] == ["type", "type", "click"] and a[2]["index"] == 6
    assert a[0]["text"] == PORTAL["email"] and a[1]["text"] == PORTAL["password"]
    assert a[0]["sensitive"] and a[1]["sensitive"]
    assert seq[2]["actions"] == [{"op": "click", "index": 14}]
    assert seq[3]["actions"] == [] and seq[3]["done"]["outcome"] == "ready_to_submit"
    assert len(llm.seen) == 4
    for user in llm.seen:                        # the model never saw a value, only names
        assert PORTAL["password"] not in user and "kofi@example.com" not in user
        assert user.rstrip().endswith("</step_info>")


def test_fake_llm_creates_an_account_when_there_is_none_and_stops_on_a_captcha():
    out = step(_req(SIGNIN_PAGE), FakeLLM(), APPLICANT, None, placeholders=["email", "phone"])
    assert out["actions"] == [{"op": "click", "index": 7}]
    captcha = "[1]<button>Apply</button>\nPlease verify you are human\n[2]<checkbox label=\"I'm not a robot\" checked=false>"
    out = step(_req(captcha), FakeLLM(), APPLICANT, None, placeholders=["email"])
    assert out["done"] == {"outcome": "blocked", "reason": "captcha"}


# --- the routes ---------------------------------------------------------------------------------

@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    mod._memory().save("default", {"identity": {"name": "Kofi Gilbert", "email": "kofi@example.com",
                                                "phone": "555-100-2000"},
                                   "education": [{"school": "State University", "degree": "MS"}],
                                   "experience": [{"org": "X"}]}, {}, [])
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Analyst", "Acme", 70, "", data={"jd_text": "SQL."})
    recs.close()
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    return mod, mod.app.test_client(), rid, tmp_path


WD = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Analyst_R123"


def test_portal_account_create_stores_and_get_never_returns_the_password(A):
    mod, c, _, tmp = A
    g = c.get("/api/autoapply/agent/portal-account?host=acme.wd5.myworkdayjobs.com").get_json()
    assert g["exists"] is False and g["email"] == ""
    d = c.post("/api/autoapply/agent/portal-account",
               json={"host": "acme.wd5.myworkdayjobs.com", "create": True}).get_json()
    assert d["ok"] is True and d["email"] == "kofi@example.com" and d["created"] is True
    raw = (tmp / "credentials.env").read_text(encoding="utf-8")
    line = next(ln for ln in raw.splitlines() if ln.startswith("PORTAL_ACME_WD5_MYWORKDAYJOBS_COM="))
    email, _, pw = line.split("=", 1)[1].partition("|")
    assert email == "kofi@example.com" and len(pw) == 16
    assert sum(ch in "!@#$%&*?" for ch in pw) == 2 and sum(ch.isalnum() for ch in pw) == 14
    g = c.get("/api/autoapply/agent/portal-account?host=acme.wd5.myworkdayjobs.com").get_json()
    assert g == {"ok": True, "host": "acme.wd5.myworkdayjobs.com", "exists": True, "email": "kofi@example.com"}
    assert pw not in json.dumps(g)
    r = c.get("/api/autoapply/agent/portal-account?host=acme.wd5.myworkdayjobs.com&include_secret=1")
    assert r.status_code == 400 and pw not in r.get_data(as_text=True)
    again = c.post("/api/autoapply/agent/portal-account",
                   json={"host": "acme.wd5.myworkdayjobs.com", "create": True}).get_json()
    assert again["created"] is False and again["email"] == "kofi@example.com"   # one account per host
    assert mod._LOCK.custodies("PORTAL_ACME_WD5_MYWORKDAYJOBS_COM")            # App Lock takes it when on


def test_portal_account_creation_refused_when_the_pref_is_off(A):
    mod, c, _, tmp = A
    assert c.get("/api/autoapply/prefs").get_json()["portal_accounts"] is True       # default on
    c.post("/api/submit/settings", json={"portal_accounts": False})
    assert c.get("/api/submit/settings").get_json()["portal_accounts"] is False
    d = c.post("/api/autoapply/agent/portal-account", json={"host": "jobs.icims.com", "create": True}).get_json()
    assert d["ok"] is False and d["reason"] == "portal_accounts_off"
    assert not (tmp / "credentials.env").exists()
    d = c.post("/api/autoapply/agent/portal-account", json={"host": "www.linkedin.com", "create": True}).get_json()
    assert d["ok"] is False and d["reason"] == "fill-by-hand"


def test_allowed_hosts_include_the_apply_host_and_never_linkedin(A):
    _, c, _, _ = A
    hosts = c.get("/api/autoapply/agent/allowed-hosts?url=" + WD).get_json()["hosts"]
    assert "acme.wd5.myworkdayjobs.com" in hosts and "myworkdayjobs.com" in hosts and "icims.com" in hosts
    assert not any("linkedin" in h for h in hosts)
    assert c.get("/api/autoapply/agent/allowed-hosts?url=https://www.linkedin.com/jobs/1").get_json()["hosts"] == []
    assert run_allowed_hosts("https://careers.acme.co.uk/jobs/1") >= {"careers.acme.co.uk", "acme.co.uk"}


def test_step_route_refuses_linkedin_and_runs_the_fake_brain(A):
    mod, c, rid, _ = A
    d = c.post("/api/autoapply/agent/step", json={"record_id": rid, "step": 1, "max_steps": 40,
                                                  "url": "https://www.linkedin.com/jobs/view/1",
                                                  "view": JOB_PAGE}).get_json()
    assert d["ok"] is False and d["reason"] == "fill-by-hand"
    c.post("/api/autoapply/agent/portal-account", json={"host": "acme.wd5.myworkdayjobs.com", "create": True})
    d = c.post("/api/autoapply/agent/step", json={"record_id": rid, "step": 2, "max_steps": 40, "url": WD,
                                                  "view": SIGNIN_PAGE, "left": [{"ref": "f1", "label": "Veteran status"}],
                                                  "history": []}).get_json()
    assert d["ok"] is True and [a["op"] for a in d["actions"]] == ["type", "type", "click"]
    assert d["actions"][0]["text"] == "kofi@example.com" and d["actions"][0]["sensitive"] is True
    assert len(d["actions"][1]["text"]) == 16 and d["actions"][1]["sensitive"] is True
    assert {"email", "phone", "school", "portal_email", "portal_password"} <= set(d["placeholders"])
    assert "first_name" in d["placeholders"] and "linkedin" not in d["placeholders"]   # only what the profile has


# ---- milestone 3: the mailbox hand-over (docs/apply-agent.md section 4) ----------------------

def test_await_email_is_a_valid_outcome_with_a_normalised_reason():
    from autoapply.brain import parse_reply
    r = parse_reply('{"memory":"m","evaluation":"e","actions":[],"done":{"outcome":"await_email","reason":"code"}}', {3})
    assert r["done"] == {"outcome": "await_email", "reason": "code"}
    r = parse_reply('{"memory":"m","evaluation":"e","actions":[],"done":{"outcome":"await_email","reason":"click the LINK"}}', {3})
    assert r["done"]["reason"] == "link"


def test_the_emailed_code_is_a_placeholder_the_brain_types_once():
    from autoapply.brain import resolve_actions
    out = resolve_actions([{"op": "type", "index": 4, "text": "{{email_code}}"}], {"email_code": "482913"}, None,
                          {4: "Verification code"}, [])
    assert out == [{"op": "type", "index": 4, "text": "482913", "sensitive": True, "placeholder": "email_code"}]


def test_fake_brain_types_the_code_when_offered_else_waits_for_the_mail():
    from llm.base import FakeLLM
    from autoapply.brain import SYSTEM_PROMPT, build_user_message
    view = ("URL: https://x.test/apply\nTitle: Verify\n[Start of page]\nCheck your email\n"
            "[3]<input type=text label=\"Verification code\" value=\"\">\n[4]<button>Verify</button>\n[End of page]")
    llm = FakeLLM()
    req = {"view": view, "step": 2, "max_steps": 40}
    waiting = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message(req, ["email"], [])))
    assert waiting["done"] == {"outcome": "await_email", "reason": "code"}
    typing = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message(req, ["email", "email_code"], [])))
    assert typing["actions"][0] == {"op": "type", "index": 3, "text": "{{email_code}}"}
    assert typing["actions"][1] == {"op": "click", "index": 4}


def test_fake_brain_never_reads_a_zip_code_box_as_an_emailed_code():
    """e2e-agent.mjs section 12 (the ?address=1 contact page): a "ZIP code" box is an address
    field, not a verification code. The scripted brain once answered await_email for it, and the
    run sat on "Waiting for the site's email" forever."""
    from llm.base import FakeLLM
    from autoapply.brain import SYSTEM_PROMPT, build_user_message
    view = ("URL: http://127.0.0.1:57434/mock_ats.html?address=1\nTitle: Mock ATS\n[Start of page]\nMy Information\n"
            "[3]<input type=text label=\"First name\" value=\"(filled)\">\n[4]<input type=text label=\"City\" value=\"(filled)\">\n"
            "[5]<input type=text label=\"State\" value=\"(filled)\">\n"
            "[6]<input type=text label=\"ZIP code\" value=\"(filled)\" required>\n"
            "[7]<input type=text label=\"Postal code\" value=\"(filled)\">\n[8]<input type=text label=\"Promo code\" value=\"\">\n"
            "[9]<button>Save and Continue</button>\n[End of page]")
    llm = FakeLLM()
    r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": view, "step": 3}, ["email", "postal_code"], [])))
    assert r["done"] is None and r["actions"] == [{"op": "click", "index": 9}]
    for label in ("One-time passcode", "Security code", "Enter the code we sent"):
        v = view.replace('label=\"Promo code\"', f'label=\"{label}\"')
        r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": v, "step": 3}, ["email"], [])))
        assert r["done"] == {"outcome": "await_email", "reason": "code"}, label


def test_fake_brain_runs_the_forgot_password_path():
    from llm.base import FakeLLM
    from autoapply.brain import SYSTEM_PROMPT, build_user_message
    llm = FakeLLM()
    signin = ("URL: https://x.test/signin\nTitle: Sign in\n[Start of page]\n"
              "[3]<input type=email label=\"Email Address\" value=\"(filled)\">\n[4]<input type=password label=\"Password\" value=\"\">\n"
              "Error: \"We could not find an account with that email.\"\n[5]<button>Sign in</button>\n[6]<a>Forgot password?</a>\n[End of page]")
    r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": signin}, ["portal_email", "portal_password"], [])))
    assert r["actions"] == [{"op": "click", "index": 6}]
    reset = ("URL: https://x.test/reset\nTitle: Reset\n[Start of page]\nReset your password\n"
             "[3]<input type=email label=\"Email Address\" value=\"\">\n[4]<button>Send reset link</button>\n[End of page]")
    r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": reset}, ["portal_email", "portal_password"], [])))
    assert r["actions"] == [{"op": "type", "index": 3, "text": "{{portal_email}}"}, {"op": "click", "index": 4}]
    sent = "URL: https://x.test/reset\nTitle: Reset\n[Start of page]\nCheck your email\nWe emailed you a link to reset your password.\n[End of page]"
    r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": sent}, ["portal_email"], [])))
    assert r["done"] == {"outcome": "await_email", "reason": "link"}
    newpw = ("URL: https://x.test/reset?t=1\nTitle: New password\n[Start of page]\nChoose a new password\n"
             "[3]<input type=password label=\"New Password\" value=\"\">\n[4]<input type=password label=\"Confirm New Password\" value=\"\">\n"
             "[5]<button>Save password</button>\n[End of page]")
    r = json.loads(llm.agent_step(SYSTEM_PROMPT, build_user_message({"view": newpw}, ["portal_email", "portal_password"], [])))
    assert r["actions"] == [{"op": "type", "index": 3, "text": "{{portal_password}}"},
                            {"op": "type", "index": 4, "text": "{{portal_password}}"}, {"op": "click", "index": 5}]


def test_needs_answers_is_a_blocked_reason_and_phone_is_digits():
    from autoapply.brain import parse_reply
    from autoapply.fields import applicant_from_record, phone_digits
    r = parse_reply('{"memory":"m","evaluation":"e","actions":[],"done":{"outcome":"blocked","reason":"needs-answers"}}', {3})
    assert r["done"] == {"outcome": "blocked", "reason": "needs-answers"}
    assert phone_digits("+1 (630) 205-1184") == "6302051184"          # Workday refused the formatted form
    assert applicant_from_record({"profile": {"identity": {"name": "K", "phone": "+1 (630) 205-1184"}}})["phone"] == "6302051184"


# ---- ask the person (docs/apply-agent.md): the bar or Telegram answers, the library remembers ----

def test_ask_route_returns_the_bars_answer_and_saves_it(A):
    import threading, time as _t
    A, _c, _rid, _tmp = A
    c = A.app.test_client()
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"question": "How many years of experience do you have?",
                                                          "options": [], "timeout_s": 10},
                          headers={"X-Tailor-Extension": "1"}).get_json()
    th = threading.Thread(target=ask); th.start()
    _t.sleep(1.2)
    r = c.post("/api/autoapply/agent/answer", json={"question": "How many years of experience do you have?", "answer": "5"},
               headers={"X-Tailor-Extension": "1"})
    assert r.status_code == 200, r.get_json()
    th.join(15)
    assert out["r"]["ok"] and out["r"]["answer"] == "5"
    lib = A._answer_lib()
    try:
        assert lib.lookup("How many years of experience do you have?")["answer"] == "5"
    finally:
        lib.close()


def test_ask_route_times_out_without_an_answer(A):
    A, _c, _rid, _tmp = A
    c = A.app.test_client()
    r = c.post("/api/autoapply/agent/ask", json={"question": "Favourite colour?", "timeout_s": 5},
               headers={"X-Tailor-Extension": "1"}).get_json()
    assert r == {"ok": False, "id": r["id"], "reason": "timeout"}


def test_telegram_button_and_text_answer_an_open_question(A):
    A, _c, _rid, _tmp = A
    with A._ASKS_LOCK:
        A._ASKS.clear()
        A._ASKS["ab12"] = {"id": "ab12", "question": "How did you hear about us?", "options": ["Job board", "Referral"],
                           "answer": None, "at": __import__("time").time()}
    assert A._agent_ask_event("button", "ask:ab12:1") == "Got it: Referral."
    assert A._ASKS["ab12"]["answer"] == "Referral"
    assert A._agent_ask_event("button", "ask:ab12:0") == "That question was already answered."
    with A._ASKS_LOCK:
        A._ASKS["cd34"] = {"id": "cd34", "question": "Years of experience?", "options": [], "answer": None,
                           "at": __import__("time").time()}
    assert A._agent_ask_event("text", "about 5") == "Got it: about 5."
    assert A._agent_ask_event("text", "hello") is None           # nothing open: the usual routing takes it
    assert A._agent_ask_event("button", "job:queue:x") is None


def test_known_answers_reach_the_brain_as_placeholders(A):
    A, _c, _rid, _tmp = A
    lib = A._answer_lib()
    try:
        lib.save("How did you hear about us?", "Job board", "you")
    finally:
        lib.close()
    c = A.app.test_client()
    view = ("URL: https://acme.test/apply\nTitle: Apply\n[Start of page]\n"
            "[3]<combobox label=\"How Did You Hear About Us?\" value=\"\" expanded=false>\n"
            "[4]<button>Save and Continue</button>\n[End of page]")
    r = c.post("/api/autoapply/agent/step", json={"record_id": None, "step": 2, "max_steps": 40, "url": "https://acme.test/apply",
                                                  "view": view, "left": ["How Did You Hear About Us?"], "history": []},
               headers={"X-Tailor-Extension": "1"}).get_json()
    assert "answer_1" in r["placeholders"], r
    typed = [a for a in r["actions"] if a.get("op") == "type"]
    assert typed and typed[0]["text"] == "Job board" and typed[0]["placeholder"] == "answer_1", r


def test_an_answer_given_on_the_bar_answers_the_same_question_next_time(A):
    A, _c, _rid, _tmp = A
    lib = A._answer_lib()
    try:
        lib.save("Are you legally authorized to work in the United States?", "Yes", "you")
    finally:
        lib.close()
    known = A._known_answers_for(["Are you legally authorized to work in the United States?", "Favourite colour?"],
                                 None, {}, {"first_name": "K"})
    assert known == {"Are you legally authorized to work in the United States?": "Yes"}


def test_the_work_auth_note_is_one_honest_sentence_from_the_profile():
    from autoapply import workauth as w
    wa = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes"}}}
    assert w.note(wa) == "I am currently on F-1 OPT and authorized to work without sponsorship at this time. I will need sponsorship in the future."
    assert w.note({"countries": {}}) == ""
    assert w.note({"countries": {"US": {"status": "us_citizen", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "No"}}}) \
        == "I am a u.s. citizen and authorized to work without sponsorship at this time. I will not need sponsorship."
