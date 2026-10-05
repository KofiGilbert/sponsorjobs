"""Telegram replies never paste a raw provider error, and a stale broker token recovers."""
from __future__ import annotations

import ui.app as app


class _Err(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


def test_out_of_credit_reads_as_an_instruction_not_a_json_dump():
    exc = _Err("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
               "'message': 'Your credit balance is too low to access the Anthropic API.'}}", 400)
    msg = app._friendly_failure("Senior Product Manager at Lindy", exc)
    assert "out of credit" in msg and "console.anthropic.com" in msg
    assert "{" not in msg and "invalid_request_error" not in msg


def test_an_unknown_error_is_generic_and_clean():
    msg = app._friendly_failure("X at Y", RuntimeError("Traceback ... secret detail {json}"))
    assert "secret detail" not in msg and "{" not in msg


def test_a_401_re_registers_only_when_the_identity_route_also_rejects(monkeypatch):
    import requests
    creds = {"TAILOR_ACCOUNT_TOKEN": "old"}
    monkeypatch.setattr(app, "_cred", lambda k: creds.get(k))
    monkeypatch.setattr(app, "_revoke_cred", lambda k: creds.pop(k, None))
    monkeypatch.setattr(app, "_save_cred", lambda k, v: creds.__setitem__(k, v))

    class R:
        def __init__(self, code, body=None): self.status_code, self._b = code, body or {}
        def json(self): return self._b

    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(("GET", url, headers))
        tok = (headers or {}).get("Authorization", "")
        return R(200, {"ok": True}) if tok.endswith("new") else R(401)

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, headers))
        if url.endswith("/account/register"):
            return R(200, {"token": "new"})
        return R(200, {"ok": True}) if (headers or {}).get("Authorization", "").endswith("new") else R(401)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    code, body = app._broker_post("/notify/telegram/link", {})
    assert code == 200 and creds["TAILOR_ACCOUNT_TOKEN"] == "new"


def test_a_401_with_a_valid_identity_never_orphans_the_account(monkeypatch):
    import requests
    creds = {"TAILOR_ACCOUNT_TOKEN": "paid"}
    monkeypatch.setattr(app, "_cred", lambda k: creds.get(k))
    monkeypatch.setattr(app, "_revoke_cred", lambda k: creds.pop(k, None))

    class R:
        def __init__(self, code): self.status_code = code
        def json(self): return {}

    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: R(200))
    monkeypatch.setattr(requests, "post", lambda url, json=None, headers=None, timeout=None: R(401))
    code, _ = app._broker_post("/some/route", {})
    assert code == 401 and creds["TAILOR_ACCOUNT_TOKEN"] == "paid"
