"""App Lock: an optional passcode that gates SponsorJobs on a shared machine.

Why this exists (the owner's real worry): SponsorJobs runs on the owner's own Anthropic key
and holds their CVs, profile and personal history locally. On a shared computer, anyone
who opens the app could spend the owner's tokens or read their data. A passcode fixes that.

What it actually protects, honestly (see the research this was built from):
  * The **secrets** (the API key, the Telegram bot token, the GitHub PAT, the Tavus/Adzuna
    keys — everything in `_CRED_NAMES`) are encrypted AT REST with a key derived from the
    passcode (scrypt), so none sit in a plaintext file a passerby can copy. While locked
    they are not even in memory.
  * The **running app** is gated: the engine refuses every data/AI route until the passcode
    is entered (enforced server-side in `ui.app`, not just a UI overlay a local process
    could skip). So nobody reads your CVs/profile *through SponsorJobs* or spends tokens while
    it is locked.

What it does NOT (yet) protect — stated plainly rather than pretended:
  * The SQLite DB and PDF files themselves are still plaintext on disk. Full at-rest file
    encryption (SQLCipher-scale) and per-person profile switching are the documented next
    step. For now, the honest OS-level answer for the files is a separate Windows account or
    full-disk encryption; App Lock secures the app surface and the key.
  * Nothing here stops software running as the SAME logged-in Windows user — no local-app
    keystore can. That is an accepted, documented limitation.

Crypto: scrypt (N=2**16, r=8, p=1) derives a 256-bit key from the passcode; AES-256-GCM
(authenticated) encrypts a small verifier and the API key. Only ciphertext + the random
salt live on disk (data/lock.json). Lose the passcode and the encrypted key is gone (the
owner re-enters their Anthropic key), which is the correct, safe failure mode.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt cost. 2**16 keeps an unlock well under a second on a laptop while staying far above
# a plain hash; OWASP lists scrypt as an acceptable memory-hard KDF when Argon2id is absent.
_N, _R, _P = 2 ** 16, 8, 1
_VERIFIER = b"tailor-lock-v1"          # a known plaintext we can round-trip to check a passcode
# The secrets App Lock takes custody of when enabled (encrypted at rest). NOT just the
# provider key: the Telegram token can drive runs and spend the owner's tokens, and the
# GitHub PAT has write access to the owner's account — both are as sensitive as the API
# key, so leaving them plaintext while encrypting only the LLM key was an inconsistency.
_CRED_NAMES = (
    # Provider API keys (cost money).
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    # Telegram remote-control channel: the bot token can steer a run and spend API tokens,
    # so it is as sensitive as the API key. The chat id (the owner's own) travels with it.
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    # GitHub: a Personal Access Token with write scope on the owner's OWN account.
    "GITHUB_TOKEN", "GITHUB_LOGIN",
    # Paid cloud avatar (Tavus) key, and the optional Adzuna sourcing dev key.
    "AVATAR_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY",
    # The Gmail OAuth token (JSON) — mailbox read access. Stored via the app's Gmail token
    # store (inbox/gmail.set_token_store), not the creds file, but custodied all the same.
    "GMAIL_TOKEN",
)

# In-memory session state. Deliberately not persisted: "is the app unlocked right now" is a
# fact about this running process, and the decrypted key must never touch disk. _KEK is held
# only while unlocked so we can re-seal a changed key without re-prompting the passcode; it is
# no more exposed than _CREDS (both live only in this process's memory) and is cleared on lock.
_UNLOCKED = False
_CREDS: dict[str, str] = {}
_KEK: bytes | None = None


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class Lock:
    """Passcode + at-rest key custody, backed by a single JSON file of ciphertext."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- state -------------------------------------------------------------
    def enabled(self) -> bool:
        """Has the owner set a passcode?"""
        return self.path.exists()

    def unlocked(self) -> bool:
        """Can data/AI routes run right now? Always true when no passcode is set."""
        return (not self.enabled()) or _UNLOCKED

    def status(self) -> dict:
        return {"enabled": self.enabled(), "unlocked": self.unlocked()}

    # ---- crypto helpers ----------------------------------------------------
    def _derive(self, passcode: str, salt: bytes, n: int, r: int, p: int) -> bytes:
        return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passcode.encode("utf-8"))

    def _seal(self, kek: bytes, plaintext: bytes) -> str:
        nonce = os.urandom(12)
        ct = AESGCM(kek).encrypt(nonce, plaintext, None)
        return _b64e(nonce + ct)

    def _open(self, kek: bytes, blob: str) -> bytes:
        raw = _b64d(blob)
        return AESGCM(kek).decrypt(raw[:12], raw[12:], None)

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    # ---- lifecycle ---------------------------------------------------------
    def set_passcode(self, passcode: str, creds: dict[str, str]) -> None:
        """Turn App Lock ON: derive a key from `passcode`, encrypt `creds` (the API key)
        under it, and write only ciphertext. Leaves the process UNLOCKED with the creds in
        memory so the owner keeps working."""
        if not passcode or len(passcode) < 4:
            raise ValueError("Choose a passcode of at least 4 characters.")
        salt = os.urandom(16)
        kek = self._derive(passcode, salt, _N, _R, _P)
        clean = {k: v for k, v in (creds or {}).items() if v}
        data = {
            "version": 1,
            "kdf": {"name": "scrypt", "n": _N, "r": _R, "p": _P, "salt": _b64e(salt)},
            "verifier": self._seal(kek, _VERIFIER),
            "creds": {k: self._seal(kek, v.encode("utf-8")) for k, v in clean.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        global _UNLOCKED, _CREDS, _KEK
        _CREDS = dict(clean)
        _KEK = kek
        _UNLOCKED = True

    def unlock(self, passcode: str) -> bool:
        """Verify `passcode`; on success decrypt the stored creds into memory and unlock."""
        if not self.enabled():
            return True
        try:
            data = self._load()
            k = data["kdf"]
            kek = self._derive(passcode, _b64d(k["salt"]), k["n"], k["r"], k["p"])
            if self._open(kek, data["verifier"]) != _VERIFIER:   # wrong passcode
                return False
            creds = {}
            for name, blob in (data.get("creds") or {}).items():
                creds[name] = self._open(kek, blob).decode("utf-8")
        except Exception:
            return False
        global _UNLOCKED, _CREDS, _KEK
        _CREDS = creds
        _KEK = kek
        _UNLOCKED = True
        return True

    def lock(self) -> None:
        """Drop the decrypted key from memory and re-gate the app (the "Log out" action)."""
        global _UNLOCKED, _CREDS, _KEK
        _CREDS = {}
        _KEK = None
        _UNLOCKED = False

    def update_cred(self, name: str, value: str) -> None:
        """Re-seal one custodied credential (e.g. the owner pasted a new API key while
        unlocked), using the in-memory key so no passcode re-prompt is needed."""
        if not (self.enabled() and self.unlocked() and _KEK is not None):
            raise ValueError("App Lock must be unlocked to change a stored key.")
        data = self._load()
        data.setdefault("creds", {})
        if value:
            data["creds"][name] = self._seal(_KEK, value.encode("utf-8"))
            _CREDS[name] = value
        else:
            data["creds"].pop(name, None)
            _CREDS.pop(name, None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def change(self, current: str, new: str) -> bool:
        """Re-key: verify `current`, then re-encrypt the same creds under `new`."""
        if not self.enabled():
            return False
        if not self.unlock(current):
            return False
        self.set_passcode(new, dict(_CREDS))
        return True

    def remove(self, passcode: str) -> dict[str, str]:
        """Turn App Lock OFF: verify, return the decrypted creds so the caller can put the
        key back in ordinary storage, then delete the lock file."""
        if not self.enabled():
            return {}
        if not self.unlock(passcode):
            raise ValueError("That passcode is not correct.")
        creds = dict(_CREDS)
        try:
            self.path.unlink()
        except OSError:
            pass
        self.lock()
        return creds

    # ---- credential access -------------------------------------------------
    def cred(self, name: str) -> str:
        """The decrypted value of a lock-custodied credential, or "" when locked/absent."""
        if self.enabled() and self.unlocked():
            return _CREDS.get(name, "")
        return ""

    def custodies(self, name: str) -> bool:
        """Does App Lock (when enabled) own this credential, vs the plaintext creds file?"""
        return name in _CRED_NAMES

    def custody_names(self) -> tuple[str, ...]:
        """Every credential App Lock takes into encrypted custody when enabled — so callers
        (enable, migrate) can sweep all of them without hard-coding the list twice."""
        return _CRED_NAMES
