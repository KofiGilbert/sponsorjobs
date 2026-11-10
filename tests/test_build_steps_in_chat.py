"""A build is told in the chat, the way Claude and ChatGPT show their work in the conversation
(Kofi, 2026-11-09: "i was expecting to see the updates you are making to the resume in the chat
side"). Each build or rebuild adds ONE agent turn carrying its steps and what changed, the state
returns it as ``build`` for the chat to render, a restored chat gets it back collapsed, and the
pane no longer carries the step log or the What changed panel. FakeLLM and real pdflatex."""

from __future__ import annotations

from pathlib import Path

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
from tailoring.changes import readable
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]
JD = "Data Analyst. Required skills: SQL, Python, dashboards, statistics, Tableau, Excel."
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."


def _session(fake_llm, workdir):
    tex, tname = app._load_template("shetty")
    s = WebIntake(JD, tex, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name=tname)
    s.start()
    return s


def _built(fake_llm, workdir):
    s = _session(fake_llm, workdir)
    st = s.submit(SKELETON)
    if s.assembled is None:
        st = s.submit(EXTRAS)
    assert s.assembled is not None and s.assembled.ok
    return s, st


def _builds(s):
    return [t for t in s.history if isinstance(t, dict) and t.get("build")]


@requires_latex
def test_a_build_adds_one_agent_turn_with_its_steps_and_what_changed(fake_llm, workdir):
    s, st = _built(fake_llm, workdir)
    turns = _builds(s)
    assert len(turns) == 1
    rec = turns[0]["build"]
    assert turns[0]["role"] == "agent" and turns[0]["content"] == rec["summary"]
    assert st["build"]["id"] == rec["id"]                       # the response carries it to the chat
    assert rec["summary"].startswith("Built your resume")
    assert rec["summary"].endswith("one page") or "% of the page" in rec["summary"]
    assert " · " in rec["summary"]
    # The steps, in order, plain: a verb, a summary, a state; never a time.
    verbs = [ev["verb"] for ev in rec["steps"]]
    assert verbs[0] == "Reading the job" and "Compiling" in verbs
    assert all("at" not in ev and ev["state"] in ("done", "retry") for ev in rec["steps"])
    assert isinstance(rec["items"], list) and isinstance(rec["cuts"], list)
    assert rec["secs"] is None or rec["secs"] >= 1


@requires_latex
def test_no_separate_first_draft_line_repeats_the_summary(fake_llm, workdir):
    s, st = _built(fake_llm, workdir)
    said = " ".join(st["messages"]) + " ".join(str(t.get("content")) for t in s.history)
    assert "Here's a first draft" not in said and "Here's your draft" not in said
    assert "Building from your saved profile" not in said
    # A short page is said once, in the summary row; no promise line ("I'll ask for a few more
    # real things to fill the page.") stands in the chat without a question (2026-11-10).
    assert "a few more real things" not in said
    if s.assembled.underfull:
        assert "% of the page" in st["build"]["summary"]


@requires_latex
def test_a_rebuild_adds_a_new_turn_and_says_what_it_did(fake_llm, workdir):
    s, _st = _built(fake_llm, workdir)
    st = s.keyword_add("Excel")
    assert st["ok"] is True, st.get("reason")
    turns = _builds(s)
    assert len(turns) == 2 and turns[1]["build"]["id"] != turns[0]["build"]["id"]
    assert st["build"]["summary"].startswith("Updated your resume")
    assert "Excel added" in st["build"]["summary"]
    # A rebuild says only what moved: an unchanged reword count is not said again.
    if turns[0]["build"]["counts"]["reworded"] == turns[1]["build"]["counts"]["reworded"]:
        assert "reworded" not in st["build"]["summary"]
    # A template switch is one more, naming the template.
    tex, _ = app._load_template("data")
    st2 = s.set_template("data", tex)
    assert st2["template_ok"] and "Data Scientist template" in st2["build"]["summary"]
    assert len(_builds(s)) == 3


def test_a_turn_that_only_asks_adds_no_build_turn(fake_llm, workdir):
    s = _session(fake_llm, workdir)
    st = s.submit("I'm Kwame Asante, kwame@example.com.")
    assert s.assembled is None and "build" not in st and not _builds(s)


@requires_latex
def test_a_restored_chat_keeps_the_build_summary(fake_llm, workdir):
    s, st = _built(fake_llm, workdir)
    app._SESSION["s"] = s
    try:
        state = app.app.test_client().get("/api/session/state").get_json()
    finally:
        app._SESSION.pop("s", None)
    got = [t for t in state["history"] if t.get("build")]
    assert len(got) == 1
    assert got[0]["build"]["id"] == st["build"]["id"]
    assert got[0]["content"] == st["build"]["summary"] and got[0]["build"]["steps"]
    # The turn sits where the build started: after the person's message that asked for it and
    # what was said before the build, above the questions asked after it.
    hist = state["history"]
    i = next(k for k, t in enumerate(hist) if t.get("build"))
    assert hist[i - 1]["content"] == "Great, I've got what I need. Building your resume now." \
        or hist[i - 1]["role"] == "user"
    assert any(t["role"] == "user" for t in hist[:i])
    assert st["messages"][:st["build_at"]] == [t["content"] for t in hist[i - st["build_at"]:i]]
    assert "build" not in state                      # a plain read does not replay a build


def test_the_pane_is_the_resume_and_the_chat_renders_builds():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for gone in ('id="buildLive"', 'id="tailorWhat"', 'id="presendReview"'):
        assert gone not in html
    for kept in ('id="pdfFrame"', 'id="editToggle"', 'id="coverage"', 'id="downloadBtn"', 'id="acceptBtn"'):
        assert kept in html
    assert "Working on your resume…" in js
    assert "if (t.build) { flush(); renderBuildMsg(t.build); return; }" in js   # restore shows it collapsed
    assert "if (st.build) finishBuild(st.build);" in js


def test_readable_names_the_employer_and_the_terms_a_rewrite_brought_in():
    ch = {"reworded": [{"label": "BlackOrigin, Data Analyst", "before": "Built reports for sales.",
                        "after": "Built Power BI reports for sales."},
                       {"label": "Stanbic Bank, Analyst", "before": "Ran SQL checks.",
                        "after": "Ran SQL checks on daily loans."}],
          "skills": {"reordered": ["Tools"], "added": [], "lines_moved": False},
          "written": [{"label": "BlackOrigin", "text": "x"}], "trimmed": [{"id": "t"}]}
    out = readable(ch, ["Power BI", "SQL"])
    assert out[0] == "Reworded a BlackOrigin bullet to mention Power BI."
    assert out[1] == "Reworded a Stanbic Bank bullet toward the job."
    assert "Skills reordered to lead with what the job asks for." in out
    assert not any("written" in x or "cut to fit" in x for x in out)
