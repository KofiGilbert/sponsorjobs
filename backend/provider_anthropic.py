"""Real Anthropic LLM provider for the broker (P3).

Uses the company/integrated Anthropic key (server-side only) to run the bundled completions and
reports token usage so the broker meters them. The key is resolved the same way the app does
(ANTHROPIC_API_KEY env, else the git-ignored config/credentials.env), kept self-contained here so
the module imports without the anthropic SDK when a client is injected for tests.

The broker picks the model per plan (meter.llm_model_for) and passes it in, so a free user runs on
Haiku and paid users on Sonnet without this provider deciding policy.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.providers import Provider


def _load_anthropic_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cred = os.environ.get("RESUME_AGENT_CRED_FILE")
    path = Path(cred) if cred else Path(__file__).resolve().parents[1] / "config" / "credentials.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# `output_config.effort` is not universal. It is rejected outright by the small/older
# models ("This model does not support the effort parameter") -- notably Haiku 4.5 and
# Sonnet 4.5, which is exactly what a free or entry plan routes to. Listed as the families
# that DO accept it, so an unknown future model degrades to "send no effort" (a working
# request) rather than to a 400.
_EFFORT_CAPABLE_PREFIXES = (
    "claude-fable-", "claude-mythos-",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-5", "claude-sonnet-4-6",
)


def _supports_effort(model: str) -> bool:
    return str(model or "").startswith(_EFFORT_CAPABLE_PREFIXES)


class AnthropicProvider(Provider):
    def __init__(self, api_key: str | None = None, client=None, *, max_tokens: int = 1024) -> None:
        self.max_tokens = max_tokens
        if client is not None:              # injected fake for offline tests
            self._client = client
            return
        import anthropic
        key = api_key or _load_anthropic_key()
        if not key:
            raise ValueError("No Anthropic API key (ANTHROPIC_API_KEY or config/credentials.env)")
        self._client = anthropic.Anthropic(api_key=key)

    def start_avatar_session(self, user, context=None):
        raise NotImplementedError("AnthropicProvider is LLM-only; the avatar uses the Tavus provider")

    def complete(self, model: str, prompt: str = "", **kw) -> dict:
        # The bundled LLM path routes the app's specialised calls through here, so honour the same
        # knobs the local AnthropicLLM uses: a system prompt, a multi-turn message history (for the
        # conversational flows), a per-call token ceiling, and the effort/reasoning level. Falling
        # back to a single user turn keeps the plain {prompt} callers working unchanged.
        messages = kw.get("messages") or [{"role": "user", "content": prompt}]
        create = {
            "model": model,
            "max_tokens": int(kw.get("max_tokens") or self.max_tokens),
            "messages": messages,
        }
        system = kw.get("system")
        if system:
            create["system"] = system
        effort = kw.get("effort")
        if effort and _supports_effort(model):
            create["output_config"] = {"effort": effort}
        try:
            msg = self._client.messages.create(**create)
        except Exception as exc:
            # A model that rejects `effort` must not take the whole request down. The app
            # sends effort on every call, and the plan decides the model -- so when a
            # cheaper tier lands on a model without effort support, EVERY completion 400s
            # and the person simply cannot build a resume. Drop the knob and retry once:
            # effort tunes reasoning depth, it is never load-bearing for correctness.
            if "output_config" in create and "effort" in str(exc).lower():
                create.pop("output_config", None)
                msg = self._client.messages.create(**create)
            else:
                raise
        text = "".join(
            b.text for b in msg.content if getattr(b, "type", "") == "text"
        ).strip()
        usage = getattr(msg, "usage", None)
        return {"text": text,
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0}
