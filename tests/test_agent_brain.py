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
