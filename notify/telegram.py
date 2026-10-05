"""Telegram Bot API transport — notify the person and take their reply while away.

Thin wrapper over the Bot API calls we need — ``sendMessage``, ``sendDocument`` (push a
CV preview), ``answerCallbackQuery`` (ack a tapped button), and ``getUpdates`` (read the
person's taps/replies by long-polling). Every call is OUTBOUND: the app reaches Telegram's
API, never the reverse. There is deliberately no webhook / no inbound listener, so the
local-first machine never needs an open port or a tunnel (CLAUDE.md §5). The HTTP callable
is injectable so all logic is testable offline with no token and no network. First-party,
GREEN-lane: the bot only ever talks to the person's OWN chat (see ``notify/service.py``).
"""

from __future__ import annotations

import json
import mimetypes
import urllib.request
import uuid
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"


def _urlopen_json(url: str, payload: dict | None = None) -> dict:  # pragma: no cover - net
    """Default OUTBOUND transport. JSON POST, or multipart when payload carries a ``_file``."""
    if payload and payload.get("_file"):
        return _urlopen_multipart(url, payload)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _urlopen_multipart(url: str, payload: dict) -> dict:  # pragma: no cover - net
    """Upload a local file (CV PDF or PNG) as multipart/form-data — still purely outbound.
    ``_file`` is the path and ``_field`` the Bot API field name (``document`` / ``photo``)."""
    path = Path(payload.pop("_file"))
    field = payload.pop("_field", "document")
    boundary = "----tailor" + uuid.uuid4().hex
    fields = {k: v for k, v in payload.items() if v is not None}
    body = bytearray()
    for k, v in fields.items():
        val = v if isinstance(v, str) else json.dumps(v)
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{val}\r\n".encode()
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
             f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def inline_keyboard(rows) -> dict:
    """Build a Telegram inline keyboard from rows of (text, callback_data) tuples."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for (t, d) in row] for row in rows]}


class TelegramBot:
    """Send messages/documents to, and read updates from, one Telegram chat.

    ``http`` is ``http(url, payload=None) -> dict`` — injected in tests, defaults to a small
    urllib client. ``chat_id`` is the person's own chat; every push goes there.
    """

    def __init__(self, token: str, chat_id: str = "", http=None):
        self.token = token
        self.chat_id = str(chat_id or "")
        self._http = http or _urlopen_json

    def _url(self, method: str) -> str:
        return _API.format(token=self.token, method=method)

    def send_message(self, text: str, buttons=None, chat_id: str = "") -> dict:
        """Push a message (optionally with inline buttons) to the owner's chat."""
        payload = {"chat_id": str(chat_id or self.chat_id), "text": text,
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = inline_keyboard(buttons)
        return self._http(self._url("sendMessage"), payload)

    def send_photo(self, file_path: str, caption: str = "", buttons=None, chat_id: str = "",
                   protect: bool = True) -> dict:
        """Upload a local image (e.g. the CV's first page PNG) shown INLINE, with a caption
        + inline buttons — so the person can review the CV at a glance in the chat.
        ``protect_content`` is on by default: Telegram then blocks forwarding and saving."""
        payload = {"chat_id": str(chat_id or self.chat_id), "_file": str(file_path),
                   "_field": "photo", "caption": caption[:1024]}
        if protect:
            payload["protect_content"] = True
        if buttons:
            payload["reply_markup"] = inline_keyboard(buttons)
        return self._http(self._url("sendPhoto"), payload)

    def send_document(self, file_path: str, caption: str = "", buttons=None, chat_id: str = "",
                      protect: bool = True) -> dict:
        """Upload a local file (e.g. the tailored CV PDF) with a caption + inline buttons."""
        payload = {"chat_id": str(chat_id or self.chat_id), "_file": str(file_path),
                   "_field": "document", "caption": caption[:1024]}
        if protect:
            payload["protect_content"] = True
        if buttons:
            payload["reply_markup"] = inline_keyboard(buttons)
        return self._http(self._url("sendDocument"), payload)

    def edit_message(self, message_id, text=None, caption=None, buttons=None,
                     chat_id: str = "") -> dict:
        """Change a message the bot already sent: its text (editMessageText), a photo's
        caption (editMessageCaption), or only its buttons (editMessageReplyMarkup; an empty
        list removes them)."""
        payload = {"chat_id": str(chat_id or self.chat_id), "message_id": message_id}
        if buttons is not None:
            payload["reply_markup"] = inline_keyboard(buttons) if buttons else {"inline_keyboard": []}
        if text is not None:
            payload["text"] = text
            payload["disable_web_page_preview"] = True
            return self._http(self._url("editMessageText"), payload)
        if caption is not None:
            payload["caption"] = caption[:1024]
            return self._http(self._url("editMessageCaption"), payload)
        return self._http(self._url("editMessageReplyMarkup"), payload)

    def answer_callback(self, callback_query_id: str, text: str = "") -> dict:
        """Acknowledge a tapped inline button (stops Telegram's spinner)."""
        return self._http(self._url("answerCallbackQuery"),
                          {"callback_query_id": callback_query_id, "text": text})

    def get_updates(self, offset: int = 0, timeout: int = 0) -> list[dict]:
        """Fetch new updates (messages + callback taps). ``offset`` acks everything below it."""
        payload = {"timeout": timeout}
        if offset:
            payload["offset"] = offset
        data = self._http(self._url("getUpdates"), payload)
        return (data or {}).get("result", []) if isinstance(data, dict) else []
