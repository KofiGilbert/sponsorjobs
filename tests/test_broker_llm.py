"""BrokerLLM (llm/broker_client.py): the bundled-AI path.

Verifies the two low-level funnels build the right /llm/complete payloads and that the transport
parses the reply, forwards the user header, and turns broker errors into a clean, user-facing
BrokerUnavailable. No network: the broker is stubbed at the urllib boundary.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

import llm.broker_client as bc
from llm.broker_client import BrokerLLM, BrokerUnavailable


def test_complete_posts_system_prompt_and_knobs():
    llm = BrokerLLM(model="m")
    sent = {}
    llm._post = lambda payload: sent.update(payload) or "OK"
    out = llm._complete("SYS", "USER", max_tokens=200, effort="low")
    assert out == "OK"
    assert sent == {"system": "SYS", "prompt": "USER", "max_tokens": 200, "effort": "low"}


def test_complete_history_maps_agent_to_assistant_and_forces_user_first():
    llm = BrokerLLM()
    sent = {}
    llm._post = lambda payload: sent.update(payload) or "R"
    llm._complete_history("S", [{"role": "agent", "content": "hi"},
                                {"role": "user", "content": "yo"}], max_tokens=1500)
    msgs = sent["messages"]
    assert msgs[0]["role"] == "user"                        # a synthetic user turn is prepended
    assert {"role": "assistant", "content": "hi"} in msgs  # 'agent' maps to 'assistant'
    assert sent["effort"] == "low"                          # falls back to the instance default


def test_a_real_specialised_method_flows_through_the_funnel():
    # shorten_bullet is inherited from AnthropicLLM and calls _complete; overriding _complete alone
    # must reroute it to the broker with no per-method work.
    llm = BrokerLLM()
    seen = {}
    llm._post = lambda payload: seen.update(payload) or "a shorter bullet"
    out = llm.shorten_bullet("a rather long bullet that should be trimmed", 20)
    assert out == "a shorter bullet"
    assert "prompt" in seen and seen["system"]


class _Resp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status = 200


def test_post_parses_text_hits_the_right_url_and_sends_the_user_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["header"] = req.get_header("X-tailor-user")
        captured["body"] = req.data
        return _Resp(b'{"text":" hi "}')

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    out = BrokerLLM(broker_url="http://x", user="u9")._post({"prompt": "p"})
    assert out == "hi"                                       # parsed and stripped
    assert captured["url"] == "http://x/llm/complete"
    assert captured["header"] == "u9"


def test_post_sends_a_bearer_token_when_set_instead_of_the_dev_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        captured["user"] = req.get_header("X-tailor-user")
        return _Resp(b'{"text":"ok"}')

    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    BrokerLLM(auth_token="tok_abc")._post({"prompt": "p"})
    assert captured["auth"] == "Bearer tok_abc"
    assert captured["user"] is None                 # the account token replaces the dev header


def test_post_quota_402_is_tagged_upgrade_required(monkeypatch):
    # a plan-allowance 402 must surface as a paywall (upgrade), NOT a hard error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("u", 402, "Payment Required", {},
                                     io.BytesIO(b'{"error":"out of plan allowance"}'))
    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(BrokerUnavailable) as exc:
        BrokerLLM()._post({})
    assert "out of plan allowance" in str(exc.value)
    assert exc.value.reason == "upgrade_required"


def test_post_connection_refused_is_tagged_service_down(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(BrokerUnavailable) as exc:
        BrokerLLM()._post({})
    assert exc.value.reason == "service_down"


def test_app_classifies_the_broker_paywall_as_upgrade_required():
    # the app's outage classifier must trust the broker's own tag, so the UI shows the plans
    # panel (upgrade) rather than the 'your Anthropic credit ran out' (no_credit) screen.
    import ui.app as A
    assert A._classify_outage(BrokerUnavailable("used up", "upgrade_required")) == "upgrade_required"
    assert A._classify_outage(BrokerUnavailable("down", "service_down")) == "service_down"
