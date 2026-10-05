"""LLM abstraction layer.

The tailoring engine and intake never call an LLM SDK directly. They depend on
the :class:`LLMBackend` protocol so the whole pipeline is testable offline with a
deterministic :class:`FakeLLM`, and swappable for a real bring-your-own-key
Anthropic client in production.
"""

from .base import LLMBackend, FakeLLM

__all__ = ["LLMBackend", "FakeLLM"]
