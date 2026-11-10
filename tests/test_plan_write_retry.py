"""The plan writer's one call must come back with an answer (Kofi, 2026-11-10).

Twice a real build's write_experience call came back with no text: a reasoning model spent its
whole token budget and stopped at max_tokens before writing. An empty answer, or one cut off at
the limit, is asked once more with no effort setting and more room; why each try stopped is kept
for the fill log.
"""

from __future__ import annotations

import json

import pytest

from llm.anthropic_client import AnthropicLLM

ROLES = [{"title": "Analyst", "company": "Acme", "dates": "2020 - 2024", "tense": "past",
          "saved": ["Built SQL reports."], "notes": "", "windows": [(90, 100, 1)]}]


class _Scripted(AnthropicLLM):
    def __init__(self, answers):
        self.model, self.effort = "claude-sonnet-5-5", "low"
        self.answers = list(answers)
        self.calls = []

    def _complete(self, system, user, max_tokens=1024, effort=None):
        text, stop = self.answers.pop(0)
        self.calls.append((effort, max_tokens))
        self.last_meta = {"stop_reason": stop, "usage": {"output_tokens": max_tokens}}
        return text


def test_an_empty_answer_is_asked_again_without_effort_and_with_more_room():
    llm = _Scripted([("", "max_tokens"), (json.dumps([["Built SQL reports on order volumes."]]), "end_turn")])
    assert llm.write_experience(ROLES, jd_text="") == [["Built SQL reports on order volumes."]]
    assert llm.calls == [("low", 16000), ("none", 24000)]
    assert [t["stop_reason"] for t in llm.last_meta["tries"]] == ["max_tokens", "end_turn"]


def test_a_readable_first_answer_is_one_call():
    llm = _Scripted([(json.dumps([["Built SQL reports on order volumes."]]), "end_turn")])
    assert llm.write_experience(ROLES)
    assert len(llm.calls) == 1


def test_two_empty_answers_raise_with_why():
    llm = _Scripted([("", "max_tokens"), ("", "max_tokens")])
    with pytest.raises(ValueError, match="max_tokens"):
        llm.write_experience(ROLES)


def test_the_broker_client_sends_no_effort_for_none():
    from llm.broker_client import BrokerLLM
    b = BrokerLLM(broker_url="http://127.0.0.1:9", auth_token="t")
    sent = {}
    b._send = lambda payload: sent.update(payload) or {"text": "ok", "stop_reason": "end_turn"}
    assert b._complete("s", "u", max_tokens=12000, effort="none") == "ok"
    assert sent["effort"] is None and sent["max_tokens"] == 12000
    assert b.last_meta["stop_reason"] == "end_turn"


def test_a_polish_never_takes_the_page_under_the_goal_or_drops_a_figure(tmp_path):
    """Run 12: the project polish tightened "...matching about 10 drivers." to one line without
    the figure or the full stop, and the page fell from 95.5% to 93.9%."""
    import inspect
    from tailoring import assembler as A
    src = inspect.getsource(A._polish_edges)
    assert "LINE_SHARE >= FILL_GOAL" in src                    # tighten only with a line to spare
    assert "lost a figure or the full stop" in src
    assert ">= FILL_GOAL > (r.fill_ratio or 0)" in src          # never under the goal
