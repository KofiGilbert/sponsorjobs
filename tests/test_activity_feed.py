"""The live activity feed (ui/activity.py, autoapply/brain.py summary, shell-electron/agent.js).

One event shape for both agents: {phase, verb, target, summary, state, at}. Pins:
* ui/activity: a no-op without a run, a new phase closes the last one as done (a retry stays a
  retry), the same phase updates in place, nested builds join one run, runs are kept per key;
* the resume builder emits its phases in order through a real build (FakeLLM, real LaTeX);
* the brain's ``summary`` is shown on screen, so it never carries a personal value, a
  self-identification answer or a chosen option, and falls back to one built from the actions;
* /api/activity returns the builder's run, never another key;
* the shell's feed (tests/js/test_agent_activity.js, run under node).
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import brain
from llm.base import FakeLLM
from ui import activity as A

ROOT = Path(__file__).resolve().parents[1]
DASH = re.compile(r"[–—]")


@pytest.fixture(autouse=True)
def _fresh():
    A._reset()
    yield
    A._reset()


# --------------------------------------------------------------------------- ui/activity
def test_step_is_a_no_op_without_a_run():
    assert A.step("fit", "Fitting the page") is None
    assert A.current() is None and A.snapshot("builder") is None


def test_phases_close_as_done_retries_stay_and_the_same_phase_updates_in_place():
    with A.run("resume", key="builder", target="Data Analyst at Acme") as r:
        A.step("read", "Reading the job")
        A.step("fit", "Fitting the page", "so every line stays inside the margins")
        A.step("fit", "Shortening", "the longest bullets, it ran over one page", state="retry")
        A.step("fit2", "Fitting the page", detail="2 compiles")
        A.step("fit2", "Fitting the page", detail="3 compiles")
        mid = A.snapshot("builder")
        assert mid["ended"] is None and mid["events"][-1]["state"] == "running"
        A.step("check", "Checking every line", "and the job's terms")
    snap = A.snapshot("builder")
    assert snap["id"] == r["id"] and snap["kind"] == "resume" and snap["ok"] is True and snap["ended"]
    evs = snap["events"]
    assert [e["phase"] for e in evs] == ["read", "fit", "fit2", "check"]
    assert [e["state"] for e in evs] == ["done", "retry", "done", "done"]
    assert evs[1]["verb"] == "Shortening" and evs[2]["detail"] == "3 compiles"
    for e in evs:                                   # the one shape
        assert set(e) >= {"phase", "verb", "target", "summary", "state", "at"}
        assert e["target"] == "Data Analyst at Acme" and isinstance(e["at"], float)


def test_nested_builds_join_one_run_and_keys_stay_apart():
    with A.run("resume", key="builder") as outer:
        A.step("read", "Reading the job")
        with A.run("resume", key="builder") as inner:
            assert inner is outer
            A.step("draft", "Drafting")
    with A.run("resume", key="unattended"):
        A.step("match", "Matching your experience")
    assert [e["phase"] for e in A.snapshot("builder")["events"]] == ["read", "draft"]
    assert [e["phase"] for e in A.snapshot("unattended")["events"]] == ["match"]


def test_an_error_parks_the_current_step_and_is_raised():
    with pytest.raises(ValueError):
        with A.run("resume", key="builder"):
            A.step("compile", "Compiling", "the PDF")
            raise ValueError("boom")
    snap = A.snapshot("builder")
    assert snap["ok"] is False and snap["events"][-1]["state"] == "parked"


def test_event_text_is_one_line_without_dashes_and_capped():
    ev = A.event("x", "Fitting — the page", summary="a\n" + "b" * 400, state="weird")
    assert not DASH.search(ev["verb"]) and "\n" not in ev["summary"] and len(ev["summary"]) <= 140
    assert ev["state"] == "running"


def test_a_long_run_keeps_its_first_step_and_the_newest():
    with A.run("resume", key="builder"):
        for i in range(A.MAX_EVENTS + 25):
            A.step(f"p{i}", "Working")
    evs = A.snapshot("builder")["events"]
    assert len(evs) == A.MAX_EVENTS and evs[0]["phase"] == "p0" and evs[-1]["phase"] == f"p{A.MAX_EVENTS + 24}"


# --------------------------------------------------------------------------- the resume builder
@pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")
def test_a_build_from_the_saved_profile_shows_each_phase_in_order(template_source, workdir):
    from intake.memory import ConversationMemory
    from ui.records import CVRecords
    from ui.session import WebIntake
    saved = {
        "identity": {"name": "Kwame Asante", "email": "kwame@example.com"},
        "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                       "date": "June 2019", "location": "Accra, Ghana"}],
        "experience": [{"org": "BlackOrigin", "location": "Accra, Ghana",
                        "roles": [{"title": "Data Analyst", "dates": "Feb 2021 - Present",
                                   "bullets": ["Built Tableau dashboards for the operations team."]}]}],
        "skills": {"Data & Analytics": "SQL, Python, Tableau", "Domain": "statistics, forecasting"},
    }
    mem = ConversationMemory(":memory:")
    mem.save("default", saved, {}, [])
    s = WebIntake("Data Analyst. Required skills: SQL, Python, Tableau.", template_source, FakeLLM(), mem,
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.essentials["override"] = True
    s.essentials["unattended"] = True            # the one path past the page-filling gate
    s.build_from_saved()
    assert s.assembled is not None
    assert A.snapshot("builder") is None, "the unattended build must not show on the builder's line"
    run = A.snapshot("unattended")
    assert run["ok"] is True and run["kind"] == "resume"
    verbs = [e["verb"] for e in run["events"]]
    order = ["Reading the job", "Matching your experience", "Crafting bullets", "Fitting the page",
             "Compiling", "Checking every line"]
    at = [verbs.index(v) for v in order]
    assert at == sorted(at), verbs
    assert all(e["state"] in ("done", "retry") for e in run["events"]), run["events"]
    for e in run["events"]:
        assert not DASH.search(e["verb"] + e["summary"])


def test_activity_route_returns_the_builders_run_only(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    c = mod.app.test_client()
    assert c.get("/api/activity").get_json() == {"ok": True, "run": None}
    with A.run("resume", key="builder", target="Analyst at Acme"):
        A.step("draft", "Drafting", "your resume from your experience")
        live = c.get("/api/activity?key=builder").get_json()["run"]
    assert live["events"][0]["verb"] == "Drafting" and live["ended"] is None
    with A.run("resume", key="unattended"):
        A.step("read", "Reading the job")
    assert c.get("/api/activity?key=nope").get_json()["run"]["target"] == "Analyst at Acme"
    assert c.get("/api/activity?key=unattended").get_json()["run"]["events"][0]["phase"] == "read"


# --------------------------------------------------------------------------- the brain's summary
LINES = brain.view_lines('[3]<input type=email label="Email" value="">\n[5]<select label="Gender" options=3>\n'
                         '[6]<select label="Country" options=200>\n[7]<button>Next</button>\n[8]<button>Apply</button>')


def test_the_prompt_asks_for_a_short_summary_and_stays_short():
    assert '"summary"' in brain.SYSTEM_PROMPT and "placeholders only" in brain.SYSTEM_PROMPT
    assert len(brain.SYSTEM_PROMPT.splitlines()) < 60 and not DASH.search(brain.SYSTEM_PROMPT)


def test_parse_reply_keeps_the_summary():
    raw = json.dumps({"memory": "", "evaluation": "", "summary": "Clicking Apply to open the form",
                      "actions": [{"op": "click", "index": 8}], "done": None})
    assert brain.parse_reply(raw, {8})["summary"] == "Clicking Apply to open the form"


def test_the_summary_reads_placeholders_as_words_and_never_carries_a_value():
    typed = [{"op": "type", "index": 3, "text": "kofi@example.com", "sensitive": True, "placeholder": "email"},
             {"op": "click", "index": 7}]
    vals = {"email": "kofi@example.com", "full_name": "Kofi Gilbert", "city": "Austin"}
    assert brain.clean_summary("Typing {{email}}, then clicking Next", typed, LINES, None, vals) \
        == "Typing your email, then clicking Next"
    for bad in ("Typing kofi@example.com then Next", "Filling in Kofi Gilbert's details",
                "Typing Austin into the city box", "Typing 5551002000 as the phone",
                "Opening https://evil.example.com/x"):
        out = brain.clean_summary(bad, typed, LINES, None, vals)
        assert out == "Filling 1 box, then clicking Next", (bad, out)
        assert "kofi" not in out.lower() and "austin" not in out.lower()


def test_the_summary_never_names_a_self_identification_answer_or_a_chosen_option():
    pick = [{"op": "select", "index": 5, "option": "Female"}]
    for bad in ("Choosing Female for gender", "Answering the gender question", "Picking female"):
        out = brain.clean_summary(bad, pick, LINES, None, {})
        assert "female" not in out.lower(), out
    country = [{"op": "select", "index": 6, "option": "Ghana"}]
    out = brain.clean_summary("Choosing Ghana as your country", country, LINES, None, {})
    assert "ghana" not in out.lower() and out == "Choosing an option for Country"
    # a value only the redact guard knows (a stored legal value) also drops it
    out = brain.clean_summary("Choosing H-1B in the visa list", [{"op": "click", "index": 7}], LINES, None, {},
                              withheld=["H-1B"])
    assert "h-1b" not in out.lower()


def test_without_a_summary_one_is_built_from_the_actions():
    assert brain.clean_summary("", [{"op": "click", "index": 8}], LINES) == "Clicking Apply"
    assert brain.clean_summary("", [{"op": "upload", "index": 9, "path": "/x.pdf"}], LINES) == "Uploading your resume"
    assert brain.clean_summary("", [{"op": "type", "index": 3, "text": "x", "placeholder": "portal_password",
                                     "sensitive": True}], LINES) == "Typing your password"
    assert brain.clean_summary("", [], LINES, {"outcome": "ready_to_submit"}) == "Stopping at the submit button for you"
    long = brain.clean_summary("Clicking " + "very " * 40 + "long", [{"op": "click", "index": 8}], LINES)
    assert len(long) <= 93 and long.endswith("...") and not DASH.search(long)


def test_every_step_reply_carries_a_screen_safe_summary():
    req = {"step": 1, "max_steps": 40, "url": "https://acme.wd5.myworkdayjobs.com/x", "lane": "assisted",
           "view": "URL: https://acme.wd5.myworkdayjobs.com/x\n[1]<button>Apply</button>\n[End of page]",
           "history": [], "job": {"role": "Analyst", "company": "Acme"}}
    out = brain.step(req, FakeLLM(), {"email": "kofi@example.com"}, None, placeholders=["email"])
    assert out["ok"] and out["summary"] == "Clicking Apply"


# --------------------------------------------------------------------------- the shell's feed
def test_the_shell_feed_shows_each_step_of_a_mock_run():
    """agent.js createFeed + a mock run under node: Opening, Signing in, Filled N fields from your
    memory, the brain's summaries, a retry when an action fails, Asking you, Ready to send, with
    the step count and "Nothing sent without you"; every progress line carries the snapshot and
    the messages are unchanged."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_activity.js")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
