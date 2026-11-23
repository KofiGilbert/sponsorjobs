"""Secrets at rest: owner-only files from the first byte, the Gmail token in the per-user data
folder (with a one-time move from the old place), a 6-character minimum for NEW App Lock
passcodes, and a growing wait after repeated wrong passcodes."""

from __future__ import annotations

import importlib
import os
import stat
import sys

import pytest

from ui import secure_files
from ui.lock import Lock

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes")


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ---- the writer -----------------------------------------------------------------------------

@posix_only
def test_write_private_creates_owner_only_even_with_an_open_umask(tmp_path):
    old = os.umask(0o000)
    try:
        p = tmp_path / "sub" / "secret.env"
        secure_files.write_private(p, "A=1\n")
    finally:
        os.umask(old)
    assert p.read_text() == "A=1\n" and _mode(p) == 0o600
    assert [x.name for x in p.parent.iterdir()] == ["secret.env"]      # no temp file left behind


@posix_only
def test_write_private_tightens_a_file_that_was_world_readable(tmp_path):
    p = tmp_path / "secret.env"
    p.write_text("old")
    os.chmod(p, 0o644)
    secure_files.write_private(p, "new")
    assert p.read_text() == "new" and _mode(p) == 0o600


def test_write_private_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    p = tmp_path / "secret.env"
    secure_files.write_private(p, "old")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(secure_files.os, "replace", boom)
    with pytest.raises(OSError):
        secure_files.write_private(p, "new")
    assert p.read_text() == "old"
    assert [x.name for x in tmp_path.iterdir()] == ["secret.env"]


@posix_only
def test_write_private_never_writes_through_a_planted_link(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    p = tmp_path / "secret.env"
    os.symlink(target, p)                     # a link where the secret goes is replaced, not followed
    secure_files.write_private(p, "s3cret")
    assert target.read_text() == "untouched"
    assert not os.path.islink(p) and p.read_text() == "s3cret"


# ---- the app ----------------------------------------------------------------------------------

@pytest.fixture()
def A(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(d))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import inbox.gmail as gm
    monkeypatch.setattr(gm, "_TOKEN_PATH", None)        # the default: the data folder
    import ui.app as app_module
    importlib.reload(app_module)
    yield app_module
    # Leave no App Lock behind: ui.app stays loaded for the next test file in this worker, and a
    # lock file left in its data folder would answer every later engine call with 423 "locked".
    app_module._LOCK.path.unlink(missing_ok=True)
    app_module._lock_tries_file().unlink(missing_ok=True)
    import ui.lock as L
    L._UNLOCKED, L._CREDS, L._KEK = False, {}, None


@posix_only
def test_saved_credentials_are_owner_only(A):
    A._save_cred("TELEGRAM_BOT_TOKEN", "tg-1")
    assert _mode(A._CRED_FILE) == 0o600
    A._remove_cred("TELEGRAM_BOT_TOKEN")
    assert _mode(A._CRED_FILE) == 0o600


@posix_only
def test_the_gmail_token_lives_in_the_data_folder_owner_only(A, tmp_path):
    import inbox.gmail as gm
    assert gm.token_path() == (tmp_path / "data" / "gmail_token.json").resolve()
    A._gmail_save_token('{"token": "t"}')
    assert gm.token_path().read_text() == '{"token": "t"}' and _mode(gm.token_path()) == 0o600
    assert A._gmail_load_token() == '{"token": "t"}'


@posix_only
def test_the_app_lock_file_is_owner_only(tmp_path):
    lk = Lock(tmp_path / "lock.json")
    lk.set_passcode("secret1", {"ANTHROPIC_API_KEY": "k"})
    assert _mode(tmp_path / "lock.json") == 0o600
    lk.update_cred("ANTHROPIC_API_KEY", "k2")
    assert _mode(tmp_path / "lock.json") == 0o600
    lk.lock()


@posix_only
def test_an_old_gmail_token_moves_to_the_data_folder_once(tmp_path):
    import inbox.gmail as gm
    legacy, dest = tmp_path / "config" / "gmail_token.json", tmp_path / "data" / "gmail_token.json"
    legacy.parent.mkdir()
    legacy.write_text('{"token": "old"}')
    os.chmod(legacy, 0o644)
    assert gm.migrate_legacy_token(legacy, dest) is True
    assert dest.read_text() == '{"token": "old"}' and _mode(dest) == 0o600
    assert not legacy.exists()                                  # deleted after the copy
    assert gm.migrate_legacy_token(legacy, dest) is False       # nothing left to do


def test_an_old_gmail_token_never_overwrites_a_newer_one(tmp_path):
    import inbox.gmail as gm
    legacy, dest = tmp_path / "old.json", tmp_path / "data" / "gmail_token.json"
    legacy.write_text('{"token": "stale"}')
    secure_files.write_private(dest, '{"token": "current"}')
    assert gm.migrate_legacy_token(legacy, dest) is True
    assert dest.read_text() == '{"token": "current"}' and not legacy.exists()


def test_a_failed_copy_keeps_the_old_token(tmp_path, monkeypatch):
    import inbox.gmail as gm
    legacy, dest = tmp_path / "old.json", tmp_path / "data" / "gmail_token.json"
    legacy.write_text('{"token": "old"}')

    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(secure_files, "write_private", boom)
    assert gm.migrate_legacy_token(legacy, dest) is False
    assert legacy.read_text() == '{"token": "old"}' and not dest.exists()


# ---- App Lock passcodes ----------------------------------------------------------------------

def test_new_passcodes_need_six_characters_but_an_old_short_one_still_unlocks(tmp_path, monkeypatch):
    import ui.lock as L
    with pytest.raises(ValueError, match="at least 6"):
        Lock(tmp_path / "a.json").set_passcode("12345", {})
    # A 4-character passcode set by an older version keeps working.
    monkeypatch.setattr(L, "MIN_PASSCODE", 4)
    old = Lock(tmp_path / "b.json")
    old.set_passcode("1234", {"ANTHROPIC_API_KEY": "k"})
    monkeypatch.setattr(L, "MIN_PASSCODE", 6)
    old.lock()
    assert old.unlock("1234") is True and old.cred("ANTHROPIC_API_KEY") == "k"
    with pytest.raises(ValueError):
        old.change("1234", "4321")              # but a new one must be 6
    old.lock()


def test_set_and_change_routes_refuse_a_short_new_passcode(A):
    c = A.app.test_client()
    r = c.post("/api/lock/set", json={"passcode": "12345"})
    assert r.status_code == 400 and "at least 6" in r.get_json()["error"]
    assert c.post("/api/lock/set", json={"passcode": "123456"}).status_code == 200
    r = c.post("/api/lock/change", json={"current": "123456", "passcode": "abc"})
    assert r.status_code == 400 and "at least 6" in r.get_json()["error"]
    assert A._lock_tries()["failures"] == 0                       # a short new one costs no try
    assert c.post("/api/lock/change", json={"current": "123456", "passcode": "abcdef"}).status_code == 200


def test_unlock_waits_after_five_wrong_tries_doubling_and_resets_on_success(A, monkeypatch):
    c = A.app.test_client()
    assert c.post("/api/lock/set", json={"passcode": "right-one"}).status_code == 200
    c.post("/api/lock/lock")
    clock = [1_000_000.0]
    monkeypatch.setattr(A.time, "time", lambda: clock[0])
    for _ in range(4):
        assert c.post("/api/lock/unlock", json={"passcode": "nope"}).status_code == 401
    assert A._lock_wait() == 0
    assert c.post("/api/lock/unlock", json={"passcode": "nope"}).status_code == 401      # the 5th
    r = c.post("/api/lock/unlock", json={"passcode": "right-one"})                      # even the right one waits
    assert r.status_code == 429 and r.get_json()["retry_after"] == 30
    assert r.headers["Retry-After"] == "30" and "Try again in 30 seconds" in r.get_json()["error"]
    assert not A._LOCK.unlocked()
    clock[0] += 31
    assert c.post("/api/lock/unlock", json={"passcode": "nope"}).status_code == 401      # the 6th
    assert A._lock_wait() == 60                                                         # doubled
    clock[0] += 61
    assert c.post("/api/lock/unlock", json={"passcode": "nope"}).status_code == 401
    assert A._lock_wait() == 120
    r = c.post("/api/lock/unlock", json={"passcode": "nope"})
    assert r.status_code == 429 and "2 minutes" in r.get_json()["error"]
    clock[0] += 121
    assert c.post("/api/lock/unlock", json={"passcode": "right-one"}).status_code == 200
    assert A._lock_tries() == {"failures": 0, "until": 0.0}                             # reset


def test_the_wait_covers_change_and_remove_and_survives_a_restart(A, monkeypatch):
    c = A.app.test_client()
    assert c.post("/api/lock/set", json={"passcode": "right-one"}).status_code == 200
    for _ in range(3):
        assert c.post("/api/lock/remove", json={"passcode": "nope"}).status_code == 401
    for _ in range(2):
        assert c.post("/api/lock/change", json={"current": "nope", "passcode": "new-one-1"}).status_code == 400
    assert c.post("/api/lock/remove", json={"passcode": "right-one"}).status_code == 429
    assert c.post("/api/lock/change", json={"current": "right-one", "passcode": "new-one-1"}).status_code == 429
    # A restart (the module reloaded) keeps the count: it lives in the data folder.
    importlib.reload(A)
    assert A._lock_wait() > 0
    assert A.app.test_client().post("/api/lock/unlock", json={"passcode": "right-one"}).status_code == 429
