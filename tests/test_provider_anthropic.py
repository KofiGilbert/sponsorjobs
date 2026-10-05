"""AnthropicProvider (P3) + CompositeProvider routing, offline with a fake Anthropic client.

Verifies the broker's LLM path: the plan's model is passed through, the prompt is sent, and the
response text + token usage come back in the broker's {text, input_tokens, output_tokens} shape.
The live check (that Anthropic really answers) is a separate smoke test with the real key.
"""

from __future__ import annotations

import pytest

from backend.provider_anthropic import AnthropicProvider
from backend.providers import CompositeProvider


class _Block:
    def __init__(self, text): self.type = "text"; self.text = text


class _Usage:
    def __init__(self, i, o): self.input_tokens = i; self.output_tokens = o


class _Msg:
    def __init__(self, text, i, o): self.content = [_Block(text)]; self.usage = _Usage(i, o)


class FakeAnthropic:
    """Stands in for anthropic.Anthropic: records the create() call and returns a canned message."""
    def __init__(self, text="tailored bullet", i=120, o=30):
        self._msg = _Msg(text, i, o); self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw); return self._msg


def test_complete_passes_model_and_returns_text_and_usage():
    fake = FakeAnthropic(text="Here is the tailored resume.", i=200, o=45)
    p = AnthropicProvider(client=fake)
    out = p.complete("claude-sonnet-4-6", "Tailor my resume for a data role", max_tokens=512)

    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 512
    assert call["messages"] == [{"role": "user", "content": "Tailor my resume for a data role"}]
    assert out == {"text": "Here is the tailored resume.", "input_tokens": 200, "output_tokens": 45}


def test_complete_forwards_system_history_and_effort():
    # the bundled LLM path drives these knobs; the provider must pass them straight to the SDK
    fake = FakeAnthropic(text="ok", i=10, o=5)
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    AnthropicProvider(client=fake).complete(
        "claude-sonnet-4-6", system="Be terse", messages=msgs, max_tokens=99, effort="low")
    call = fake.calls[0]
    assert call["system"] == "Be terse"
    assert call["messages"] == msgs                      # history used verbatim, not a single prompt
    assert call["max_tokens"] == 99
    assert call["output_config"] == {"effort": "low"}


def test_effort_is_dropped_for_models_that_reject_it():
    # Haiku 4.5 / Sonnet 4.5 400 on `effort`; the cheaper plans route there, so sending it
    # would make every completion fail for exactly those users.
    fake = FakeAnthropic(text="ok", i=10, o=5)
    AnthropicProvider(client=fake).complete("claude-haiku-4-5-20251001", "hi", effort="low")
    assert "output_config" not in fake.calls[0]


def test_complete_omits_system_and_effort_when_not_given():
    # a plain {prompt} caller must not send an empty system or an effort override
    fake = FakeAnthropic()
    AnthropicProvider(client=fake).complete("m", "just a prompt")
    call = fake.calls[0]
    assert "system" not in call and "output_config" not in call
    assert call["messages"] == [{"role": "user", "content": "just a prompt"}]


def test_avatar_is_not_supported_on_the_llm_provider():
    with pytest.raises(NotImplementedError):
        AnthropicProvider(client=FakeAnthropic()).start_avatar_session("u")


def test_missing_key_raises(monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", "/nonexistent/credentials.env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError):
        AnthropicProvider()


def test_composite_routes_avatar_and_llm_to_the_right_backends():
    class FakeAvatar:
        def start_avatar_session(self, user, context=None): return {"who": "tavus", "user": user}
        def complete(self, *a, **k): raise AssertionError("avatar must not handle llm")

    comp = CompositeProvider(avatar=FakeAvatar(), llm=AnthropicProvider(client=FakeAnthropic(text="ok")))
    assert comp.start_avatar_session("u1")["who"] == "tavus"
    assert comp.complete("claude-haiku-4-5", "hi")["text"] == "ok"
