"""The inbox's user-facing surface (Phase 5 completion): status diagnosis, the
verify-by-click endpoint, and the DOUBLE opt-in gate on autonomous verification
(CLAUDE.md §5: auto-completing a verification link is opt-in on top of autonomous)."""

from __future__ import annotations

import ui.app as app

VERIFY_MSG = {
    "id": "m1", "from": "no-reply@greenhouse.io",
    "subject": "Please verify your email to complete your application",
    "date": "Thu, 17 Jul 2026",
    "body": "Confirm your application: https://boards.greenhouse.io/verify?t=abc",
}


def test_status_reports_setup_state(monkeypatch, tmp_path):
    from inbox import gmail as g
    monkeypatch.setattr(g, "_CREDS_PATH", tmp_path / "gmail_credentials.json")
    monkeypatch.setattr(g, "_TOKEN_PATH", tmp_path / "gmail_token.json")
    client = app.app.test_client()
    s = client.get("/api/inbox/status").get_json()
    assert s["creds"] is False and s["token"] is False and s["configured"] is False
    assert "gmail_credentials.json" in s["creds_path"]

    (tmp_path / "gmail_credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "gmail_token.json").write_text("{}", encoding="utf-8")
    s = client.get("/api/inbox/status").get_json()
    assert s["creds"] is True and s["token"] is True


def test_verify_endpoint_visits_the_link_on_click(monkeypatch):
    calls = []
    monkeypatch.setattr("inbox.gmail.complete_verification",
                        lambda url, opener=None: (calls.append(url) or {"ok": True, "status": 200}))
    client = app.app.test_client()
    r = client.post("/api/inbox/verify", json={"url": "https://x.example/verify"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert calls == ["https://x.example/verify"]
    assert client.post("/api/inbox/verify", json={}).status_code == 400


def test_scan_auto_verifies_only_with_double_opt_in(monkeypatch):
    visited = []
    monkeypatch.setattr("inbox.gmail.complete_verification",
                        lambda url, opener=None: (visited.append(url) or {"ok": True, "status": 200}))
    monkeypatch.setattr(app, "_saved_full_profile", lambda: {})
    client = app.app.test_client()
    scan = lambda: client.post("/api/inbox/scan",
                               json={"messages": [VERIFY_MSG], "draft": False}).get_json()

    # OFF by default: surfaced, never visited.
    monkeypatch.setattr(app, "_autonomous_on", lambda: False)
    monkeypatch.setattr(app, "_prefs", lambda: {"inbox_auto_verify": False})
    out = scan()
    assert out["verifications"] and "auto" not in out["verifications"][0]
    assert visited == []

    # Autonomous alone is NOT enough.
    monkeypatch.setattr(app, "_autonomous_on", lambda: True)
    out = scan()
    assert visited == []

    # Both switches on -> the application's own link is completed, and reported.
    monkeypatch.setattr(app, "_prefs", lambda: {"inbox_auto_verify": True})
    out = scan()
    assert visited == ["https://boards.greenhouse.io/verify?t=abc"]
    assert out["verifications"][0]["auto"]["ok"] is True
