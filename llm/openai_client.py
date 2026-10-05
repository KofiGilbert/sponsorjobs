"""Bring-your-own-key OpenAI backend.

Tailor is Claude-first (the prompts are tuned for Claude, and Claude is the recommended
default). This is the OpenAI alternative for people who already have an OpenAI/ChatGPT key
and would rather use it. It reuses EVERY prompt method from :class:`AnthropicLLM` unchanged,
the whole LLM interface funnels through ``_complete`` / ``_complete_history``, so only the
transport (client construction and those two calls) differs. That keeps the two backends
behaviour-identical apart from the model itself.

Like the Anthropic client, this is import-safe without the ``openai`` package or a key; it
only fails when actually constructed.
"""

from __future__ import annotations

import os
from pathlib import Path

from .anthropic_client import AnthropicLLM

# A solid, widely available default. Overridable via RESUME_AGENT_OPENAI_MODEL.
DEFAULT_OPENAI_MODEL = "gpt-4o"


def _load_openai_key() -> str | None:
    """OPENAI_API_KEY from the environment or the same git-ignored credentials file the
    app saves to (SAME resolution as the Anthropic loader)."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    root = Path(__file__).resolve().parents[1]
    env_file = Path(os.environ.get("RESUME_AGENT_CRED_FILE") or (root / "config" / "credentials.env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "OPENAI_API_KEY":
                return v.strip().strip('"').strip("'")
    return None


class OpenAILLM(AnthropicLLM):
    """OpenAI transport. Inherits all of :class:`AnthropicLLM`'s prompt methods; overrides
    only construction and the two completion calls. Does NOT call ``super().__init__`` (that
    would require the anthropic package + an Anthropic key)."""

    def __init__(self, model: str | None = None, effort: str = "low",
                 api_key: str | None = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ImportError(
                "The 'openai' package is required for OpenAILLM. Install it with: "
                "pip install openai"
            ) from exc
        key = api_key or _load_openai_key()
        if not key:
            raise RuntimeError(
                "No OPENAI_API_KEY found. Set it in the environment or in "
                "config/credentials.env (git-ignored)."
            )
        self.model = model or os.environ.get("RESUME_AGENT_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        # Low temperature stands in for Anthropic's low-effort setting: this is faithful
        # rephrasing, not open-ended reasoning (CLAUDE.md §5).
        self.effort = effort
        self._client = OpenAI(api_key=key)

    def _complete(self, system: str, user: str, max_tokens: int = 1024,
                  effort: str | None = None) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=0.3,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return (resp.choices[0].message.content or "").strip()

    def _complete_history(self, system, history, max_tokens: int = 1500,
                          effort: str | None = None) -> str:
        messages = [{"role": "system", "content": system}]
        for h in history:
            role = "assistant" if h.get("role") == "agent" else "user"
            content = h.get("content", "")
            if content:
                messages.append({"role": role, "content": content})
        resp = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, temperature=0.3, messages=messages,
        )
        return (resp.choices[0].message.content or "").strip()
