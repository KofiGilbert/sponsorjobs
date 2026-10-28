"""The SponsorJobs mail alias, read through the broker (docs/mail-alias.md).

Each person can take an address on our domain (first.last@sponsorjobs.ai) and apply with it. A
job site's verification link or one-time code lands there; the broker's Email Worker keeps ONLY
the extracted code or link for 30 minutes and forwards the whole message to the person's real
inbox. The app polls ``GET /mail/inbox?after=<cursor>`` with its own account token, so nothing
about the person's real mailbox is ever touched (CLAUDE.md section 5, read-first and by consent).

``RelayMailbox`` turns those events into the same message dicts inbox/service.py and
inbox/detect.py consume, so /api/inbox/await works unchanged whichever mailbox is connected.
Transport is injected (``fetch(method, path) -> dict``) so the tests run with no network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

EVENT_WINDOW = 30 * 60              # the broker keeps an event this long; we never ask for older


def urllib_fetch(broker_url: str, token: str, timeout: float = 15.0):
    """A ``fetch(method, path, body=None) -> dict`` over the broker with the app's bearer token."""
    base = (broker_url or "").rstrip("/")

    def fetch(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:   # nosec - our broker
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode() or "{}")
            except ValueError:
                err = {}
            err.setdefault("error", f"http {exc.code}")
            err["status"] = exc.code
            return err
    return fetch


def alias_status(fetch) -> dict:
    """{alias, forward_to} for this account, or {alias: None} when none is taken yet."""
    out = fetch("GET", "/mail/alias") or {}
    return {"alias": out.get("alias") or None, "forward_to": out.get("forward_to") or None,
            "error": out.get("error") if not out.get("alias") else None}


def claim_alias(fetch, name: str = "") -> dict:
    """Take an alias (idempotent). {ok, alias, forward_to} or {ok: False, error}."""
    out = fetch("POST", "/mail/alias", {"name": name or ""}) or {}
    if out.get("alias"):
        return {"ok": True, "alias": out["alias"], "forward_to": out.get("forward_to")}
    return {"ok": False, "error": out.get("error") or "could not create the address"}


def release_alias(fetch) -> dict:
    out = fetch("DELETE", "/mail/alias") or {}
    return {"ok": not out.get("error"), "error": out.get("error")}


def event_message(ev: dict) -> dict:
    """One relay event as the message dict the detectors read. The body carries only what the
    Worker extracted (a code or a link), so the detectors find it where they expect to."""
    kind = str(ev.get("kind") or "")
    code, link = str(ev.get("code") or ""), str(ev.get("link") or "")
    if kind == "code" and code:
        body = f"Your verification code is {code}"
    elif kind == "reset" and link:
        body = f"Reset your password: {link}"
    elif link:
        body = f"Verify your email: {link}"
    else:
        body = ""
    at = ev.get("at")
    try:
        date = datetime.fromtimestamp(float(at), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        date = ""
    return {"from": str(ev.get("sender") or ""), "subject": str(ev.get("subject") or ""),
            "body": body, "date": date, "id": f"relay:{ev.get('cursor')}", "_at": at, "_kind": kind}


class RelayMailbox:
    """Read the alias events the broker holds for this account. Read-only, no real mailbox."""

    name = "alias"

    def __init__(self, fetch, alias: str = "") -> None:
        self._fetch = fetch
        self.alias = alias
        self._seen: list[dict] = []          # events already acknowledged, kept for this process
        self._cursor = 0

    def search(self, since_epoch: float, *, max_results: int = 25) -> list[dict]:
        out = self._fetch("GET", f"/mail/inbox?after={self._cursor}") or {}
        if out.get("error"):
            raise RuntimeError(str(out.get("error")))
        for ev in out.get("events") or []:
            if not isinstance(ev, dict):
                continue
            self._seen.append(event_message(ev))
            try:
                self._cursor = max(self._cursor, int(ev.get("cursor") or 0))
            except (TypeError, ValueError):
                pass
        # The broker drops an event after 30 minutes; so do we, and never one before ``since``.
        now_floor = float(since_epoch or 0)
        keep = []
        for m in self._seen:
            try:
                at = float(m.get("_at") or 0)
            except (TypeError, ValueError):
                at = 0.0
            if at >= now_floor:
                keep.append(m)
        self._seen = keep[-200:]
        rows = sorted(self._seen, key=lambda m: float(m.get("_at") or 0), reverse=True)
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in rows[:max_results]]
