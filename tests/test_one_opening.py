"""Tailor chat: one opening message (2026-11-26).

The owner saw the returning-user opening twice in one resume chat: "Welcome back ... tailor what I
have to the Business Intelligence (Cognos Developer) role at DCM Infotech Limited?" with its
"Tailor from my profile" card, then a second, differently worded opening for "this role". Two
starts had landed a second apart (a job's Tailor and a pasted link): two sessions, and the older
answer was painted into the newer chat after it had been reset, header and all; its "build it for
me" then built the other session. Now:

  * the client drops a start's answer once a newer start began (START_GEN in ui/static/app.js);
  * a session's start() is once only: a second call adds and narrates nothing.
Offline: FakeLLM. Made-up job and person.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JD = ("Business Intelligence Analyst at Northwind. Required: SQL, Power BI, Tableau, Python, "
      "KPI dashboards, stakeholder management, data modeling.")


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as app
    importlib.reload(app)
    yield app
    app._SESSION.pop("s", None)


def test_a_second_start_adds_no_second_opening(A, template_source, workdir):
    from llm.base import FakeLLM
    from ui.records import CVRecords
    from ui.session import WebIntake
    s = WebIntake(JD, template_source, FakeLLM(), A._memory(), CVRecords(":memory:"), workdir,
                  jobname="cvone", palace_dir=workdir / "p")
    first = s.start()
    again = s.start()
    agent_lines = [t for t in s.history if t.get("role") == "agent"]
    assert len(agent_lines) == 1, s.history
    assert first["messages"] and not again.get("messages"), (first.get("messages"), again.get("messages"))


def test_the_client_drops_a_start_answer_a_newer_start_replaced():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert re.search(r"let START_GEN = 0;", js)
    job = js[js.index("async function tailorJob(sid) {"):]
    job = job[:job.index("\n  }\n")]
    assert "const gen = ++START_GEN;" in job and "if (gen !== START_GEN) return;" in job
    assert job.index("if (gen !== START_GEN) return;") < job.index("applyState(st)")
    paste = js[js.index('$("#jdStart").addEventListener("click"'):]
    paste = paste[:paste.index("\n  });\n")]
    assert "const gen = ++START_GEN;" in paste and paste.index("if (gen !== START_GEN) return;") < paste.index("applyState(st)")
