"""The apply agent's engineering mindset (docs/apply-agent.md sections 12 to 16), from the real
batch of 2026-11-02: Barclays, Eliassen and Nelnet.

* the brain's read-only tools (read_page, find, inspect, list_options, read_errors, screenshot,
  wait_for) and page tools (back, reload, reopen_job) parse, the page-changing one goes last,
  and what they found comes back as <observations>;
* a bad or empty reply is retried three times with backoff before anything else; a model call
  that fails for credits or a key is "fatal" (the job and the batch stop plainly, never "needs
  you"), a passing one is retried, and the real error text comes back;
* the deterministic ready gate: never ready_to_submit on a sign-in page (Gartner);
* the plan, the portal block (a just-created account is created, not signed into), the
  reflection block and the critic;
* the cost estimate per call;
* the FakeLLM scripted brain on the new pages: a skeleton (wait_for), Workday's "an email has
  been sent" over the sign-in form (await the link), the activated page (sign in), the job
  search page after the link (reopen_job), the create-account form;
* the routes: /step with no model says fatal; /critic; /unavailable notes the row;
* the shell's loop, under node with a fake pane (tests/js/test_agent_engineer.js): capped pane
  calls and Stop that preempts, the hang ladder, the wait cap, escalation on bad replies, the
  account-problem stop, the tools as observations, the page check, the ready gate and the
  critic round, the mail that never arrives (park, then one more look on resume), the batch's
  time cap, and a debug log without secrets.
"""
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from autoapply import brain
from autoapply.brain import (STRATEGIES, SYSTEM_PROMPT, build_user_message, classify_error, clean_plan, critic,
                             estimate_cost, not_ready, parse_reply, step, unavailable_message)
from llm.base import FakeLLM

ROOT = Path(__file__).resolve().parents[1]
APPLICANT = {"first_name": "Kofi", "email": "kofi@example.com", "phone": "555-100-2000"}
PORTAL = {"host": "acme.wd5.myworkdayjobs.com", "email": "kofi@example.com", "password": "Zq8!kL2m9Xp#4Rt7"}
PH = ["first_name", "email", "phone", "portal_email", "portal_password"]

SIGNIN = """URL: https://acme.wd5.myworkdayjobs.com/login
Title: Sign In
Sign In
[3]<input type=email label="Email Address" value="" required>
[4]<input type=password label="Password" value="">
[6]<button>Sign In</button>
[7]<button>Create Account</button>
[End of page]"""
CREATE = """URL: https://acme.wd5.myworkdayjobs.com/login
Create Account
[3]<input type=email label="Email Address" value="">
[4]<input type=password label="Create Password" value="">
[5]<input type=password label="Verify New Password" value="">
[6]<button>Create Account</button>
[End of page]"""
MAILED = """URL: https://acme.wd5.myworkdayjobs.com/login
Error: "An email has been sent to you. Please verify your account."
[3]<input type=email label="Email Address" value="">
[4]<input type=password label="Password" value="">
[6]<button>Sign In</button>
[End of page]"""
ACTIVATED = """URL: https://acme.wd5.myworkdayjobs.com/activate/tok
Your account has been activated
Thank you for verifying your email address.
[2]<button>Sign In</button>
[End of page]"""
SEARCH = """URL: https://acme.wd5.myworkdayjobs.com/en-US/External
Search for Jobs
[1]<input type=search label="Search for jobs" value="" search=true>
[2]<button>Search</button>
[End of page]"""
SKELETON = """URL: https://acme.wd5.myworkdayjobs.com/apply/applyManually
Title: Careers
[1]<a>Skip to main content</a>
Loading
[End of page]"""
REVIEW = """URL: https://acme.wd5.myworkdayjobs.com/apply/review
Review
[20]<button>Submit</button>
[End of page]"""


def _req(view, n=1, **kw):
    return {"step": n, "max_steps": 60, "url": "https://acme.wd5.myworkdayjobs.com/x", "lane": "assisted",
            "view": view, "history": [], "job": {"role": "Analyst", "company": "Acme"}, **kw}


class _Scripted:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def agent_step(self, system, user):
        self.calls.append(user)
        r = self.replies.pop(0) if self.replies else ""
        if isinstance(r, Exception):
            raise r
        return r


class _Fails(Exception):
    def __init__(self, msg, reason=""):
        super().__init__(msg)
        self.reason = reason


# --- the tools and the reply shape --------------------------------------------------------------

def test_parse_reply_takes_the_tools_and_puts_the_page_tool_last():
    raw = json.dumps({"plan": ["done: Job page", "now: Sign in", "bogus", "todo: Review"],
                      "actions": [{"op": "read_page", "offset": "40"}, {"op": "find", "text": "  Next  "},
                                  {"op": "inspect", "index": 3}, {"op": "list_options", "index": 99},
                                  {"op": "read_errors"}, {"op": "screenshot"},
                                  {"op": "wait_for", "text": "My Information", "selector": "form input", "seconds": 99},
                                  {"op": "reload"}, {"op": "click", "index": 6}]})
    out = parse_reply(raw, {3, 6})
    ops = [a["op"] for a in out["actions"]]
    assert ops == ["read_page", "find", "inspect", "read_errors", "screenshot"]      # five at most; [99] dropped
    assert out["actions"][0]["offset"] == 40 and out["actions"][1]["text"] == "Next"
    assert out["plan"] == ["done: Job page", "now: Sign in", "todo: Review"]
    out = parse_reply(json.dumps({"actions": [{"op": "wait_for", "selector": "form input", "seconds": 99}, {"op": "reload"}, {"op": "click", "index": 6}]}), {6})
    assert [a["op"] for a in out["actions"]] == ["wait_for", "reload"] and out["actions"][0]["seconds"] == 30
    bad = parse_reply(json.dumps({"actions": [{"op": "wait_for", "selector": "a[onclick=javascript:x()]{}"}]}), {6})
    assert bad is None                                        # an unsafe selector and no text: nothing usable
    assert parse_reply(json.dumps({"actions": [{"op": "find", "text": ""}]}), set()) is None
    assert clean_plan(["NOW: Sign in", "x"]) == ["now: Sign in"]


def test_observations_plan_and_portal_reach_the_message_and_values_are_redacted():
    req = _req(SIGNIN, plan=["done: Job page", "now: Sign in"],
               observations=["read_page from [5]:\nContact: kofi@example.com", ""], portal={"account": True, "new": True})
    msg = build_user_message(req, PH, [], values={"email": "kofi@example.com"})
    assert "<plan>\ndone: Job page\nnow: Sign in\n</plan>" in msg
    assert "<observations>\nread_page from [5]:\nContact: {{email}}\n</observations>" in msg
    assert "created just now" in msg and msg.rstrip().endswith("</step_info>")
    msg = build_user_message(_req(SIGNIN, portal={"account": True, "new": False}), PH, [])
    assert "existing: sign in with it" in msg and "<plan>\n(none yet: write one)\n</plan>" in msg
    msg = build_user_message(_req(SIGNIN, reflect={"problem": "the page did not change", "tried": ["a hint to the AI"]}), PH, [])
    assert "<reflection>" in msg and "a hint to the AI" in msg and all(s in msg for s in STRATEGIES)
    big = build_user_message(_req(SIGNIN, observations=["x" * 9000, "y" * 9000]), PH, [])
    assert "(more observations cut)" in big and len(big) < 20000


def test_the_prompt_tells_the_brain_to_investigate_not_to_give_up():
    assert "If the same action failed twice, do something different, or report blocked" not in SYSTEM_PROMPT
    assert "read_page" in SYSTEM_PROMPT and "reopen_job" in SYSTEM_PROMPT and "wait_for" in SYSTEM_PROMPT
    assert "Never answer with a plain wait more than twice" in SYSTEM_PROMPT
    assert "<portal>" in SYSTEM_PROMPT and "<plan>" in SYSTEM_PROMPT
    assert "—" not in SYSTEM_PROMPT and "–" not in SYSTEM_PROMPT


def test_reflection_reply_carries_causes_and_a_strategy_from_the_menu():
    raw = json.dumps({"causes": ["not rendered", "slow"], "likeliest": "not rendered", "strategy": "reload", "actions": [], "done": None})
    out = parse_reply(raw, set())
    assert out["strategy"] == "reload" and out["causes"] == ["not rendered", "slow"] and out["actions"] == []
    out = parse_reply(json.dumps({"strategy": "hack the site", "actions": [{"op": "read_errors"}]}), set())
    assert out["strategy"] == "" and out["actions"] == [{"op": "read_errors"}]
    llm = _Scripted(raw)
    r = step(_req(SKELETON, reflect={"problem": "stuck"}, image="aGk="), llm, APPLICANT, None, placeholders=PH, sleep=lambda s: None)
    assert r["ok"] and r["strategy"] == "reload" and r["likeliest"] == "not rendered" and r["actions"] == []


# --- retries, the error classes, the ready gate -------------------------------------------------

def test_a_transient_model_error_is_retried_and_the_text_comes_back():
    llm = _Scripted(_Fails("HTTP 529 overloaded"), json.dumps({"actions": [{"op": "click", "index": 6}]}))
    out = step(_req(SIGNIN), llm, APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["ok"] and out["tries"] == 2 and out["actions"][0]["index"] == 6
    llm = _Scripted(_Fails("timed out"), _Fails("timed out"), _Fails("connection reset"))
    out = step(_req(SIGNIN), llm, APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["ok"] is False and out["reason"] == "no-reply" and "fatal" not in out
    assert out["error"].startswith("offline: connection reset") and len(out["errors"]) == 3


def test_an_account_problem_is_fatal_at_once_never_needs_you():
    llm = _Scripted(_Fails("out of usage credits", reason="no_credit"), json.dumps({"actions": [{"op": "click", "index": 6}]}))
    out = step(_req(SIGNIN), llm, APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["ok"] is False and out["fatal"] == "no_credit" and out["tries"] == 1    # no retry on credits
    assert "credits" in out["message"] and "Nothing was changed" in out["message"]
    assert "needs you" not in out["message"].lower() and "no_credit: out of usage credits" == out["error"]
    assert classify_error(_Fails("x", reason="upgrade_required")) == "upgrade_required"
    # the broker's own tags (PR #166): a company outage is fatal at once, a transient one is retried
    out = step(_req(SIGNIN), _Scripted(_Fails("ours", reason="ai_company")), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["fatal"] == "ai_company" and out["tries"] == 1 and "We have been alerted" in out["message"] and "needs you" not in out["message"]
    out = step(_req(SIGNIN), _Scripted(_Fails("busy", reason="ai_transient"), _Fails("busy", reason="ai_transient"),
                                       json.dumps({"actions": [{"op": "click", "index": 6}]})), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["ok"] and out["tries"] == 3
    assert classify_error(RuntimeError("invalid x-api-key")) == "no_key"
    assert classify_error(RuntimeError("Your Claude API access is turned off: out of usage credits")) == "no_credit"
    assert classify_error(RuntimeError("rate limit hit")) == "rate_limited" and classify_error(RuntimeError("overloaded")) == "busy"
    assert "credits" in unavailable_message("upgrade_required") and "key" in unavailable_message("no_key")


def test_the_ready_gate_refuses_ready_on_a_sign_in_page():
    assert not_ready(SIGNIN) == "the page has a password box"
    assert not_ready("[3]<input type=email label=\"Email\" value=\"\">\n[6]<button>Sign In</button>") == "the page is a sign-in or create-account form"
    assert not_ready(SEARCH) == "the page shows no Submit control"
    assert not_ready(REVIEW) == "" and not_ready(REVIEW.replace("Submit", "Review and Submit")) == ""
    llm = _Scripted(json.dumps({"done": {"outcome": "ready_to_submit", "reason": "at the submit button"}}))
    out = step(_req(SIGNIN), llm, APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["ok"] and out["done"] is None and out["actions"] == [] and out["refused"] == "the page has a password box"
    llm = _Scripted(json.dumps({"done": {"outcome": "ready_to_submit", "reason": "x"}}))
    out = step(_req(REVIEW), llm, APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["done"] == {"outcome": "ready_to_submit", "reason": "x"}


# --- the critic and the cost --------------------------------------------------------------------

def test_the_critic_lists_what_is_blank_and_the_fake_one_is_deterministic():
    view = REVIEW.replace("[20]", "Error: \"The field Country is required\"\n[9]<select label=\"Country\" value=\"\" options=3 first=\"a|b|c\" required>\n[20]")
    out = critic({"view": view, "plan": ["todo: Review"], "left": ["Country"]}, FakeLLM())
    assert out["ok"] is False and any("Country" in i for i in out["issues"]) and out["cost"] > 0
    assert critic({"view": REVIEW, "plan": [], "left": []}, FakeLLM())["ok"] is True

    class _Bad:
        def agent_critic(self, s, u):
            raise RuntimeError("down")
    assert critic({"view": REVIEW}, _Bad()) == {"ok": True, "issues": [], "cost": 0.0, "skipped": True}


def test_cost_from_usage_or_from_characters_by_model_family():
    assert estimate_cost({"input": 7000, "output": 300}, 0, 0, "claude-sonnet-4-6") == pytest.approx(0.0255, abs=1e-4)
    assert estimate_cost(None, 28000, 1200, "claude-sonnet-4-6") == pytest.approx(0.0255, abs=1e-4)
    assert estimate_cost({"input": 7000, "output": 300}, 0, 0, "claude-haiku-5-5") < 0.001
    assert estimate_cost({"input": 7000, "output": 300}, 0, 0, "claude-sonnet-5-5") == pytest.approx(0.017, abs=1e-4)
    assert estimate_cost({"input": 1, "output": 1}, 0, 0, "") > 0


# --- the FakeLLM on the new pages ---------------------------------------------------------------

def test_fake_brain_waits_for_the_form_on_a_skeleton_then_the_shell_escalates():
    out = step(_req(SKELETON), FakeLLM(), APPLICANT, None, placeholders=PH, sleep=lambda s: None)
    assert out["actions"][0]["op"] == "wait_for" and out["actions"][0]["seconds"] == 3 and out["done"] is None


def test_fake_brain_runs_workdays_activation_end_to_end():
    # "An email has been sent to you" over the sign-in form: the mail first
    out = step(_req(MAILED), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["done"] == {"outcome": "await_email", "reason": "link"}
    # the activate page the link opens: Sign In
    out = step(_req(ACTIVATED), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [{"op": "click", "index": 2}]
    # the same sign-in form with the stale banner, once the link was opened: sign in
    hist = [{"step": 3, "evaluation": "", "actions": ["await email"], "results": ["opened the emailed link"]}]
    out = step(_req(MAILED, 4, history=hist), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert [a["op"] for a in out["actions"]] == ["type", "type", "click"] and out["actions"][2]["index"] == 6
    # the job search page the sign-in lands on: open the job again
    out = step(_req(SEARCH, 5, history=hist), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [{"op": "reopen_job"}]


def test_fake_brain_never_reopens_a_form_page_under_a_header_search_box():
    # The e2e of 2026-11-02: every mock page has the site's search box in its header, and the
    # scripted brain reopened the job from the questions page after a reset link, round and round.
    hist = [{"step": 3, "evaluation": "", "actions": ["await email"], "results": ["opened the emailed link"]}]
    questions = ("URL: https://acme.example/apply?reset=tok1\nAcme Careers\n[1]<input type=search label=\"Search jobs\" value=\"\" search=true>\n"
                 "[2]<button>Search</button>\nApplication Questions\n[6]<input type=text label=\"Years\" value=\"\">\n[8]<button>Save and Continue</button>")
    out = step(_req(questions, 5, history=hist), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["actions"] != [{"op": "reopen_job"}]
    header_activated = ACTIVATED.replace("[2]<button>Sign In</button>", "[1]<input type=search label=\"Search jobs\" value=\"\" search=true>\n[2]<button>Sign In</button>")
    out = step(_req(header_activated, 4, history=hist), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [{"op": "click", "index": 2}]


def test_fake_brain_waits_for_the_mail_when_the_account_is_not_verified():
    unverified = SIGNIN.replace("Sign In\n[3]", "Sign In\nError: \"Your account is not verified yet. Check your email.\"\n[3]")
    out = step(_req(unverified), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["done"] == {"outcome": "await_email", "reason": "link"}


def test_the_reflection_menu_has_the_account_strategies():
    for name in ("create account", "reset password", "wait for the site's email"):
        assert name in STRATEGIES
        out = parse_reply(json.dumps({"strategy": name, "actions": []}), set())
        assert out["strategy"] == name


def test_fake_brain_creates_a_just_created_account_instead_of_signing_in():
    out = step(_req(SIGNIN, portal={"account": True, "new": True}), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [{"op": "click", "index": 7}]
    out = step(_req(CREATE, portal={"account": True, "new": True}), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert [a["op"] for a in out["actions"]] == ["type", "type", "type", "click"]
    assert out["actions"][0]["text"] == PORTAL["email"] and out["actions"][1]["text"] == PORTAL["password"] and out["actions"][3]["index"] == 6
    out = step(_req(SIGNIN, portal={"account": True, "new": False}), FakeLLM(), APPLICANT, PORTAL, placeholders=PH, sleep=lambda s: None)
    assert [a["op"] for a in out["actions"]] == ["type", "type", "click"]


# --- the routes ---------------------------------------------------------------------------------

@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    mod._memory().save("default", {"identity": {"name": "Kofi Gilbert", "email": "kofi@example.com", "phone": "555-100-2000"},
                                   "experience": [{"org": "X"}]}, {}, [])
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Analyst", "Acme", 70, "", data={"jd_text": "SQL.", "status": "approved"})
    recs.close()
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    return mod, mod.app.test_client(), rid


H = {"X-Tailor-Extension": "1"}


def test_step_route_carries_plan_observations_and_cost_and_says_fatal_without_a_model(A, monkeypatch):
    mod, c, rid = A
    r = c.post("/api/autoapply/agent/step", headers=H, json={
        "record_id": rid, "step": 2, "max_steps": 60, "url": "https://acme.wd5.myworkdayjobs.com/x", "view": SKELETON,
        "plan": ["now: Sign in"], "observations": ["errors on the page: none shown"], "nudges": ["x"]}).get_json()
    assert r["ok"] and r["actions"][0]["op"] == "wait_for" and r["cost"] > 0 and r["tries"] == 1
    monkeypatch.setattr(mod, "_make_llm", lambda: (_ for _ in ()).throw(RuntimeError("No ANTHROPIC_API_KEY found")))
    r = c.post("/api/autoapply/agent/step", headers=H, json={"record_id": rid, "url": "https://acme.wd5.myworkdayjobs.com/x", "view": SKELETON}).get_json()
    assert r["ok"] is False and r["fatal"] == "no_key" and "Nothing was changed" in r["message"] and "ANTHROPIC_API_KEY" in r["error"]


def test_critic_and_unavailable_routes(A):
    mod, c, rid = A
    view = REVIEW.replace("[20]", "[9]<input label=\"ZIP code\" value=\"\" required>\n[20]")
    r = c.post("/api/autoapply/agent/critic", headers=H, json={"record_id": rid, "view": view, "plan": [], "left": ["ZIP code"]}).get_json()
    assert r["ok"] is False and any("ZIP code" in i for i in r["issues"])
    r = c.post("/api/autoapply/agent/critic", headers=H, json={"record_id": rid, "view": ""}).get_json()
    assert r["ok"] is True and r.get("skipped")
    r = c.post("/api/autoapply/agent/unavailable", headers=H, json={"record_id": rid, "message": "SponsorJobs' AI is unavailable right now (credits). Nothing was changed; try again later.", "batch": True}).get_json()
    assert r["ok"] is True
    row = next(it for t in c.get("/api/autoapply/board", headers=H).get_json()["tiles"].values() for it in t if it["id"] == rid)
    assert "credits" in row["agent_note"] and row["tile"] != "needs" and "needs you" not in (row.get("stage_detail") or "").lower()
    c.post("/api/autoapply/agent/unavailable", headers=H, json={"record_id": rid, "clear": True})
    row = next(it for t in c.get("/api/autoapply/board", headers=H).get_json()["tiles"].values() for it in t if it["id"] == rid)
    assert row["agent_note"] == ""


# --- the shell's loop ---------------------------------------------------------------------------

def test_shell_loop_under_node():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_engineer.js")], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().endswith("ok")


# --- the owner's real pages of 2026-11-02 (tests/fixtures/forms/real/) ---------------------------

REAL = ROOT / "tests" / "fixtures" / "forms" / "real"


def test_the_brain_drops_a_click_on_linkedin_from_the_real_eliassen_options():
    view = (REAL / "eliassen_apply_options.txt").read_text(encoding="utf-8")
    li = next(i for i, ln in brain.view_lines(view).items() if ln["text"] == "Apply with LinkedIn")
    em = next(i for i, ln in brain.view_lines(view).items() if ln["text"] == "Apply with Email")
    out = step(_req(view), _Scripted(json.dumps({"actions": [{"op": "click", "index": li}]})), APPLICANT, None,
               placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [] and any("LinkedIn" in n for n in out["notes"])
    out = step(_req(view), _Scripted(json.dumps({"actions": [{"op": "click", "index": em}]})), APPLICANT, None,
               placeholders=PH, sleep=lambda s: None)
    assert out["actions"] == [{"op": "click", "index": em}]
    assert "Never LinkedIn" in SYSTEM_PROMPT and "chat-style apply" in SYSTEM_PROMPT


def test_closed_route_takes_the_job_off_waiting_and_out_of_a_batch(A):
    mod, c, rid = A
    r = c.post("/api/autoapply/agent/closed", headers=H, json={"record_id": rid, "url": "https://eeho.fa.us2.oraclecloud.com/x",
                                                               "phrase": "This job is no longer available."}).get_json()
    assert r["ok"] is True
    row = next(it for t in c.get("/api/autoapply/board", headers=H).get_json()["tiles"].values() for it in t if it["id"] == rid)
    assert row["tile"] == "needs" and row["reason"] == "posting_closed" and row["action"] == "view"   # never "fill": no batch picks it
    data = mod._record_data(mod._get_record(rid))
    assert data["job_closed"]["phrase"] == "This job is no longer available." and data["submission"]["message"].startswith("Job closed")
    assert c.post("/api/autoapply/agent/closed", headers=H, json={"record_id": 999999}).get_json()["ok"] is False


def test_real_pages_under_node():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_real_pages.js")], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip().endswith("ok")
