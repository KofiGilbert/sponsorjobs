"""The apply agent's questions to the person, and how their reply is read.

When a run meets a required question nothing in the person's material answers, it asks them on
the bar and on Telegram (ui/app.py, /api/autoapply/agent/ask). This module holds the pure parts:

* ``known_answer``: before asking, the person's own material (the answer library, work
  authorization, the standing "decline to self-identify" choice on EEO questions). Nothing is
  guessed: a demographic answer only ever comes from the person.
* ``order_options``: at most 8 choices, the decline choice FIRST on an EEO question.
* ``prompt_text``: one message for one question, or numbered lines for several.
* ``read_reply``: the person's reply goes through one small model call that takes ONLY the part
  that answers each question ("Yes, I will need H1B / Male / don't you have this?" answers
  Gender with "Male"), notices a question back ("what are the options?") and an unclear reply.
  The reply is never saved raw.
"""
from __future__ import annotations

import json
import re

from autoapply import workauth
from autoapply.planner import _DECLINE, _EEO, decline_option, match_option

MAX_OPTIONS = 8

# Placeholder entries a dropdown shows before a choice, never an answer.
_PLACEHOLDER = re.compile(r"^(select|choose|please select|please choose|--|none selected|no selection)\b", re.I)


def is_eeo(question: str) -> bool:
    """A voluntary self-identification question (gender, race, ethnicity, veteran, disability,
    sexual orientation). Its answer only ever comes from the person."""
    q = str(question or "")
    return bool(_EEO.search(q)) and not workauth.classify(q)


def clean_options(options) -> list[str]:
    out: list[str] = []
    for o in options or []:
        s = " ".join(str(o or "").split())[:80]
        if s and not _PLACEHOLDER.search(s) and s not in out:
            out.append(s)
    return out


def order_options(question: str, options) -> list[str]:
    """Up to MAX_OPTIONS choices for the person, the decline choice first on an EEO question
    (even when it sat beyond the first eight on the page)."""
    opts = clean_options(options)
    if is_eeo(question):
        dec = [o for o in opts if _DECLINE.search(o)]
        if dec:
            opts = dec[:1] + [o for o in opts if o != dec[0]]
    return opts[:MAX_OPTIONS]


def known_answer(question: str, options, *, library=None, work_auth=None, eeo_decline: bool = False,
                 job_country: str = "US") -> tuple[str, str] | None:
    """(answer, origin) when the person's own material answers this question, else None.
    Origins: "library", "workauth", "eeo_decline". With options, the answer is the exact option."""
    q = str(question or "").strip()
    if not q:
        return None
    opts = clean_options(options)

    def fit(text):
        text = str(text or "").strip()
        if not text:
            return None
        return match_option(text, opts) if opts else text

    if workauth.classify(q):
        v = workauth.answer(q, work_auth or {}, options=opts, field_type="select" if opts else "text",
                            job_country=job_country)
        if v:
            return v, "workauth"
    hit = library.lookup(q) if library is not None else None
    if hit:
        v = fit(hit.get("answer"))
        if v:
            return v, "library"
    if is_eeo(q) and eeo_decline and opts:
        v = decline_option(opts)
        if v:
            return v, "eeo_decline"
    return None


def prompt_text(items: list[dict], context: str = "") -> str:
    """The Telegram message for one question or a batch."""
    ctx = " ".join(str(context or "").split())[:120]
    if len(items) == 1:
        it = items[0]
        c = " ".join(str(it.get("context") or ctx or "").split())[:120]
        head = (f"After “{c}”, the application asks: " if c else "The application asks: ") + it["question"]
        if it.get("options"):
            return head + "\nTap a choice below, or reply with your answer."
        return head + "\nReply with your answer and I will carry on."
    lines = ["The application asks " + str(len(items)) + " questions:"]
    for n, it in enumerate(items, start=1):
        opts = it.get("options") or []
        lines.append(f"{n}. {it['question']}" + (f" (choices: {', '.join(opts)})" if opts else ""))
    lines.append("Reply in one message, one answer per number, and I will carry on.")
    return "\n".join(lines)


def options_reply(items: list[dict]) -> str:
    """The answer to "what are the options?": the choices when known, else how to answer."""
    parts = []
    for n, it in enumerate(items, start=1):
        tag = (f"{n}. " if len(items) > 1 else "") + it["question"]
        opts = it.get("options") or []
        if opts:
            parts.append(f"The choices for “{tag}” are: " + "; ".join(opts) + ".")
        else:
            parts.append(f"I cannot read the choices for “{tag}” on the page. Type the exact choice"
                         + (", or reply Prefer not to say." if is_eeo(it["question"]) else "."))
    return "\n".join(parts)


def unclear_reply(items: list[dict]) -> str:
    if len(items) == 1:
        return "I could not tell your answer from that. " + options_reply(items) if items[0].get("options") \
            else "I could not tell your answer from that. Please reply with just the answer to: " + items[0]["question"]
    return "I could not match that to the questions. Please reply with one numbered line per question."


# --------------------------------------------------------------------------- reading a reply
SYSTEM = (
    "You read a person's chat reply to questions a job application asked them. Return ONLY JSON:\n"
    '{"answers": [{"n": <question number>, "answer": "<the value for THAT question>"}], '
    '"question": "<what the person asked back, or empty>", "unclear": true|false}\n'
    "Rules:\n"
    "1. A reply can mix several things. For each question take ONLY the part that answers it "
    "(\"Yes, I need H1B. Male. Don't you have this?\" answers Gender with \"Male\").\n"
    "2. When the question lists options, the answer must be one option, copied exactly.\n"
    "3. Never invent or infer an answer the reply does not give. Leave that question out.\n"
    "4. If the person asks something back (\"what are the options?\"), put it in \"question\" and do "
    "not treat it as an answer.\n"
    "5. unclear is true only when the reply neither answers any question nor asks one."
)


def build_user(items: list[dict], reply: str) -> str:
    qs = [{"n": n, "question": it["question"], "options": it.get("options") or []}
          for n, it in enumerate(items, start=1)]
    return ("<questions>\n" + json.dumps(qs, ensure_ascii=False) + "\n</questions>\n"
            "<reply>\n" + str(reply or "")[:1500] + "\n</reply>")


def _parse(raw: str) -> dict:
    text = str(raw or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return {}
    try:
        data = json.loads(text[s:e + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_reply(llm, items: list[dict], reply: str) -> dict:
    """{"answers": {index: value}, "question": str, "unclear": bool} for the person's reply.
    Each value is validated: an option when the question has options and one fits, never a
    question, never longer than a short answer. A model error reads as unclear."""
    reply = str(reply or "").strip()
    if not reply:
        return {"answers": {}, "question": "", "unclear": True}
    try:
        data = _parse(llm.ask_reply(SYSTEM, build_user(items, reply)))
    except Exception:          # noqa: BLE001 - a model blip: ask again rather than guess
        data = {}
    answers: dict[int, str] = {}
    for a in data.get("answers") or []:
        if not isinstance(a, dict):
            continue
        try:
            i = int(a.get("n")) - 1
        except (TypeError, ValueError):
            continue
        val = " ".join(str(a.get("answer") or "").split())[:300]
        if not (0 <= i < len(items)) or not val or val.endswith("?"):
            continue
        opts = items[i].get("options") or []
        if opts:
            val = match_option(val, opts) or val
        answers[i] = val
    asked = " ".join(str(data.get("question") or "").split())[:200]
    unclear = not answers and not asked
    return {"answers": answers, "question": asked, "unclear": unclear}
