"""One way to reach the person on Telegram, whichever bot carries it.

Two channels share one tiny interface, so the alert, digest and command code never cares
which is in use:

* ``OwnBotChannel`` - the person's OWN bot (token + chat id from BotFather, the original
  setup). Talks straight to the Bot API: sendMessage with an inline keyboard, getUpdates
  for replies and button taps (callback_query), answerCallbackQuery to stop the spinner.
* ``OfficialBotChannel`` - the official SponsorJobs bot, run by the broker. The app only
  calls the broker (``/notify/telegram/send`` and ``/notify/telegram/inbox``); it never
  holds the bot token.

``select_channel`` picks official when the broker says this account is linked, else the
own bot when it is configured, else none.

Events (what ``poll`` returns) are plain dicts, the same for both channels:
``{cursor, type: "button"|"text"|"command", data, text, at, ack}`` where ``data`` is the
button id for a tap and ``ack`` is an opaque handle passed back to ``ack()``.
Buttons are rows of ``{"id", "label"}``. Every call is OUTBOUND (CLAUDE.md section 5):
nothing listens on this machine.
"""

from __future__ import annotations

import time


class ChannelError(RuntimeError):
    """A send/poll that the channel refused. ``code`` is machine-readable
    (not_linked, paused, rate_limited, telegram_not_configured, unreachable, ...)."""

    def __init__(self, code: str, message: str = "", retry_after: float = 0.0):
        super().__init__(message or code)
        self.code = code
        self.retry_after = float(retry_after or 0)


class Channel:
    name = "none"

    def send(self, text: str, buttons=None) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError

    def ack(self, event: dict, text: str = "") -> None:
        """Acknowledge a button tap (own bot: answerCallbackQuery). Default: nothing."""
        return None

    # Media and edits. Every photo and document goes out with protect_content (Telegram then
    # blocks forwarding and saving); there is no way to turn it off from the app.
    def send_photo(self, path: str, caption: str = "", buttons=None) -> dict:  # pragma: no cover
        raise ChannelError("media_unsupported", "This channel can't send pictures.")

    def send_document(self, path: str, caption: str = "", buttons=None) -> dict:  # pragma: no cover
        raise ChannelError("media_unsupported", "This channel can't send files.")

    def edit_text(self, message_id, text=None, buttons=None, caption=None) -> dict:  # pragma: no cover
        raise ChannelError("edit_unsupported", "This channel can't edit messages.")


def _as_rows(buttons) -> list:
    """Normalise buttons to rows of {id, label}. Accepts [[{id,label}]] or [[(label, id)]]."""
    rows = []
    for row in buttons or []:
        out = []
        for b in row or []:
            if isinstance(b, dict):
                out.append({"id": str(b.get("id") or ""), "label": str(b.get("label") or "")})
            else:
                label, bid = b
                out.append({"id": str(bid), "label": str(label)})
        if out:
            rows.append(out)
    return rows


def _kind(text: str) -> str:
    return "command" if (text or "").strip().startswith("/") else "text"


class OwnBotChannel(Channel):
    """The person's own bot. ``offset_get`` / ``offset_set`` persist the getUpdates offset so a
    tap is handled once, even across restarts (Telegram keeps updates for 24 hours)."""

    name = "own"

    def __init__(self, bot, offset_get, offset_set):
        self.bot = bot
        self._get = offset_get
        self._set = offset_set

    def send(self, text: str, buttons=None) -> dict:
        if not self.bot.chat_id:
            raise ChannelError("no_owner", "No owner chat configured (set TELEGRAM_CHAT_ID).")
        rows = _as_rows(buttons)
        tuples = [[(b["label"], b["id"]) for b in row] for row in rows] or None
        res = self.bot.send_message(text, buttons=tuples)
        ok = bool((res or {}).get("ok", True)) if isinstance(res, dict) else True
        mid = ((res or {}).get("result") or {}).get("message_id") if isinstance(res, dict) else None
        return {"ok": ok, "message_id": mid}

    def _res(self, res) -> dict:
        ok = bool((res or {}).get("ok", True)) if isinstance(res, dict) else True
        mid = ((res or {}).get("result") or {}).get("message_id") if isinstance(res, dict) else None
        if not ok:
            raise ChannelError("telegram_error", str((res or {}).get("description") or "Telegram refused it."))
        return {"ok": ok, "message_id": mid}

    def _tuples(self, buttons):
        return [[(b["label"], b["id"]) for b in row] for row in _as_rows(buttons)] or None

    def send_photo(self, path: str, caption: str = "", buttons=None) -> dict:
        if not self.bot.chat_id:
            raise ChannelError("no_owner", "No owner chat configured (set TELEGRAM_CHAT_ID).")
        return self._res(self.bot.send_photo(path, caption=caption[:1024],
                                             buttons=self._tuples(buttons), protect=True))

    def send_document(self, path: str, caption: str = "", buttons=None) -> dict:
        if not self.bot.chat_id:
            raise ChannelError("no_owner", "No owner chat configured (set TELEGRAM_CHAT_ID).")
        return self._res(self.bot.send_document(path, caption=caption[:1024],
                                                buttons=self._tuples(buttons), protect=True))

    def edit_text(self, message_id, text=None, buttons=None, caption=None) -> dict:
        if not self.bot.chat_id or not message_id:
            raise ChannelError("no_owner", "Nothing to edit.")
        tuples = None if buttons is None else (self._tuples(buttons) or [])
        return self._res(self.bot.edit_message(message_id, text=text, caption=caption,
                                               buttons=tuples))

    def poll(self) -> list[dict]:
        # FAIL-CLOSED: no owner chat configured means we obey nobody.
        if not self.bot.chat_id:
            raise ChannelError("no_owner", "No owner chat configured (set TELEGRAM_CHAT_ID).")
        offset = int(self._get() or 0)
        updates = self.bot.get_updates(offset=offset)
        events, nxt = [], offset
        for up in updates or []:
            try:
                uid = int(up.get("update_id", 0))
            except (TypeError, ValueError):
                continue
            nxt = max(nxt, uid + 1)
            ev = parse_update(up, self.bot.chat_id)
            if ev is not None:
                events.append(ev)
        if nxt != offset:
            self._set(nxt)
        return events

    def ack(self, event: dict, text: str = "") -> None:
        cb = event.get("ack")
        if cb:
            try:
                self.bot.answer_callback(cb, (text or "")[:200])
            except Exception:   # noqa: BLE001 - a missed ack only leaves a spinner
                pass


def parse_update(up: dict, owner_chat: str) -> dict | None:
    """One Bot API update -> an event, or None (not the owner, or nothing actionable).
    A callback_query (inline button tap) becomes a ``button`` event carrying its data."""
    owner = str(owner_chat or "")
    if not owner:
        return None
    cursor = str(up.get("update_id", ""))
    cb = up.get("callback_query")
    if cb:
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", "")
                   or (cb.get("from") or {}).get("id", ""))
        if chat != owner:
            return None
        return {"cursor": cursor, "type": "button", "data": str(cb.get("data") or ""),
                "text": "", "at": ((cb.get("message") or {}).get("date")),
                "ack": cb.get("id") or ""}
    msg = up.get("message") or up.get("edited_message") or {}
    text = msg.get("text") or ""
    if not text:
        return None
    if str((msg.get("chat") or {}).get("id", "")) != owner:
        return None
    return {"cursor": cursor, "type": _kind(text), "data": "", "text": text,
            "at": msg.get("date"), "ack": ""}


class OfficialBotChannel(Channel):
    """The official SponsorJobs bot behind the broker. ``get(path)`` / ``post(path, body)``
    return ``(status, json_or_None)``, status 0 = unreachable (ui/app.py's _broker_get /
    _broker_post). ``cursor_get`` / ``cursor_set`` persist the inbox cursor (the broker keeps
    seven days of events, so a tap sent while the app was closed is handled on next start)."""

    name = "official"

    # A rate-limit wait this short is slept through once instead of failing the message: a
    # preview is often a text plus a picture, and the broker allows one send per 3 seconds.
    RETRY_SHORT_WAIT = 6.0

    def __init__(self, get, post, cursor_get, cursor_set, on_unlinked=None, post_media=None,
                 sleep=time.sleep):
        self._get_http = get
        self._post_http = post
        self._post_media = post_media     # (path, fields, file_path, content_type) -> (status, json)
        self._sleep = sleep
        self._cget = cursor_get
        self._cset = cursor_set
        self._on_unlinked = on_unlinked     # the broker unlinked us (e.g. the bot was blocked)

    def _check(self, code, data) -> None:
        try:
            _raise_for(code, data)
        except ChannelError as exc:
            if exc.code == "not_linked" and self._on_unlinked is not None:
                try:
                    self._on_unlinked()
                except Exception:   # noqa: BLE001
                    pass
            raise

    # -- link management (Settings) ------------------------------------------- #
    def status(self) -> dict:
        code, data = self._get_http("/notify/telegram/status")
        _raise_for(code, data)
        return data or {}

    def link(self) -> dict:
        code, data = self._post_http("/notify/telegram/link", {})
        _raise_for(code, data)
        return data or {}

    def unlink(self) -> dict:
        code, data = self._post_http("/notify/telegram/unlink", {})
        _raise_for(code, data)
        return data or {"ok": True}

    # -- messaging -------------------------------------------------------------- #
    def _call(self, fn):
        """Run one broker call; a short 429 is waited out once."""
        code, data = fn()
        if code == 429:
            wait = float((data or {}).get("retry_after") or 0) if isinstance(data, dict) else 0.0
            if 0 < wait <= self.RETRY_SHORT_WAIT:
                self._sleep(wait)
                code, data = fn()
        self._check(code, data)
        return {"ok": bool((data or {}).get("ok", True)), "message_id": (data or {}).get("message_id")}

    def send(self, text: str, buttons=None) -> dict:
        body = {"text": text}
        rows = _as_rows(buttons)
        if rows:
            body["buttons"] = rows
        return self._call(lambda: self._post_http("/notify/telegram/send", body))

    def _media(self, kind: str, path: str, caption: str, buttons) -> dict:
        if self._post_media is None:
            raise ChannelError("media_unsupported", "This app can't send files to the bot.")
        import json as _json
        fields = {"kind": kind, "caption": (caption or "")[:1024]}
        rows = _as_rows(buttons)
        if rows:
            fields["buttons"] = _json.dumps(rows)
        ctype = "image/png" if kind == "photo" else "application/pdf"
        return self._call(lambda: self._post_media("/notify/telegram/send_media", fields, path, ctype))

    def send_photo(self, path: str, caption: str = "", buttons=None) -> dict:
        return self._media("photo", path, caption, buttons)

    def send_document(self, path: str, caption: str = "", buttons=None) -> dict:
        return self._media("document", path, caption, buttons)

    def edit_text(self, message_id, text=None, buttons=None, caption=None) -> dict:
        body = {"message_id": message_id}
        if text is not None:
            body["text"] = text
        if caption is not None:
            body["caption"] = caption
        if buttons is not None:
            body["buttons"] = _as_rows(buttons)
        return self._call(lambda: self._post_http("/notify/telegram/edit", body))

    def poll(self) -> list[dict]:
        cursor = str(self._cget() or "")
        path = "/notify/telegram/inbox" + (f"?after={cursor}" if cursor else "")
        code, data = self._get_http(path)
        if code == 400 and cursor:
            self._cset("")              # a cursor the broker can't read: start over
        self._check(code, data)
        data = data or {}
        events = []
        for e in data.get("events") or []:
            t = e.get("type")
            if t not in ("button", "text", "command"):
                continue
            ev_data, ev_text = str(e.get("data") or ""), str(e.get("text") or "")
            if t == "command":
                # The broker strips the slash: {data: "stop"} or {data: "approve", text: "12"}.
                # Rebuild "/approve 12" so the existing command router handles it unchanged.
                name = ev_data.lstrip("/")
                ev_text = ("/" + name + (" " + ev_text if ev_text else "")) if name else ev_text
            events.append({"cursor": str(e.get("cursor") if e.get("cursor") is not None else ""),
                           "type": t, "data": ev_data if t == "button" else "", "text": ev_text,
                           "at": e.get("at"), "ack": ""})
        nxt = data.get("cursor")
        if nxt is None and events:
            nxt = events[-1]["cursor"]
        if nxt not in (None, "") and str(nxt) != cursor:
            self._cset(str(nxt))
        return events


def _raise_for(code: int, data) -> None:
    if code == 0:
        raise ChannelError("unreachable", "The SponsorJobs service is not reachable right now.")
    if 200 <= code < 300:
        return
    data = data if isinstance(data, dict) else {}
    err = str(data.get("error") or data.get("code") or "")
    if code == 429:
        raise ChannelError("rate_limited", "Telegram is rate limiting; retrying later.",
                           retry_after=data.get("retry_after") or 30)
    if code == 503:
        raise ChannelError(err or "telegram_not_configured", "The official bot is not available.")
    if code == 502:
        raise ChannelError(err or "telegram_unavailable", "Telegram is unavailable; retrying later.")
    if code == 409:
        raise ChannelError(err or "not_linked", "Telegram is not linked.")
    raise ChannelError(err or f"http_{code}", f"The service answered {code}.")


def select_channel(official_status, own_bot):
    """Pick the active channel.

    ``official_status()`` -> (linked: bool, channel_or_None); ``own_bot()`` -> a configured
    OwnBotChannel or None. Official wins when linked; else the own bot; else None."""
    try:
        linked, official = official_status()
    except Exception:   # noqa: BLE001 - broker trouble falls through to the own bot
        linked, official = False, None
    if linked and official is not None:
        return official
    try:
        return own_bot()
    except Exception:   # noqa: BLE001
        return None


class TextOnlyBot:
    """Lets bot-shaped senders (the batch review) talk through any Channel. A preview picture
    goes through the channel's send_photo (protected) when it can carry one; the PDF itself
    stays on the computer, so a document becomes its caption as a message."""

    def __init__(self, channel):
        self.channel = channel
        self.chat_id = "channel"

    def send_message(self, text: str, buttons=None, chat_id: str = "") -> dict:
        return self.channel.send(text, buttons)

    def send_photo(self, file_path: str, caption: str = "", buttons=None, chat_id: str = "") -> dict:
        if file_path:
            try:
                return self.channel.send_photo(file_path, caption, buttons)
            except ChannelError as exc:
                if exc.code not in ("media_unsupported", "telegram_not_configured"):
                    raise
        return self.channel.send(caption or "Resume preview is on your computer.", buttons)

    def send_document(self, file_path: str, caption: str = "", buttons=None, chat_id: str = "") -> dict:
        if not buttons:
            return {"ok": True}          # the PDF itself can't travel; its caption adds nothing
        return self.channel.send(caption, buttons)

    def answer_callback(self, callback_query_id: str, text: str = "") -> dict:
        return {"ok": True}
