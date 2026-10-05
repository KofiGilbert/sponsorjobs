"""The provider slot the broker forwards to: the real AI/avatar services in production, a Fake
under test.

The broker NEVER talks to Anthropic or Tavus directly. It talks to a ``Provider``. In production
we plug in the real one; in tests and offline dev we plug in ``FakeProvider`` (the same pattern
as the app's FakeLLM). The real ``TavusProvider`` / ``AnthropicProvider`` are written in P2/P3
once the company keys exist; they implement this SAME interface, so they drop into the already
tested broker with no other changes. The fake never ships to users.
"""

from __future__ import annotations


class Provider:
    def start_avatar_session(self, user: str, context: dict | None = None) -> dict:
        """Mint an interview session on the avatar service. Real (Tavus): the broker POSTs
        /v2/conversations with the server-side company key and gets back a conversation_url and a
        short-lived meeting_token; the user's browser then streams peer-to-peer with Tavus, never
        through the broker. Returns {session_url, token, provider_session_id}."""
        raise NotImplementedError

    def complete(self, model: str, prompt: str, **kw) -> dict:
        """Run one LLM completion on ``model``. Returns {text, input_tokens, output_tokens}."""
        raise NotImplementedError


class FakeProvider(Provider):
    """Offline stand-in: deterministic, free, no network, no key. Never ships."""

    def __init__(self) -> None:
        self._n = 0

    def start_avatar_session(self, user, context=None):
        self._n += 1
        sid = f"fake-sess-{self._n}"
        return {"session_url": f"https://fake.local/avatar/{sid}",
                "token": f"fake-token-{self._n}",
                "provider_session_id": sid}

    def complete(self, model, prompt, **kw):
        prompt = prompt or ""
        text = f"[fake:{model}] tailored from: {prompt.strip()[:40]}"
        # deterministic token estimate (~1 token / 4 chars), so tests are stable
        return {"text": text,
                "input_tokens": max(1, len(prompt) // 4),
                "output_tokens": max(1, len(text) // 4)}


class CompositeProvider(Provider):
    """Routes avatar calls to one provider and LLM calls to another, so the broker's single
    provider slot can be backed by two real services at once. Production wiring:
    ``CompositeProvider(avatar=TavusProvider(...), llm=AnthropicProvider(...))``."""

    def __init__(self, avatar: Provider, llm: Provider) -> None:
        self.avatar = avatar
        self.llm = llm

    def start_avatar_session(self, user, context=None):
        return self.avatar.start_avatar_session(user, context)

    def complete(self, model, prompt, **kw):
        return self.llm.complete(model, prompt, **kw)
