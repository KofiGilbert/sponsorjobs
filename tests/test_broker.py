"""End-to-end skeleton test for the managed-AI broker (backend/broker.py).

Fully offline: FakeProvider + InMemoryStore + a fixed billing period, no real keys and no
network. This proves the exact plumbing the real Tavus/Anthropic providers slot into at P2/P3:
identify the user -> check the plan quota -> call the provider -> meter what was used -> hard
stop at zero. Swapping FakeProvider for the real one must not change any of this.
"""

from __future__ import annotations

import pytest

from backend.broker import create_app
from backend.metering import InMemoryStore, Meter
from backend.providers import FakeProvider

H = {"X-Tailor-User": "u1"}


@pytest.fixture
def client():
    meter = Meter(InMemoryStore())
    app = create_app(meter=meter, provider=FakeProvider(), period_fn=lambda: "2026-07")
    app.config.update(TESTING=True)
    c = app.test_client()
    c.meter = meter
    return c


def test_a_request_with_no_user_is_rejected():
    app = create_app(period_fn=lambda: "2026-07")
    assert app.test_client().post("/avatar/session/start", json={}).status_code == 401


def test_free_user_cannot_start_an_avatar_session(client):
    r = client.post("/avatar/session/start", json={}, headers=H)
    assert r.status_code == 402 and r.get_json()["remaining"] == 0
    assert r.get_json()["reason"] == "pass_required"


def test_pass_holder_starts_a_session_meters_down_and_hard_stops(client):
    client.post("/billing/plan", json={"plan": "pass30"}, headers=H)

    r = client.post("/avatar/session/start", json={"context": {"role": "SWE"}}, headers=H)
    body = r.get_json()
    assert r.status_code == 200
    assert body["session_url"].startswith("https://fake.local/") and body["remaining"] == 2700

    assert client.post("/avatar/heartbeat", json={"seconds": 2640}, headers=H).get_json() \
        == {"remaining": 60, "stop": False}
    # asks for 2 more minutes but only 1 is left: consume what's there, then hard stop
    assert client.post("/avatar/heartbeat", json={"seconds": 120}, headers=H).get_json() \
        == {"remaining": 0, "stop": True}
    # a fresh session is now blocked until they buy credits
    assert client.post("/avatar/session/start", json={}, headers=H).status_code == 402


def test_credits_top_up_reopens_the_avatar(client):
    client.post("/billing/plan", json={"plan": "pass30"}, headers=H)
    client.post("/avatar/heartbeat", json={"seconds": 2700}, headers=H)     # exhaust the pass
    assert client.post("/avatar/session/start", json={}, headers=H).status_code == 402
    client.post("/billing/credits", json={"seconds": 300}, headers=H)       # 5 min: not a full interview
    assert client.post("/avatar/session/start", json={}, headers=H).status_code == 402
    client.post("/billing/credits", json={"seconds": 600}, headers=H)       # now 900 s = one interview
    r = client.post("/avatar/session/start", json={}, headers=H)
    assert r.status_code == 200 and r.get_json()["remaining"] == 900


def test_llm_complete_meters_tokens_picks_the_plan_model_and_free_cap_blocks(client):
    # free user over the monthly safety cap is blocked
    client.meter.record_llm("u1", "2026-07", 400_001)
    r = client.post("/llm/complete", json={"prompt": "hi"}, headers=H)
    assert r.status_code == 402 and r.get_json()["reason"] == "token_cap"
    # a pass has a far higher cap and runs on the better model
    client.post("/billing/plan", json={"plan": "pass30"}, headers=H)
    body = client.post("/llm/complete",
                       json={"prompt": "tailor my resume for a data role"}, headers=H).get_json()
    assert body["model"] == "claude-sonnet-4-6" and "fake:" in body["text"]
    assert body["tokens"]["used"] > 0


def test_me_usage_reports_tier_pass_and_balances(client):
    assert client.get("/me/usage", headers=H).get_json() == {
        "plan": "free", "tier": "free", "pass_until": None, "interviews_left": 0, "packages_left": 3,
        "avatar_seconds_left": 0, "llm_model": "claude-haiku-4-5"}
    client.post("/billing/plan", json={"plan": "pass90"}, headers=H)
    body = client.get("/me/usage", headers=H).get_json()
    assert body["plan"] == body["tier"] == "pass90" and body["pass_until"]
    assert body["avatar_seconds_left"] == 8100 and body["interviews_left"] == 9
    assert body["packages_left"] == 150 and body["llm_model"] == "claude-sonnet-4-6"


def test_package_route_counts_runs_and_stops_at_the_free_three(client):
    for left in (2, 1, 0):
        r = client.post("/llm/package", headers=H)
        assert r.status_code == 200 and r.get_json()["packages_left"] == left
        assert r.get_json()["model"] == "claude-haiku-4-5" and r.get_json()["tier"] == "free"
    r = client.post("/llm/package", headers=H)
    assert r.status_code == 402 and r.get_json()["reason"] == "package_limit"
    assert r.get_json()["tier"] == "free"
    client.post("/billing/plan", json={"plan": "pass30"}, headers=H)
    r = client.post("/llm/package", headers=H)
    assert r.status_code == 200 and r.get_json()["packages_left"] == 59
    assert r.get_json()["model"] == "claude-sonnet-4-6"
    assert client.post("/llm/package").status_code == 401


def test_unknown_plan_is_rejected(client):
    assert client.post("/billing/plan", json={"plan": "platinum"}, headers=H).status_code == 400


# -- QA hardening: robustness + margin protection -------------------------------------------------
def test_heartbeat_rejects_bad_seconds_and_clamps_negative(client):
    client.post("/billing/plan", json={"plan": "pass30"}, headers=H)
    assert client.post("/avatar/heartbeat", json={"seconds": "abc"}, headers=H).status_code == 400
    r = client.post("/avatar/heartbeat", json={"seconds": -50}, headers=H)   # clamped to 0, not a 500
    assert r.status_code == 200 and r.get_json()["remaining"] == 2700


def test_free_tier_llm_cap_blocks_an_oversized_single_prompt(client):
    # free user (default): a prompt whose estimated input alone exceeds the cap is rejected BEFORE
    # the provider is called, so the company key is never spent (the old code let one huge overage through).
    assert client.post("/llm/complete", json={"prompt": "x" * 2_000_000}, headers=H).status_code == 402


def test_llm_complete_forwards_system_history_and_effort_to_the_provider():
    # the bundled app calls carry a system prompt + a multi-turn history; the broker must pass them
    # through so tailoring quality matches the local AnthropicLLM.
    class Recorder(FakeProvider):
        def complete(self, model, prompt="", **kw):
            self.seen = {"prompt": prompt, **kw}
            return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    rec = Recorder()
    app = create_app(meter=Meter(InMemoryStore()), provider=rec, period_fn=lambda: "2026-07")
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/billing/plan", json={"plan": "pass30"}, headers=H)     # a pass: high cap
    msgs = [{"role": "user", "content": "hi"}]
    c.post("/llm/complete", json={"system": "Be terse", "messages": msgs,
                                  "max_tokens": 321, "effort": "low"}, headers=H)
    assert rec.seen["system"] == "Be terse"
    assert rec.seen["messages"] == msgs
    assert rec.seen["max_tokens"] == 321
    assert rec.seen["effort"] == "low"


def test_llm_complete_meters_a_history_payload_by_its_total_size(client):
    # a big multi-turn history must count against a capped free plan the same as a big prompt would
    client.meter.record_llm("u1", "2026-07", 400_001)
    huge = [{"role": "user", "content": "x" * 800_000}]
    assert client.post("/llm/complete", json={"messages": huge}, headers=H).status_code == 402


def test_provider_failure_fails_clean_not_500_and_leaks_nothing():
    class Boom:
        def start_avatar_session(self, u, c=None): raise RuntimeError("secret sk-ant-leak")
        def complete(self, m, p, **k): raise RuntimeError("secret sk-ant-leak")
    meter = Meter(InMemoryStore()); meter.set_plan("u1", "pass30")
    app = create_app(meter=meter, provider=Boom(), period_fn=lambda: "2026-07")
    app.config.update(TESTING=True)
    c = app.test_client()
    for path, body in [("/avatar/session/start", {}), ("/llm/complete", {"prompt": "hi"})]:
        r = c.post(path, json=body, headers=H)
        assert r.status_code == 502 and "sk-ant" not in r.get_data(as_text=True)
