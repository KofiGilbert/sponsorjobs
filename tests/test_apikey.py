"""Connect-your-AI: status + verify-and-save for the Anthropic key. The key lives
ONLY in the git-ignored local credentials file; an invalid key is never saved."""
from __future__ import annotations

import ui.app as app


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_CRED_FILE", tmp_path / "credentials.env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_status_no_key(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/apikey").get_json()
    assert d["configured"] is False and d["masked"] == ""


def test_status_with_key_is_masked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "credentials.env").write_text("ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop\n", encoding="utf-8")
    d = app.app.test_client().get("/api/apikey").get_json()
    assert d["configured"] is True
    assert d["masked"].startswith("sk-ant") and d["masked"].endswith("mnop")
    assert "abcdefgh" not in d["masked"]      # the middle is hidden


def test_save_validates_then_persists(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_validate_anthropic_key", lambda k: (True, ""))
    r = app.app.test_client().post("/api/apikey", json={"key": "sk-ant-realkey123456"})
    assert r.get_json()["ok"] is True
    assert "ANTHROPIC_API_KEY=sk-ant-realkey123456" in (tmp_path / "credentials.env").read_text(encoding="utf-8")


def test_save_bad_format_never_persists(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = app.app.test_client().post("/api/apikey", json={"key": "not-a-key"})   # fails the sk- check, no network
    assert r.status_code == 400 and "sk-" in r.get_json()["error"]
    assert not (tmp_path / "credentials.env").exists()


def test_save_surfaces_validation_error(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_validate_anthropic_key", lambda k: (False, "That key was rejected."))
    r = app.app.test_client().post("/api/apikey", json={"key": "sk-ant-whatever"})
    assert r.status_code == 400 and r.get_json()["error"] == "That key was rejected."
    assert not (tmp_path / "credentials.env").exists()


def test_save_upserts_and_preserves_other_creds(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "credentials.env").write_text(
        "TELEGRAM_BOT_TOKEN=abc\nANTHROPIC_API_KEY=sk-ant-old\n", encoding="utf-8")
    monkeypatch.setattr(app, "_validate_anthropic_key", lambda k: (True, ""))
    app.app.test_client().post("/api/apikey", json={"key": "sk-ant-new999"})
    saved = (tmp_path / "credentials.env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-new999" in saved
    assert "sk-ant-old" not in saved                 # replaced, not duplicated
    assert "TELEGRAM_BOT_TOKEN=abc" in saved          # other creds untouched
