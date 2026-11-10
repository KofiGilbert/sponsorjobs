"""One voice in the resume chat (Kofi, 2026-11-10: "Isn't the ai the one doing the talking all the
time? cos i think that should be the case").

The code decides WHAT is said in a turn (the brief: what is missing, the page fill, what changed,
what was cut, the connections, the buttons); the AI decides HOW, in ONE message per turn
(agent/narrate.py, WebIntake._speak). The fixed lines are only the fallback. FakeLLM, real
pdflatex where a page is built.
"""

from __future__ import annotations

import json

import pytest

from agent import narrate as N
from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from test_chat_one_voice import BUILD_BUTTON, EXTRAS, JD, SAVED, SKELETON
from ui.records import CVRecords
from ui.session import WebIntake

FIXED = (WebIntake.EDIT_TIP, "Your template has", "won't look complete", "Before I build",
         "I filled the rest", "That's everything then", "Welcome back", "To fit one page",
         "Heads up", "Without dates", "I aligned a job")


def _ai(system, user, items):
    """A model that writes its own words and carries every exact string the brief names."""
    exact = [x for it in items for x in it.get("exact") or []]
    asks = [it for it in items if it.get("kind") in N.ASK_KINDS]
    out = "From the AI: here is where we are" + ("; " + "; ".join(exact) if exact else "") + "."
    return out + (" What would you like to add?" if asks else "")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")
    monkeypatch.delenv(N.FLAG, raising=False)


def _s(template_source, fake_llm, workdir, memory=None):
    s = WebIntake(JD, template_source, fake_llm, memory or ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    return s


def _returning(template_source, fake_llm, workdir):
    mem = ConversationMemory(":memory:")
    mem.save("default", SAVED, {}, [])
    return _s(template_source, fake_llm, workdir, memory=mem)


def _agent_rows(s, since=0):
    return [t for t in s.history[since:] if t.get("role") == "agent" and not t.get("build")]


# ------------------------------------------------------------------ one message, from the AI

@requires_latex
def test_every_turn_is_one_message_written_by_the_ai(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _s(template_source, fake_llm, workdir)
    for st in (s.start(), s.submit(SKELETON), s.submit(EXTRAS)):
        assert len(st["messages"]) == 1, st["messages"]
        said = st["messages"][0]
        assert said.startswith("From the AI"), said
        assert not any(f in said for f in FIXED), said
        assert said.count("?") <= 1
    assert s.assembled is not None and st.get("build")
    assert st["build_at"] == 0                      # the build's row first, then the message
    # The history holds the same one message per turn, after the build's row.
    rows = [t for t in s.history if t.get("role") == "agent"]
    assert sum(1 for t in rows if not t.get("build")) == 3
    last_build = max(i for i, t in enumerate(s.history) if t.get("build"))
    assert s.history[-1]["content"] == st["messages"][0] and len(s.history) - 1 > last_build
    assert all(log["narrated"] for log in s._voice_log if log.get("lines", 0) and "why" in log)


@requires_latex
def test_the_fallback_is_one_message_of_the_fixed_lines(template_source, fake_llm, workdir):
    fake_llm.narrate_script = RuntimeError("the broker is down")
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    assert len(st["messages"]) == 1
    assert WebIntake.EDIT_TIP in st["messages"][0]             # the fixed lines, joined
    assert st["messages"][0].count("?") <= 1
    assert s._voice_log[-1]["narrated"] is False
    assert s._voice_log[-1]["why"].startswith("call failed")


def test_narration_off_says_the_fallback(template_source, fake_llm, workdir, monkeypatch):
    monkeypatch.setenv(N.FLAG, "0")
    fake_llm.narrate_script = _ai
    s = _returning(template_source, fake_llm, workdir)
    st = s.start()
    assert st["messages"][0].startswith("Welcome back Kwame.")
    assert not getattr(fake_llm, "narrate_calls", 0)


def test_the_opening_is_written_from_its_facts(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _returning(template_source, fake_llm, workdir)
    st = s.start()
    assert st["messages"][0].startswith("From the AI")
    it = fake_llm.last_narrate_items[0]
    assert it["kind"] == "opening" and it["first_name"] == "Kwame" and it["returning"] is True
    assert "University of Ghana" in it["background_on_file"] and "BlackOrigin" in it["background_on_file"]
    assert s.history[-1]["content"] == st["messages"][0]


# ------------------------------------------------------------------ buttons stay attached

def test_the_outside_work_question_keeps_its_button_and_states_the_sections(
        template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _returning(template_source, fake_llm, workdir)
    s.start()
    calls = []
    real = fake_llm.ask_enrich
    fake_llm.ask_enrich = lambda *a, **kw: calls.append(1) or real(*a, **kw)
    st = s.submit(BUILD_BUTTON)
    assert s.assembled is None and len(st["messages"]) == 1
    said = st["messages"][0]
    assert said.startswith("From the AI") and "Interests" in said
    assert [o["label"] for o in st["required_open"]] == ["Interests"]  # the button
    assert not calls, "the question was worded by a second model call"
    item = next(i for i in fake_llm.last_narrate_items if i["kind"] == "ask_required")
    assert item["sections"] == ["Interests"]
    # Asked again by the build button: the nudge, one message, the button named exactly.
    st = s.submit(BUILD_BUTTON)
    if s.assembled is None:
        assert "None, leave it off" in st["messages"][0]


@requires_latex
def test_the_project_offer_keeps_its_buttons(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    offer = [c for c in st["cards"] if c.get("offer") == WebIntake.PROJECT_OFFER]
    if offer:            # the job asks for skills the page does not show
        labels = [b["label"] for b in offer[0]["buttons"]]
        assert "Build a project for this job" in labels
        turn = s.history[-1]
        assert turn.get("card") is offer[0] or offer[0] in (turn.get("cards") or [])
        it = next(i for i in fake_llm.last_narrate_items if i["kind"] == "project_offer")
        assert it["buttons_below"] == labels


# ------------------------------------------------------------------ the facts, exactly

@requires_latex
def test_thats_everything_states_the_page_fill_exactly(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit(EXTRAS)
    st = s.submit("that's everything")
    assert len(st["messages"]) == 1
    items = fake_llm.last_narrate_items
    if s.assembled.underfull:
        it = next(i for i in items if i["kind"] == "page_still_short")
        pct = s._page_fill_pct()
        assert it["fill_percent"] == pct and f"{pct}%" in it["exact"]
        assert f"{pct}%" in st["messages"][0] and "Accept & Save" not in st["messages"][0]
    else:
        assert any(i["kind"] == "done" for i in items)
        assert "Accept & Save" in st["messages"][0]


def test_a_message_that_drops_a_fact_or_invents_one_is_not_said():
    items = [N.item("page_still_short", "The page is at 62%, so it isn't finished yet.",
                    exact=["62%"], fill_percent=62),
             N.item("ask", "Could you tell me about a project?")]
    assert N.check("Your page is 62% full. Any project to add?", items) == ""
    assert "missing" in N.check("Your page is nearly full. Any project to add?", items)
    assert "number" in N.check("Your page is 62% full after 3 new bullets.", items)
    assert "question" in N.check("It is 62% full. A project? A club?", items)
    assert N.check("", items) == "empty"


def test_a_failed_check_falls_back(fake_llm):
    items = [N.item("page_still_short", "The page is at 62%.", exact=["62%"], fill_percent=62)]
    fake_llm.narrate_script = "The page is at 70% now."
    out = N.narrate(fake_llm, items, [], "that's everything")
    assert out["ok"] is False and out["text"] == "The page is at 62%."
    fake_llm.narrate_script = "Your page sits at 62%, with room for more of your work."
    out = N.narrate(fake_llm, items, [], "that's everything")
    assert out["ok"] is True and out["text"].startswith("Your page sits at 62%")
    assert out["usage"]["model"] == "fake"


def test_the_written_message_is_cleaned():
    assert N.clean("Here is your CV — it fits one page.") == "Here is your resume, it fits one page."
    assert N.clean("**Accept & Save** when ready") == "<b>Accept & Save</b> when ready"
    assert N.clean('"Quoted."') == "Quoted."


def test_the_fallback_asks_one_thing():
    items = [N.item("reply_draft", "Thanks! Could you share your education?"),
             N.item("edit_tip", WebIntake.EDIT_TIP),
             N.item("ask_required", "Could you tell me a few interests or hobbies?")]
    fb = N.fallback(items)
    assert fb.count("?") == 1 and fb.startswith("Thanks!") and WebIntake.EDIT_TIP in fb


def test_the_brief_carries_the_chat_and_the_person_message():
    items = [N.item("done", "Check the page, then <b>Accept & Save</b>.", exact=["Accept & Save"])]
    hist = [{"role": "agent", "content": "Hi"}, {"role": "user", "content": "that's everything"}]
    prompt = N.user_prompt(items, hist, "that's everything", "Data Analyst", "Acme")
    assert "JOB: Data Analyst at Acme" in prompt and "You: Hi" in prompt
    assert prompt.count("that's everything") == 1                  # not twice
    assert json.loads(prompt.split("in order):\n")[1].split("\nWrite")[0])[0]["kind"] == "done"


# ------------------------------------------------------------------ the post-checks

def test_a_claim_of_a_change_that_did_not_happen_is_not_said(template_source, fake_llm, workdir):
    fake_llm.narrate_script = "Welcome back Kwame. I added your certifications and rebuilt the page."
    s = _returning(template_source, fake_llm, workdir)
    st = s.start()
    assert len(st["messages"]) == 1 and "I added" not in st["messages"][0]
    assert st["messages"][0].startswith("Welcome back Kwame. I still have your background")
    assert s._voice_log[-1]["narrated"] is False and s._voice_log[-1]["why"] == "checks"


def test_share_your_github_becomes_the_connect_button(template_source, fake_llm, workdir):
    fake_llm.narrate_script = ("Welcome back Kwame, your background is on file. Share your GitHub "
                               "so I can check it, or should I tailor what I have?")
    s = _returning(template_source, fake_llm, workdir)
    st = s.start()
    assert "Share your GitHub" not in st["messages"][0]
    assert "connect your GitHub with the button below" in st["messages"][0]
    assert any(s._connect_buttons(c) for c in st["cards"])
    assert s._connect_buttons(s.history[-1].get("card"))


# ------------------------------------------------------------------ what is not narrated

def test_a_single_agent_reply_is_not_narrated_again(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _returning(template_source, fake_llm, workdir)
    s.start()
    n = fake_llm.narrate_calls
    fake_llm.agent_script = [{"text": "Your GitHub is not connected yet."}]
    st = s.submit("Which of my GitHub repositories fit this job?")
    assert st["messages"] == ["Your GitHub is not connected yet."]
    assert fake_llm.narrate_calls == n


def test_an_unattended_build_never_calls_the_narrator(template_source, fake_llm, workdir):
    fake_llm.narrate_script = _ai
    s = _returning(template_source, fake_llm, workdir)
    s.essentials["unattended"] = True
    s.start()
    assert not getattr(fake_llm, "narrate_calls", 0)


# ------------------------------------------------------------------ the model calls

def test_the_byo_client_narrates_on_the_cheap_model_without_thinking(monkeypatch):
    from llm.anthropic_client import NARRATE_MODEL, AnthropicLLM
    sent = []

    class _Msg:
        content = [type("B", (), {"type": "text", "text": "Hello there."})()]
        usage = type("U", (), {"input_tokens": 120, "output_tokens": 9})()

    class _Client:
        class messages:            # noqa: N801 - the SDK's shape
            @staticmethod
            def create(**kw):
                sent.append(kw)
                return _Msg()

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.model, llm.effort, llm._client = "claude-sonnet-5-5", "low", _Client()
    monkeypatch.delenv("RESUME_AGENT_NARRATE_MODEL", raising=False)
    assert llm.narrate("sys", "user", max_tokens=400) == "Hello there."
    kw = sent[0]
    assert kw["model"] == NARRATE_MODEL == "claude-haiku-5-5"
    assert kw["max_tokens"] == 400 and kw["thinking"] == {"type": "disabled"}
    assert kw["output_config"] == {"effort": "low"}
    assert llm.last_narrate_usage == {"input": 120, "output": 9, "model": "claude-haiku-5-5"}


def test_the_broker_client_narrates_at_low_effort(monkeypatch):
    from llm.broker_client import BrokerLLM
    llm = BrokerLLM(broker_url="http://127.0.0.1:1")
    sent = []

    def send(payload):
        sent.append(payload)
        return {"text": "Hi.", "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 300, "output_tokens": 20}}

    monkeypatch.setattr(llm, "_send", send)
    assert llm.narrate("sys", "user") == "Hi."
    assert sent[0]["effort"] == "low" and sent[0]["max_tokens"] == 400
    assert llm.last_narrate_usage == {"input": 300, "output": 20, "model": "claude-sonnet-5-5"}


# ------------------------------------------------------------------ the chat renders it safely

def _chat_text_js():
    """The chat's own renderer (esc, unDash, the markdown blocks, chatText) cut out of app.js."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    esc = js[js.index("  const esc = (s)"):js.index("\n\n", js.index("  const esc = (s)"))]
    start = js.index("  const unDash = (s)")
    end = js.index(".split(/\\n{2,}/).map(mdBlock).join(\"\");", start) + len(".split(/\\n{2,}/).map(mdBlock).join(\"\");")
    return esc + "\n" + js[start:end]


@pytest.mark.parametrize("raw,want", [
    ("Your page is at **62%**.\n\nAdd a project:\n- Route Planner\n- Gate Check",
     "<p>Your page is at <b>62%</b>.</p><p>Add a project:</p><ul><li>Route Planner</li><li>Gate Check</li></ul>"),
    ("Line one\nline two", "<p>Line one<br>line two</p>"),
    ("1. Edit a bullet\n2. Click <b>Accept & Save</b>",
     "<ol><li>Edit a bullet</li><li>Click <b>Accept &amp; Save</b></li></ol>"),
    ('<img src=x onerror="alert(1)"> <b onclick="x">hi</b>',
     "<p>&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &lt;b onclick=&quot;x&quot;&gt;hi</b></p>"),
])
def test_the_chat_renders_a_little_markdown_and_never_raw_html(raw, want):
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    src = _chat_text_js() + "\nprocess.stdout.write(chatText(" + json.dumps(raw) + "));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout == want
