"""Broker entrypoint (backend/server.py): build_broker assembles a working app over the real
persistent store, and _cred resolves keys from the credentials file. Offline: a FakeProvider is
injected and the DB is a tmp file.
"""

from __future__ import annotations

from backend.providers import FakeProvider
from backend.server import _cred, build_broker

H = {"X-Tailor-User": "u1"}


def test_build_broker_returns_a_working_app_over_sqlite(tmp_path):
    app = build_broker(db_path=str(tmp_path / "b.db"), provider=FakeProvider())
    app.config.update(TESTING=True)
    c = app.test_client()

    # a real end-to-end slice through the assembled app: upgrade, start avatar, meter, /me
    c.post("/billing/plan", json={"plan": "pass30"}, headers=H)
    assert c.post("/avatar/session/start", json={}, headers=H).status_code == 200
    assert c.post("/avatar/heartbeat", json={"seconds": 2700}, headers=H).get_json()["stop"] is True
    body = c.get("/me/usage", headers=H).get_json()
    assert body["plan"] == "pass30" and body["avatar_seconds_left"] == 0


def test_injected_provider_marks_the_app_as_not_real(tmp_path):
    app = build_broker(db_path=str(tmp_path / "b.db"), provider=FakeProvider())
    assert app.config["BROKER_REAL_PROVIDERS"] is False


def test_dev_plan_seeds_the_local_user_so_the_avatar_works_on_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("TAILOR_DEV_PLAN", "pass30")             # what the Electron shell sets in dev
    app = build_broker(db_path=str(tmp_path / "b.db"), provider=FakeProvider())
    app.config.update(TESTING=True)
    body = app.test_client().get("/me/usage", headers={"X-Tailor-User": "local"}).get_json()
    assert body["plan"] == "pass30" and body["avatar_seconds_left"] == 2700   # minutes ready, no setup
    # a restart does not stack another dev pass on top
    app2 = build_broker(db_path=str(tmp_path / "b.db"), provider=FakeProvider())
    app2.config.update(TESTING=True)
    body2 = app2.test_client().get("/me/usage", headers={"X-Tailor-User": "local"}).get_json()
    assert body2["avatar_seconds_left"] == 2700 and body2["pass_until"] == body["pass_until"]


def test_billing_needs_a_pass_price_and_reads_the_pass_and_pack_envs(monkeypatch):
    from backend.server import build_billing
    from backend.metering import InMemoryStore, Meter
    for k in ("STRIPE_SECRET_KEY", "STRIPE_TEST_SECRET_KEY", "STRIPE_PRICE_PASS30", "STRIPE_PRICE_PASS90",
              "STRIPE_PRICE_PACK_1", "STRIPE_PRICE_PACK_3", "STRIPE_PRICE_PACK_5"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", "/nonexistent/credentials.env")
    monkeypatch.setenv("STRIPE_TEST_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_STUDENT", "price_old")          # retired: never enough on its own
    assert build_billing(Meter(InMemoryStore())) is None
    import sys, types
    monkeypatch.setitem(sys.modules, "stripe", types.SimpleNamespace(api_key=None))
    monkeypatch.setenv("STRIPE_PRICE_PASS30", "price_p30")
    monkeypatch.setenv("STRIPE_PRICE_PACK_5", "price_k5")
    b = build_billing(Meter(InMemoryStore()))
    assert [p["id"] for p in b.offered_passes()] == ["pass30"]
    assert [p["id"] for p in b.offered_packs()] == ["pack_5"]


def test_no_dev_plan_leaves_the_local_user_on_free(tmp_path, monkeypatch):
    monkeypatch.delenv("TAILOR_DEV_PLAN", raising=False)
    app = build_broker(db_path=str(tmp_path / "b.db"), provider=FakeProvider())
    app.config.update(TESTING=True)
    body = app.test_client().get("/me/usage", headers={"X-Tailor-User": "local"}).get_json()
    assert body["plan"] == "free"


def test_cred_reads_from_the_credentials_file(tmp_path, monkeypatch):
    cred = tmp_path / "credentials.env"
    cred.write_text('OTHER=x\nTAVUS_API_KEY="tav-123"\n')
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(cred))
    monkeypatch.delenv("TAVUS_API_KEY", raising=False)
    assert _cred("TAVUS_API_KEY") == "tav-123"
    assert _cred("MISSING") is None


def test_env_var_wins_over_the_file(tmp_path, monkeypatch):
    cred = tmp_path / "credentials.env"
    cred.write_text("TAVUS_API_KEY=from-file\n")
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(cred))
    monkeypatch.setenv("TAVUS_API_KEY", "from-env")
    assert _cred("TAVUS_API_KEY") == "from-env"
