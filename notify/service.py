"""Approve-while-away: alert the person when a package is ready and act on their reply.

The review queue (CLAUDE.md §5, submit) can hand off to any device — this routes it
through Telegram. When a tailored package is ready, ``announce_ready`` pushes a summary;
the person replies with a short command (``/list``, ``/approve 12``, ``/status 12``) and
``poll_once`` dispatches it back into the queue. Guardrails:

  * The bot obeys ONLY the owner's own chat id — a message from any other chat is ignored,
    so a stranger who finds the bot can't approve the person's applications.
  * ``approve`` marks a package *applied* in the queue; it does NOT submit anything. Actual
    submission still follows the per-site policy (submit/), where the person decides.

``actions`` is a tiny adapter the app supplies (see ``ui/app.py``): ``pending()``,
``approve(rid)``, ``status(rid)``. Both the bot and the actions are injected, so the whole
command loop is testable offline.
"""

from __future__ import annotations

HELP = ("SponsorJobs commands:\n"
        "/list  packages waiting for you\n"
        "/approve <id>  mark that application applied\n"
        "/status <id>  status of one application\n"
        "/stop  pause the run (it finishes the current one first)\n"
        "/resume  continue a paused run\n"
        "/skip [id]  skip the current one, or a specific application\n"
        "/handle <id> <note>  tell me how to handle one\n"
        "/show <id>  a picture of that CV\n"
        "/edit <id> <change>  change it, e.g. /edit 12 rewrite E1.2 shorter\n"
        "/versions <id>  its saved versions\n"
        "/help  this message\n"
        "Or just message me and I'll reply.")


def format_ready(record: dict) -> str:
    """The push sent when a tailored package is ready for review."""
    role = record.get("role") or "New role"
    company = record.get("company") or ""
    cov = record.get("coverage")
    who = f"{role} at {company}" if company else role
    cov_bit = f" · {round(cov)}% JD match" if isinstance(cov, (int, float)) else ""
    rid = record.get("id")
    return (f"✅ Package ready: {who}{cov_bit}\n"
            f"Reply /approve {rid} to mark it applied, or /status {rid} for details.")


class NotifyService:
    def __init__(self, bot, actions):
        self.bot = bot
        self.actions = actions

    # -- outbound ---------------------------------------------------------- #
    def announce_ready(self, record: dict) -> dict:
        return self.bot.send_message(format_ready(record))

    # -- inbound ----------------------------------------------------------- #
    def handle_command(self, text: str) -> str:
        """Turn one owner message into a reply. A slash command is dispatched; anything else is
        free text answered conversationally by Annalisa (P1 shared memory). Unknown slash commands
        return help."""
        t = (text or "").strip()
        if not t:
            return HELP
        if not t.startswith("/"):
            return self._chat(t)          # free text -> Annalisa

        parts = t.split()
        cmd = parts[0].lower().lstrip("/")
        cmd = cmd.split("@", 1)[0]      # tolerate /approve@TailorBot group syntax
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "list":
            return self._list()
        if cmd in ("approve", "status"):
            rid = _as_int(arg)
            if rid is None:
                return f"Which one? Use /{cmd} <id>, see /list."
            return self._approve(rid) if cmd == "approve" else self._status(rid)
        if cmd in ("stop", "pause"):
            return self._stop()
        if cmd == "resume":
            return self._resume()
        if cmd == "skip":
            return self._skip(_as_int(arg) if arg else None)
        if cmd == "handle":
            return self._handle(_as_int(arg), " ".join(parts[2:]).strip())
        if cmd in ("help", "start"):
            return HELP
        return HELP

    def poll_once(self, offset: int = 0) -> dict:
        """Fetch pending updates, dispatch each owner command, reply, and return the next
        offset. Messages from any chat other than the owner's are ignored (not answered).

        FAIL-CLOSED: with no owner chat configured we ignore everything — an unset owner
        must never mean 'obey everyone'."""
        if not self.bot.chat_id:
            return {"offset": offset, "handled": 0,
                    "error": "No owner chat configured (set TELEGRAM_CHAT_ID); ignoring all messages."}
        updates = self.bot.get_updates(offset=offset)
        handled, next_offset = 0, offset
        for up in updates:
            try:
                next_offset = max(next_offset, int(up.get("update_id", 0)) + 1)
            except (TypeError, ValueError):
                continue   # a malformed update can't abort the whole poll; skip it
            msg = up.get("message") or up.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if not text:
                continue
            if chat_id != self.bot.chat_id:
                continue  # not the owner — ignore silently
            reply = self.handle_command(text)
            self.bot.send_message(reply, chat_id=chat_id)
            handled += 1
        return {"offset": next_offset, "handled": handled}

    # -- helpers ----------------------------------------------------------- #
    def _list(self) -> str:
        pending = self.actions.pending() or []
        if not pending:
            return "Nothing waiting. You're all caught up. 🎉"
        lines = [f"#{r.get('id')}: {r.get('role') or 'role'}"
                 + (f" at {r.get('company')}" if r.get("company") else "")
                 for r in pending]
        head = f"{len(lines)} waiting for you:"
        return "\n".join([head, *lines, "", "Reply /approve <id> to mark one applied."])

    def _approve(self, rid: int) -> str:
        rec = self.actions.approve(rid)
        if not rec:
            return f"No application #{rid} found. Try /list."
        who = rec.get("role") or "that application"
        if rec.get("company"):
            who += f" at {rec['company']}"
        return f"Approved #{rid}: {who} marked as applied. ✅"

    def _status(self, rid: int) -> str:
        rec = self.actions.status(rid)
        if not rec:
            return f"No application #{rid} found. Try /list."
        who = rec.get("role") or "role"
        if rec.get("company"):
            who += f" at {rec['company']}"
        return f"#{rid}: {who}, {rec.get('status') or 'ready'}."

    # -- P3 control -------------------------------------------------------- #
    def _stop(self) -> str:
        self.actions.pause()
        return "Paused. I'll finish the current application and stop. Send /resume to continue."

    def _resume(self) -> str:
        self.actions.resume()
        return "Resumed. Back to it."

    def _skip(self, rid) -> str:
        rec = self.actions.skip(rid)
        if rid is None:
            return "Skipping the current one and moving on."
        if not rec:
            return f"No application #{rid} found. Try /list."
        who = rec.get("role") or "that application"
        if rec.get("company"):
            who += f" at {rec['company']}"
        return f"Skipped #{rid}, {who}. It won't be submitted."

    def _handle(self, rid, note: str) -> str:
        if rid is None:
            return "Which one? Use /handle <id> <what to do>, see /list."
        if not note:
            return f"Tell me how to handle #{rid}, for example: /handle {rid} use my analyst profile."
        rec = self.actions.handle(rid, note)
        if not rec:
            return f"No application #{rid} found. Try /list."
        return f"Got it. I'll handle #{rid} with: {note}"

    def _chat(self, text: str) -> str:
        """Free text goes to Annalisa (P1 shared memory). If no handler is wired (or it can't
        answer, e.g. no key), fall back to help so a message is never met with silence."""
        chat = getattr(self.actions, "chat", None)
        if callable(chat):
            try:
                reply = str(chat(text) or "").strip()
            except Exception:
                reply = ""
            if reply:
                return reply
        return HELP


def _as_int(s: str):
    try:
        return int(str(s).lstrip("#"))
    except (TypeError, ValueError):
        return None
