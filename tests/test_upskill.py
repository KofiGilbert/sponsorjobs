"""Upskill: turn a role's skill GAPS into a short learning plan (2026-08-09).

The plan is on-demand (a button in the job detail), so the model runs only when asked. The LLM
method lives on AnthropicLLM (so BrokerLLM + OpenAILLM inherit it via the shared _complete), with
a deterministic FakeLLM stand-in for tests. These pin the method + the route contract.
"""

from __future__ import annotations

import importlib

import pytest


def test_fake_llm_upskill_plan_one_entry_per_gap():
    from llm.base import FakeLLM
    out = FakeLLM().upskill_plan("Data Engineer", ["Kubernetes", "Airflow"], {})
    assert [p["skill"] for p in out["plan"]] == ["Kubernetes", "Airflow"]
    assert all(p["how"] and p["resource"] for p in out["plan"])
    assert FakeLLM().upskill_plan("X", [], {}) == {"plan": []}   # no gaps -> empty plan


def test_broker_and_openai_inherit_the_method():
    # BrokerLLM/OpenAILLM subclass AnthropicLLM, so one method there serves every backend.
    from llm.anthropic_client import AnthropicLLM
    from llm.broker_client import BrokerLLM
    from llm.openai_client import OpenAILLM
    for cls in (AnthropicLLM, BrokerLLM, OpenAILLM):
        assert callable(getattr(cls, "upskill_plan", None))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A


def test_upskill_route_returns_a_plan_for_gaps(app):
    r = app.app.test_client().post("/api/jobs/upskill",
                                   json={"gaps": ["Kubernetes", "Airflow", "Terraform"],
                                         "role": "Data Engineer"})
    assert r.status_code == 200
    plan = r.get_json()["plan"]
    assert [p["skill"] for p in plan] == ["Kubernetes", "Airflow", "Terraform"]


def test_upskill_route_empty_gaps_is_empty_plan(app):
    d = app.app.test_client().post("/api/jobs/upskill", json={"gaps": [], "role": "X"}).get_json()
    assert d == {"plan": []}


def test_upskill_never_dead_ends_when_the_model_returns_nothing(app, monkeypatch):
    # If the LLM comes back empty or the AI service is down, the user must still get an honest,
    # actionable plan for their gaps -- not the "couldn't build a plan" dead-end.
    class Empty:
        def upskill_plan(self, role, gaps, profile):
            return {"plan": []}                                  # model returned nothing usable
    monkeypatch.setattr(app, "_make_llm", lambda: Empty())
    plan = app.app.test_client().post("/api/jobs/upskill",
                                      json={"gaps": ["Kafka", "Spark"], "role": "DE"}).get_json()["plan"]
    assert [p["skill"] for p in plan] == ["Kafka", "Spark"]
    assert all(p["how"] and p["resource"] for p in plan)

    class Down:
        def upskill_plan(self, role, gaps, profile):
            raise RuntimeError("broker down")
    monkeypatch.setattr(app, "_make_llm", lambda: Down())
    r = app.app.test_client().post("/api/jobs/upskill", json={"gaps": ["Kafka"], "role": "DE"})
    assert r.status_code == 200 and r.get_json()["plan"][0]["skill"] == "Kafka"


def test_anthropic_upskill_plan_falls_back_on_empty_completion():
    # The fallback lives on AnthropicLLM (so Broker/OpenAI inherit it): an empty completion still
    # yields one honest entry per gap.
    from llm.anthropic_client import AnthropicLLM
    llm = AnthropicLLM.__new__(AnthropicLLM)                     # skip __init__ (no API key needed)
    llm._complete = lambda *a, **k: ""                          # model returns nothing parseable
    out = llm.upskill_plan("Data Engineer", ["Kubernetes"], {})
    assert [p["skill"] for p in out["plan"]] == ["Kubernetes"]
    assert out["plan"][0]["how"] and out["plan"][0]["resource"]


# --- smarter upskill (feature #4): must-haves first + priority/effort/leverage fields ------------

def test_fake_llm_upskill_orders_must_haves_first_and_labels_them():
    from llm.base import FakeLLM
    out = FakeLLM().upskill_plan("Data Engineer", ["Airflow", "Kubernetes", "Docker"],
                                 {}, required=["kubernetes"])
    # The must-have is pulled to the front and labelled; the rest keep their order.
    assert [p["skill"] for p in out["plan"]] == ["Kubernetes", "Airflow", "Docker"]
    assert out["plan"][0]["priority"] == "must-have"
    assert all(p["priority"] == "nice-to-have" for p in out["plan"][1:])
    assert all(p["effort"] for p in out["plan"])          # every step carries an effort estimate


def test_anthropic_upskill_sets_priority_server_side_and_sorts():
    # Priority is decided from the required set, not from what the model echoes, and must-haves sort
    # to the front even if the model returned them last.
    from llm.anthropic_client import AnthropicLLM
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm._complete = lambda *a, **k: (
        '{"plan":[{"skill":"Airflow","effort":"a weekend","how":"Build a DAG.","resource":"docs",'
        '"leverage":""},{"skill":"Kubernetes","effort":"a week","how":"Deploy an app.",'
        '"resource":"a free course","leverage":"you know Docker, so this is close"}]}')
    out = llm.upskill_plan("DE", ["Airflow", "Kubernetes"], {}, required=["Kubernetes"])
    assert [p["skill"] for p in out["plan"]] == ["Kubernetes", "Airflow"]   # must-have first
    assert out["plan"][0]["priority"] == "must-have"
    assert out["plan"][0]["leverage"] and out["plan"][1]["priority"] == "nice-to-have"


def test_upskill_route_prioritizes_required(app):
    r = app.app.test_client().post("/api/jobs/upskill",
                                   json={"gaps": ["Airflow", "Kubernetes"], "role": "DE",
                                         "required": ["Kubernetes"]})
    plan = r.get_json()["plan"]
    assert plan[0]["skill"] == "Kubernetes" and plan[0]["priority"] == "must-have"


def test_upskill_fallback_still_prioritizes_required(app, monkeypatch):
    # Even the model-down fallback labels and sorts must-haves first.
    class Down:
        def upskill_plan(self, role, gaps, profile, required=None):
            raise RuntimeError("broker down")
    monkeypatch.setattr(app, "_make_llm", lambda: Down())
    plan = app.app.test_client().post("/api/jobs/upskill",
                                      json={"gaps": ["Airflow", "Kubernetes"], "role": "DE",
                                            "required": ["Kubernetes"]}).get_json()["plan"]
    assert plan[0]["skill"] == "Kubernetes" and plan[0]["priority"] == "must-have"
    assert all(p["effort"] for p in plan)
