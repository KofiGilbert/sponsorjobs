"""Fill-through (2026-11-17, the owner: "ours must never stop to ask questions" mid-application;
docs/apply-agent.md section 24).

A required question nothing answers no longer parks the run. The run fills everything else; a
required choice with a neutral option ("Prefer not to say") takes it, flagged, unless it asks
about a fact; the rest waits for the person, and is asked ONCE, as one list on the bar and one
Telegram message, at Ready to send (or on the one page that will not move on without it).

* the engine's parts: which question takes a neutral choice, what counts as placeholder text,
  the known-only check's neutral picks, the ONE batch message (no self-identification value),
  the step route's mark on what is left for the person;
* the shell's loop under node with a fake pane and brain (tests/js/test_agent_fill_through.js).
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply import brain

# The apply agent is off by default (assisted apply only); these tests cover it, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
NEUTRAL_Q = "Do you consider yourself a member of an underrepresented group?"
TEXT_Q = "Describe a project you are proud of"
LEGAL_Q = "Are you legally authorized to work in the United States?"
YNP = ["Yes", "No", "Prefer not to say"]


# --------------------------------------------------------------------------- pure parts
@pytest.mark.parametrize("q", [
    NEUTRAL_Q, "Would you like to tell us more about your interests?", "Do you identify as a caregiver?",
])
def test_a_non_fact_choice_takes_its_neutral_option(q):
    assert AK.neutral_option(q, ["Select...", "Yes", "No", "Prefer not to say"]) == "Prefer not to say"
    assert AK.neutral_option(q, ["Yes", "No", "I do not wish to answer"]) == "I do not wish to answer"


@pytest.mark.parametrize("q", [
    LEGAL_Q, "Will you now or in the future require visa sponsorship?", "What is your highest degree?",
    "Are you bound by a non-compete agreement?", "Do you hold an active security clearance?",
    "What was your salary at your last job?", "Have you ever been convicted of a felony?",
    "Have you worked for Acme before?", "Are you at least 18 years of age?",
])
def test_a_fact_question_never_takes_a_neutral_option(q):
    assert AK.is_fact_question(q)
    assert AK.neutral_option(q, YNP) is None


def test_no_neutral_option_or_two_of_them_means_none():
    assert AK.neutral_option(NEUTRAL_Q, ["Yes", "No"]) is None
    assert AK.neutral_option(NEUTRAL_Q, ["Prefer not to say", "Decline to answer", "Yes"]) is None


@pytest.mark.parametrize("t", ["N/A", "n/a", "NA", "Not provided", "Not applicable", "In", "TBD", "-", "None", ""])
def test_placeholder_text_is_filler(t):
    assert AK.is_filler(t)


@pytest.mark.parametrize("t", ["Indeed", "No", "Chicago, IL", "I led our billing migration", "Internal referral"])
def test_real_answers_are_not_filler(t):
    assert not AK.is_filler(t)


def test_the_batch_message_leads_with_what_was_filled_and_never_a_value():
    items = [{"question": TEXT_Q, "options": [], "all_options": []},
             {"question": LEGAL_Q, "options": ["Yes", "No"], "all_options": ["Yes", "No"]}]
    lead = AK.batch_lead(2, "Analyst", "Acme")
    text, buttons = AK.telegram_ask("b1", items, lead=lead)
    assert text.startswith("I filled everything I could on your application for Analyst at Acme; these 2 need you.\n")
    assert "1. " + TEXT_Q in text and "2. " + LEGAL_Q in text and buttons is None
    one, rows = AK.telegram_ask("b2", items[1:], lead=AK.batch_lead(1))
    assert one.startswith("I filled everything I could on your application; this one needs you.\n") and rows


def test_the_prompt_tells_the_brain_to_leave_the_marked_lines_and_carry_on():
    assert brain.DEFERRED_MARK in brain.SYSTEM_PROMPT
    assert "never type a stand-in such as N/A" in brain.SYSTEM_PROMPT


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
    return mod, sent


def _qs():
    return [{"question": NEUTRAL_Q, "options": YNP}, {"question": TEXT_Q, "options": []},
            {"question": LEGAL_Q, "options": ["Yes", "No"]}]


def test_known_only_fills_through_with_the_neutral_choice_only_when_asked(A):
    mod, sent = A
    c = mod.app.test_client()
    r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "fill_through": True, "questions": _qs()}, headers=H).get_json()
    assert r["answers"] == {NEUTRAL_Q: "Prefer not to say"} and r["origins"] == {NEUTRAL_Q: "neutral"}
    assert r["select"] == {NEUTRAL_Q: "Prefer not to say"}                 # the shell picks it with real clicks
    assert sorted(r["open"]) == sorted([TEXT_Q, LEGAL_Q]) and sent == []
    # never saved: the next check without fill-through has nothing for it
    r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": _qs()}, headers=H).get_json()
    assert r["answers"] == {} and len(r["open"]) == 3
    # a neutral pick that did not stick is the person's now
    r = c.post("/api/autoapply/agent/ask", json={"known_only": True, "fill_through": True, "questions": _qs(),
                                                  "force_questions": [NEUTRAL_Q]}, headers=H).get_json()
    assert NEUTRAL_Q in r["open"]


def test_the_batch_goes_out_once_with_every_question_and_no_self_identification_value(A):
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.save("Gender", "Male", "you")
    finally:
        lib.close()
    qs = [{"question": TEXT_Q, "options": []}, {"question": LEGAL_Q, "options": ["Yes", "No"]}] + \
         [{"question": f"Question {k}", "options": []} for k in range(5)]
    out = {}

    def run():
        out["r"] = mod.app.test_client().post("/api/autoapply/agent/ask", headers=H, json={
            "id": "fb1", "timeout_s": 15, "batch": True, "questions": qs}).get_json()
    th = threading.Thread(target=run)
    th.start()
    for _ in range(50):
        if sent:
            break
        time.sleep(0.1)
    assert len(sent) == 1, sent                                         # ONE message
    text, buttons = sent[0]
    assert text.startswith("I filled everything I could on your application; these 7 need you.")
    assert "7. Question 4" in text and "Male" not in text and buttons is None   # past the 6 of a page
    with mod._ASKS_LOCK:
        view = mod._ask_view(mod._ASKS["fb1"])
    assert view["batch"] is True and len(view["questions"]) == 7
    reply = "\n".join(["1. I rebuilt our billing pipeline", "2. Yes"] + [f"{k}. answer {k}" for k in range(3, 8)])
    assert mod._agent_ask_event("text", reply).startswith("Got it")
    th.join(20)
    assert out["r"]["ok"] and len(out["r"]["answers"]) == 7 and out["r"]["answers"][LEGAL_Q] == "Yes", out
    assert len(sent) == 1


def test_the_step_route_marks_what_is_left_for_the_person(A, monkeypatch):
    mod, sent = A
    got = {}

    def fake_step(req, llm, applicant, portal=None, **kw):
        got["left"] = list(req["left"])
        return {"ok": True, "actions": [], "done": None, "memory": "", "evaluation": "", "notes": [], "cost": 0.0}
    monkeypatch.setattr(brain, "step", fake_step)
    view = ("URL: https://acme.test/apply\n[3]<textarea label=\"" + TEXT_Q + "\" value=\"\" required>\n"
            "[4]<input label=\"Postal code\" value=\"\" required>\n[5]<button>Next</button>")
    mod.app.test_client().post("/api/autoapply/agent/step", headers=H, json={
        "record_id": None, "step": 2, "max_steps": 40, "url": "https://acme.test/apply", "view": view,
        "left": [TEXT_Q, "Postal code"], "history": [], "deferred": [TEXT_Q]})
    assert got["left"][0] == TEXT_Q + " " + brain.DEFERRED_MARK
    assert got["left"][1] == "Postal code"


# --------------------------------------------------------------------------- the shell's loop
def test_the_shell_fills_through_and_asks_once_at_the_end():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_fill_through.js")],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
