"""Connect GitHub, the one-click way (the SponsorJobs GitHub App).

The person clicks Connect GitHub, their OWN browser opens GitHub's page for the SponsorJobs
GitHub App (Authorize, and the first time also Install, where they pick which repositories),
and the browser hands them straight back to the app, already connected. No code to copy, no
token to paste: the flow GitHub Desktop, VS Code and Vercel use.

GitHub needs a client secret to finish this flow and to refresh its 8-hour tokens, and a secret
inside a desktop app is not a secret, so the secret lives only in the SponsorJobs Worker at
api.sponsorjobs.ai (worker/src/github.ts). The app's side, here:

- ``start(broker, done_url)`` makes a random verifier and keeps it in memory, and returns the
  Worker's connect URL with ``state = base64url(sha256(verifier))`` and this engine's own
  loopback page (``done_url``) as where the browser lands.
- The browser comes back to that page with a one-time key in the URL FRAGMENT (never sent to a
  server, so never in a log). The page hands the key to ``finish(broker, nonce)``, which claims
  the tokens from the Worker with ``{verifier, nonce}``. Both must match, and the Worker hands
  them out once. ui/app.py then checks the token with GET /user and saves it through the same
  credential path as before (GITHUB_TOKEN, GITHUB_LOGIN), plus the refresh token and expiry.
- ``refresh(broker, refresh_token)`` swaps a refresh token for new tokens through the Worker.
  ui/app.py refreshes shortly before expiry and once on a 401.

Until the Worker is configured, ``available()`` is False and Settings keeps its fallback (the
device flow, ui/github_device.py, or the token box). No token is ever in a page response or a
log line. Every call to the Worker goes through ``BROKER_FETCH(method, url, body) -> (status,
json)`` so tests replace it and nothing touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from urllib.parse import urlencode

MANAGE_URL = "https://github.com/settings/installations"
PROBE_TTL = 60                 # ask the Worker whether it is configured at most once a minute
WAIT_LIMIT = 15 * 60           # a connect left open longer than this is stale
REFRESH_EARLY = 5 * 60         # refresh a token this long before it expires

UNREACHABLE = "Couldn't reach GitHub. Check your connection and try again."
FAILED = "Couldn't connect GitHub. Try again."
DENIED = "You cancelled on GitHub."
APPROVAL = "An owner of that organization has to approve SponsorJobs first."
EXPIRED = "That took too long. Try again."


def _default_fetch(method: str, url: str, body: dict | None = None,
                   timeout: float = 10) -> tuple[int, object]:
    """One call to the SponsorJobs Worker. Returns (status, parsed JSON or None); a network
    failure is status 0."""
    import requests
    try:
        from llm.broker_client import BROKER_USER_AGENT
    except Exception:          # noqa: BLE001 - the header is a courtesy
        BROKER_USER_AGENT = "SponsorJobs-desktop"
    try:
        r = requests.request(method, url, json=body, timeout=timeout,
                             headers={"User-Agent": BROKER_USER_AGENT, "Accept": "application/json"})
    except Exception:
        return 0, None
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data


BROKER_FETCH = _default_fetch

_LOCK = threading.Lock()
_PENDING: dict = {}            # the one connect in progress: verifier, started, state, error, login
_PROBE: dict = {}              # broker -> (checked_at, available, manage_url)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def state_for(verifier: str) -> str:
    """The Worker's key for a connect: base64url(sha256(verifier)), as PKCE's S256."""
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def available(broker: str, now: float | None = None) -> bool:
    """Whether the Worker can run the one-click connect (its GitHub App settings are set)."""
    if os.environ.get("GITHUB_APP_PROBE") == "0":     # the test suite's default (tests/conftest.py)
        return False
    now = time.time() if now is None else now
    hit = _PROBE.get(broker)
    if hit and now - hit[0] < PROBE_TTL:
        return hit[1]
    status, data = BROKER_FETCH("GET", f"{broker}/github/available", None)
    ok = status == 200 and isinstance(data, dict) and bool(data.get("configured"))
    manage = str((data or {}).get("manage_url") or MANAGE_URL) if ok else MANAGE_URL
    if status != 0:            # offline is asked again next time, not remembered
        _PROBE[broker] = (now, ok, manage)
    return ok


def manage_url(broker: str) -> str:
    hit = _PROBE.get(broker)
    return hit[2] if hit else MANAGE_URL


def clear() -> None:
    """Forget the probe and any connect in progress (tests, and a disconnect)."""
    _PROBE.clear()
    cancel()


def start(broker: str, done_url: str) -> dict:
    """Begin a connect. {'ok', 'url'}: the URL to open in the person's own browser. The verifier
    stays here in memory and never leaves this process except in the one claim."""
    verifier = _b64url(secrets.token_bytes(48))
    state = state_for(verifier)
    with _LOCK:
        _PENDING.clear()
        _PENDING.update(verifier=verifier, started=time.time(), state="waiting")
    q = urlencode({"state": state, "redirect": done_url})
    return {"ok": True, "url": f"{broker}/github/connect?{q}"}


def cancel() -> None:
    with _LOCK:
        _PENDING.clear()


def _set(**kw) -> None:
    with _LOCK:
        if _PENDING:
            _PENDING.update(kw)


def status() -> dict:
    """What the Settings row shows while it waits: {'state': idle|waiting|connected|denied|
    error, 'login'?, 'error'?}. Never carries a token or the verifier."""
    with _LOCK:
        p = dict(_PENDING)
    if not p:
        return {"state": "idle"}
    if p.get("state") == "waiting" and time.time() - p.get("started", 0) > WAIT_LIMIT:
        cancel()
        return {"state": "error", "error": EXPIRED}
    out = {"state": p.get("state", "idle")}
    if p.get("login"):
        out["login"] = p["login"]
    if p.get("error"):
        out["error"] = p["error"]
    return out


def mark(state: str, error: str = "", login: str = "") -> None:
    """Record how the connect ended, for the Settings row's next status check."""
    _set(state=state, error=error, login=login)


def give_up(why: str) -> dict:
    """The browser came back saying it did not finish (cancelled, needs approval, failed)."""
    msg = {"cancelled": DENIED, "approval": APPROVAL}.get(why, FAILED)
    mark("denied" if why == "cancelled" else "error", error=msg)
    return {"ok": False, "error": msg}


def finish(broker: str, nonce: str) -> dict:
    """Claim the tokens with the kept verifier and the one-time key from the browser.
    {'ok', 'tokens': {access_token, refresh_token?, expires_in?, refresh_token_expires_in?}} or
    {'ok': False, 'error'}. The caller verifies and saves; it never echoes the tokens."""
    with _LOCK:
        verifier = _PENDING.get("verifier", "")
    if not verifier:
        return {"ok": False, "error": "Start again from SponsorJobs."}
    nonce = str(nonce or "").strip()
    if not nonce or len(nonce) > 200:
        return give_up("failed")
    status_code, data = BROKER_FETCH("POST", f"{broker}/github/claim",
                                     {"verifier": verifier, "nonce": nonce})
    if status_code == 0:
        mark("error", error=UNREACHABLE)
        return {"ok": False, "error": UNREACHABLE}
    data = data if isinstance(data, dict) else {}
    if status_code == 200 and data.get("access_token"):
        keep = ("access_token", "refresh_token", "expires_in", "refresh_token_expires_in")
        return {"ok": True, "tokens": {k: data[k] for k in keep if data.get(k)}}
    if data.get("reason") == "denied":
        return give_up("cancelled")
    if data.get("reason") == "expired" or status_code in (404, 410):
        mark("error", error=EXPIRED)
        return {"ok": False, "error": EXPIRED}
    return give_up("failed")


def expiry_from(tokens: dict, now: float | None = None) -> str:
    """The unix time the access token expires, as a string for the credential file ('' if it
    never does)."""
    now = time.time() if now is None else now
    try:
        secs = int(tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        secs = 0
    return str(int(now) + secs) if secs > 0 else ""


def needs_refresh(expires_at: str, now: float | None = None) -> bool:
    """True when a saved expiry is within REFRESH_EARLY of now (or past)."""
    now = time.time() if now is None else now
    try:
        at = int(str(expires_at or "").strip())
    except ValueError:
        return False
    return at > 0 and now >= at - REFRESH_EARLY


def refresh(broker: str, refresh_token: str) -> dict:
    """New tokens for a refresh token. {'ok', 'tokens'} or {'ok': False, 'reason'}: 'expired'
    (GitHub refused it; connect again), 'offline', or 'error'."""
    if not refresh_token:
        return {"ok": False, "reason": "expired"}
    status_code, data = BROKER_FETCH("POST", f"{broker}/github/refresh",
                                     {"refresh_token": refresh_token})
    if status_code == 0:
        return {"ok": False, "reason": "offline"}
    data = data if isinstance(data, dict) else {}
    if status_code == 200 and data.get("access_token"):
        keep = ("access_token", "refresh_token", "expires_in", "refresh_token_expires_in")
        return {"ok": True, "tokens": {k: data[k] for k in keep if data.get(k)}}
    if data.get("reason") == "bad_refresh_token":
        return {"ok": False, "reason": "expired"}
    return {"ok": False, "reason": "error"}
