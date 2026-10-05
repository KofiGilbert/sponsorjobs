"""Review and edit a tailored CV from Telegram (docs/notify.md, "CV review in the chat").

After every tailoring the chat started, the person gets a picture of the page (contact
line covered unless they turned that off), a caption saying what changed, and four
buttons: Show changes, Send PDF, Edit, Looks good. They can then change anything by
typing what they want ("rewrite E1.2 shorter", "change my title at Stanbic to Product
Lead", "move Projects above Experience"). Every edit:

1. is planned by the model into ``{op, target, value}`` steps over the page's span ids
   (``notify/cvdoc.py``) and echoed back as a question when unclear ("Did you mean E1.2?");
2. asks first when it changes a FACT (dates, titles, employer, education), with the choice
   to change this CV only or the saved profile too;
3. touches only the targeted spans: a model rewrite that changes anything else is thrown
   away (the over-editing guard);
4. goes back through the honesty and layout checks of the tailoring engine (CLAUDE.md §8,
   §9): role-scoped skills (a skill the person typed is allowed and flagged), new figures
   flagged, the per-bullet length budget, compile, one page, no new overfull lines. When the
   page overflows, only the edited text is shortened (three tries), else nothing changes;
5. comes back as before/after text plus a fresh preview with Accept and Undo. Nothing is
   saved until Accept. Each Accept is a version; Undo goes back one; ``/versions <id>``.

Accepting never submits (§7). Submission stays where it was: "Apply for me" only on a
verified auto-allowlisted site with autonomous submission on, otherwise the person opens
SponsorJobs and clicks submit.

Callback data is ``cv:<action>:<short>`` where ``short`` is a random 8 character key
mapped locally to the record (and pending edit) id. No source id, profile text or résumé
text is ever put in a button.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path

from . import cvdoc
from .channel import ChannelError

# --------------------------------------------------------------------------- copy
BTN_CHANGES = "Show changes"
BTN_PDF = "Send PDF"
BTN_EDIT = "Edit"
BTN_OK = "Looks good"
BTN_ACCEPT = "Accept"
BTN_UNDO = "Undo"
BTN_THIS_CV = "This CV only"
BTN_PROFILE = "Also update my profile"
BTN_CANCEL = "Cancel"
BTN_YES = "Yes"
BTN_NO = "No"
BTN_APPLY = "Apply for me"

MSG_PDF_WARNING = ("Before the PDF: chats with a bot are stored on Telegram's servers (they are "
                   "not end to end encrypted), and the bot can only delete its own messages "
                   "within 48 hours. I send it with forwarding and saving turned off.")
MSG_EDIT_PROMPT = ("Tell me what to change, in your own words. For example: rewrite E1.2 "
                   "shorter, use the word PnL in E2.1, reword the summary, change E1 dates to "
                   "Jan 2020 to Mar 2023, move Projects above Experience, drop P2, add a bullet "
                   "to E1: Led the card launch. Tap Show changes to see every id.")
MSG_EDIT_FOOTER = "Nothing is saved until you tap Accept."
MSG_NO_CHANGE = "That didn't change anything on your CV."
MSG_DISCARDED = "Discarded. Your CV is as it was."
MSG_CANCELLED = "Cancelled. Nothing changed."
MSG_EXPIRED = "That button has expired. Open SponsorJobs to review this CV."
MSG_NO_PREVIEW = "(I couldn't draw the preview picture on this computer.)"
MSG_SUBMIT_IN_APP = "Open SponsorJobs to submit."
MSG_CAN_APPLY = "This site accepts applications from SponsorJobs. Tap Apply for me to send it."

EDIT_WORDS = re.compile(
    r"\b(rewrite|reword|rephrase|shorten|shorter|longer|use the (?:word|term|phrase)|change|"
    r"replace|move|drop|remove|delete|add a bullet|swap|fix|edit|make)\b", re.I)
SHOW_ME = re.compile(r"^\s*(show me|show the cv|show my cv|preview|show it|show)\b[\s.!?]*", re.I)

ACTIVE_TTL = 24 * 3600        # a previewed CV stays the "current" one for edits this long
AWAIT_TTL = 30 * 60           # after Edit, the next message is the instruction for this long
MAX_SHORTS = 400
HEAL_TRIES = 3


def _who(role: str, company: str) -> str:
    return f"{role} at {company}" if company else (role or "this role")


class CVReview:
    """``records`` () -> a CVRecords-like store (closed after use); ``llm_factory`` () -> the
    model; ``template_fn(name)`` -> template .tex; ``compile_fn(tex, workdir, jobname)`` ->
    CompileResult; ``can_apply(rid)`` / ``apply(rid)`` -> the §7 submit path;
    ``update_profile(change, doc)`` -> bool writes one confirmed fact to the saved profile."""

    def __init__(self, state, records, llm_factory, workdir, template_fn, compile_fn=None,
                 can_apply=None, apply=None, update_profile=None, now=time.time,
                 preview_fn=None):
        self.state = state
        self.records = records
        self.llm_factory = llm_factory
        self.workdir = Path(workdir)
        self.template_fn = template_fn
        if compile_fn is None:
            from tailoring.compiler import compile_tex as compile_fn
        self.compile_fn = compile_fn
        self.can_apply = can_apply or (lambda rid: False)
        self.apply_fn = apply
        self.update_profile = update_profile
        self.now = now
        if preview_fn is None:
            from .redact import render_preview as preview_fn
        self.preview_fn = preview_fn

    # ------------------------------------------------------------------ records
    def _get(self, rid):
        store = self.records()
        try:
            return store.get(int(rid))
        finally:
            _close(store)

    def _merge(self, rid, patch: dict) -> dict:
        store = self.records()
        try:
            return store.merge_data(int(rid), patch)
        finally:
            _close(store)

    def _set_coverage(self, rid, ratio) -> None:
        store = self.records()
        try:
            fn = getattr(store, "set_coverage", None)
            if fn is not None:
                fn(int(rid), ratio)
        finally:
            _close(store)

    def ctx(self, rid) -> dict | None:
        """Everything one record's review needs, from its local data bag."""
        rec = self._get(rid)
        if not rec:
            return None
        data = rec.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data or "{}")
            except ValueError:
                data = {}
        data = data or {}
        jd = str(data.get("jd_text") or "")
        page = data.get("page_profile")
        base = data.get("base_profile") or {}
        if not page:
            # Records from before the page was persisted: rebuild the same selection.
            from tailoring.assembler import normalize_profile, select_roles_for_jd
            page = select_roles_for_jd(normalize_profile(data.get("render_profile") or {}), jd)
            base = {}
        sections = data.get("sections")
        if not sections:
            from intake.template_manifest import load_manifest
            from intake.template_manifest import sections as _sections
            sections = _sections(load_manifest(data.get("template") or None))
        return {"rid": int(rid), "role": str(rec.get("jd_label") or rec.get("role") or ""),
                "company": str(rec.get("company") or ""), "jd": jd, "data": data,
                "doc": {"profile": page, "sections": list(sections)}, "base": base,
                "template": str(data.get("template") or "")}

    def pdf_path(self, rid) -> Path:
        return self.workdir / f"cv-{int(rid)}.pdf"

    # ------------------------------------------------------------------ short ids
    def _short(self, rid, pending: str = "") -> str:
        key = secrets.token_urlsafe(6)[:8].replace("-", "x").replace("_", "y")

        def fn(d):
            m = d.get("cv_shorts") or {}
            m[key] = {"rid": int(rid), "p": pending, "at": self.now()}
            if len(m) > MAX_SHORTS:
                for k in sorted(m, key=lambda k: m[k].get("at", 0))[: len(m) - MAX_SHORTS]:
                    m.pop(k, None)
            d["cv_shorts"] = m
        self.state.update(fn)
        return key

    def _resolve(self, short: str):
        return (self.state.get("cv_shorts") or {}).get(short)

    def _set_active(self, rid) -> None:
        self.state.set("cv_active", {"rid": int(rid), "at": self.now()})

    def _active(self):
        a = self.state.get("cv_active") or {}
        if a.get("rid") and self.now() - float(a.get("at") or 0) < ACTIVE_TTL:
            return int(a["rid"])
        return None

    # ------------------------------------------------------------------ previews
    def _preview_png(self, ctx: dict, pdf: Path, name: str) -> str:
        show = bool(self.state.prefs().get("show_contact"))
        try:
            out = self.preview_fn(pdf, self.workdir / f"{name}.png",
                                  (ctx["doc"]["profile"] or {}).get("identity") or {},
                                  redact=not show)
        except Exception:   # noqa: BLE001 - no picture is a degraded preview, not a failure
            return ""
        return (out or {}).get("path") or ""

    def _send_picture(self, channel, png: str, caption: str, buttons) -> dict:
        if png:
            try:
                return channel.send_photo(png, caption, buttons)
            except ChannelError as exc:
                if exc.code not in ("media_unsupported",):
                    raise
        return channel.send((caption + "\n\n" + MSG_NO_PREVIEW).strip(), buttons)

    def caption(self, ctx: dict) -> str:
        cov = (ctx["data"].get("coverage") or {})
        return cvdoc.change_summary(ctx["role"], ctx["company"], ctx["base"],
                                    ctx["doc"]["profile"], cov)

    def preview_buttons(self, short: str, ctx: dict) -> list:
        rows = [[{"id": f"cv:chg:{short}", "label": BTN_CHANGES},
                 {"id": f"cv:pdf:{short}", "label": BTN_PDF}]]
        submitted = str((ctx["data"].get("submission") or {}).get("status") or "") == "auto_submitted"
        if submitted:
            rows.append([{"id": f"cv:ok:{short}", "label": BTN_OK}])
        else:
            rows.append([{"id": f"cv:edit:{short}", "label": BTN_EDIT},
                         {"id": f"cv:ok:{short}", "label": BTN_OK}])
        return rows

    def send_preview(self, channel, rid, quiet: bool = False) -> dict:
        """The default after every tailoring from Telegram, and on "show me"."""
        ctx = self.ctx(rid)
        if not ctx:
            if not quiet:
                channel.send(f"I can't find application #{rid}. Try /list.")
            return {"ok": False}
        pdf = self.pdf_path(rid)
        png = self._preview_png(ctx, pdf, f"tg-{int(rid)}-preview") if pdf.exists() else ""
        short = self._short(rid)
        res = self._send_picture(channel, png, self.caption(ctx), self.preview_buttons(short, ctx))
        self._set_active(rid)
        return {"ok": True, "message_id": (res or {}).get("message_id"), "image": png}

    # ------------------------------------------------------------------ buttons
    def handle_button(self, channel, data: str) -> str:
        """``cv:<act>:<short>`` -> act, sending whatever messages it needs. Returns the short
        toast for the tap."""
        parts = (data or "").split(":")
        if len(parts) != 3 or parts[0] != "cv":
            return "I didn't recognise that button."
        _, act, short = parts
        ref = self._resolve(short)
        if not ref:
            channel.send(MSG_EXPIRED)
            return "Expired"
        rid, pid = int(ref["rid"]), str(ref.get("p") or "")
        self._set_active(rid)
        if act == "chg":
            self.show_changes(channel, rid)
            return "Changes"
        if act == "pdf":
            self.send_pdf(channel, rid)
            return "PDF"
        if act == "edit":
            self.state.set("cv_await", {"rid": rid, "at": self.now()})
            channel.send(MSG_EDIT_PROMPT)
            return "Edit"
        if act == "ok":
            self.looks_good(channel, rid)
            return "Good"
        if act == "apply":
            return self._apply(channel, rid)
        if act == "acc":
            return self.accept(channel, rid, pid)
        if act == "undo":
            return self.undo(channel, rid, pid)
        if act in ("f1", "fp", "fx"):
            return self.fact_choice(channel, rid, pid, act)
        if act in ("yes", "no"):
            return self.clarify_choice(channel, rid, pid, act == "yes")
        return "I didn't recognise that button."

    def show_changes(self, channel, rid) -> list:
        ctx = self.ctx(rid)
        if not ctx:
            channel.send(f"I can't find application #{rid}.")
            return []
        msgs = cvdoc.chunk(cvdoc.show_changes(ctx["role"], ctx["company"], ctx["base"], ctx["doc"]))
        for m in msgs:
            channel.send(m)
        return msgs

    def send_pdf(self, channel, rid) -> None:
        ctx = self.ctx(rid)
        pdf = self.pdf_path(rid)
        if not ctx or not pdf.exists():
            channel.send("The PDF for that application isn't on this computer any more.")
            return
        if not self.state.get("pdf_warned"):
            channel.send(MSG_PDF_WARNING)
            self.state.set("pdf_warned", True)
        channel.send_document(str(pdf), f"{_who(ctx['role'], ctx['company'])}: your tailored CV.")

    def _submit_line(self, rid) -> tuple:
        """(text, buttons) for what happens next, by the per-site policy (§7)."""
        try:
            ok = bool(self.can_apply(rid))
        except Exception:   # noqa: BLE001
            ok = False
        if ok and self.apply_fn is not None:
            short = self._short(rid)
            return MSG_CAN_APPLY, [[{"id": f"cv:apply:{short}", "label": BTN_APPLY}]]
        return MSG_SUBMIT_IN_APP, None

    def looks_good(self, channel, rid) -> None:
        ctx = self.ctx(rid)
        who = _who(ctx["role"], ctx["company"]) if ctx else f"#{rid}"
        line, buttons = self._submit_line(rid)
        channel.send(f"Great. {who} is ready in your review queue (#{rid}). {line}", buttons)

    def _apply(self, channel, rid) -> str:
        if not self.can_apply(rid) or self.apply_fn is None:
            channel.send(MSG_SUBMIT_IN_APP)
            return "Open the app"
        channel.send(str(self.apply_fn(rid) or ""))
        return "On it"

    # ------------------------------------------------------------------ text
    def handle_text(self, channel, text: str):
        """A typed message. Returns the reply it sent (a str) when it handled it, else None so
        the regular command and chat routing takes it."""
        t = (text or "").strip()
        if not t:
            return None
        if t.startswith("/"):
            parts = t.split(maxsplit=2)
            cmd = parts[0].lower().lstrip("/").split("@", 1)[0]
            arg = parts[1] if len(parts) > 1 else ""
            rid = _as_int(arg)
            if cmd == "versions":
                if rid is None:
                    rid = self._active()
                if rid is None:
                    return self._say(channel, "Which one? Use /versions <id>, see /list.")
                return self._say(channel, self.versions_text(rid))
            if cmd == "show":
                rid = rid if rid is not None else self._active()
                if rid is None:
                    return self._say(channel, "Which one? Use /show <id>, see /list.")
                self.send_preview(channel, rid)
                return "preview"
            if cmd == "edit":
                if rid is None or len(parts) < 3:
                    return self._say(channel, "Use /edit <id> <what to change>, for example: "
                                              "/edit 12 rewrite E1.2 shorter.")
                return self.run_edit(channel, rid, parts[2])
            return None
        if SHOW_ME.match(t) and len(t) <= 30:
            rid = self._active()
            if rid is not None:
                self.send_preview(channel, rid)
                return "preview"
            return None
        aw = self.state.get("cv_await") or {}
        if aw.get("rid") and self.now() - float(aw.get("at") or 0) < AWAIT_TTL:
            self.state.set("cv_await", {})
            return self.run_edit(channel, int(aw["rid"]), t)
        rid = self._active()
        if rid is not None and (cvdoc.SPAN_ID_RE.search(t) or EDIT_WORDS.search(t)) \
                and _looks_like_cv_edit(t):
            return self.run_edit(channel, rid, t)
        return None

    def _say(self, channel, text: str) -> str:
        channel.send(text)
        return text

    # ------------------------------------------------------------------ editing
    def run_edit(self, channel, rid, instruction: str) -> str:
        ctx = self.ctx(rid)
        if not ctx:
            return self._say(channel, f"I can't find application #{rid}. Try /list.")
        if str((ctx["data"].get("submission") or {}).get("status") or "") == "auto_submitted":
            return self._say(channel, "That application was already submitted, so its CV can't "
                                      "change any more. Open SponsorJobs to make a new version.")
        doc = ctx["doc"]
        try:
            llm = self.llm_factory()
            raw = llm.plan_cv_edit(instruction, cvdoc.outline(doc)) or {}
        except Exception:   # noqa: BLE001
            return self._say(channel, "I couldn't work out that edit right now. Try again in a "
                                      "minute, or open SponsorJobs to edit it there.")
        plan = cvdoc.normalize_plan(raw, doc)
        if not plan["edits"]:
            if plan["suggest"] and plan["unknown"]:
                fixed = cvdoc.retarget(raw, plan["unknown"][0], plan["suggest"][0])
                pid = self._new_plan(rid, {"plan": fixed, "instruction": instruction})
                short = self._short(rid, pid)
                return self._say_buttons(channel, plan["clarify"], [[
                    {"id": f"cv:yes:{short}", "label": BTN_YES},
                    {"id": f"cv:no:{short}", "label": BTN_NO}]])
            return self._say(channel, plan["clarify"] or (
                "I couldn't tell what to change. Name the part and what you want, for example: "
                "rewrite E1.2 shorter. Tap Show changes to see every id."))
        facts = cvdoc.fact_changes(plan, doc)
        if facts:
            pid = self._new_plan(rid, {"plan": plan, "instruction": instruction, "facts": facts})
            short = self._short(rid, pid)
            return self._say_buttons(channel, fact_question(facts), [[
                {"id": f"cv:f1:{short}", "label": BTN_THIS_CV}],
                [{"id": f"cv:fp:{short}", "label": BTN_PROFILE}],
                [{"id": f"cv:fx:{short}", "label": BTN_CANCEL}]])
        return self.execute(channel, rid, plan, instruction)

    def _say_buttons(self, channel, text, buttons) -> str:
        channel.send(text, buttons)
        return text

    def _new_plan(self, rid, body: dict) -> str:
        pid = "q" + secrets.token_hex(4)
        self._merge(rid, {"tg_plan": {"id": pid, **body}})
        return pid

    def _take_plan(self, rid, pid):
        ctx = self.ctx(rid)
        plan = (ctx["data"].get("tg_plan") or {}) if ctx else {}
        if not plan or plan.get("id") != pid:
            return None
        self._merge(rid, {"tg_plan": {}})
        return plan

    def clarify_choice(self, channel, rid, pid, yes: bool) -> str:
        p = self._take_plan(rid, pid)
        if not p:
            channel.send(MSG_EXPIRED)
            return "Expired"
        if not yes:
            channel.send("OK. Tell me which part, or tap Show changes to see every id.")
            return "OK"
        ctx = self.ctx(rid)
        plan = cvdoc.normalize_plan(p["plan"], ctx["doc"])
        if not plan["edits"]:
            channel.send(MSG_NO_CHANGE)
            return "Nothing"
        facts = cvdoc.fact_changes(plan, ctx["doc"])
        if facts:
            pid2 = self._new_plan(rid, {"plan": plan, "instruction": p.get("instruction") or "",
                                        "facts": facts})
            short = self._short(rid, pid2)
            self._say_buttons(channel, fact_question(facts), [[
                {"id": f"cv:f1:{short}", "label": BTN_THIS_CV}],
                [{"id": f"cv:fp:{short}", "label": BTN_PROFILE}],
                [{"id": f"cv:fx:{short}", "label": BTN_CANCEL}]])
            return "Confirm"
        self.execute(channel, rid, plan, p.get("instruction") or "")
        return "On it"

    def fact_choice(self, channel, rid, pid, act: str) -> str:
        p = self._take_plan(rid, pid)
        if not p:
            channel.send(MSG_EXPIRED)
            return "Expired"
        if act == "fx":
            channel.send(MSG_CANCELLED)
            return "Cancelled"
        self.execute(channel, rid, p["plan"], p.get("instruction") or "",
                     profile_facts=p.get("facts") if act == "fp" else None)
        return "On it"

    def execute(self, channel, rid, plan: dict, instruction: str, profile_facts=None) -> str:
        """Apply a confirmed plan to a COPY of the page, check it, and send the preview with
        Accept / Undo. Nothing is written to the record's CV until Accept."""
        ctx = self.ctx(rid)
        try:
            out = self.build_candidate(ctx, plan, instruction)
        except cvdoc.EditRejected as exc:
            return self._say(channel, exc.reason)
        except ChannelError:
            raise
        except Exception:   # noqa: BLE001 - say so instead of going silent
            return self._say(channel, "I couldn't finish that edit right now, so your CV is as "
                                      "it was. Try again in a minute, or edit it in SponsorJobs.")
        if out is None:
            return self._say(channel, MSG_NO_CHANGE)
        pid = "e" + secrets.token_hex(4)
        pending_pdf = self.workdir / f"cv-{int(rid)}-pending.pdf"
        shutil.copyfile(out["pdf"], pending_pdf)
        pending = {"id": pid, "doc": out["doc"], "base": out["base"],
                   "report": out["report"], "notes": out["notes"],
                   "coverage": out["coverage"], "instruction": instruction,
                   "profile_facts": profile_facts or [], "at": self.now()}
        self._merge(rid, {"tg_pending": pending})
        short = self._short(rid, pid)
        report = "\n\n".join(out["report"])
        notes = ("\n\n" + "\n".join(out["notes"])) if out["notes"] else ""
        head = "Here's your edit." + (" I'll also update your saved profile when you accept."
                                      if profile_facts else "")
        body = f"{head}\n\n{report}{notes}\n\n{MSG_EDIT_FOOTER}"
        tmp_ctx = dict(ctx, doc=out["doc"])
        png = self._preview_png(tmp_ctx, pending_pdf, f"tg-{int(rid)}-pending")
        buttons = [[{"id": f"cv:acc:{short}", "label": BTN_ACCEPT},
                    {"id": f"cv:undo:{short}", "label": BTN_UNDO}]]
        if len(body) <= 1024:
            res = self._send_picture(channel, png, body, buttons)
        else:
            for m in cvdoc.chunk([f"{head}\n\n{report}{notes}"]):
                channel.send(m)
            res = self._send_picture(channel, png, MSG_EDIT_FOOTER, buttons)
        self._merge(rid, {"tg_pending": {**pending, "message_id": (res or {}).get("message_id")}})
        return body

    def build_candidate(self, ctx: dict, plan: dict, instruction: str):
        """The edited page, or None when nothing changed. Raises EditRejected with the reason."""
        from tailoring.assembler import _bullet_budget
        doc = ctx["doc"]
        spans = cvdoc.span_map(doc)
        llm = self.llm_factory()
        edits = plan.get("edits") or []
        sets = {e["target"]: e["value"] for e in edits if e["op"] == "set"}
        rewrites = [e["target"] for e in edits if e["op"] == "rewrite" and e["target"] not in sets]
        new_texts = dict(sets)
        budgets = {sid: _bullet_budget(spans[sid]["text"]) for sid in rewrites
                   if spans[sid]["kind"] in ("bullet", "summary")}
        if rewrites:
            targets = {sid: spans[sid]["text"] for sid in rewrites}
            entries = {spans[sid]["entry"] for sid in rewrites}
            context = {k: s["text"] for k, s in spans.items()
                       if k not in targets and (s["entry"] in entries or s["kind"] == "summary")}
            got = llm.edit_cv_spans(instruction, targets, context, ctx["jd"], budgets) or {}
            got = {str(k).strip(): str(v) for k, v in got.items()}
            stray = [k for k, v in got.items()
                     if k not in targets and (k not in spans or spans[k]["text"] != v)]
            if stray:
                raise cvdoc.EditRejected(
                    "I only change the part you point at, and that rewrite also touched "
                    f"{', '.join(sorted(stray))}. I left your CV as it was. Try again, or name "
                    "the exact id.", code="over_edit")
            for sid in rewrites:
                new = str(got.get(sid) or "").strip()
                if not new:
                    continue
                if sid in budgets and len(new) > budgets[sid]:
                    new = str(llm.shorten_bullet(new, budgets[sid]) or new).strip()
                new_texts[sid] = new
        notes = []
        for sid, new in list(new_texts.items()):
            notes += self._honesty(ctx, sid, spans[sid], new, instruction)
        cand = cvdoc.apply_text_edits(doc, new_texts, set(new_texts)) if new_texts else \
            json.loads(json.dumps(doc))
        struct = [e for e in edits if e["op"] in cvdoc.STRUCT_OPS]
        order = list(spans)

        def rank(e):
            if e["op"] == "drop":
                t = e["target"]
                pos = order.index(t) if t in order else next(
                    (i for i, k in enumerate(order) if spans[k]["entry"] == t), 0)
                return (2, -pos)
            return (0 if e["op"] == "add_bullet" else 1, 0)
        struct_report = []
        base_doc = {"profile": json.loads(json.dumps(ctx["base"] or {})), "sections": []}
        for e in sorted(struct, key=rank):
            struct_report.append(cvdoc.describe_struct(e, cand))
            cand = cvdoc.apply_struct_edit(cand, e)
            if ctx["base"] and e["op"] in ("drop", "add_bullet"):
                # Keep the "before" side aligned so Show changes still pairs E1.3 with E1.3
                # after a bullet above it is dropped; an added bullet has no before ("new").
                try:
                    base_doc = cvdoc.apply_struct_edit(
                        base_doc, dict(e, value="") if e["op"] == "add_bullet" else e)
                except (KeyError, IndexError, TypeError, AttributeError, cvdoc.EditRejected):
                    pass
            if e["op"] == "add_bullet":
                notes += self._honesty(ctx, e["target"] + ".new", {"kind": "bullet", "text": "",
                                       "entry": e["target"].split(".")[0]}, e["value"], instruction)
        if cand == doc:
            return None
        healable = [sid for sid in new_texts if spans[sid]["kind"] in ("bullet", "summary")
                    and sid not in sets]
        cand, pdf = self._compile_checked(ctx, cand, healable, instruction)
        report = []
        after = cvdoc.span_texts(cand)
        for sid in new_texts:
            report.append(cvdoc.before_after(sid, spans[sid]["text"].strip(),
                                             after.get(sid, "").strip()))
        report += struct_report
        return {"doc": cand, "pdf": pdf, "report": report, "notes": notes,
                "coverage": self._coverage(ctx, cand), "base": base_doc["profile"]}

    def _honesty(self, ctx, sid, span, new, instruction) -> list:
        """The fabrication gate for one edited span. Raises on a skill the person's entry does
        not show and they did not type; returns notes for what they typed and new figures."""
        from tailoring.fact_gate import figures, source_figures
        from tailoring.keywords import introduced_skills, profile_text, term_present
        kind = span.get("kind")
        if kind not in ("bullet", "summary", "skill", "interests"):
            return []
        doc, base = ctx["doc"], ctx["base"]
        if kind in ("summary", "skill", "interests"):
            grounding = profile_text(doc["profile"]) + " " + profile_text(base or {})
        else:
            grounding = cvdoc.grounding_text(doc, span["entry"], {"profile": base or {}})
        old = span.get("text") or ""
        intro = introduced_skills(old, new, grounding, ctx["jd"])
        typed = [t for t in intro if term_present(t, instruction)]
        untyped = [t for t in intro if t not in typed]
        if untyped:
            where = cvdoc.entry_label(doc, span["entry"]) if span.get("entry") else "your CV"
            raise cvdoc.EditRejected(
                f"That rewrite of {sid.replace('.new', '')} added {', '.join(untyped)}, which "
                f"{where} doesn't show anywhere, so I left it as it was. If it's true, say so "
                f"in your own words, for example: use the word {untyped[0]} in "
                f"{sid.replace('.new', '')}.", code="fabrication")
        notes = []
        label = sid.replace(".new", " (new bullet)")
        for t in typed:
            notes.append(f"Check: {t} in {label} is from your message, not your saved "
                         "material. Keep it only if it's true.")
        have = source_figures(doc["profile"], base or {}) | figures(instruction)
        for f in sorted(figures(new) - have):
            notes.append(f"Check: the figure {f} in {label} isn't in your material.")
        return notes

    def _coverage(self, ctx, doc) -> dict:
        from tailoring.assembler import _rendered_cv_text
        from tailoring.keywords import build_coverage_report
        rep = build_coverage_report(ctx["jd"], _rendered_cv_text(doc["profile"], doc["sections"]),
                                    doc["profile"])
        drop = {w.lower() for seg in (ctx["role"], ctx["company"])
                for w in re.findall(r"[A-Za-z]+", seg or "")}
        keep = lambda terms: [t for t in terms if t.lower() not in drop]   # noqa: E731
        present, missing, sup = keep(rep.present), keep(rep.missing_unsupported), keep(rep.missing_supported)
        total = len(present) + len(missing) + len(sup)
        return {"ratio": round(len(present) / total * 100) if total else 100,
                "present": present, "missing": missing, "missing_supported": sup}

    # ------------------------------------------------------------------ compile checks
    def _baseline_overfull(self, ctx, preamble) -> int:
        cached = ctx["data"].get("tg_baseline_overfull")
        if isinstance(cached, int):
            return cached
        from tailoring.assembler import render_cv
        res = self.compile_fn(render_cv(preamble, ctx["doc"]["profile"], ctx["doc"]["sections"],
                                        probe=True), self.workdir, jobname=f"tgbase-{ctx['rid']}")
        n = int(getattr(res, "overfull_count", 0) or 0)
        self._merge(ctx["rid"], {"tg_baseline_overfull": n})
        return n

    def _compile_checked(self, ctx, cand, healable, instruction):
        """Compile the candidate; one page, no new overfull lines, not past the bottom edge.
        On overflow, shorten ONLY the edited text (HEAL_TRIES), else reject."""
        from tailoring.assembler import MAX_FILL, extract_preamble, render_cv
        preamble = extract_preamble(self.template_fn(ctx["template"] or None))
        llm = None
        baseline = None
        reason = "That change would push your CV past one page, so I left it as it was. Try a shorter wording."
        for attempt in range(HEAL_TRIES + 1):
            res = self.compile_fn(render_cv(preamble, cand["profile"], cand["sections"], probe=True),
                                  self.workdir, jobname=f"tgedit-{ctx['rid']}")
            if not getattr(res, "ok", False):
                if getattr(res, "pages", None) is None:
                    raise cvdoc.EditRejected("That change broke the layout, so I left your CV as "
                                             "it was.", code="compile")
            over_bottom = bool(getattr(res, "fill_ratio", None) and res.fill_ratio > MAX_FILL)
            one_page = bool(res.ok and res.pages == 1 and not over_bottom)
            if one_page:
                n = int(res.overfull_count or 0)
                if n == 0:
                    return cand, res.pdf_path
                if baseline is None:
                    baseline = self._baseline_overfull(ctx, preamble)
                if n <= baseline:
                    return cand, res.pdf_path
                reason = ("That wording runs past the right margin, so I left your CV as it was. "
                          "Try a shorter wording.")
            if attempt == HEAL_TRIES or not healable:
                break
            llm = llm or self.llm_factory()
            spans = cvdoc.span_map(cand)
            heal = {}
            for sid in healable:
                text = spans[sid]["text"]
                short = str(llm.shorten_bullet(text, max(40, int(len(text) * 0.85))) or "").strip()
                if short and len(short) < len(text):
                    try:
                        self._honesty(ctx, sid, cvdoc.span_map(ctx["doc"])[sid], short, instruction)
                    except cvdoc.EditRejected:
                        continue
                    heal[sid] = short
            if not heal:
                break
            cand = cvdoc.apply_text_edits(cand, heal, set(healable))
        raise cvdoc.EditRejected(reason, code="layout")

    # ------------------------------------------------------------------ accept / undo / versions
    def _versions(self, ctx) -> list:
        return list(ctx["data"].get("versions") or [])

    def accept(self, channel, rid, pid) -> str:
        ctx = self.ctx(rid)
        pend = (ctx["data"].get("tg_pending") or {}) if ctx else {}
        if not pend or pend.get("id") != pid:
            channel.send("That edit was already handled.")
            return "Done"
        pending_pdf = self.workdir / f"cv-{int(rid)}-pending.pdf"
        if not pending_pdf.exists():
            channel.send(MSG_EXPIRED)
            return "Expired"
        versions = self._versions(ctx)
        cur_pdf = self.pdf_path(rid)
        if not versions:
            v1 = self.workdir / f"cv-{int(rid)}-v1.pdf"
            if cur_pdf.exists():
                shutil.copyfile(cur_pdf, v1)
            versions.append({"v": 1, "at": "", "note": "as tailored", "doc": ctx["doc"],
                             "base": ctx["base"], "coverage": ctx["data"].get("coverage") or {},
                             "pdf": v1.name, "edit": ""})
        n = max(v["v"] for v in versions) + 1
        vpdf = self.workdir / f"cv-{int(rid)}-v{n}.pdf"
        shutil.copyfile(pending_pdf, vpdf)
        shutil.copyfile(pending_pdf, cur_pdf)
        pending_pdf.unlink(missing_ok=True)
        doc = pend["doc"]
        versions.append({"v": n, "at": _iso(self.now()), "note": (pend.get("instruction") or "")[:120],
                         "doc": doc, "base": pend.get("base", ctx["base"]),
                         "coverage": pend.get("coverage") or {}, "pdf": vpdf.name, "edit": pid})
        cov = pend.get("coverage") or {}
        self._merge(rid, {"page_profile": doc["profile"], "sections": doc["sections"],
                          "base_profile": pend.get("base", ctx["base"]),
                          "render_profile": doc["profile"], "coverage": cov,
                          "versions": versions, "version": n, "tg_pending": {}})
        if cov.get("ratio") is not None:
            self._set_coverage(rid, cov["ratio"])
        updated = 0
        for ch in pend.get("profile_facts") or []:
            if self.update_profile is not None:
                try:
                    updated += 1 if self.update_profile(ch, ctx["doc"]) else 0
                except Exception:   # noqa: BLE001
                    pass
        self._strip_buttons(channel, pend.get("message_id"))
        who = _who(ctx["role"], ctx["company"])
        prof = ""
        if pend.get("profile_facts"):
            prof = (" Your saved profile is updated too." if updated else
                    " I couldn't find that entry in your saved profile, so only this CV changed.")
        line, buttons = self._submit_line(rid)
        short = self._short(rid, pid)
        rows = [[{"id": f"cv:undo:{short}", "label": BTN_UNDO}]] + (buttons or [])
        channel.send(f"Saved as version {n} of {who}.{prof} {line}", rows)
        return "Saved"

    def undo(self, channel, rid, pid) -> str:
        ctx = self.ctx(rid)
        if not ctx:
            channel.send(MSG_EXPIRED)
            return "Expired"
        pend = ctx["data"].get("tg_pending") or {}
        if pend and pend.get("id") == pid:
            self._merge(rid, {"tg_pending": {}})
            (self.workdir / f"cv-{int(rid)}-pending.pdf").unlink(missing_ok=True)
            self._strip_buttons(channel, pend.get("message_id"))
            channel.send(MSG_DISCARDED)
            return "Discarded"
        versions = self._versions(ctx)
        cur = int(ctx["data"].get("version") or (versions[-1]["v"] if versions else 1))
        latest = versions[-1] if versions else None
        if not latest or latest.get("edit") != pid or latest["v"] != cur or len(versions) < 2:
            channel.send("That edit is no longer the latest, so there's nothing to undo here. "
                         f"See /versions {int(rid)}.")
            return "Nothing"
        return self._restore(channel, rid, ctx, versions[:-1])

    def _restore(self, channel, rid, ctx, versions) -> str:
        prev = versions[-1]
        src = self.workdir / prev["pdf"]
        if not src.exists():
            channel.send("I can't find the earlier version on this computer.")
            return "Missing"
        shutil.copyfile(src, self.pdf_path(rid))
        doc = prev["doc"]
        cov = prev.get("coverage") or {}
        self._merge(rid, {"page_profile": doc["profile"], "sections": doc["sections"],
                          "base_profile": prev.get("base", ctx["base"]),
                          "render_profile": doc["profile"], "coverage": cov,
                          "versions": versions, "version": prev["v"]})
        if cov.get("ratio") is not None:
            self._set_coverage(rid, cov["ratio"])
        channel.send(f"Undone. {_who(ctx['role'], ctx['company'])} is back to version {prev['v']}.")
        return "Undone"

    def versions_text(self, rid) -> str:
        ctx = self.ctx(rid)
        if not ctx:
            return f"No application #{rid} found. Try /list."
        versions = self._versions(ctx)
        who = _who(ctx["role"], ctx["company"])
        if not versions:
            return f"{who} (#{rid}) has one version, as tailored. No edits yet."
        cur = int(ctx["data"].get("version") or versions[-1]["v"])
        lines = [f"Versions of {who} (#{rid}):"]
        for v in versions:
            mark = " (current)" if v["v"] == cur else ""
            when = (v.get("at") or "")[:16].replace("T", " ")
            note = v.get("note") or ""
            lines.append(f"v{v['v']}{mark}: {note}" + (f", {when}" if when else ""))
        return "\n".join(lines)

    def _strip_buttons(self, channel, message_id) -> None:
        if not message_id:
            return
        try:
            channel.edit_text(message_id, buttons=[])
        except Exception:   # noqa: BLE001 - stale buttons are handled by id checks anyway
            pass


def fact_question(facts: list) -> str:
    """'This changes a fact on your CV: Stanbic Bank title 'Senior Product Manager' ->
    'Product Lead'. Apply to this application only, or also update your profile?'"""
    parts = []
    for f in facts:
        label = cvdoc.FIELD_LABEL.get(f["field"], f["field"])
        if f["field"] in ("company", "school", "name"):
            parts.append(f"{label} from '{f['old']}' to '{f['new']}'")
        else:
            parts.append(f"{f['label']} {label} from '{f['old']}' to '{f['new']}'")
    lead = "This changes a fact on your CV: " if len(parts) == 1 else "This changes facts on your CV: "
    return lead + "; ".join(parts) + ". Apply to this application only, or also update your profile?"


def _looks_like_cv_edit(text: str) -> bool:
    """A typed message that is an edit, not a chat question. Span ids or a CV part named with
    an edit verb; questions ("can you change jobs?") go to the regular chat."""
    t = text.strip()
    if cvdoc.SPAN_ID_RE.search(t) and re.search(r"[A-Z]+\d", t):
        return True
    low = t.lower()
    parts = ("summary", "bullet", "title", "dates", "projects", "experience", "skills",
             "education", "section", "my cv", "the cv", "résumé", "resume", "headline")
    return bool(EDIT_WORDS.search(t)) and any(p in low for p in parts) and not t.endswith("?")


def _as_int(s):
    try:
        return int(str(s).lstrip("#"))
    except (TypeError, ValueError):
        return None


def _close(store) -> None:
    fn = getattr(store, "close", None)
    if callable(fn):
        try:
            fn()
        except Exception:   # noqa: BLE001
            pass


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
