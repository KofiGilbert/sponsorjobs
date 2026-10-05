"""App Lock: passcode crypto + the engine-side gate.

Acceptance: a passcode encrypts the API key at rest (no plaintext on disk), the engine
refuses data routes while locked and serves them once unlocked, a wrong passcode is rejected,
and removing the passcode restores the key to ordinary storage. The default (no passcode)
path is unchanged.
"""

import importlib
import os
import tempfile

import pytest

from ui.lock import Lock


def test_lock_crypto_roundtrip_and_no_plaintext(tmp_path):
    lk = Lock(tmp_path / "lock.json")
    assert not lk.enabled() and lk.unlocked()          # default: open

    lk.set_passcode("secret1", {"ANTHROPIC_API_KEY": "sk-ant-XYZ"})
    assert lk.enabled() and lk.unlocked()
    assert lk.cred("ANTHROPIC_API_KEY") == "sk-ant-XYZ"
    on_disk = (tmp_path / "lock.json").read_text()
    assert "sk-ant-XYZ" not in on_disk                 # only ciphertext lands on disk

    lk.lock()
    assert not lk.unlocked() and lk.cred("ANTHROPIC_API_KEY") == ""

    assert lk.unlock("wrong") is False and not lk.unlocked()
    assert lk.unlock("secret1") is True and lk.cred("ANTHROPIC_API_KEY") == "sk-ant-XYZ"


def test_lock_change_and_update(tmp_path):
    lk = Lock(tmp_path / "lock.json")
    lk.set_passcode("aaaa", {"ANTHROPIC_API_KEY": "k1"})
    lk.update_cred("ANTHROPIC_API_KEY", "k2")          # change key while unlocked
    assert lk.cred("ANTHROPIC_API_KEY") == "k2"
    assert lk.change("aaaa", "bbbb") is True
    lk.lock()
    assert lk.unlock("aaaa") is False
    assert lk.unlock("bbbb") is True and lk.cred("ANTHROPIC_API_KEY") == "k2"


def test_short_passcode_rejected(tmp_path):
    with pytest.raises(ValueError):
        Lock(tmp_path / "lock.json").set_passcode("ab", {})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    cred = tmp_path / "credentials.env"
    cred.write_text("ANTHROPIC_API_KEY=sk-ant-PLAIN\n")
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(d))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(cred))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import ui.app as app_module
    importlib.reload(app_module)
    return app_module, app_module.app.test_client()


def test_engine_gate_and_key_custody(client):
    app_module, c = client
    # default: reachable
    assert c.get("/api/records").status_code != 423

    # turn on: plaintext key removed, sealed, still works (unlocked)
    assert c.post("/api/lock/set", json={"passcode": "1234"}).status_code == 200
    assert "sk-ant-PLAIN" not in os.environ["RESUME_AGENT_CRED_FILE"] or \
        "sk-ant-PLAIN" not in open(os.environ["RESUME_AGENT_CRED_FILE"]).read()
    assert app_module._cred("ANTHROPIC_API_KEY") == "sk-ant-PLAIN"

    # lock -> data routes 423, key hidden; lock endpoints still reachable
    c.post("/api/lock/lock")
    r = c.get("/api/records")
    assert r.status_code == 423 and r.get_json().get("reason") == "locked"
    assert app_module._cred("ANTHROPIC_API_KEY") == ""
    assert c.get("/api/lock/status").status_code == 200

    # wrong then right passcode
    assert c.post("/api/lock/unlock", json={"passcode": "0000"}).status_code == 401
    assert c.post("/api/lock/unlock", json={"passcode": "1234"}).status_code == 200
    assert c.get("/api/records").status_code != 423

    # remove -> key restored to plaintext, lock disabled
    assert c.post("/api/lock/remove", json={"passcode": "1234"}).status_code == 200
    assert not c.get("/api/lock/status").get_json()["enabled"]
    assert "sk-ant-PLAIN" in open(os.environ["RESUME_AGENT_CRED_FILE"]).read()


def test_lock_custodies_all_sensitive_secrets(tmp_path):
    """App Lock must take custody of every secret, not just the API key — the Telegram
    token (remote control), the GitHub PAT (write access), and the paid Tavus/Adzuna keys."""
    lk = Lock(tmp_path / "lock.json")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "TELEGRAM_CHAT_ID", "GITHUB_TOKEN", "GITHUB_LOGIN",
                 "AVATAR_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        assert lk.custodies(name), f"{name} should be custodied"
    assert not lk.custodies("AI_PROVIDER")   # a preference, not a secret


def test_enabling_lock_seals_and_scrubs_all_secrets(client):
    """Turning App Lock on encrypts EVERY secret at rest (not just the API key) and leaves
    no plaintext copy on disk; sealed values read back while unlocked, vanish while locked."""
    app_module, c = client
    app_module._save_cred("TELEGRAM_BOT_TOKEN", "tg-secret-123")
    app_module._save_cred("GITHUB_TOKEN", "ghp_write_abc")
    cred_path = os.environ["RESUME_AGENT_CRED_FILE"]
    assert "tg-secret-123" in open(cred_path).read()

    assert c.post("/api/lock/set", json={"passcode": "1234"}).status_code == 200
    disk = open(cred_path).read()
    for secret in ("tg-secret-123", "ghp_write_abc", "sk-ant-PLAIN"):
        assert secret not in disk, f"{secret} left in plaintext after lock"
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == "tg-secret-123"   # unlocked: readable
    assert app_module._cred("GITHUB_TOKEN") == "ghp_write_abc"

    c.post("/api/lock/lock")
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == ""                # locked: hidden
    assert app_module._cred("GITHUB_TOKEN") == ""


def test_unlock_migrates_legacy_plaintext_secret(client):
    """An existing setup locked BEFORE custody covered Telegram left the token in plaintext.
    Unlocking must sweep it into the encrypted lock and scrub the plaintext — no re-prompt."""
    app_module, c = client
    cred_path = os.environ["RESUME_AGENT_CRED_FILE"]
    # Simulate the legacy state: only the API key was sealed; a Telegram token sits plaintext.
    app_module._LOCK.set_passcode("1234", {"ANTHROPIC_API_KEY": "sk-ant-PLAIN"})
    with open(cred_path, "a") as f:
        f.write("TELEGRAM_BOT_TOKEN=tg-legacy-xyz\n")
    app_module._LOCK.lock()
    assert "tg-legacy-xyz" in open(cred_path).read()

    assert c.post("/api/lock/unlock", json={"passcode": "1234"}).status_code == 200
    assert "tg-legacy-xyz" not in open(cred_path).read()               # plaintext scrubbed
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == "tg-legacy-xyz"   # now sealed + readable
    app_module._LOCK.lock()
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == ""                # and hidden when locked


def test_removing_lock_restores_all_secrets_to_plaintext(client):
    """Turning App Lock OFF must put every custodied secret back into ordinary storage,
    not just the API key, so nothing is lost."""
    app_module, c = client
    app_module._save_cred("TELEGRAM_BOT_TOKEN", "tg-restore-me")
    c.post("/api/lock/set", json={"passcode": "1234"})
    assert c.post("/api/lock/remove", json={"passcode": "1234"}).status_code == 200
    disk = open(os.environ["RESUME_AGENT_CRED_FILE"]).read()
    assert "tg-restore-me" in disk and "sk-ant-PLAIN" in disk


def _patch_gmail_token(monkeypatch, tmp_path):
    """Point the Gmail token at a throwaway path so tests never touch the real config file."""
    import inbox.gmail as gm
    p = tmp_path / "gmail_token.json"
    monkeypatch.setattr(gm, "_TOKEN_PATH", p)
    return p


def test_gmail_token_sealed_and_file_scrubbed_on_enable(client, monkeypatch, tmp_path):
    """The Gmail OAuth token (mailbox access) must be encrypted too — enabling App Lock seals
    it into the lock and deletes the plaintext token file."""
    app_module, c = client
    p = _patch_gmail_token(monkeypatch, tmp_path)
    p.write_text('{"token": "gmail-abc"}')
    assert c.post("/api/lock/set", json={"passcode": "1234"}).status_code == 200
    assert not p.exists()                                           # plaintext file gone
    assert app_module._gmail_load_token() == '{"token": "gmail-abc"}'   # sealed, readable
    c.post("/api/lock/lock")
    assert app_module._gmail_load_token() is None                  # hidden while locked


def test_unlock_migrates_a_legacy_plaintext_gmail_token(client, monkeypatch, tmp_path):
    app_module, c = client
    p = _patch_gmail_token(monkeypatch, tmp_path)
    app_module._LOCK.set_passcode("1234", {"ANTHROPIC_API_KEY": "sk-ant-PLAIN"})  # pre-gmail
    p.write_text('{"token": "legacy-gmail"}')
    app_module._LOCK.lock()
    assert c.post("/api/lock/unlock", json={"passcode": "1234"}).status_code == 200
    assert not p.exists()
    assert app_module._gmail_load_token() == '{"token": "legacy-gmail"}'


def test_removing_lock_restores_gmail_token_to_its_own_file(client, monkeypatch, tmp_path):
    """Turning App Lock OFF puts the Gmail token back in its own file, NOT the creds file."""
    app_module, c = client
    p = _patch_gmail_token(monkeypatch, tmp_path)
    p.write_text('{"token": "restore-gmail"}')
    c.post("/api/lock/set", json={"passcode": "1234"})
    assert not p.exists()
    assert c.post("/api/lock/remove", json={"passcode": "1234"}).status_code == 200
    assert p.exists() and p.read_text() == '{"token": "restore-gmail"}'
    assert "restore-gmail" not in open(os.environ["RESUME_AGENT_CRED_FILE"]).read()


def test_revoke_cred_drops_the_secret_from_the_lock(client):
    """Disconnecting a secret while App Lock is on must actually revoke it, not leave the
    sealed copy live (which _cred would still return)."""
    app_module, c = client
    app_module._save_cred("TELEGRAM_BOT_TOKEN", "tg-123")
    c.post("/api/lock/set", json={"passcode": "1234"})          # seal it, scrub plaintext
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == "tg-123"   # sealed and readable
    app_module._revoke_cred("TELEGRAM_BOT_TOKEN")               # the Disconnect action
    assert app_module._cred("TELEGRAM_BOT_TOKEN") == ""         # actually gone, not still usable


def test_host_allowlist_blocks_dns_rebinding(client):
    _, c = client
    assert c.get("/api/lock/status", headers={"Host": "evil.example.com"}).status_code == 403
    assert c.get("/api/lock/status", headers={"Host": "127.0.0.1:57000"}).status_code == 200
