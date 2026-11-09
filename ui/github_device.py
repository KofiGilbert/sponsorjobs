"""Sign in with GitHub, using GitHub's OAuth device flow (RFC 8628).

Why the device flow: it is GitHub's sign-in for apps that cannot keep a secret (desktop apps
and CLIs, the way `gh auth login` works). The app shows a short code, the person approves it on
github.com in their OWN browser, and the app receives a token. No client secret exists anywhere,
no local callback server, and the person never sees or pastes a token.

The flow, in two calls from the page:
- ``start()`` asks GitHub for a device code. The page gets only what it shows the person (the
  user code, where to enter it, how long it lasts, how often to check). The ``device_code`` stays
  here in memory: it is never sent to the page and never logged.
- ``poll()`` asks GitHub whether the person approved yet, at most once per ``interval``. On
  approval it returns the token to ui/app.py, which verifies it with GET /user, saves it with the
  existing credential path (so everything reading GITHUB_TOKEN keeps working) and answers the
  page with the login only. The token never appears in a response or a log line.

Every HTTP call goes through ``FETCH(method, url, token, body, params) -> (status, json)``, the
same seam as ui/github_publish.py, so tests replace it and nothing touches the network.
"""

from __future__ import annotations

import threading
import time

from config.github_oauth import SCOPE, client_id

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SLOW_DOWN_STEP = 5            # seconds GitHub adds to the interval on slow_down (its docs)

EXPIRED = "That code expired. Try again."
DENIED = "You cancelled on GitHub."
UNREACHABLE = "Couldn't reach GitHub. Check your connection and try again."
FAILED = "Couldn't connect GitHub. Try again."


def _default_fetch(method: str, url: str, token: str = "", body: dict | None = None,
                   params: dict | None = None, timeout: float = 10) -> tuple[int, object]:
    """One call to github.com's OAuth endpoints: a form post that asks for a JSON answer.
    Returns (status, parsed JSON or None); a network failure is status 0."""
    import requests
    headers = {"Accept": "application/json", "User-Agent": "SponsorJobs-desktop"}
    try:
        r = requests.request(method, url, headers=headers, data=body, params=params,
                             timeout=timeout)
    except Exception:
        return 0, None
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data


FETCH = _default_fetch

# The one sign-in in progress. A new start replaces it; success, expiry and denial clear it.
_LOCK = threading.Lock()
_PENDING: dict = {}


def available() -> bool:
    """Whether Sign in with GitHub can run: only once the company's Client ID is filled in."""
    return bool(client_id())


def start() -> dict:
    """Ask GitHub for a code to show the person. {'ok', user_code, verification_uri,
    expires_in, interval} or {'ok': False, 'error'}. Never carries the device code."""
    cid = client_id()
    if not cid:
        return {"ok": False, "error": "Sign in with GitHub isn't set up in this build yet."}
    status, data = FETCH("POST", DEVICE_CODE_URL, "", {"client_id": cid, "scope": SCOPE}, None)
    if status == 0:
        return {"ok": False, "error": UNREACHABLE}
    if status != 200 or not isinstance(data, dict) or not data.get("device_code") \
            or not data.get("user_code"):
        return {"ok": False, "error": FAILED}
    interval = max(1, int(data.get("interval") or 5))
    expires_in = max(1, int(data.get("expires_in") or 900))
    now = time.monotonic()
    with _LOCK:
        _PENDING.clear()
        _PENDING.update(device_code=str(data["device_code"]), interval=interval,
                        expires_at=now + expires_in, next_at=now + interval)
    return {"ok": True, "user_code": str(data["user_code"]),
            "verification_uri": str(data.get("verification_uri") or "https://github.com/login/device"),
            "expires_in": expires_in, "interval": interval}


def cancel() -> None:
    """Forget the sign-in in progress (the person closed it)."""
    with _LOCK:
        _PENDING.clear()


def poll() -> dict:
    """One check with GitHub. Returns {'state': ...}:
    - 'pending' (with 'interval'): not approved yet, check again after interval seconds
    - 'ok' (with 'token'): approved; the caller verifies and saves it, and never echoes it
    - 'expired' / 'denied' / 'error' (with 'error'): one plain line for the page
    """
    with _LOCK:
        p = dict(_PENDING)
    if not p:
        return {"state": "error", "error": "Start again to get a new code."}
    now = time.monotonic()
    if now >= p["expires_at"]:
        cancel()
        return {"state": "expired", "error": EXPIRED}
    if now < p["next_at"]:
        # Asked too soon (a second tab, a double click): don't spend a request GitHub would
        # answer with slow_down.
        return {"state": "pending", "interval": p["interval"]}
    status, data = FETCH("POST", TOKEN_URL, "", {"client_id": client_id(),
                                                 "device_code": p["device_code"],
                                                 "grant_type": GRANT_TYPE}, None)
    if status == 0:
        _set(next_at=time.monotonic() + p["interval"])
        return {"state": "pending", "interval": p["interval"]}
    data = data if isinstance(data, dict) else {}
    if data.get("access_token"):
        cancel()
        return {"state": "ok", "token": str(data["access_token"])}
    err = str(data.get("error") or "")
    if err == "authorization_pending":
        _set(next_at=time.monotonic() + p["interval"])
        return {"state": "pending", "interval": p["interval"]}
    if err == "slow_down":
        interval = max(p["interval"] + SLOW_DOWN_STEP, int(data.get("interval") or 0))
        _set(interval=interval, next_at=time.monotonic() + interval)
        return {"state": "pending", "interval": interval}
    cancel()
    if err == "expired_token":
        return {"state": "expired", "error": EXPIRED}
    if err == "access_denied":
        return {"state": "denied", "error": DENIED}
    return {"state": "error", "error": FAILED}


def _set(**kw) -> None:
    with _LOCK:
        if _PENDING:
            _PENDING.update(kw)
