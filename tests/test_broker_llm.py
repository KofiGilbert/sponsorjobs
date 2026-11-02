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


# ---- a COMPANY-side outage (worker/src/outage.ts): ours, said plainly, never an upgrade ------- #

def _http(code: int, body: bytes):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))
    return fake_urlopen


def test_company_outage_is_tagged_ai_company_with_the_plain_message(monkeypatch):
    # The broker's structured answer when the company key has no credit or is disabled.
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        _http(503, b'{"error":"ai_unavailable","reason":"company"}'))
    with pytest.raises(BrokerUnavailable) as exc:
        BrokerLLM()._post({"prompt": "p"})
    assert exc.value.reason == "ai_company"
    assert str(exc.value) == bc.AI_COMPANY_TEXT
    assert "pass and packages are not affected" in str(exc.value)
    assert exc.value.retry_after is None
    # Nothing the person could buy or add, and none of the provider's words.
    low = str(exc.value).lower()
    for word in ("upgrade", "key", "credit", "billing", "anthropic", "connection"):
        assert word not in low
    assert "SponsorJobs' AI is temporarily unavailable" in str(exc.value)


def test_transient_outage_is_retried_three_times_then_tagged_ai_transient(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 503, "err", {}, io.BytesIO(
            b'{"error":"ai_unavailable","reason":"transient","retry_after":0}'))
    slept = []
    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bc.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(BrokerUnavailable) as exc:
        BrokerLLM()._post({"prompt": "p"})
    assert len(calls) == bc.TRANSIENT_TRIES == 3
    assert slept == [0, 0]                           # retry_after honoured (0 here)
    assert exc.value.reason == "ai_transient"
    assert exc.value.retry_after == 0
    assert str(exc.value) == bc.AI_TRANSIENT_TEXT


def test_transient_outage_that_clears_on_retry_succeeds_quietly(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 2:
            raise urllib.error.HTTPError("u", 503, "err", {}, io.BytesIO(
                b'{"error":"ai_unavailable","reason":"transient","retry_after":30}'))
        return _Resp(b'{"text":"fine"}')
    slept = []
    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bc.time, "sleep", lambda s: slept.append(s))
    assert BrokerLLM()._post({"prompt": "p"}) == "fine"
    assert slept == [bc.TRANSIENT_MAX_WAIT]          # a long retry_after is capped


def test_a_company_outage_is_never_retried(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 503, "err", {}, io.BytesIO(b'{"error":"ai_unavailable","reason":"company"}'))
    monkeypatch.setattr(bc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(BrokerUnavailable):
        BrokerLLM()._post({"prompt": "p"})
    assert len(calls) == 1


def test_the_persons_own_cap_keeps_its_upgrade_reason(monkeypatch):
    # A 402 token cap / package limit is the person's plan, not our outage: still the paywall.
    monkeypatch.setattr(bc.urllib.request, "urlopen",
                        _http(402, b'{"error":"this pass is used up","reason":"token_cap"}'))
    with pytest.raises(BrokerUnavailable) as exc:
        BrokerLLM()._post({})
    assert exc.value.reason == "upgrade_required"


def test_outage_texts_have_no_dashes_and_say_resume_not_cv():
    import re
    for text in (bc.AI_COMPANY_TEXT, bc.AI_TRANSIENT_TEXT):
        assert not re.search(r"[–—]", text)
        assert " CV" not in text


def test_app_surfaces_pass_the_company_reason_through_untouched(monkeypatch):
    import ui.app as A
    exc = BrokerUnavailable(bc.AI_COMPANY_TEXT, "ai_company")
    assert A._classify_outage(exc) == "ai_company"
    assert A._classify_outage(BrokerUnavailable(bc.AI_TRANSIENT_TEXT, "ai_transient")) == "ai_transient"
    # The Telegram "tailor and queue" reply and the file import use the same plain words.
    msg = A._friendly_failure("Analyst at Acme", exc)
    assert msg == "I couldn't tailor Analyst at Acme: " + bc.AI_COMPANY_TEXT
    for word in ("upgrade", "key", "credit", "connection", "needs you"):
        assert word not in msg.lower()
    assert A._FAILURE_TEXT["ai_company"] == bc.AI_COMPANY_TEXT
    # The app's error wrapper hands the UI the structured reason (the overlay keys off it).
    @A._guard
    def boom():
        raise exc
    with A.app.test_request_context():
        resp, status = boom()
    assert status == 503
    assert resp.get_json() == {"error": bc.AI_COMPANY_TEXT, "reason": "ai_company"}


def test_telegram_assistant_says_the_plain_message_on_a_company_outage(monkeypatch):
    import ui.app as A
    class Down:
        def app_assistant(self, system, user):
            raise BrokerUnavailable(bc.AI_COMPANY_TEXT, "ai_company")
    monkeypatch.setattr(A, "_make_llm", lambda *a, **k: Down())
    monkeypatch.setattr(A, "_palace", lambda: None)
    monkeypatch.setattr(A, "_assistant_summary", lambda: "")
    assert A._annalisa_reply("what jobs fit me?") == bc.AI_COMPANY_TEXT
    class Busy(Down):
        def app_assistant(self, system, user):
            raise BrokerUnavailable(bc.AI_TRANSIENT_TEXT, "ai_transient")
    monkeypatch.setattr(A, "_make_llm", lambda *a, **k: Busy())
    assert A._annalisa_reply("what jobs fit me?") == bc.AI_TRANSIENT_TEXT
    class Dead(Down):
        def app_assistant(self, system, user):
            raise RuntimeError("no key")
    monkeypatch.setattr(A, "_make_llm", lambda *a, **k: Dead())
    assert A._annalisa_reply("what jobs fit me?") == ""          # the old "help" path is unchanged
