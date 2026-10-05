"""The official SponsorJobs Telegram bot, relayed by the broker (docs/notify.md).

One bot for everyone: the token lives ONLY here, server-side (env TELEGRAM_BOT_TOKEN), so a user
connects by tapping a ``t.me/<bot>?start=<code>`` link instead of creating a bot of their own.

The broker is a RELAY, not a store of personal data. It never sees a résumé. It keeps exactly:
  * telegram_links  -- a hashed one-time link code -> account, 15 minutes;
  * telegram_chats  -- account -> chat_id, Telegram username, paused flag, linked time;
  * telegram_events -- the user's button taps / replies waiting for their app to poll, deleted
    once the app acknowledges them (a later ``after`` cursor) or after 7 days.
Outgoing messages carry only what the app sends (job title, company, short text, button ids) and
are not persisted. A CV preview picture or PDF (``/notify/telegram/send_media``) is streamed
straight through to Telegram in the same request and never written anywhere; only its size is
logged. Every photo and document goes out with ``protect_content`` (no forwarding or saving).
Send rate limits are kept in memory (one worker process; see render.yaml).

Everything is injected (store, HTTP transport, clock) so the tests run with no network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import jsonify, request

LINK_TTL = 900                       # seconds a /start code stays valid
EVENT_TTL = 7 * 24 * 3600            # inbox events are dropped after 7 days
INBOX_LIMIT = 50
SEND_PER_DAY = 100   # a CV review session (preview, changes, edits) uses about 10
SEND_MIN_INTERVAL = 3.0
MAX_TEXT = 4096                      # Telegram's own message limit (a "Show changes" chunk)
MAX_EVENT_TEXT = 1000                # what we keep of a typed reply in the inbox
MAX_CAPTION = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
EDIT_MIN_INTERVAL = 1.0
EDIT_PER_DAY = 200
MAX_ROWS = MAX_COLS = 3
MAX_BUTTON_ID = 40
MAX_BUTTON_LABEL = 30
_BUTTON_ID_RE = re.compile(r"^[A-Za-z0-9:_-]+$")

MSG_CONNECTED = "Connected. I'll message you about new jobs and applications. Send /stop to pause."
MSG_EXPIRED = "That link expired, open SponsorJobs and try again."
MSG_UNBOUND = "Open SponsorJobs, Settings, Notifications to connect."
MSG_PAUSED = "Paused. I won't message you until you send /resume."
MSG_RESUMED = "Resumed. I'll message you about new jobs and applications again."
MSG_HELP = ("I'm the SponsorJobs bot. I message you about new jobs and applications.\n"
            "/stop pauses messages, /resume turns them back on.\n"
            "To disconnect, open SponsorJobs, Settings, Notifications.")
MSG_DISCONNECTED = "Disconnected from SponsorJobs."


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hash_code(code: str) -> str:
    return hashlib.sha256((code or "").encode()).hexdigest()


class TelegramError(Exception):
    """A Telegram Bot API call failed (network, or ok:false)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def urllib_transport(token: str, timeout: float = 10.0):
    """The real transport: POST JSON to api.telegram.org. Returns the API ``result``."""
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, payload: dict) -> dict:
        req = urllib.request.Request(base + method, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed https host
                body = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode() or "{}")
            except Exception:                                          # noqa: BLE001
                body = {}
            raise TelegramError(body.get("description") or f"HTTP {exc.code}", exc.code) from None
        except Exception as exc:                                       # noqa: BLE001
            raise TelegramError(f"{type(exc).__name__}") from None    # never echo the URL (has the token)
        if not body.get("ok"):
            raise TelegramError(body.get("description") or "telegram error", body.get("error_code"))
        return body.get("result") or {}
    return call


def urllib_media_transport(token: str, timeout: float = 60.0):
    """Multipart upload to api.telegram.org (sendPhoto / sendDocument / editMessageMedia).
    ``call(method, fields, file_field, filename, data, content_type)``; ``data`` is bytes held
    only for this request, never written to disk."""
    import uuid
    base = f"https://api.telegram.org/bot{token}/"

    def call(method: str, fields: dict, file_field: str, filename: str, data: bytes,
             content_type: str) -> dict:
        boundary = "----sj" + uuid.uuid4().hex
        body = bytearray()
        for k, v in fields.items():
            if v is None:
                continue
            val = v if isinstance(v, str) else json.dumps(v)
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{val}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
        body += data + b"\r\n" + f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(base + method, data=bytes(body), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed https host
                out = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read().decode() or "{}")
            except Exception:                                          # noqa: BLE001
                out = {}
            raise TelegramError(out.get("description") or f"HTTP {exc.code}", exc.code) from None
        except Exception as exc:                                       # noqa: BLE001
            raise TelegramError(f"{type(exc).__name__}") from None
        if not out.get("ok"):
            raise TelegramError(out.get("description") or "telegram error", out.get("error_code"))
        return out.get("result") or {}
    return call


# --------------------------------------------------------------------------------------------- #
# Storage: the in-memory double (tests). The SQLite store lives in backend/store_sqlite.py.
# --------------------------------------------------------------------------------------------- #
class InMemoryTelegramStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._links: dict[str, tuple[str, float]] = {}       # code_hash -> (account, expires_at)
        self._chats: dict[str, dict] = {}                    # account -> chat row
        self._events: list[dict] = []
        self._next_id = 1

    def add_link(self, code_hash: str, account: str, expires_at: float) -> None:
        with self._lock:
            self._links[code_hash] = (account, expires_at)

    def take_link(self, code_hash: str, now: float) -> str | None:
        with self._lock:
            for h in [h for h, (_, exp) in self._links.items() if exp <= now]:
                del self._links[h]
            row = self._links.pop(code_hash, None)
        return row[0] if row else None

    def bind_chat(self, account: str, chat_id: int, username: str | None, linked_at: str) -> None:
        with self._lock:
            for acct in [a for a, r in self._chats.items() if r["chat_id"] == chat_id]:
                del self._chats[acct]
            self._chats[account] = {"chat_id": chat_id, "username": username,
                                    "paused": False, "linked_at": linked_at}

    def get_chat(self, account: str) -> dict | None:
        row = self._chats.get(account)
        return dict(row) if row else None

    def account_for_chat(self, chat_id: int) -> str | None:
        for acct, r in self._chats.items():
            if r["chat_id"] == chat_id:
                return acct
        return None

    def set_paused(self, account: str, paused: bool) -> None:
        with self._lock:
            if account in self._chats:
                self._chats[account]["paused"] = bool(paused)

    def unbind(self, account: str) -> None:
        with self._lock:
            self._chats.pop(account, None)
            self._events = [e for e in self._events if e["account"] != account]

    def add_event(self, account: str, type_: str, data: str, text: str, at: float) -> int:
        with self._lock:
            eid = self._next_id
            self._next_id += 1
            self._events.append({"id": eid, "account": account, "type": type_,
                                 "data": data, "text": text, "at": at})
            return eid

    def ack_and_list(self, account: str, after: int, older_than: float, limit: int) -> list[dict]:
        with self._lock:
            self._events = [e for e in self._events
                            if e["at"] >= older_than and not (e["account"] == account and e["id"] <= after)]
            return [dict(e) for e in self._events if e["account"] == account][:limit]


# --------------------------------------------------------------------------------------------- #
# The relay
# --------------------------------------------------------------------------------------------- #
class TelegramRelay:
    def __init__(self, store, transport, bot_username: str, webhook_secret: str, now=time.time,
                 media_transport=None):
        self.store = store
        self.transport = transport
        self.media_transport = media_transport        # multipart uploads; None -> media is 503
        self.bot_username = bot_username.lstrip("@")
        self.webhook_secret = webhook_secret or ""
        self.now = now
        self._sends: dict[str, deque] = defaultdict(deque)   # account -> recent send timestamps
        self._edits: dict[str, deque] = defaultdict(deque)   # account -> recent edit timestamps
        self._rate_lock = threading.Lock()

    # -- link ------------------------------------------------------------------------------- #
    def new_link(self, account: str) -> dict:
        code = secrets.token_urlsafe(18)                     # [A-Za-z0-9_-], fits Telegram's 64
        self.store.add_link(_hash_code(code), account, self.now() + LINK_TTL)
        return {"code": code, "url": f"https://t.me/{self.bot_username}?start={code}",
                "expires_in": LINK_TTL}

    def status(self, account: str) -> dict:
        chat = self.store.get_chat(account)
        if not chat:
            return {"linked": False}
        out = {"linked": True, "linked_at": chat["linked_at"], "paused": bool(chat["paused"])}
        if chat.get("username"):
            out["username"] = chat["username"]
        return out

    def unlink(self, account: str) -> None:
        chat = self.store.get_chat(account)
        if chat:
            self._say(chat["chat_id"], MSG_DISCONNECTED)
            self.store.unbind(account)

    # -- send ------------------------------------------------------------------------------- #
    def rate_check(self, account: str) -> float | None:
        """Reserve a send slot; returns seconds to wait when over a limit, else None."""
        now = self.now()
        with self._rate_lock:
            q = self._sends[account]
            while q and q[0] <= now - 86400:
                q.popleft()
            if q and now - q[-1] < SEND_MIN_INTERVAL:
                return SEND_MIN_INTERVAL - (now - q[-1])
            if len(q) >= SEND_PER_DAY:
                return q[0] + 86400 - now
            q.append(now)
            return None

    def edit_check(self, account: str) -> float | None:
        """Edits change a message already sent, so they don't use the 30 a day; they get their
        own small budget (1 a second, 200 a day). Returns seconds to wait, else None."""
        now = self.now()
        with self._rate_lock:
            q = self._edits[account]
            while q and q[0] <= now - 86400:
                q.popleft()
            if q and now - q[-1] < EDIT_MIN_INTERVAL:
                return EDIT_MIN_INTERVAL - (now - q[-1])
            if len(q) >= EDIT_PER_DAY:
                return q[0] + 86400 - now
            q.append(now)
            return None

    @staticmethod
    def _markup(buttons) -> dict:
        return {"inline_keyboard": [[{"text": b["label"], "callback_data": b["id"]} for b in row]
                                    for row in (buttons or [])]}

    def send_media(self, chat_id: int, kind: str, filename: str, data: bytes, content_type: str,
                   caption: str, buttons) -> int:
        """sendPhoto / sendDocument, always with protect_content. Pass-through: ``data`` lives
        only in this call."""
        method, field = ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")
        fields = {"chat_id": str(chat_id), "protect_content": "true"}
        if caption:
            fields["caption"] = caption
        if buttons:
            fields["reply_markup"] = json.dumps(self._markup(buttons))
        result = self.media_transport(method, fields, field, filename, data, content_type)
        return int((result or {}).get("message_id") or 0)

    def edit(self, chat_id: int, message_id: int, text=None, caption=None, buttons=None) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id}
        if buttons is not None:
            payload["reply_markup"] = self._markup(buttons)
        if text is not None:
            payload.update(text=text, disable_web_page_preview=True)
            self.transport("editMessageText", payload)
        elif caption is not None:
            payload["caption"] = caption
            self.transport("editMessageCaption", payload)
        else:
            self.transport("editMessageReplyMarkup", payload)

    def send(self, chat_id: int, text: str, buttons) -> int:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": b["label"], "callback_data": b["id"]} for b in row] for row in buttons]}
        result = self.transport("sendMessage", payload)
        return int(result.get("message_id") or 0)

    def _say(self, chat_id, text: str) -> None:
        try:
            self.transport("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception:                                    # noqa: BLE001 - a courtesy reply
            pass

    # -- webhook ---------------------------------------------------------------------------- #
    def secret_ok(self, header: str | None) -> bool:
        return bool(self.webhook_secret) and hmac.compare_digest(
            (header or "").encode(), self.webhook_secret.encode())

    def handle_update(self, update: dict) -> None:
        now = self.now()
        cq = update.get("callback_query")
        if isinstance(cq, dict):
            self._handle_callback(cq, now)
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return                                           # edits, channel posts, etc.: ignored
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type", "private") != "private":
            return
        text = str(msg.get("text") or "").strip()
        username = (msg.get("from") or {}).get("username") or chat.get("username")
        account = self.store.account_for_chat(chat_id)

        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            cmd = head[1:].split("@", 1)[0].lower()
            arg = arg.strip()
            if cmd == "start" and arg:
                bound = self.store.take_link(_hash_code(arg), now)
                if not bound:
                    self._say(chat_id, MSG_EXPIRED)
                    return
                self.store.bind_chat(bound, chat_id, username, _iso(now))
                self._say(chat_id, MSG_CONNECTED)
                return
            if account is None:
                self._say(chat_id, MSG_UNBOUND)
                return
            if cmd == "start":
                self._say(chat_id, MSG_CONNECTED)
            elif cmd == "stop":
                self.store.set_paused(account, True)
                self.store.add_event(account, "command", "stop", "", now)
                self._say(chat_id, MSG_PAUSED)
            elif cmd == "resume":
                self.store.set_paused(account, False)
                self.store.add_event(account, "command", "resume", "", now)
                self._say(chat_id, MSG_RESUMED)
            elif cmd == "help":
                self._say(chat_id, MSG_HELP)
            else:
                self.store.add_event(account, "command", cmd[:40], arg[:MAX_EVENT_TEXT], now)
            return

        if account is None:
            self._say(chat_id, MSG_UNBOUND)
            return
        if text:
            self.store.add_event(account, "text", "", text[:MAX_EVENT_TEXT], now)

    def _handle_callback(self, cq: dict, now: float) -> None:
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is None:
            chat_id = (cq.get("from") or {}).get("id")
        account = self.store.account_for_chat(chat_id) if chat_id is not None else None
        try:
            self.transport("answerCallbackQuery", {
                "callback_query_id": cq.get("id"),
                "text": "Got it" if account else MSG_UNBOUND})
        except Exception:                                    # noqa: BLE001
            pass
        if account is None:
            return
        data = str(cq.get("data") or "")[:64]
        self.store.add_event(account, "button", data, "", now)

    # -- inbox ------------------------------------------------------------------------------ #
    def inbox(self, account: str, after: int) -> dict:
        rows = self.store.ack_and_list(account, after, self.now() - EVENT_TTL, INBOX_LIMIT)
        events = [{"cursor": r["id"], "type": r["type"], "data": r["data"] or "",
                   "text": r["text"] or "", "at": _iso(r["at"])} for r in rows]
        return {"events": events, "cursor": events[-1]["cursor"] if events else after}


def validate_send(body: dict):
    """Return (text, buttons) or raise ValueError with a short reason."""
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text is longer than {MAX_TEXT} characters")
    return text, validate_buttons(body.get("buttons"))


def validate_buttons(buttons):
    """Rows of {id, label}; raises ValueError with a short reason."""
    buttons = buttons or []
    if not isinstance(buttons, list) or len(buttons) > MAX_ROWS:
        raise ValueError(f"buttons must be at most {MAX_ROWS} rows")
    clean = []
    for row in buttons:
        if not isinstance(row, list) or not row or len(row) > MAX_COLS:
            raise ValueError(f"each button row must hold 1 to {MAX_COLS} buttons")
        out = []
        for b in row:
            if not isinstance(b, dict):
                raise ValueError("a button must be {id, label}")
            bid, label = b.get("id"), b.get("label")
            if not isinstance(bid, str) or not _BUTTON_ID_RE.match(bid) or len(bid) > MAX_BUTTON_ID:
                raise ValueError("a button id must be 1-40 chars of A-Z a-z 0-9 : _ -")
            if not isinstance(label, str) or not label.strip() or len(label) > MAX_BUTTON_LABEL:
                raise ValueError(f"a button label must be 1-{MAX_BUTTON_LABEL} characters")
            out.append({"id": bid, "label": label})
        clean.append(out)
    return clean


def validate_media(form: dict) -> tuple:
    """(kind, caption, buttons) from a send_media form, or ValueError."""
    kind = str(form.get("kind") or "")
    if kind not in ("photo", "document"):
        raise ValueError('kind must be "photo" or "document"')
    caption = str(form.get("caption") or "")
    if len(caption) > MAX_CAPTION:
        raise ValueError(f"caption is longer than {MAX_CAPTION} characters")
    raw = form.get("buttons") or ""
    try:
        buttons = json.loads(raw) if raw else []
    except ValueError:
        raise ValueError("buttons must be JSON") from None
    return kind, caption, validate_buttons(buttons)


def register_telegram(app, relay, identify) -> None:
    """Mount the /notify/telegram/* and /telegram/webhook routes. ``relay`` None -> every route
    answers 503 telegram_not_configured (the bot token is unset)."""

    def _off():
        return jsonify(error="telegram_not_configured"), 503

    def _user():
        return identify(request)

    @app.route("/notify/telegram/link", methods=["POST"])
    def tg_link():
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        return jsonify(relay.new_link(user))

    @app.route("/notify/telegram/status", methods=["GET"])
    def tg_status():
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        return jsonify(relay.status(user))

    @app.route("/notify/telegram/unlink", methods=["POST"])
    def tg_unlink():
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        relay.unlink(user)
        return jsonify(linked=False)

    @app.route("/notify/telegram/send", methods=["POST"])
    def tg_send():
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        try:
            text, buttons = validate_send(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        chat = relay.store.get_chat(user)
        if not chat:
            return jsonify(error="not_linked"), 409
        if chat["paused"]:
            return jsonify(error="paused"), 409
        wait = relay.rate_check(user)
        if wait is not None:
            retry = max(1, int(wait + 0.999))
            resp = jsonify(error="rate_limited", retry_after=retry)
            resp.headers["Retry-After"] = str(retry)
            return resp, 429
        try:
            message_id = relay.send(chat["chat_id"], text, buttons)
        except Exception as exc:                             # noqa: BLE001
            if isinstance(exc, TelegramError) and exc.status == 403:
                # The person blocked the bot or deleted the chat: the link is dead.
                relay.store.unbind(user)
                return jsonify(error="not_linked"), 409
            app.logger.warning("telegram send failed: %s", exc)
            return jsonify(error="telegram_unavailable"), 502
        return jsonify(ok=True, message_id=message_id)

    def _gate(user, check):
        """(chat, error_response): linked, not paused, inside the rate limit."""
        chat = relay.store.get_chat(user)
        if not chat:
            return None, (jsonify(error="not_linked"), 409)
        if chat["paused"]:
            return None, (jsonify(error="paused"), 409)
        wait = check(user)
        if wait is not None:
            retry = max(1, int(wait + 0.999))
            resp = jsonify(error="rate_limited", retry_after=retry)
            resp.headers["Retry-After"] = str(retry)
            return None, (resp, 429)
        return chat, None

    def _upstream_failed(user, exc):
        if isinstance(exc, TelegramError) and exc.status == 403:
            relay.store.unbind(user)
            return jsonify(error="not_linked"), 409
        app.logger.warning("telegram call failed: %s", exc)
        return jsonify(error="telegram_unavailable"), 502

    @app.route("/notify/telegram/send_media", methods=["POST"])
    def tg_send_media():
        """A CV preview (photo) or PDF (document), streamed through to Telegram and never
        stored. multipart: kind, file, caption?, buttons? (JSON rows)."""
        if relay is None or relay.media_transport is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        if (request.content_length or 0) > MAX_DOCUMENT_BYTES + 64 * 1024:
            return jsonify(error="file too large"), 413
        try:
            kind, caption, buttons = validate_media(request.form)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        f = request.files.get("file")
        if f is None:
            return jsonify(error="file is required"), 400
        cap = MAX_PHOTO_BYTES if kind == "photo" else MAX_DOCUMENT_BYTES
        data = f.stream.read(cap + 1)
        if not data:
            return jsonify(error="file is empty"), 400
        if len(data) > cap:
            return jsonify(error=f"{kind} is larger than {cap // (1024 * 1024)} MB"), 413
        chat, err = _gate(user, relay.rate_check)
        if err:
            return err
        name = "cv.png" if kind == "photo" else "cv.pdf"
        ctype = "image/png" if kind == "photo" else "application/pdf"
        app.logger.info("telegram media: %s, %d bytes", kind, len(data))   # size only, never content
        try:
            message_id = relay.send_media(chat["chat_id"], kind, name, data, ctype, caption, buttons)
        except Exception as exc:                             # noqa: BLE001
            return _upstream_failed(user, exc)
        finally:
            del data
        return jsonify(ok=True, message_id=message_id)

    @app.route("/notify/telegram/edit", methods=["POST"])
    def tg_edit():
        """Change a message the bot sent: {message_id, text? | caption?, buttons?}. Only buttons
        (an empty list removes them) is editMessageReplyMarkup."""
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        body = request.get_json(silent=True) or {}
        try:
            message_id = int(body.get("message_id"))
        except (TypeError, ValueError):
            return jsonify(error="message_id is required"), 400
        text, caption = body.get("text"), body.get("caption")
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT):
            return jsonify(error=f"text must be 1 to {MAX_TEXT} characters"), 400
        if caption is not None and (not isinstance(caption, str) or len(caption) > MAX_CAPTION):
            return jsonify(error=f"caption is longer than {MAX_CAPTION} characters"), 400
        try:
            buttons = validate_buttons(body["buttons"]) if "buttons" in body else None
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if text is None and caption is None and buttons is None:
            return jsonify(error="nothing to edit"), 400
        chat, err = _gate(user, relay.edit_check)
        if err:
            return err
        try:
            relay.edit(chat["chat_id"], message_id, text=text, caption=caption, buttons=buttons)
        except Exception as exc:                             # noqa: BLE001
            if isinstance(exc, TelegramError) and exc.status == 400:
                return jsonify(error="cannot_edit"), 409     # too old, deleted, or unchanged
            return _upstream_failed(user, exc)
        return jsonify(ok=True)

    @app.route("/notify/telegram/inbox", methods=["GET"])
    def tg_inbox():
        if relay is None:
            return _off()
        user = _user()
        if not user:
            return jsonify(error="no user"), 401
        try:
            after = max(0, int(request.args.get("after") or 0))
        except (TypeError, ValueError):
            return jsonify(error="bad cursor"), 400
        return jsonify(relay.inbox(user, after))

    @app.route("/telegram/webhook", methods=["POST"])
    def tg_webhook():
        if relay is None:
            return _off()
        if not relay.secret_ok(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
            return jsonify(error="forbidden"), 403
        update = request.get_json(silent=True) or {}
        try:
            relay.handle_update(update)
        except Exception:                                    # noqa: BLE001 - 200 so Telegram won't retry forever
            app.logger.exception("telegram webhook update failed")
        return jsonify(ok=True)
