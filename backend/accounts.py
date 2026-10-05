"""Per-account identity for the managed-AI broker (P6).

Each account authenticates with a secret **bearer token** that maps to a unique ``account_id``, so
metering, plans, and billing are per-account. An account can hold several tokens (one per device
that logs in). Two ways an account comes to exist:

  - ``register()``  -- an anonymous device account (no signup), used to try the app before paying.
  - ``signup(email, password)`` / ``login(email, password)`` -- an EMAIL account, so a paid
    pass follows the person to a new device (the reason we chose email accounts).

Security: bearer tokens are stored **hashed** (sha256) and passwords with **scrypt + a per-password
salt**; a leaked DB yields neither a usable token nor a password. Password *reset* (which needs to
send email) is a later addition; signup + login here need no email service.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path


class EmailTaken(Exception):
    """signup()/claim() with an email that already has an account."""


class AlreadyClaimed(Exception):
    """claim() on an account that already has an email (it's not anonymous anymore)."""


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _hash_pw(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
    return f"{salt}${h.hex()}"


def _verify_pw(password: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return hmac.compare_digest(_hash_pw(password, salt), stored)   # constant-time


def _new_account_id() -> str:
    return "acct_" + secrets.token_hex(8)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


class InMemoryAccountStore:
    """Offline/test store."""

    def __init__(self) -> None:
        self._accounts: dict[str, dict] = {}          # account_id -> {email, pw}
        self._email_ix: dict[str, str] = {}           # email -> account_id
        self._tokens: dict[str, str] = {}             # token_hash -> account_id

    def _issue(self, account_id: str) -> str:
        token = _new_token()
        self._tokens[_hash(token)] = account_id
        return token

    def register(self) -> tuple[str, str]:
        account_id = _new_account_id()
        self._accounts[account_id] = {"email": None, "pw": None}
        return account_id, self._issue(account_id)

    def signup(self, email: str, password: str) -> tuple[str, str]:
        if email in self._email_ix:
            raise EmailTaken(email)
        account_id = _new_account_id()
        self._accounts[account_id] = {"email": email, "pw": _hash_pw(password)}
        self._email_ix[email] = account_id
        return account_id, self._issue(account_id)

    def login(self, email: str, password: str) -> tuple[str, str] | None:
        account_id = self._email_ix.get(email)
        if not account_id or not _verify_pw(password, self._accounts[account_id]["pw"]):
            return None
        return account_id, self._issue(account_id)

    def upsert_google(self, email: str) -> tuple[str, str]:
        """Find-or-create an account for a Google-VERIFIED email (no password) and issue a token.
        Same email -> same account, so Google and email login land on the one account."""
        account_id = self._email_ix.get(email)
        if not account_id:
            account_id = _new_account_id()
            self._accounts[account_id] = {"email": email, "pw": None}
            self._email_ix[email] = account_id
        return account_id, self._issue(account_id)

    def claim(self, account_id: str, email: str, password: str) -> None:
        acct = self._accounts.get(account_id)
        if acct is None:
            raise KeyError(account_id)
        if acct["email"]:
            raise AlreadyClaimed(account_id)
        if email in self._email_ix:
            raise EmailTaken(email)
        acct["email"], acct["pw"] = email, _hash_pw(password)
        self._email_ix[email] = account_id

    def email_of(self, account_id: str) -> str | None:
        acct = self._accounts.get(account_id)
        return acct["email"] if acct else None

    def resolve(self, token: str | None) -> str | None:
        return self._tokens.get(_hash(token or "")) if token else None


class SqliteAccountStore:
    """Persistent store; shares the broker's SQLite DB file (its own tables)."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS accounts ("
                      "account_id TEXT PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS tokens ("
                      "token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL)")

    def _issue(self, c, account_id: str) -> str:
        token = _new_token()
        c.execute("INSERT INTO tokens(token_hash, account_id) VALUES(?, ?)", (_hash(token), account_id))
        return token

    def register(self) -> tuple[str, str]:
        account_id = _new_account_id()
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO accounts(account_id) VALUES(?)", (account_id,))
            return account_id, self._issue(c, account_id)

    def signup(self, email: str, password: str) -> tuple[str, str]:
        account_id = _new_account_id()
        try:
            with sqlite3.connect(self.path) as c:
                c.execute("INSERT INTO accounts(account_id, email, password_hash) VALUES(?, ?, ?)",
                          (account_id, email, _hash_pw(password)))
                return account_id, self._issue(c, account_id)
        except sqlite3.IntegrityError as exc:
            raise EmailTaken(email) from exc

    def login(self, email: str, password: str) -> tuple[str, str] | None:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT account_id, password_hash FROM accounts WHERE email = ?",
                            (email,)).fetchone()
            if not row or not _verify_pw(password, row[1]):
                return None
            return row[0], self._issue(c, row[0])

    def upsert_google(self, email: str) -> tuple[str, str]:
        """Find-or-create an account for a Google-VERIFIED email (no password) and issue a token."""
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT account_id FROM accounts WHERE email = ?", (email,)).fetchone()
            if row:
                account_id = row[0]
            else:
                account_id = _new_account_id()
                c.execute("INSERT INTO accounts(account_id, email) VALUES(?, ?)", (account_id, email))
            return account_id, self._issue(c, account_id)

    def claim(self, account_id: str, email: str, password: str) -> None:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT email FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            if row is None:
                raise KeyError(account_id)
            if row[0]:
                raise AlreadyClaimed(account_id)
            try:
                c.execute("UPDATE accounts SET email = ?, password_hash = ? WHERE account_id = ?",
                          (email, _hash_pw(password), account_id))
            except sqlite3.IntegrityError as exc:
                raise EmailTaken(email) from exc

    def email_of(self, account_id: str) -> str | None:
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT email FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        return row[0] if row and row[0] else None

    def resolve(self, token: str | None) -> str | None:
        if not token:
            return None
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT account_id FROM tokens WHERE token_hash = ?",
                            (_hash(token),)).fetchone()
        return row[0] if row else None


def bearer_identify(accounts, fallback):
    """Build an identify(request) that prefers a bearer token (-> account_id) and falls back to the
    given identify (the X-Tailor-User header) so dev/tests keep working without a token."""
    def identify(request):
        auth = request.headers.get("Authorization", "")
        if accounts is not None and auth.startswith("Bearer "):
            account_id = accounts.resolve(auth[len("Bearer "):].strip())
            if account_id:
                return account_id
        return fallback(request)
    return identify
