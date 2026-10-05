"""Assisted-lane batch review over Telegram (CLAUDE.md §5/§7).

When applications are filled and ready, the app SENDS them to the person's Telegram — a
batch summary, then per application a CV-preview document + a short summary of what was
filled + inline **Approve / Skip** buttons — and POLLS for the taps. Everything is
outbound (send*/getUpdates); nothing inbound ever reaches the local machine.

Review-first by default: the person sees the CV + summary before Approve. An OPT-IN
"Approve all" button appears only when the person has deliberately enabled it. On Approve
the app submits through the SANCTIONED channel (``actions.approve`` → ``submit_record``,
which auto-submits only for the verified auto-lane and marks the rest assisted); on Skip it
drops the item. Obeys ONLY the owner's chat.

``actions`` is supplied by the app and provides:
  * ``pending() -> [ {id, role, company, cv_path, summary} ]``
  * ``approve(rid) -> {message: str, ...}``   (sanctioned submit; rate-limited)
  * ``skip(rid)   -> {message: str, ...}``
  * ``approve_all() -> {message: str, ...}``
"""

from __future__ import annotations


def item_caption(item: dict) -> str:
    """The short 'what was filled' summary shown under an application's CV preview."""
    filled = item.get("filled") or {}
    got = [k for k in ("name", "email", "phone") if filled.get(k)]
    who = f"{item.get('role') or 'Role'} at {item.get('company') or 'Company'}"
    lane = item.get("lane_label") or item.get("lane") or "assisted"
    lines = [
        f"🏢 {who}",
        f"Lane: {lane}",
        "Filled: " + (", ".join(got) if got else "none")
        + f" · {item.get('screening_count', 0)} screening answer(s)"
        + (" · cover letter ✓" if item.get("has_cover_letter") else ""),
    ]
    if item.get("coverage") is not None:
        lines.append(f"JD match: {item['coverage']}%")
    return "\n".join(lines)


def item_buttons(rid) -> list:
    return [[("✅ Approve", f"approve:{rid}"), ("⏭ Skip", f"skip:{rid}")]]


def batch_summary_text(items: list) -> str:
    if not items:
        return "No applications waiting for review. You're all caught up. 🎉"
    lines = [f"📥 {len(items)} application(s) ready for your review:"]
    for it in items:
        company = it.get("company")
        lines.append(f"• #{it.get('id')} {it.get('role') or 'role'}" + (f" at {company}" if company else ""))
    lines.append("\nEach one below has the resume preview, what was filled, and Approve / Skip.")
    return "\n".join(lines)


class BatchReview:
    def __init__(self, bot, actions, approve_all_enabled: bool = False):
        self.bot = bot
        self.actions = actions
        self.approve_all_enabled = bool(approve_all_enabled)

    # -- outbound: send the batch -------------------------------------------- #
    def send(self, items=None) -> dict:
        items = self.actions.pending() if items is None else items
        # Batch summary; the opt-in one-tap "Approve all" appears ONLY when enabled.
        buttons = [[("✅ Approve all", "approveall")]] if (self.approve_all_enabled and items) else None
        self.bot.send_message(batch_summary_text(items), buttons=buttons)
        for it in items:
            caption, btns = item_caption(it), item_buttons(it["id"])
            image, pdf = it.get("image_path"), it.get("cv_path")
            if image:
                # Image-FIRST: a viewable CV preview carrying the summary + Approve/Skip,
                # then the exact PDF attached BELOW it to open before approving.
                self.bot.send_photo(image, caption=caption, buttons=btns)
                if pdf:
                    self.bot.send_document(pdf, caption="📎 Exact resume (PDF). Open it to read in full.")
            elif pdf:
                self.bot.send_document(pdf, caption=caption, buttons=btns)   # no image renderer
            else:
                self.bot.send_message(caption + "\n(resume preview unavailable)", buttons=btns)
        return {"sent": len(items)}

    # -- inbound taps (read by outbound polling) ----------------------------- #
    def handle_callback(self, cb: dict) -> dict:
        res = self.act(cb.get("data") or "")
        self.bot.answer_callback(cb.get("id") or "", (res or {}).get("message", "")[:200])
        return res or {}

    def act(self, data: str) -> dict:
        """Run one Approve / Skip / Approve-all tap (by its callback data) and return the
        result. Channel-neutral: the notification hub calls this for either bot."""
        cmd, _, arg = (data or "").partition(":")
        if cmd == "approve" and arg:
            res = self.actions.approve(_as_int(arg))
        elif cmd == "skip" and arg:
            res = self.actions.skip(_as_int(arg))
        elif cmd == "approveall":
            if not self.approve_all_enabled:
                res = {"message": "One-tap approve-all isn't enabled. Turn it on in settings first."}
            else:
                res = self.actions.approve_all()
        else:
            res = {"message": "Unrecognized action."}
        return res or {}

    def poll(self, offset: int = 0) -> dict:
        """Read taps/replies by OUTBOUND getUpdates, act on the owner's, advance the offset.

        FAIL-CLOSED: if no owner chat is configured we ignore every update — an unset owner
        must never mean 'obey everyone'."""
        if not self.bot.chat_id:
            return {"offset": offset, "handled": 0, "results": [],
                    "error": "No owner chat configured (set TELEGRAM_CHAT_ID); ignoring all taps."}
        updates = self.bot.get_updates(offset=offset)
        handled, next_offset, results = 0, offset, []
        for up in updates:
            try:
                next_offset = max(next_offset, int(up.get("update_id", 0)) + 1)
            except (TypeError, ValueError):
                continue   # a malformed update can't abort the whole poll; skip it
            cb = up.get("callback_query")
            if not cb:
                continue
            chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", "")
                          or (cb.get("from") or {}).get("id", ""))
            if chat_id != self.bot.chat_id:
                continue                      # not the owner — ignore silently
            results.append(self.handle_callback(cb))
            handled += 1
        return {"offset": next_offset, "handled": handled, "results": results}


def _as_int(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None
