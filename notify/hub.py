"""One notification tick: read the person's taps and replies, act, send what is due.

The app runs ``tick`` every 30 seconds while it is open (and on demand from the poll
endpoints). Button taps and messages that arrived while the app was closed are read on the
first tick after the next start: the official bot's inbox keeps seven days, the person's
own bot (getUpdates) keeps 24 hours.

Event routing (same for both channels):
  * ``job:<action>:<short>`` buttons -> AlertEngine.handle_button (alerts.py)
  * ``approve:<id>`` / ``skip:<id>`` / ``approveall`` buttons -> the batch review actions
  * text and slash commands -> NotifyService.handle_command (/list, /approve, /status, /stop,
    /resume, /skip, /handle, free-text chat)
"""

from __future__ import annotations

from datetime import datetime, timezone

from .alerts import away_activity, digest_due, digest_text
from .channel import ChannelError


class NotifyHub:
    def __init__(self, state, engine, commands, batch_callback=None, job_actions=None,
                 review=None):
        self.state = state
        self.review = review                      # notify/cvreview.CVReview: preview + chat edits
        self.engine = engine
        self.commands = commands                  # has handle_command(text) -> str
        self.batch_callback = batch_callback      # fn(data) -> {"message": str}
        self.job_actions = job_actions            # see AlertEngine.handle_button

    # -- inbound ----------------------------------------------------------------- #
    def handle_event(self, channel, ev: dict) -> str:
        kind = ev.get("type")
        if kind == "button":
            data = ev.get("data") or ""
            if data.startswith("cv:") and self.review is not None:
                toast = self.review.handle_button(channel, data)
                channel.ack(ev, toast)
                return toast
            if data.startswith("job:"):
                act = data.split(":")[1] if data.count(":") >= 2 else ""
                if act in ("queue", "apply"):
                    channel.ack(ev, "On it")
                    channel.send("On it. Tailoring your résumé now; I'll message you when it's ready.")
                reply = self.engine.handle_button(data, self.job_actions)
                if act not in ("queue", "apply"):
                    channel.ack(ev, reply)
            elif self.batch_callback is not None:
                reply = str((self.batch_callback(data) or {}).get("message") or "")
                channel.ack(ev, reply)
            else:
                reply = "I didn't recognise that button."
                channel.ack(ev, reply)
            if reply:
                channel.send(reply)
            self._preview_ready(channel)
            return reply
        text = ev.get("text") or ""
        if not text:
            return ""
        if self.review is not None:
            handled = self.review.handle_text(channel, text)
            if handled is not None:
                return handled
        reply = self.commands.handle_command(text)
        channel.send(reply)
        return reply

    def _preview_ready(self, channel) -> None:
        """A tailoring the chat started just finished: send its preview (picture, change
        summary, Show changes / Send PDF / Edit / Looks good)."""
        take = getattr(self.job_actions, "take_ready", None)
        if self.review is None or not callable(take):
            return
        rid = take()
        if rid:
            try:
                self.review.send_preview(channel, rid, quiet=True)
            except ChannelError:
                raise
            except Exception:   # noqa: BLE001 - the text reply already went out
                pass

    def poll(self, channel) -> dict:
        try:
            events = channel.poll()
        except ChannelError as exc:
            return {"handled": 0, "error": exc.code}
        handled, results = 0, []
        for ev in events:
            try:
                results.append(self.handle_event(channel, ev))
                handled += 1
            except ChannelError as exc:
                results.append(f"error: {exc.code}")
            except Exception as exc:   # noqa: BLE001 - one bad event never stalls the queue
                results.append(f"error: {type(exc).__name__}")
        return {"handled": handled, "results": results}

    # -- outbound ---------------------------------------------------------------- #
    def send_digest(self, channel, records, now: datetime, site_name_fn=None) -> str:
        """The away digest. Returns what happened: sent / wait / reset / none / off / quiet."""
        st = self.state.load()
        prefs = self.state.prefs()
        now_utc = now.astimezone(timezone.utc)
        since_raw = st.get("last_digest_at")
        if not since_raw:
            self.state.set("last_digest_at", now_utc.isoformat())   # never digest history
            return "none"
        if not prefs.get("app_updates"):
            self.state.set("last_digest_at", now_utc.isoformat())
            return "off"
        since = datetime.fromisoformat(since_raw)
        presence = st.get("presence_at")
        presence_at = datetime.fromtimestamp(float(presence), tz=timezone.utc) if presence else None
        act = away_activity(records, since, site_name_fn)
        verdict = digest_due(act, presence_at, now)
        if verdict == "reset":
            self.state.set("last_digest_at", now_utc.isoformat())
            return "reset"
        if verdict != "send":
            return verdict
        from .alerts import in_quiet_hours
        local = now.astimezone()
        if in_quiet_hours(local.hour, prefs["quiet_start"], prefs["quiet_end"]):
            return "quiet"
        channel.send(digest_text(act))
        self.state.set("last_digest_at", now_utc.isoformat())
        return "sent"

    def tick(self, channel, now: datetime, can_apply_fn, records_fn=None,
             site_name_fn=None) -> dict:
        if channel is None:
            return {"ok": False, "channel": "none"}
        out = {"ok": True, "channel": getattr(channel, "name", "?")}
        out.update(self.poll(channel))
        if out.get("error") == "no_owner":       # fail-closed: obey nobody, send nothing
            return out
        try:
            out["alerts_sent"] = len(self.engine.send_due(channel, now, can_apply_fn))
        except ChannelError as exc:
            out["alerts_sent"], out["send_error"] = 0, exc.code
        if records_fn is not None:
            try:
                out["digest"] = self.send_digest(channel, records_fn(), now, site_name_fn)
            except ChannelError as exc:
                out["digest"] = f"error: {exc.code}"
        return out
