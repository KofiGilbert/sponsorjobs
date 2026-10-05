"""Connections: connect Telegram from the UI instead of hand-editing credentials.env.

Acceptance: status reports configured/not; saving verifies the token via Telegram's getMe
(mocked here, no network) and persists token + chat id; a rejected token errors; disconnect
clears it.
"""

import importlib

import pytest


@pytest.fixture()
def app_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    import ui.app as A
    importlib.reload(A)
    return A


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_telegram_connect_flow(app_mod, monkeypatch):
    A = app_mod
    c = A.app.test_client()

    assert c.get("/api/telegram/status").get_json()["configured"] is False

    # missing fields
    assert c.post("/api/telegram", json={"token": "", "chat_id": ""}).status_code == 400

    # valid token (getMe returns ok) -> saved, chat masked to last 4
    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=8: _Resp({"ok": True, "result": {"username": "mybot"}}))
    r = c.post("/api/telegram", json={"token": "123:ABC", "chat_id": "987654"})
    assert r.status_code == 200 and r.get_json()["bot"] == "mybot"
    assert A._cred("TELEGRAM_BOT_TOKEN") == "123:ABC" and A._cred("TELEGRAM_CHAT_ID") == "987654"
    st = c.get("/api/telegram/status").get_json()
    assert st["configured"] and st["chat"] == "7654"

    # rejected token (getMe ok:false)
    monkeypatch.setattr(requests, "get", lambda url, timeout=8: _Resp({"ok": False}))
    assert c.post("/api/telegram", json={"token": "bad", "chat_id": "1"}).status_code == 400

    # disconnect clears it
    assert c.post("/api/telegram/disconnect").status_code == 200
    assert c.get("/api/telegram/status").get_json()["configured"] is False
