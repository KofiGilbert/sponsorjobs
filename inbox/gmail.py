"""Gmail adapter — read the person's OWN mailbox via the official API (CLAUDE.md §5).

OAuth first, IMAP/SMTP as a documented fallback (not built here yet). This module turns
the person's real Gmail into the plain message dicts ``inbox/service.py`` consumes, and
creates *draft* replies for send-on-consent — it never auto-sends.

The Google client libraries are imported lazily so the rest of the app (and all offline
tests) runs without them installed. Install them only when wiring a live mailbox:

    pip install google-api-python-client google-auth-oauthlib

Scope is deliberately the narrowest that works: READ-ONLY. The wired feature only reads
job-related mail and returns drafted reply TEXT for the person to send from Gmail
themselves, so no write/send scope is requested — the token literally cannot send or
modify anything. The OAuth token + client credentials are stored locally under config/ and
git-ignored (the rules live in the repo-root .gitignore: config/gmail_token.json,
config/gmail_credentials.json) — no cloud, like the LLM key.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

# Read-only, from THEIR account only. We intentionally do NOT request a write/send scope:
# Google's `gmail.compose` ("Manage drafts and send emails") would grant send capability,
# and there is no draft-only scope. Everything wired here only READS mail and produces
# reply text the person sends themselves — so read-only is the correct, narrowest grant.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

_CONFIG = Path(__file__).resolve().parents[1] / "config"
_TOKEN_PATH = _CONFIG / "gmail_token.json"
_CREDS_PATH = _CONFIG / "gmail_credentials.json"

# Optional token-storage override. The OAuth token grants mailbox access, so when App Lock
# is on the app installs a store that keeps it ENCRYPTED (in the lock) instead of a plaintext
# file. Default (nothing installed) = the git-ignored token file, unchanged behavior. This
# module stays decoupled from the lock: the app owns that policy and injects it here.
_TOKEN_LOAD = None   # () -> str | None   : the token JSON, or None if not connected
_TOKEN_SAVE = None   # (str) -> None      : persist a new/refreshed token JSON


def set_token_store(load, save) -> None:
    """Install how the Gmail OAuth token is loaded/saved (the app wires this to App Lock)."""
    global _TOKEN_LOAD, _TOKEN_SAVE
    _TOKEN_LOAD, _TOKEN_SAVE = load, save


def token_present(token_path=_TOKEN_PATH) -> bool:
    """Is a Gmail token available (via the installed store, else the plaintext file)?
    Used for the connection-status check, which must see a lock-held token, not just a file."""
    if _TOKEN_LOAD is not None:
        return bool(_TOKEN_LOAD())
    return Path(token_path).exists()

# Application-related mail only — we never sweep the whole mailbox. This narrows the
# read to job-relevant senders/subjects (read-first, CLAUDE.md §5).
DEFAULT_QUERY = (
    'newer_than:30d ('
    'subject:(verify OR confirm OR "your application" OR interview OR "next steps") '
    'OR from:(greenhouse OR lever OR ashby OR workable OR smartrecruiters OR recruiter))'
)


def _require_google():
    """Import the Google client libs lazily, with an actionable message if absent."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        return Request, Credentials, InstalledAppFlow, build
    except ImportError as e:  # pragma: no cover - only hit without the optional deps
        raise RuntimeError(
            "Gmail support needs the Google client libraries. Install them with:\n"
            "  pip install google-api-python-client google-auth-oauthlib"
        ) from e


def build_service(token_path=_TOKEN_PATH, creds_path=_CREDS_PATH):
    """Authorize against the person's Gmail and return an API service.

    Uses a cached local token when valid; otherwise runs the installed-app OAuth flow
    against ``creds_path`` (the OAuth client the person downloads from Google Cloud) and
    caches the resulting token locally.
    """
    Request, Credentials, InstalledAppFlow, build = _require_google()
    token_path, creds_path = Path(token_path), Path(creds_path)
    # Token storage is pluggable (App Lock installs an encrypted store); default = the file.
    load = _TOKEN_LOAD or (lambda: token_path.read_text(encoding="utf-8")
                           if token_path.exists() else None)

    def _default_save(s: str) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(s, encoding="utf-8")
    save = _TOKEN_SAVE or _default_save

    creds = None
    raw = load()
    if raw:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise RuntimeError(
                    f"Gmail OAuth client not found at {creds_path}. Create an OAuth "
                    "'Desktop app' client in Google Cloud and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        save(creds.to_json())      # a refreshed token goes back to wherever it came from
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_messages(service, query=DEFAULT_QUERY, max_results=25) -> list[dict]:
    """Return recent application-related messages as plain dicts for ``scan_inbox``."""
    listing = (service.users().messages()
               .list(userId="me", q=query, maxResults=max_results).execute())
    out: list[dict] = []
    for ref in listing.get("messages", []):
        full = (service.users().messages()
                .get(userId="me", id=ref["id"], format="full").execute())
        out.append(parse_message(full))
    return out


def parse_message(raw: dict) -> dict:
    """Normalize one Gmail API message resource into our message dict."""
    payload = raw.get("payload", {}) or {}
    headers = {h.get("name", "").lower(): h.get("value", "")
               for h in payload.get("headers", [])}
    return {
        "id": raw.get("id", ""),
        "thread_id": raw.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": _extract_body(payload) or raw.get("snippet", ""),
    }


def _extract_body(payload: dict) -> str:
    """Pull the readable body out of a (possibly multipart) Gmail payload.

    Prefer text/plain anywhere in the tree; only if there is none do we fall back to
    whatever body data the payload carries (e.g. a single text/html-only message)."""
    return _find_plain(payload) or _any_body(payload)


# A real email nests a few MIME levels; a hostile deeply-nested tree is capped here so it
# returns "" gracefully instead of RecursionError-ing (which would 500 the live scan route).
_MAX_MIME_DEPTH = 40


def _find_plain(payload: dict, _depth: int = 0) -> str:
    if _depth > _MAX_MIME_DEPTH:
        return ""
    if payload.get("mimeType", "") == "text/plain":
        data = (payload.get("body", {}) or {}).get("data")
        if data:
            return _b64(data)
    for part in payload.get("parts", []) or []:
        text = _find_plain(part, _depth + 1)
        if text:
            return text
    return ""


def _any_body(payload: dict, _depth: int = 0) -> str:
    if _depth > _MAX_MIME_DEPTH:
        return ""
    data = (payload.get("body", {}) or {}).get("data")
    if data:
        return _b64(data)
    for part in payload.get("parts", []) or []:
        text = _any_body(part, _depth + 1)
        if text:
            return text
    return ""


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
    except Exception:
        return ""


def create_draft(service, to: str, subject: str, body: str, thread_id: str = "") -> str:
    """Create a Gmail DRAFT reply (send-on-consent) and return its draft id.

    The person opens the draft in Gmail and hits send — we never send for them.

    NOTE: this is an unwired seam. It needs the ``gmail.compose`` write scope, which
    ``SCOPES`` intentionally does NOT request (we ship read-only). Before wiring this to an
    endpoint, add ``gmail.compose`` back to ``SCOPES`` — or, to stay read-only, open a
    pre-filled Gmail compose URL client-side instead so no write scope is ever needed.
    """
    import email.message

    m = email.message.EmailMessage()
    m["To"] = to
    m["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    m.set_content(body)
    encoded = base64.urlsafe_b64encode(m.as_bytes()).decode("utf-8")
    message = {"raw": encoded}
    if thread_id:
        message["threadId"] = thread_id
    created = (service.users().drafts()
               .create(userId="me", body={"message": message}).execute())
    return created.get("id", "")


def _is_safe_public_url(url: str) -> bool:
    """A verification link must be a PUBLIC http(s) URL. Reject non-http schemes (file://,
    ftp://, gopher://…) and internal/loopback/link-local/private hosts — so auto-opening a
    link taken from an email can never be turned into an SSRF or a local-file read when this
    seam is eventually wired to an autonomous route (defense-in-depth for §5 'that and only
    that'). DNS-rebinding of a hostname is out of scope for this static check."""
    import ipaddress
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True                     # a hostname, not a literal IP — allow
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate EVERY redirect target, so a public verification link can't 302 into a
    private/loopback host (cloud metadata at 169.254.169.254, or this very app at
    127.0.0.1). Refuses the unsafe hop instead of following it — the initial-URL check alone
    doesn't cover redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_safe_public_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "unsafe redirect target refused",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def complete_verification(link: str, opener=None) -> dict:
    """Visit an application's OWN email-verification link (autonomous mode only).

    Returns ``{"ok": bool, "status": int, "link": str}``. ``opener`` is injected in tests;
    by default a short GET is issued with urllib. Only ever call this for a link that came
    from a verification email tied to an application the person is actually submitting — and
    the link is additionally validated to be a public http(s) URL before it is opened.
    """
    if not link:
        return {"ok": False, "status": 0, "link": ""}
    if not _is_safe_public_url(link):
        # Never open a non-public / non-http link from an email (SSRF / local-file / scheme abuse).
        return {"ok": False, "status": 0, "link": link,
                "error": "refused: not a public http(s) verification URL"}
    try:
        if opener is not None:
            status = int(opener(link))
        else:  # pragma: no cover - real network path
            # Follow redirects only to public hosts (re-validated per hop), never a private
            # or loopback target smuggled in via a 302.
            safe_opener = urllib.request.build_opener(_SafeRedirectHandler)
            with safe_opener.open(link, timeout=15) as resp:
                status = int(getattr(resp, "status", 0) or resp.getcode())
        return {"ok": 200 <= status < 400, "status": status, "link": link}
    except Exception as e:  # pragma: no cover - network failure path
        return {"ok": False, "status": 0, "link": link, "error": str(e)}
