"""Project dates from the person's OWN public GitHub repos (owner decision 2026-10-26).

A project's dates are SOFT (ui/session._missing_slots): they never block a build. Before
asking for them, the app fills them from a real source when it has one: the repo's
``created_at`` and ``pushed_at`` from the public GitHub REST API, for a project that links its
repo, or whose name matches a repo under the person's own GitHub profile link. Never a date
without that source.

- Unauthenticated, or with the person's saved GitHub token when Connections has one.
- Cached per repo (a found repo and a 404 alike) in a local JSON file, so a run of many roles
  costs one call per repo per week, and a rate limit (403/429) pauses all lookups for an hour.
- Offline-safe: a network failure fills nothing and caches nothing.
- Every HTTP call goes through ``FETCH`` (ui/github_publish's seam), which tests replace.
  ``GITHUB_DATES_FETCH=0`` turns lookups off (the test suite's default, tests/conftest.py).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ui import github_publish

API = github_publish.API
FETCH = github_publish._default_fetch
CACHE_TTL = 7 * 86400          # a repo's dates are re-read at most weekly
BLOCK_FOR = 3600               # after a rate limit, no lookups for an hour
RECENT_DAYS = 60               # pushed within this many days reads as "Present"

_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", re.I)
_USER_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)/?$", re.I)


def repo_for(project: dict, github_profile: str = "") -> tuple[str, str] | None:
    """(owner, repo) for a project: its own repo link first, else the person's GitHub
    profile link plus the project's name as a repo slug. None when neither applies."""
    for k in ("link", "url"):
        m = _REPO_RE.search(str(project.get(k) or ""))
        if m:
            return m.group(1), re.sub(r"\.git$", "", m.group(2))
    m = _USER_RE.search(str(github_profile or "").strip())
    name = str(project.get("title") or project.get("org") or "").strip()
    if m and name:
        return m.group(1), github_publish.slugify(name)
    return None


def _when(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def dates_from(meta: dict, now: datetime) -> str:
    """"Mon YYYY - Mon YYYY", or "Mon YYYY - Present" when pushed in the last 60 days."""
    start, end = _when(meta.get("created_at")), _when(meta.get("pushed_at"))
    if not start:
        return ""
    if end and (now - end).days <= RECENT_DAYS:
        return f"{start:%b %Y} - Present"
    return f"{start:%b %Y} - {(end or start):%b %Y}"


class _Cache:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def save(self):
        if self.dirty:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data), encoding="utf-8")
            except OSError:
                pass


def repo_meta(owner: str, repo: str, cache: _Cache, token: str = "", now_ts: float | None = None):
    """The repo's {created_at, pushed_at}, or None (no such repo, rate limited, offline)."""
    now_ts = time.time() if now_ts is None else now_ts
    if os.environ.get("GITHUB_DATES_FETCH") == "0":
        return None
    key = f"{owner}/{repo}".lower()
    hit = cache.data.get(key)
    if hit and now_ts - float(hit.get("at") or 0) < CACHE_TTL:
        return hit.get("meta")
    if now_ts < float(cache.data.get("_blocked_until") or 0):
        return None
    status, data = FETCH("GET", f"{API}/repos/{owner}/{repo}", token, None, None)
    if status == 200 and isinstance(data, dict):
        meta = {"created_at": data.get("created_at"), "pushed_at": data.get("pushed_at")}
    elif status == 404:
        meta = None
    elif status in (403, 429):
        cache.data["_blocked_until"] = now_ts + BLOCK_FOR
        cache.dirty = True
        return None
    else:                         # offline (0) or a server error: try again next time
        return None
    cache.data[key] = {"at": now_ts, "meta": meta}
    cache.dirty = True
    return meta


def fill_project_dates(projects: list, github_profile: str, cache_path: Path,
                       token: str = "", now: datetime | None = None) -> list[str]:
    """Fill each undated project's dates from its repo, in place. Returns the names filled."""
    now = now or datetime.now(timezone.utc)
    cache = _Cache(cache_path)
    filled = []
    for p in projects or []:
        if not isinstance(p, dict) or str(p.get("dates") or "").strip():
            continue
        target = repo_for(p, github_profile)
        if not target:
            continue
        meta = repo_meta(*target, cache, token, now.timestamp())
        d = dates_from(meta, now) if meta else ""
        if d:
            p["dates"] = d
            filled.append(str(p.get("title") or p.get("org") or target[1]))
    cache.save()
    return filled
