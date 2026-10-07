"""Editions (decided 2026-10-07). The official installer runs on SponsorJobs' own AI, so the app
never asks for a key while that service is up; the open-source build from GitHub is
bring-your-own-key. The key UI is hidden only while the managed AI is reachable, so an outage
still leaves the own-key path open."""

import importlib

import pytest


@pytest.fixture
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A


def _status(A):
    return A.app.test_client().get("/api/apikey").get_json()


def test_community_build_is_bring_your_own_key(app_mod, monkeypatch):
    monkeypatch.delenv("TAILOR_EDITION", raising=False)
    monkeypatch.setattr(app_mod, "_broker_reachable", lambda: True)
    s = _status(app_mod)
    assert s["edition"] == "community" and s["managed"] is False


def test_official_build_hides_the_key_only_while_the_company_ai_is_up(app_mod, monkeypatch):
    monkeypatch.setenv("TAILOR_EDITION", "official")
    monkeypatch.setattr(app_mod, "_broker_reachable", lambda: True)
    assert _status(app_mod)["managed"] is True
    monkeypatch.setattr(app_mod, "_broker_reachable", lambda: False)
    assert _status(app_mod)["managed"] is False          # outage: the own-key path stays open


def test_the_broker_probe_is_remembered_for_a_minute(app_mod, monkeypatch):
    import urllib.request
    calls = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true, "real_providers": true}'

    monkeypatch.setenv("TAILOR_EDITION", "official")
    monkeypatch.setenv("TAILOR_BROKER_URL", "https://broker.test")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or Resp())
    app_mod._BROKER_UP.clear()
    assert app_mod._broker_reachable() and app_mod._broker_reachable()
    assert len(calls) == 1                                # one probe, not one per AI call


def test_official_build_refuses_a_broker_without_a_real_model(app_mod, monkeypatch):
    import urllib.request

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true, "real_providers": false}'

    monkeypatch.setenv("TAILOR_EDITION", "official")
    monkeypatch.setenv("TAILOR_BROKER_URL", "https://fake-broker.test")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    app_mod._BROKER_UP.clear()
    assert app_mod._broker_reachable() is False
