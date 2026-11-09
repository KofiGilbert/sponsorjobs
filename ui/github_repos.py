"""The person's PUBLIC GitHub repositories, read without signing in, for the resume chat.

GitHub lists any account's public repositories at GET /users/{login}/repos with no token (60
requests an hour per address). The login comes from the GitHub link on the person's own resume
header (identity.github) or from their connected account. When GitHub is connected the stored
token goes with the request (a higher limit); it never leaves this module: no result, card, log
line or model call ever carries it.

What comes back is a compact card per repository the person OWNS and worked on: forks,
archived repositories and empty ones (size 0) are left out. Results are cached per login for
a while, so a chat that asks twice costs one call. A rate limit (403 or 429) and being offline
are said plainly, never as an error trace.

Every HTTP call goes through ``github_publish.FETCH`` (read at call time), the seam tests
replace so nothing touches the network.
"""

from __future__ import annotations

import re
import time

from ui import github_publish

CACHE_TTL = 15 * 60            # a repo list is re-read at most every 15 minutes
BLOCK_FOR = 15 * 60            # after a rate limit, no calls for 15 minutes
MAX_REPOS = 100

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_PROFILE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})/?(?:[?#].*)?$", re.I)

_CACHE: dict[str, tuple[float, dict]] = {}
_BLOCKED_UNTIL = {"t": 0.0}

RATE_LIMITED = ("GitHub is limiting how often the app can look right now (about 60 lookups an "
                "hour without signing in). Try again in a few minutes.")
OFFLINE = "I could not reach GitHub just now. Check the internet connection and ask again."
NOT_FOUND = "GitHub has no account named {login}."


def login_from(value: str) -> str:
    """The GitHub login in a profile link ("github.com/KofiGilbert") or a bare login. "" when
    the value is neither (a repository link is not a profile)."""
    v = str(value or "").strip().rstrip("/")
    if not v:
        return ""
    m = _PROFILE.search(v)
    if m:
        return m.group(1)
    if "/" in v or "." in v:
        return ""
    v = v.lstrip("@")
    return v if _LOGIN.match(v) else ""


def _card(r: dict) -> dict:
    return {
        "name": str(r.get("name") or ""),
        "url": str(r.get("html_url") or ""),
        "description": str(r.get("description") or "")[:300],
        "topics": [str(t) for t in (r.get("topics") or [])][:10],
        "language": str(r.get("language") or ""),
        "stars": int(r.get("stargazers_count") or 0),
        "pushed_at": str(r.get("pushed_at") or ""),
        "created_at": str(r.get("created_at") or ""),
        # A private repository would 404 for a recruiter, so an entry made from it has no link.
        "private": bool(r.get("private")),
    }


def _keep(r: dict) -> bool:
    return (isinstance(r, dict) and not r.get("fork") and not r.get("archived")
            and int(r.get("size") or 0) > 0 and bool(r.get("name")))


def list_repos(login: str, token: str = "", now: float | None = None) -> dict:
    """{"ok", "login", "repos": [card...], "dropped": {...}, "error"?} for a login's own public
    repositories, newest push first. Never raises, never returns the token."""
    login = login_from(login)
    if not login:
        return {"ok": False, "login": "", "repos": [],
                "error": "I need a GitHub username or profile link to look up repositories."}
    now = time.time() if now is None else now
    key = login.lower()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return dict(hit[1])
    if now < _BLOCKED_UNTIL["t"] and not token:
        return {"ok": False, "login": login, "repos": [], "error": RATE_LIMITED, "reason": "rate_limited"}
    try:
        status, data = github_publish.FETCH(
            "GET", f"{github_publish.API}/users/{login}/repos", token or "", None,
            {"type": "owner", "sort": "pushed", "direction": "desc", "per_page": MAX_REPOS})
    except Exception:          # noqa: BLE001 - a broken transport reads as offline
        status, data = 0, None
    if status == 0:
        return {"ok": False, "login": login, "repos": [], "error": OFFLINE, "reason": "offline"}
    if status in (403, 429):
        _BLOCKED_UNTIL["t"] = now + BLOCK_FOR
        return {"ok": False, "login": login, "repos": [], "error": RATE_LIMITED, "reason": "rate_limited"}
    if status == 404:
        return {"ok": False, "login": login, "repos": [], "error": NOT_FOUND.format(login=login),
                "reason": "not_found"}
    if status != 200 or not isinstance(data, list):
        return {"ok": False, "login": login, "repos": [], "error": OFFLINE, "reason": "error"}
    dropped = {"forks": 0, "archived": 0, "empty": 0}
    repos = []
    for r in data[:MAX_REPOS]:
        if not isinstance(r, dict):
            continue
        if r.get("fork"):
            dropped["forks"] += 1
        elif r.get("archived"):
            dropped["archived"] += 1
        elif int(r.get("size") or 0) <= 0:
            dropped["empty"] += 1
        if _keep(r):
            repos.append(_card(r))
    out = {"ok": True, "login": login, "repos": repos, "dropped": dropped}
    _CACHE[key] = (now, out)
    return dict(out)


def clear_cache() -> None:
    _CACHE.clear()
    _LOGINS.clear()
    _BLOCKED_UNTIL["t"] = 0.0


# ----------------------------------------------------------------------------- connection

_LOGINS: dict[str, tuple[float, int, str]] = {}   # sha256(token) -> (when, status, login)
LOGIN_TTL = 6 * 3600


def connection(token: str, login: str = "", now: float | None = None) -> dict:
    """{"connected", "login", "reason"?} that agrees with the token the repo lookup uses.

    A saved token IS a connection, so the chat never says "isn't connected" while that token
    makes the call. When the saved login is missing, GitHub's own GET /user names it, once
    (cached by a hash of the token, never the token itself). A token GitHub rejects (401) reads
    as not connected, with the reason; offline keeps it connected with no login. The token is
    never returned."""
    token = str(token or "").strip()
    login = str(login or "").strip()
    if not token:
        return {"connected": False, "login": ""}
    if login:
        return {"connected": True, "login": login}
    import hashlib
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time() if now is None else now
    hit = _LOGINS.get(key)
    if hit is None or (now - hit[0] > LOGIN_TTL and hit[1] != 401):
        try:
            status, data = github_publish.FETCH("GET", f"{github_publish.API}/user", token, None, None)
        except Exception:      # noqa: BLE001 - a broken transport reads as offline
            status, data = 0, None
        got = str((data or {}).get("login") or "") if isinstance(data, dict) and status == 200 else ""
        hit = (now, int(status or 0), got)
        if status in (200, 401):           # offline and rate limits are asked again next time
            _LOGINS[key] = hit
    status, got = hit[1], hit[2]
    if status == 401:
        return {"connected": False, "login": "", "reason": "token_rejected"}
    return {"connected": True, "login": got}
