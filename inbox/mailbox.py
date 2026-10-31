"""The person's OWN mailbox as one small interface (CLAUDE.md section 5, docs/apply-agent.md
milestone 3).

An unattended apply run needs three things that only arrive by email: the verification
link a portal sends after creating an account, a one-time code, and a password-reset link.
``Mailbox.search(since_epoch)`` returns the messages that arrived at or after a moment,
newest first, as the same plain dicts ``inbox/service.py`` and ``inbox/detect.py`` consume:
{"from", "subject", "body", "date", "id"}.

Three implementations, all READ-ONLY (nothing is ever marked read, moved or flagged):

  * GmailApiMailbox: the existing OAuth Gmail API connector (inbox/gmail.py).
  * ImapMailbox: stdlib imaplib over IMAP4_SSL with an app password, for Gmail (the
    cheapest setup: no Google Cloud project) and for other providers.
  * FakeMailbox: in memory, for tests and the shell e2e (RESUME_AGENT_FAKE_MAILBOX=1 in a
    dev session only, never shipped).

The caller filters the results by ``detect.about_site`` before anything is used, so mail
that is not about the site being applied to is never looked at (read-first, section 5).
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import re
import socket
import time
from typing import Protocol

from . import gmail

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class Mailbox(Protocol):
    def search(self, since_epoch: float, *, max_results: int = 25) -> list[dict]:
        """Messages received at/after ``since_epoch`` (epoch seconds), newest first."""
        ...


# ---------------------------------------------------------------------------- Gmail API
class GmailApiMailbox:
    """The OAuth Gmail connector behind the Mailbox interface. ``service`` is injected in
    tests; otherwise it is built lazily on the first search (the token must already exist)."""

    method = "gmail"

    def __init__(self, service=None):
        self._service = service

    def _svc(self):
        if self._service is None:
            self._service = gmail.build_service()
        return self._service

    def search(self, since_epoch: float, *, max_results: int = 25) -> list[dict]:
        # Gmail's after: takes epoch seconds. Promotions never carry a verification mail.
        query = f"after:{int(since_epoch)} -category:promotions"
        return gmail.fetch_messages(self._svc(), query=query, max_results=max_results)


# ---------------------------------------------------------------------------- IMAP
class ImapMailbox:
    """IMAP over SSL with the person's email + app password. One short-lived connection per
    call; the INBOX is selected read-only so the server itself refuses any write."""

    method = "imap"

    def __init__(self, email: str, password: str, host: str = "imap.gmail.com", port: int = 993,
                 timeout: float = 20.0):
        self.email = email
        self._password = password
        self.host = host or "imap.gmail.com"
        self.port = int(port or 993)
        self.timeout = timeout

    def _connect(self):
        # imaplib.IMAP4_SSL is looked up at call time so tests can stub it.
        conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        conn.login(self.email, self._password)
        return conn

    def test_login(self) -> dict:
        """{ok, error}: sign in and straight out again. Reads no mail."""
        try:
            conn = self._connect()
        except imaplib.IMAP4.error:
            if "gmail" in self.host or "google" in self.host:
                return {"ok": False, "error": "Google refused the sign-in. Check the app "
                                              "password and that IMAP is on."}
            return {"ok": False, "error": "The mail server refused the sign-in. Check the "
                                          "email, the password and that IMAP is on."}
        except (OSError, socket.timeout):
            return {"ok": False, "error": f"Could not reach {self.host}. Check the server "
                                          "name and your connection."}
        try:
            conn.logout()
        except Exception:
            pass
        return {"ok": True, "error": ""}

    def search(self, since_epoch: float, *, max_results: int = 25) -> list[dict]:
        conn = self._connect()
        try:
            conn.select("INBOX", readonly=True)
            # SINCE is day-granular in the server's zone: ask from the day before and let the
            # INTERNALDATE filter below do the exact cut.
            t = time.gmtime(since_epoch - 86400)
            day = f"{t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year}"
            status, data = conn.uid("search", None, f"(SINCE {day})")
            if status != "OK" or not data or not data[0]:
                return []
            uids = sorted((int(u) for u in data[0].split()), reverse=True)[:max_results]
            if not uids:
                return []
            status, items = conn.uid("fetch", ",".join(str(u) for u in uids),
                                     "(INTERNALDATE BODY.PEEK[])")
            if status != "OK":
                return []
            out = []
            for item in items or []:
                if not (isinstance(item, tuple) and len(item) >= 2):
                    continue
                head, raw = item[0], item[1]
                received = _internaldate_epoch(head)
                if received is not None and received < since_epoch:
                    continue
                m = re.search(rb"UID (\d+)", head or b"")
                msg = parse_rfc822(raw, uid=(m.group(1).decode() if m else ""))
                msg["received"] = received if received is not None else since_epoch
                out.append(msg)
            out.sort(key=lambda d: d.get("received", 0), reverse=True)
            for d in out:
                d.pop("received", None)
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _internaldate_epoch(head: bytes) -> float | None:
    """The server's receipt time from a FETCH response line, as epoch seconds."""
    try:
        tt = imaplib.Internaldate2tuple(head or b"")
    except Exception:
        return None
    return time.mktime(tt) if tt else None


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+\n|\n{3,}")


def html_to_text(src: str) -> str:
    """Drop tags, keep the words. Hrefs are kept inline so link detection still works."""
    src = _TAG_RE.sub(" ", src or "")
    src = re.sub(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>', r" \1 ", src, flags=re.I)
    src = re.sub(r"<\s*(br|/p|/div|/tr|/li|/h\d)\s*/?>", "\n", src, flags=re.I)
    text = html.unescape(_ANY_TAG_RE.sub(" ", src))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def parse_rfc822(raw: bytes, uid: str = "") -> dict:
    """One raw RFC822 message as our message dict. Prefers text/plain; strips html otherwise.
    The default email policy decodes RFC2047 subjects and names for us."""
    try:
        m = email.message_from_bytes(raw or b"", policy=email.policy.default)
    except Exception:
        return {"id": uid, "from": "", "subject": "", "body": "", "date": ""}
    body = ""
    try:
        part = m.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            body = html_to_text(content) if part.get_content_type() == "text/html" else content
    except Exception:
        body = ""
    rfc_id = str(m.get("Message-ID") or "").strip()
    msg_id = rfc_id or uid
    return {
        "id": msg_id,
        "from": str(m.get("From") or ""),
        "subject": str(m.get("Subject") or ""),
        "body": body or "",
        "date": str(m.get("Date") or ""),
        "rfc_id": rfc_id,
        "in_reply_to": str(m.get("In-Reply-To") or "").strip(),
        "references": " ".join(str(m.get("References") or "").split()),
    }


# ---------------------------------------------------------------------------- fake
class FakeMailbox:
    """In-memory mailbox for tests and the shell e2e. ``deliver`` is what a server would do."""

    method = "fake"
    email = "fake@example.com"

    def __init__(self):
        self._messages: list[dict] = []
        self._n = 0

    def deliver(self, msg: dict, received: float | None = None) -> dict:
        self._n += 1
        now = time.time() if received is None else float(received)
        stored = {
            "id": str(msg.get("id") or f"fake-{self._n}"),
            "from": str(msg.get("from") or ""),
            "subject": str(msg.get("subject") or ""),
            "body": str(msg.get("body") or ""),
            "date": str(msg.get("date") or email.utils.formatdate(now)),
            "received": now,
        }
        self._messages.append(stored)
        return {k: v for k, v in stored.items() if k != "received"}

    def search(self, since_epoch: float, *, max_results: int = 25) -> list[dict]:
        hits = [m for m in self._messages if m["received"] >= since_epoch]
        hits.sort(key=lambda m: m["received"], reverse=True)
        return [{k: v for k, v in m.items() if k != "received"} for m in hits[:max_results]]

    def clear(self) -> None:
        self._messages.clear()


_FAKE: FakeMailbox | None = None


def fake_mailbox() -> FakeMailbox:
    """The one FakeMailbox a dev session shares between /dev/deliver and /await."""
    global _FAKE
    if _FAKE is None:
        _FAKE = FakeMailbox()
    return _FAKE


# ---------------------------------------------------------------------------- picking one
def guess_imap_host(address: str) -> str:
    """The IMAP server for a mailbox address, for the common providers."""
    domain = (address or "").rpartition("@")[2].lower()
    known = {
        "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
        "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
        "live.com": "outlook.office365.com", "msn.com": "outlook.office365.com",
        "yahoo.com": "imap.mail.yahoo.com", "ymail.com": "imap.mail.yahoo.com",
        "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com",
        "aol.com": "imap.aol.com",
    }
    return known.get(domain) or (f"imap.{domain}" if domain else "imap.gmail.com")


def pick_mailbox(cred, fake: bool = False, relay=None):
    """The mailbox to read, by what is connected: the FakeMailbox in a dev session that asked
    for it, else the SponsorJobs alias through the broker (``relay``, an inbox.relay.RelayMailbox
    the app builds when the person took an address), else IMAP when its credentials are saved,
    else the Gmail API when its token exists, else None. ``cred(name)`` is the app's credential
    reader."""
    if fake:
        return fake_mailbox()
    if relay is not None:
        return relay
    address, password = cred("IMAP_EMAIL"), cred("IMAP_PASSWORD")
    if address and password:
        return ImapMailbox(address, password, host=cred("IMAP_HOST") or guess_imap_host(address))
    if gmail.token_present():
        return GmailApiMailbox()
    return None


def mask_email(address: str) -> str:
    """k***@gmail.com: enough to recognise the account, never the whole address."""
    address = (address or "").strip()
    if "@" not in address:
        return ""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"
