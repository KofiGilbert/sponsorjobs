"""One voice in the resume chat (Kofi, 2026-11-10: "Isn't the ai the one doing the talking all the
time? cos i think that should be the case").

The session code decides WHAT happens in a turn and what must be said: what is missing, how full
the page is, what the build changed, what was cut or flagged, which connections are live and which
buttons show. Each of those is a brief item: a ``kind``, its facts, and ``point``, the plain line
the app used to say for it. This module turns a turn's brief into ONE chat message written by the
model (``narrate``), from those facts plus the conversation so far, so the chat never stacks a
model reply on top of fixed lines that do not know what it just said.

The fixed lines stay as the fallback (``fallback``): when the model call fails, the broker is
down, or the message it writes breaks a rule (``check``), the person gets the points joined into
one message instead. Buttons and cards never come from here: the session attaches them.

Rules the written message is held to, in code (``check`` and ``clean``):
  * every ``exact`` string of every item appears verbatim (a page fill of 62%, a section name, a
    button label), and no number appears that is not in the brief or the conversation;
  * at most one question in the whole message;
  * no em or en dashes, "resume" never "CV", no markdown.
The session then runs its own checks on top (the GitHub-ask rewrite and the claim check).
"""

from __future__ import annotations

import json
import os
import re
import time

FLAG = "TAILOR_ONE_VOICE"          # "0" turns narration off: the fallback lines only
MAX_TOKENS = 400                   # the message is a few sentences; room for a short think
HISTORY_TURNS = 6                  # chat turns the narrator reads, newest last
TURN_CHARS = 500                   # each clipped to this many characters
MAX_CHARS = 1100                   # a written message longer than this is not "short"

# Kinds whose point is a question to the person. A turn asks at most one.
ASK_KINDS = ("ask", "ask_required", "ask_missing", "ask_skills", "nudge_required", "opening",
             "keep_choice")


def enabled() -> bool:
    return os.environ.get(FLAG, "1").strip().lower() not in ("0", "false", "off", "no")


def item(kind: str, point: str, exact=None, **facts) -> dict:
    """One brief item. ``point`` is the fallback line; ``exact`` the strings the written message
    must carry verbatim; ``facts`` everything the model may say about it."""
    out = {"kind": str(kind), "point": str(point or "").strip()}
    ex = [str(x) for x in (exact or []) if str(x).strip()]
    if ex:
        out["exact"] = ex
    for k, v in facts.items():
        if v not in (None, "", [], {}):
            out[k] = v
    return out


SYSTEM = "\n".join([
    "You are the voice of the resume chat in the SponsorJobs desktop app. The app has already "
    "decided what happens in this turn and what the person must be told; you write the ONE chat "
    "message they read now, in your own words, as one person talking.",
    "",
    "You get the chat so far and the brief: the points for this turn, in order, each with its "
    "facts. Rules:",
    "- Cover every point in the brief, merged into one short, warm message: two to five plain "
    "sentences, no list, no greeting, no sign off. Keep the brief's order unless another reads "
    "more naturally.",
    "- Use every number, name, skill, section and button label exactly as the brief gives it. "
    "Every string listed under \"exact\" must appear in your message character for character. "
    "Button labels go in <b></b>, for example <b>Accept & Save</b>.",
    "- Never add a fact the brief and the chat do not give: no new number, skill, project, date, "
    "employer, link or claim about the person. Never say you did, saved, added or changed "
    "anything the brief does not say was done.",
    "- Ask at most ONE question, and only the brief's own question (a point whose kind starts "
    "with ask, or the opening). If the brief has no question, ask none.",
    "- A \"reply_draft\" point is what the intake helper wrote back to the person's last message. "
    "Keep what in it answers them, drop anything the other points contradict or repeat, and drop "
    "its questions unless the brief has no question of its own.",
    "- A \"build\" point means the page was just built or rebuilt; the person sees its summary row "
    "right above your message, so do not repeat that row's numbers unless a point needs them.",
    "- When the person asked something, answer it first, from the brief and the chat only.",
    "- Plain words. No dashes as punctuation, no markdown, no headings. Say resume, never CV. "
    "Never mention the brief, points or the app's rules.",
    "Answer with the message text only.",
])


def _clip(text: str, n: int = TURN_CHARS) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def user_prompt(items: list[dict], history: list[dict], message: str = "",
                role: str = "", company: str = "") -> str:
    """The narrator's user turn: the job, the chat so far (clipped), what the person just said,
    and the brief as JSON."""
    lines = []
    if role or company:
        lines.append("JOB: " + " at ".join(x for x in (role, company) if x))
    turns = [t for t in history or [] if isinstance(t, dict) and t.get("role") in ("user", "agent")
             and str(t.get("content") or "").strip()]
    if message and turns and turns[-1].get("role") == "user" \
            and str(turns[-1].get("content") or "").strip() == message.strip():
        turns = turns[:-1]
    if turns:
        lines.append("THE CHAT SO FAR (newest last):")
        for t in turns[-HISTORY_TURNS:]:
            who = "Person" if t["role"] == "user" else ("App (summary row)" if t.get("build") else "You")
            lines.append(f"{who}: {_clip(t.get('content'))}")
    lines.append("THE PERSON JUST SAID: " + (_clip(message, 1200) if message else
                                              "(nothing typed: the app is acting on a click or an event)"))
    brief = [{k: v for k, v in it.items()} for it in items]
    lines.append("THE BRIEF (this turn's points, in order):")
    lines.append(json.dumps(brief, ensure_ascii=False, default=str))
    lines.append("Write the one message now.")
    return "\n".join(lines)


# ------------------------------------------------------------------------------ cleaning

def clean(text: str) -> str:
    """No AI dashes, "resume" never "CV", markdown bold as the chat's <b>, no headings, no
    surrounding quotes."""
    from tailoring.conform import strip_ai_dashes
    t = str(text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    t = strip_ai_dashes(t)
    t = re.sub(r"\bCVs\b", "resumes", re.sub(r"\bCV\b", "resume", t))
    t = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^#{1,6}\s+", "", t)
    t = re.sub(r"(?m)^\s*[-*•]\s+", "", t)              # a list the prompt forbade: plain lines
    return re.sub(r"\n{3,}", "\n\n", t).strip()


_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")
_QUESTION = re.compile(r"[^.?!\n]*\?")


def questions(text: str) -> int:
    return len(_QUESTION.findall(str(text or "")))


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s or ""))).replace("&amp;", "&").strip().lower()


def check(text: str, items: list[dict], context: str = "") -> str:
    """Why the written message cannot be shown ("" when it can): a missing exact string, a number
    the brief and the chat never gave, more than one question, empty or too long."""
    t = str(text or "").strip()
    if not t:
        return "empty"
    if len(t) > MAX_CHARS:
        return "too long"
    low = _plain(t)
    for it in items:
        for ex in it.get("exact") or []:
            if _plain(ex) not in low:
                return f"missing {ex!r}"
    known = _plain(json.dumps(items, ensure_ascii=False, default=str) + " " + str(context or ""))
    for n in _NUM.findall(re.sub(r"<[^>]+>", "", t)):
        if n not in known:
            return f"number {n} not in the brief"
    if questions(t) > 1:
        return "more than one question"
    return ""


# A claim that the page or the profile changed ("I added", "I've updated", "I filled the page").
_CHANGED = re.compile(
    r"\b(?:i['’]?ve|i have|i|we['’]?ve|we have|we)\s+(?:just\s+|already\s+|also\s+|now\s+)?"
    r"(?:saved|added|updated|changed|filled|refilled|rebuilt|rewrote|reworded|removed|cut|put)\b", re.I)
_NOT = re.compile(r"\b(?:not|never|haven['’]?t|didn['’]?t|won['’]?t|can['’]?t|cannot)\b", re.I)
# "share your GitHub so I can check": the person cannot share it in the chat; the Connect GitHub
# button does it (Kofi, 2026-11-10). The same rewrite as agent/resume_chat.py.
_SHARE_GITHUB = re.compile(
    r"\b(?:share|send|give|paste|drop)(?:\s+(?:me|us))?\s+(?:your|ur)\s+github"
    r"(?:\s+(?:link|username|profile|url|handle))?", re.I)


def claims_change(text: str) -> bool:
    """A sentence that says the app saved, added, changed or filled something (not a negated one,
    not a promise)."""
    for sent in re.split(r"(?<=[.!?])\s+", str(text or "")):
        if _CHANGED.search(sent) and not _NOT.search(sent):
            return True
    return False


def share_to_connect(text: str) -> tuple[str, bool]:
    """("share your GitHub" -> "connect your GitHub with the button below", whether it was said)."""
    if not _SHARE_GITHUB.search(text or ""):
        return text, False
    return _SHARE_GITHUB.sub("connect your GitHub with the button below", text), True


# ------------------------------------------------------------------------------ fallback

def fallback(items: list[dict]) -> str:
    """The points joined into one message, as the app said them before: the reply first, then the
    facts, then the one question. A draft reply's own questions are dropped when another point
    asks, so the fallback asks at most one thing too."""
    asks = [it for it in items if it.get("kind") in ASK_KINDS and it.get("point")]
    out = []
    for it in items:
        p = str(it.get("point") or "").strip()
        if not p:
            continue
        if it.get("kind") == "reply_draft" and asks:
            kept = [s for s in re.split(r"(?<=[.!?])\s+", p) if s.strip() and not s.strip().endswith("?")]
            p = " ".join(kept).strip()
            if not p:
                continue
        if any(it is a for a in asks) and it is not asks[-1]:
            continue                    # one question: the last ask is the one that stands
        out.append(p)
    return " ".join(out).strip()


# ------------------------------------------------------------------------------ the call

def narrate(llm, items: list[dict], history: list[dict], message: str = "", role: str = "",
            company: str = "") -> dict:
    """{"text", "ok", "why", "usage", "secs"}: the one message written by the model, cleaned and
    checked, or the fallback with ``ok`` False and ``why``."""
    fb = fallback(items)
    fn = getattr(llm, "narrate", None)
    if not callable(fn) or not enabled() or not items:
        return {"text": fb, "ok": False, "why": "off", "usage": None, "secs": 0.0}
    prompt = user_prompt(items, history, message, role, company)
    t0 = time.time()
    try:
        raw = fn(SYSTEM, prompt, max_tokens=MAX_TOKENS)
    except Exception as exc:      # noqa: BLE001 - the broker down, out of credit, a bad answer
        return {"text": fb, "ok": False, "why": f"call failed: {type(exc).__name__}",
                "usage": None, "secs": round(time.time() - t0, 2)}
    usage = getattr(llm, "last_narrate_usage", None)
    text = clean(raw)
    why = "" if text == clean(fb) else check(text, items, context=prompt)
    if why:
        return {"text": fb, "ok": False, "why": why, "usage": usage, "raw": str(raw or "")[:600],
                "secs": round(time.time() - t0, 2)}
    return {"text": text, "ok": True, "why": "", "usage": usage, "secs": round(time.time() - t0, 2)}
