"""Provider-agnostic model layer: Claude (default) or OpenAI, both BYO-key.

Acceptance: OpenAILLM reuses every AnthropicLLM prompt method (only transport differs);
the factory routes by AI_PROVIDER; the key endpoint saves the right key + remembers the
provider; App Lock custodies both keys.
"""

import importlib
import os

import pytest

from llm.anthropic_client import AnthropicLLM
from llm.openai_client import OpenAILLM


def test_openai_backend_inherits_all_prompt_methods():
    assert issubclass(OpenAILLM, AnthropicLLM)
    for m in ("reword_bullet", "draft_cover_letter", "draft_profile", "extract_profile",
              "answer_screening_questions", "expand_bullets", "extract_intake",
              "ask_enrich", "select_courses", "draft_email_reply"):
        assert getattr(OpenAILLM, m) is getattr(AnthropicLLM, m), f"{m} should be inherited unchanged"
    # constructing the client does not hit the network
    o = OpenAILLM(api_key="sk-dummy")
    assert o.model == "gpt-4o"
    assert o._complete.__qualname__.startswith("OpenAILLM")   # transport overridden


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    import ui.app as A
    importlib.reload(A)
    A._require_connectivity = lambda: None            # don't ping the network in tests
    return A


def test_factory_routes_by_provider(app_mod):
    A = app_mod
    A._save_cred("ANTHROPIC_API_KEY", "sk-ant-x")
    A._save_cred("AI_PROVIDER", "anthropic")
    # Default is the bundled broker path (no user key needed) whenever the broker is reachable.
    A._broker_reachable = lambda: True
    assert type(A._make_llm()).__name__ == "BrokerLLM"
    # Bring-your-own-key: with the broker down (or TAILOR_OWN_KEY set) it runs on the saved key.
    A._broker_reachable = lambda: False
    assert type(A._make_llm()).__name__ == "AnthropicLLM"

    A._save_cred("OPENAI_API_KEY", "sk-oai-x")
    A._save_cred("AI_PROVIDER", "openai")
    assert A._ai_provider() == "openai"
    assert type(A._make_llm()).__name__ == "OpenAILLM"


def test_apikey_endpoint_is_provider_aware(app_mod, monkeypatch):
    A = app_mod
    c = A.app.test_client()
    monkeypatch.setattr(A, "_validate_openai_key", lambda k: (True, ""))
    monkeypatch.setattr(A, "_validate_anthropic_key", lambda k: (True, ""))

    # save an OpenAI key -> stored under OPENAI_API_KEY, provider remembered
    r = c.post("/api/apikey", json={"key": "sk-oai-abc", "provider": "openai"})
    assert r.status_code == 200 and r.get_json()["provider"] == "openai"
    assert A._cred("OPENAI_API_KEY") == "sk-oai-abc"
    assert A._ai_provider() == "openai"
    st = c.get("/api/apikey").get_json()
    assert st["provider"] == "openai" and st["provider_label"] == "OpenAI" and st["configured"]

    # switch back to Anthropic
    r = c.post("/api/apikey", json={"key": "sk-ant-abc", "provider": "anthropic"})
    assert r.get_json()["provider"] == "anthropic"
    assert c.get("/api/apikey").get_json()["provider"] == "anthropic"


def test_app_lock_custodies_both_provider_keys(app_mod):
    """App Lock custodies BOTH provider keys — and, since custody expanded, the other
    sensitive secrets too (Telegram/GitHub/Tavus/Adzuna). Provider keys stay a subset."""
    from ui.lock import _CRED_NAMES
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} <= set(_CRED_NAMES)
    # The remote-control and write-access secrets must be custodied, not just the API key.
    for secret in ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "AVATAR_API_KEY"):
        assert secret in _CRED_NAMES
