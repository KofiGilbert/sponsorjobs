"""A restored page is printed by today's renderer, and a projects offer is not repeated once the
person acted on one (Kofi, 2026-11-16)."""
import tempfile
from pathlib import Path

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from ui.records import CVRecords
from ui.session import WebIntake
from ui.app import _load_template


def _s():
    src, _ = _load_template(None)
    wd = Path(tempfile.mkdtemp())
    return WebIntake("Data Analyst. Python and SQL.", src, FakeLLM(), ConversationMemory(":memory:"),
                     CVRecords(":memory:"), wd, jobname="cv", palace_dir=wd / "p")


def test_a_used_projects_offer_settles_the_projects():
    s = _s()
    assert not s._projects_settled()
    s.history = [{"role": "agent", "content": "x",
                  "card": {"type": "buttons", "offer": s.PROJECT_OFFER, "used": True,
                           "buttons": [{"label": "Swap it in", "token": "t"}]}}]
    assert s._projects_settled()
    s.assembled = object()
    assert s._project_verdict() is None


def test_refresh_render_is_a_no_op_without_a_page():
    assert _s().refresh_render() is False
