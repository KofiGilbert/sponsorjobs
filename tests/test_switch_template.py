"""Switching the template in a running resume chat (Kofi, test build 13, 2026-11-09): from a job's
"Tailor & apply" he clicked "change", picked Data Scientist and "Use this template", and landed on a
brand new resume, losing the job and the chat. Now the same chat switches: the JD, the chat and the
answers stay; a built page is rebuilt in the new template (one page, no new overfull); one chat line
says so. FakeLLM and real pdflatex."""

from __future__ import annotations

from pathlib import Path

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
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


def _session(fake_llm, workdir, name="shetty"):
    tex, tname = app._load_template(name)
    s = WebIntake(JD, tex, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name=tname)
    s.start()
    return s


def _built(fake_llm, workdir):
    s = _session(fake_llm, workdir)
    s.submit(SKELETON)
    if s.assembled is None:
        s.submit(EXTRAS)
    assert s.assembled is not None and s.assembled.ok
    return s


def test_switching_before_a_build_keeps_the_job_and_the_chat(fake_llm, workdir):
    s = _session(fake_llm, workdir)
    s.submit("I'm Kwame Asante, kwame@example.com.")
    before = [dict(t) for t in s.history]
    tex, _ = app._load_template("data")
    st = s.set_template("data", tex)
    assert st["template_ok"] is True and s.template_name == "data"
    assert st["template_label"] == "Data Scientist" and s.jd == JD
    assert s.history[:len(before)] == before                          # the chat is all there
    assert s.history[-1]["content"] == "Switched to the Data Scientist template."
    assert st["messages"] == ["Switched to the Data Scientist template."]
    assert s.assembled is None and "projects" in s.required_fill      # the next build uses it
    assert s.essentials["identity"].get("name")                       # answers kept


@requires_latex
def test_switching_after_a_build_rebuilds_one_page_in_the_new_template(fake_llm, workdir):
    s = _built(fake_llm, workdir)
    users = [t for t in s.history if t.get("role") == "user"]
    v = s._pdf_v
    tex, _ = app._load_template("data")
    st = s.set_template("data", tex)
    assert st["template_ok"] is True and s.template_name == "data"
    assert s.assembled.ok and s.assembled.compile.pages == 1           # one clean page
    assert s._pdf_v == v + 1 and st["pdf"].endswith(f"v={s._pdf_v}")   # the preview refreshes
    # The rebuild is told by its build summary in the chat, naming the template; no second line.
    assert st["messages"] == []
    summary = st["build"]["summary"]
    assert "Data Scientist template" in summary and ("one page" in summary or "% of the page" in summary)
    assert s.history[-1].get("build", {}).get("id") == st["build"]["id"]
    assert [t for t in s.history if t.get("role") == "user"] == users


@requires_latex
def test_the_route_switches_and_returns_the_new_label(fake_llm, workdir):
    s = _built(fake_llm, workdir)
    app._SESSION["s"] = s
    try:
        client = app.app.test_client()
        st = client.post("/api/session/template", json={"name": "software"}).get_json()
        assert st["template_ok"] and st["template"] == "software"
        assert st["template_label"] == "Software Engineer"
        assert client.get("/api/session/state").get_json()["template_label"] == "Software Engineer"
        bad = client.post("/api/session/template", json={"name": "../../etc/passwd"})
        assert bad.status_code == 400 and s.template_name == "software"
    finally:
        app._SESSION.pop("s", None)


def test_a_rebuild_that_cannot_fit_keeps_the_old_template(fake_llm, workdir, monkeypatch):
    s = _session(fake_llm, workdir)

    class _Built:
        ok, status, pdf_path, profile_used = True, "clean", None, {}
    s.assembled = _Built()
    calls = []

    def fake_assemble(fit_lines=False):
        calls.append(s.template_name)
        s.assembled = type("R", (), {"ok": s.template_name == "shetty", "status": "x",
                                      "pdf_path": None, "profile_used": {}})()
    monkeypatch.setattr(s, "_assemble", fake_assemble)
    monkeypatch.setattr(s, "_state", lambda msgs=None, **k: {"messages": list(msgs or [])})
    n = len(s.history)
    st = s.set_template("data", app._load_template("data")[0])
    assert st["template_ok"] is False and "Classic" in st["template_error"]
    assert s.template_name == "shetty" and s.assembled.ok and len(s.history) == n
    assert calls == ["data", "shetty"]                                # the old page built again


def test_use_this_template_from_the_builder_switches_in_place():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    use = js.split("async function useTplFromPreview()", 1)[1].split("\n  }\n", 1)[0]
    assert 'TPL_ORIGIN === "builder"' in use and "switchTemplate(t)" in use
    assert "openBuilder(t.name)" in use                               # any other origin: as before
    sw = js.split("async function switchTemplate(t)", 1)[1].split("\n  }\n", 1)[0]
    assert '"/api/session/template"' in sw and "restoreSession()" in sw and "toast(" in sw
