"""generate_intake_questions parsing (llm/anthropic_client.py).

The model sometimes wraps its JSON array in a ```json code fence. A bare json.loads used to fail
on that and the line-split fallback leaked the fence line in as a bogus "question". These lock in
that fenced arrays parse cleanly and that no fence line ever survives.
"""

from __future__ import annotations

from llm.anthropic_client import AnthropicLLM


class _StubLLM(AnthropicLLM):
    """AnthropicLLM with the network stubbed: _complete returns a canned raw string, so the parser
    is exercised directly with no SDK, key, or request."""
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.model = "m"
        self.effort = "low"

    def _complete(self, system, user, max_tokens=1024, effort=None):
        return self._raw


def test_fenced_json_array_parses_cleanly():
    raw = '```json\n["What is your name?", "Which role are you targeting?"]\n```'
    assert _StubLLM(raw).generate_intake_questions("jd", None) == \
        ["What is your name?", "Which role are you targeting?"]


def test_plain_json_array_still_parses():
    raw = '["A?", "B?"]'
    assert _StubLLM(raw).generate_intake_questions("jd", None) == ["A?", "B?"]


def test_fallback_line_split_drops_fence_lines():
    raw = "```\nWhat did you build?\n- Where did you deploy it?\n```"
    assert _StubLLM(raw).generate_intake_questions("jd", None) == \
        ["What did you build?", "Where did you deploy it?"]
