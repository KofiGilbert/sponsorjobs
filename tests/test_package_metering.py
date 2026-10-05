"""App side of package counting (ui/app.py _count_package): one tailoring run on the bundled AI is
one "package", counted by the broker BEFORE any model call. Free gets 3 a month; a pass has its
own balance; the person's own key (BYOK) is never counted and keeps tailoring unlimited.

Offline: the broker is a monkeypatched _broker_post; no network, no Stripe, no model.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import ui.app as A
    importlib.reload(A)
    return A


def _broker_llm():
    from llm.broker_client import BrokerLLM
    return BrokerLLM(broker_url="http://broker.invalid")


def _calls(monkeypatch, A, status, data=None):
    calls = []

    def post(path, body):
        calls.append(path)
        return status, data
    monkeypatch.setattr(A, "_broker_post", post)
    return calls


def test_own_key_and_test_llms_are_never_counted(A, monkeypatch):
    calls = _calls(monkeypatch, A, 402, {"reason": "package_limit"})
    from llm.base import FakeLLM
    llm = FakeLLM()
    assert A._count_package(llm) is llm
    assert calls == []


def test_bundled_run_is_counted_once_and_keeps_the_broker(A, monkeypatch):
    calls = _calls(monkeypatch, A, 200, {"packages_left": 2, "tier": "free"})
    llm = _broker_llm()
    assert A._count_package(llm) is llm
    assert calls == ["/llm/package"]


def test_an_old_or_unreachable_broker_never_blocks_a_run(A, monkeypatch):
    llm = _broker_llm()
    for st in (0, 404, 500):
        _calls(monkeypatch, A, st)
        assert A._count_package(llm) is llm


def test_used_up_with_no_key_shows_the_passes(A, monkeypatch):
    from llm.broker_client import BrokerUnavailable
    _calls(monkeypatch, A, 402, {"reason": "package_limit", "tier": "free"})
    with pytest.raises(BrokerUnavailable) as e:
        A._count_package(_broker_llm())
    assert e.value.reason == "upgrade_required" and "3 free" in str(e.value)
    assert A._classify_outage(e.value) == "upgrade_required"
    _calls(monkeypatch, A, 402, {"reason": "package_limit", "tier": "pass30"})
    with pytest.raises(BrokerUnavailable) as e:
        A._count_package(_broker_llm())
    assert "pass" in str(e.value).lower()


def test_used_up_with_an_own_anthropic_key_runs_on_that_key(A, monkeypatch):
    _calls(monkeypatch, A, 402, {"reason": "package_limit", "tier": "free"})
    A._save_cred("ANTHROPIC_API_KEY", "sk-ant-own")
    sentinel = object()
    monkeypatch.setattr(A, "_make_byo_llm", lambda provider: sentinel if provider == "anthropic" else None)
    assert A._count_package(_broker_llm()) is sentinel


def test_unattended_tailor_reports_the_limit_instead_of_crashing(A, monkeypatch):
    _calls(monkeypatch, A, 402, {"reason": "package_limit", "tier": "free"})
    out = A._tailor_job_unattended({"title": "Data Analyst", "company": "Acme"}, _broker_llm(),
                                   "", "t", "_scratch")
    assert out["ok"] is False and out["upgrade"] is True and out["role"] == "Data Analyst"


def test_session_start_hits_the_paywall_with_the_upgrade_reason(A, monkeypatch):
    _calls(monkeypatch, A, 402, {"reason": "package_limit", "tier": "free"})
    monkeypatch.setattr(A, "_make_llm", _broker_llm)
    r = A.app.test_client().post("/api/session/start", json={"jd": "Data analyst, SQL, Python."})
    assert r.status_code == 503 and r.get_json()["reason"] == "upgrade_required"
