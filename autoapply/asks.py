"""The apply agent's questions to the person, and how their reply is read.

When a run meets a required question nothing in the person's material answers, it asks them on
the bar and on Telegram (ui/app.py, /api/autoapply/agent/ask). This module holds the pure parts:

* ``known_answer``: before asking, the person's own material (the answer library, work
  authorization, the standing "decline to self-identify" choice on EEO questions). Nothing is
  guessed: a demographic answer only ever comes from the person, and it is used only on the
  choice that says the same thing in the form's own words (autoapply/eeo.py), or a decline for
  a decline.
* ``order_options``: at most 8 choices to offer, the decline choice FIRST on an EEO question,
  picked from every choice the page has (``all_options``, up to 40). A list the page did not
  show in full is marked ``partial``: then a saved answer or a typed reply outside it is the
  person's own words, not an error.
* ``telegram_ask`` / ``prompt_text``: one message for one question (its choices as buttons
  inside the broker's limits: 3 rows of up to 3, labels of 30 characters), or numbered lines for
  several, always under Telegram's 4096 characters.
* ``read_reply``: the person's reply goes through one small model call that takes ONLY the part
  that answers each question ("Yes, I will need H1B / Male / don't you have this?" answers
  Gender with "Male"), notices a question back ("what are the options?") and an unclear reply.
  A tapped-like reply (a choice's own words, or its number) needs no model, and when the model
  fails or stalls the plain reading (``read_plain``) takes over. The reply is never saved raw.
"""
from __future__ import annotations

import json
import re
import threading

from autoapply import eeo as EEO
from autoapply import workauth
from autoapply.answers import normalize
from autoapply.planner import (_DECLINE, _EEO, _n, both_halves, citizenship_text, decline_option, eeo_option,
                               listed_citizenship, literal_for, match_option, residence_withheld)

MAX_OPTIONS = 8          # choices offered as buttons
MAX_ALL = 40             # choices kept to match a saved answer or a reply against
MAX_OPTION_CHARS = 120   # one choice's words (Workday's EEO choices run past 90 characters)
MAX_TEXT = 3900          # one Telegram message, inside its 4096 limit (docs/notify.md)
LABEL_MAX = 30           # one Telegram button label (docs/notify.md)
READ_TIMEOUT_S = 25      # the model's reading of one reply; past it the plain reading answers

# A dropdown's prompt entry ("Select...", "-- Choose one --", "Please select"), never a choice.
# Only a pure prompt: "Choose not to disclose" (a decline) and "Search engine" are real choices.
# Mirrored in extension/agent_view.js, extension/autofill_core.js and shell-electron/agent.js.
_PLACEHOLDER = re.compile(
    r"^[\s\-_*.\u2013\u2014]*((please )?(select|choose)\b|please (enter|type|pick)\b|make a selection\b)"
    r"|^-{2,}|^(none selected|no selection|nothing selected)\W*$"
    r"|^(search|start typing|type to search)(\W*$|\s*(\.{3}|\u2026)|\s+(for|by|here)\b)", re.I)


def is_placeholder(text) -> bool:
    s = " ".join(str(text or "").split())
    return bool(s) and bool(_PLACEHOLDER.search(s)) and not _DECLINE.search(s)


def is_eeo(question: str) -> bool:
    """A voluntary self-identification question (gender, race, ethnicity, veteran, disability,
    sexual orientation). Its answer only ever comes from the person."""
    q = str(question or "")
    return bool(_EEO.search(q)) and not workauth.classify(q)


def clean_options(options, limit: int = MAX_ALL) -> list[str]:
    out: list[str] = []
    for o in options or []:
        s = " ".join(str(o or "").split())[:MAX_OPTION_CHARS]
        if s and not is_placeholder(s) and s not in out:
            out.append(s)
            if len(out) >= limit:
                break
    return out


def order_options(question: str, options, limit: int = MAX_OPTIONS) -> list[str]:
    """Up to ``limit`` choices for the person, the decline choice first on an EEO question
    (even when it sat beyond the first eight on the page)."""
    opts = clean_options(options)
    if is_eeo(question):
        dec = [o for o in opts if _DECLINE.search(o)]
        if dec:
            opts = dec[:1] + [o for o in opts if o != dec[0]]
    return opts[:limit]


def _squash(s) -> str:
    return " ".join(str(s or "").split()).lower()


def same_question(a, b) -> bool:
    """One question in two wordings of the same page: the same words, or one cut short of the
    other (the agent's view cuts a label at 80 characters, a Workday "Error-" link at 74)."""
    x, y = _squash(a), _squash(b)
    if not x or not y:
        return False
    if x == y:
        return True
    s, longer = (x, y) if len(x) <= len(y) else (y, x)
    return len(s) >= 70 and longer.startswith(s)


def known_answer(question: str, options, *, partial: bool = False, library=None, work_auth=None,
                 eeo_decline: bool = False, job_country: str = "US", hint: str = "") -> tuple[str, str] | None:
    """(answer, origin) when the person's own material answers this question, else None.
    Origins: "library", "workauth", "eeo_decline". ``options`` is every choice the page has (or
    as many as were read; ``partial`` says the list is cut). With options, the answer is the
    exact option (a saved country by name or alias). On an EEO question the standing decline
    comes first, then the saved answers on the form's own wording (autoapply/eeo.py: "Male" is
    "Man", a combined race list takes the race and the Hispanic or Latino answer together), or
    the page's decline choice for a saved decline, never a guess. On a cut list a saved answer
    that fits none of the choices read is still the person's answer: it goes to the page as is,
    where the brain picks the entry. A free-text citizenship question that asks for each country
    gets the second one too. ``hint`` is the page's own note right above the field (Lam
    Research's "Veteran" sits under a paragraph about protected veterans, so its plain No says
    "not a protected veteran"); it only ever narrows an EEO choice, never answers alone."""
    q = str(question or "").strip()
    if not q:
        return None
    opts = clean_options(options)
    key = normalize(q)

    def fit(text):
        text = str(text or "").strip()
        if not text:
            return None
        if not opts:
            return text
        hit = literal_for(key, text, opts)
        return hit or (text if partial else None)

    v = listed_citizenship(q, library, opts, "select" if opts else "text")
    if v:
        return v, "library"
    if workauth.classify(q):
        v = workauth.answer(q, work_auth or {}, options=opts, field_type="select" if opts else "text",
                            job_country=job_country)
        if v:
            return v, "workauth"
    if is_eeo(q):
        # The standing decline first and alone: while it is on, a saved answer is never used.
        # The page's decline choice, else the question stays open (the person is asked).
        if eeo_decline:
            v = decline_option(opts)
            return (v, "eeo_decline") if v else None
        # Then their own saved answers on the form's own wording (autoapply/eeo.py), never a guess.
        qkey, answers, _rows = EEO.saved(library, q)
        own = answers.get(qkey, "")
        if not opts:
            if qkey in ("@race", "@ethnicity") and own and not both_halves(own):
                return None                 # a combined race list needs its choices read
            return (own, "library") if own else None
        about = " ".join((q + " " + " ".join(str(hint or "").split())[:400]).split())
        v = eeo_option(own, opts, qkey, answers, question=about) if answers else None
        if v:
            return v, "library"
        if partial and own and (qkey not in ("@race", "@ethnicity") or both_halves(own)):
            return own, "library"
        return None
    hit = library.lookup(q) if library is not None else None
    if hit and residence_withheld(q, work_auth):
        return None
    if hit:
        ans = hit.get("answer")
        if key == "@citizenship" and not opts:
            ans = citizenship_text(q, library, ans)
        v = fit(ans)
        if v:
            return v, "library"
    return None


# --------------------------------------------------------------------------- the messages
def _num(it: dict, i: int) -> int:
    """The question's number as the person saw it (kept when some are answered first)."""
    try:
        return int(it.get("n") or i)
    except (TypeError, ValueError):
        return i


def _every(it: dict) -> list:
    return list(it.get("all_options") or it.get("options") or [])


def _more(it: dict, shown: int) -> str:
    """", and 3 more" for choices the page has beyond those shown; ", and more on the page" for
    a list that was only partly read."""
    if it.get("partial"):
        return ", and more on the page"
    extra = len(_every(it)) - shown
    return f", and {extra} more" if extra > 0 else ""


def fit_text(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "…"


def prompt_text(items: list[dict], context: str = "", *, list_choices: bool = False,
                buttons: bool = True) -> str:
    """The Telegram message for one question or a batch, under MAX_TEXT. ``list_choices`` lists
    a single question's choices in full, numbered (its buttons are cut to 30 characters, or there
    are no buttons at all)."""
    ctx = " ".join(str(context or "").split())[:120]
    for cap in (MAX_OPTIONS, 4, 2, 0):
        if len(items) == 1:
            it = items[0]
            c = " ".join(str(it.get("context") or ctx or "").split())[:120]
            head = (f"After “{c}”, the application asks: " if c else "The application asks: ") + it["question"]
            shown = list(it.get("options") or [])[:cap] if cap else []
            if not (it.get("options") or []):
                text = head + "\nReply with your answer and I will carry on."
            else:
                lines = [head]
                if list_choices and shown:
                    lines.append("The choices:")
                    lines += [f"{k}. {o}" for k, o in enumerate(shown, start=1)]
                if _more(it, len(it.get("options") or [])):
                    lines.append("The page has more choices: reply with yours if it is not listed.")
                lines.append("Tap a choice below, or reply with your answer." if buttons
                             else "Reply with the number of your choice, or with your answer.")
                text = "\n".join(lines)
        else:
            lines = [f"The application asks {len(items)} questions:"]
            for i, it in enumerate(items, start=1):
                opts = list(it.get("options") or [])
                shown = opts[:cap] if cap else []
                choice = ""
                if shown:
                    choice = " (choices: " + ", ".join(shown) + _more(it, len(shown)) + ")"
                elif opts:
                    choice = f" ({len(_every(it))} choices on the page)"
                lines.append(f"{_num(it, i)}. {it['question']}" + choice)
            lines.append("Reply in one message, one answer per number, and I will carry on.")
            text = "\n".join(lines)
        if len(text) <= MAX_TEXT:
            return text
    return fit_text(text)


def _label(o: str, k: int, numbered: bool) -> str:
    lab = f"{k}. {o}" if numbered else o
    return lab if len(lab) <= LABEL_MAX else lab[:LABEL_MAX - 1].rstrip() + "…"


def telegram_ask(aid: str, items: list[dict], context: str = "") -> tuple[str, list | None]:
    """The Telegram message for the open questions, and its buttons: only for a single question
    with choices, laid out inside the broker's contract (docs/notify.md: at most 3 rows of 1 to 3
    buttons, labels of 1 to 30 characters, ids of 40). Up to three choices go one per row with
    their own words; more go two or three to a row, numbered, with the full choices listed in the
    message, and so does any choice too long for a label. Each id, ask:<id>:<k>, points at the
    full choice, so a cut label still picks the right one."""
    if len(items) == 1 and items[0].get("options"):
        opts = list(items[0]["options"])[:MAX_OPTIONS]
        cols = 1 if len(opts) <= 3 else 2 if len(opts) <= 6 else 3
        numbered = cols > 1 or any(len(o) > LABEL_MAX for o in opts)
        flat = [(_label(o, k + 1, numbered), f"ask:{aid}:{k}") for k, o in enumerate(opts)]
        rows = [flat[k:k + cols] for k in range(0, len(flat), cols)]
        return prompt_text(items, context, list_choices=numbered), rows
    return prompt_text(items, context), None


def options_reply(items: list[dict]) -> str:
    """The answer to "what are the options?": the choices when known, else how to answer."""
    text = ""
    for cap in (20, 8, 4, 0):
        parts = []
        for i, it in enumerate(items, start=1):
            tag = (f"{_num(it, i)}. " if len(items) > 1 else "") + it["question"]
            every = _every(it)
            shown = order_options(it["question"], every, limit=cap) if cap else []
            if shown:
                lead = "Some of the choices" if it.get("partial") else "The choices"
                parts.append(f"{lead} for “{tag}” are: " + "; ".join(shown) + _more(it, len(shown)) + ".")
            elif every:
                parts.append(f"“{tag}” has {len(every)} choices on the page. Type yours.")
            else:
                parts.append(f"I cannot read the choices for “{tag}” on the page. Type the exact choice"
                             + (", or reply Prefer not to say." if is_eeo(it["question"]) else "."))
        text = "\n".join(parts)
        if len(text) <= MAX_TEXT:
            return text
    return fit_text(text)


def unclear_reply(items: list[dict]) -> str:
    if len(items) == 1:
        if _every(items[0]):
            return fit_text("I could not tell your answer from that. " + options_reply(items))
        return fit_text("I could not tell your answer from that. Please reply with just the answer to: "
                    + items[0]["question"])
    return "I could not match that to the questions. Please reply with one numbered line per question."


# --------------------------------------------------------------------------- reading a reply
SYSTEM = (
    "You read a person's chat reply to questions a job application asked them. Return ONLY JSON:\n"
    '{"answers": [{"n": <question number>, "answer": "<the value for THAT question>"}], '
    '"question": "<what the person asked back, or empty>", "unclear": true|false}\n'
    "Rules:\n"
    "1. A reply can mix several things. For each question take ONLY the part that answers it "
    "(\"Yes, I need H1B. Male. Don't you have this?\" answers Gender with \"Male\").\n"
    "2. Use each question's own number n. When a question lists options and partial is false, the "
    "answer must be one option, copied exactly. When partial is true only some choices are listed: "
    "copy the matching one, or give the person's own short answer.\n"
    "3. Never invent or infer an answer the reply does not give. Leave that question out.\n"
    "4. If the person asks something back (\"what are the options?\"), put it in \"question\" and do "
    "not treat it as an answer.\n"
    "5. unclear is true only when the reply neither answers any question nor asks one."
)


def build_user(items: list[dict], reply: str) -> str:
    qs = []
    for i, it in enumerate(items, start=1):
        every = _every(it)[:MAX_ALL]
        q = {"n": _num(it, i), "question": it["question"], "options": every}
        if every:
            q["partial"] = bool(it.get("partial"))
        qs.append(q)
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


def _got(answers: dict | None = None, *, question: str = "", unclear: bool = False, error: bool = False) -> dict:
    return {"answers": dict(answers or {}), "question": question, "unclear": unclear, "error": error}


_BREAK = re.compile(r"[.;!?]\s+\S")


def _one_choice(raw) -> bool:
    """One short value: a self-identification answer, or a choice the page did not list. Never a
    paragraph: "Yes, I will need H1B. Male" is two answers in one."""
    s = str(raw or "").strip()
    return bool(s) and "\n" not in s and not _BREAK.search(s) and len(s) <= 60


def _accept(it: dict, raw) -> str | None:
    """The value to keep for one question from what was read off the reply, or None: never a
    question, an option when the question has options (the person's own short words when the
    list was only partly read), and one short choice on a self-identification question."""
    raw = str(raw or "").strip()
    val = " ".join(raw.split())[:300]
    if not val or val.endswith("?"):
        return None
    every = _every(it)
    if every:
        hit = match_option(val, every)
        if hit:
            return hit
        return val if it.get("partial") and _one_choice(raw) else None
    if is_eeo(it["question"]) and not _one_choice(raw):
        return None
    return val


def _read_exact(items: list[dict], reply: str) -> dict | None:
    """A tap-like reply to ONE question with choices: a choice's own words, or its number in the
    list the message showed. No model is needed for that."""
    if len(items) != 1:
        return None
    it = items[0]
    shown, every = list(it.get("options") or []), _every(it)
    if not every:
        return None
    s = str(reply or "").strip()
    eq = [o for o in every if _n(o) and _n(o) == _n(s)]
    if eq:
        return _got({0: eq[0]})
    # "2": the second choice as listed, unless the choices are numbers themselves ("1-3 years").
    m = re.fullmatch(r"#?\s*(\d{1,2})\s*[.)]?", s)
    if m and shown and 1 <= int(m.group(1)) <= len(shown) and not any(re.match(r"\s*\d", o) for o in every):
        return _got({0: shown[int(m.group(1)) - 1]})
    return None


def read_plain(items: list[dict], reply: str) -> dict:
    """The reply read without the model (none configured, or it failed or stalled): a choice's
    words or number, numbered lines ("1. Male"), a question back, or one short line for a
    single question. Anything else is unclear, never a guess."""
    s = str(reply or "").strip()
    if not s:
        return _got(unclear=True)
    exact = _read_exact(items, s)
    if exact:
        return exact
    by_n = {_num(it, i): i - 1 for i, it in enumerate(items, start=1)}
    answers = {}
    for line in s.splitlines():
        m = re.match(r"^\s*(\d{1,2})\s*[.):]\s*(.+)$", line)
        if m and int(m.group(1)) in by_n:
            j = by_n[int(m.group(1))]
            v = _accept(items[j], m.group(2))
            if v:
                answers[j] = v
    if answers:
        return _got(answers)
    if s.endswith("?"):
        return _got(question=" ".join(s.split())[:200])
    if len(items) == 1 and "\n" not in s and len(s) <= 120:
        v = _accept(items[0], s)
        if v:
            return _got({0: v})
    return _got(unclear=True)


def _with_deadline(fn, seconds: float):
    """fn() in a worker thread, at most ``seconds``: a stalled model never holds the person's
    reply (or the Telegram tick) for minutes."""
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:   # noqa: BLE001 - handed to the caller below
            box["e"] = exc
    th = threading.Thread(target=run, daemon=True, name="ask-reply")
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise TimeoutError("the reply was not read in time")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def read_reply(llm, items: list[dict], reply: str, *, timeout: float = READ_TIMEOUT_S) -> dict:
    """{"answers": {index: value}, "question": str, "unclear": bool, "error": bool} for the
    person's reply to ``items`` (each {question, options, all_options?, partial?, n?}; n is the
    number the person saw, the index is the position in ``items``). Each value is validated: an
    option when the question has options and one fits, never a question, one short choice on a
    self-identification question. When the model fails, stalls or answers nothing readable, the
    plain reading answers instead and ``error`` says so (the caller then does not count it as
    the person's unclear reply)."""
    s = str(reply or "").strip()
    if not s:
        return _got(unclear=True)
    exact = _read_exact(items, s)
    if exact:
        return exact
    if llm is None:
        return read_plain(items, s)
    try:
        data = _parse(_with_deadline(lambda: llm.ask_reply(SYSTEM, build_user(items, s)), timeout))
        if not any(k in data for k in ("answers", "question", "unclear")):
            raise ValueError("unreadable reply")
    except Exception:          # noqa: BLE001 - a model blip: read it plainly, never guess
        out = read_plain(items, s)
        out["error"] = True
        return out
    by_n = {_num(it, i): i - 1 for i, it in enumerate(items, start=1)}
    answers: dict[int, str] = {}
    for a in data.get("answers") or []:
        if not isinstance(a, dict):
            continue
        try:
            j = by_n.get(int(a.get("n")))
        except (TypeError, ValueError):
            continue
        if j is None:
            continue
        v = _accept(items[j], a.get("answer"))
        if v:
            answers[j] = v
    asked = " ".join(str(data.get("question") or "").split())[:200]
    return _got(answers, question=asked, unclear=not answers and not asked)


# --------------------------------------------------------------------------- terms a site requires
# A privacy notice, data privacy agreement, terms or E-Verify acknowledgement a site needs accepted
# before it takes the application (Lam Research's "Data Privacy Agreement", 2026-10-30). The person
# decides: these are the three choices on the bar and on Telegram. "Accept for all applications"
# also sets the standing preference (accept_required_terms = "always"); "Stop" ends the run and
# nothing is accepted. Mirrored in shell-electron/agent.js TERMS_CHOICES and termsQuestion.
TERMS_ALL = "Accept for all applications"
TERMS_ONCE = "Just this one"
TERMS_STOP = "Stop"
TERMS_CHOICES = (TERMS_ALL, TERMS_ONCE, TERMS_STOP)
TERMS_PREF = ("ask", "always")


def terms_question(company: str, title: str) -> str:
    """"Lam Research needs you to accept its Data Privacy Agreement to apply. Accept it?\""""
    who = " ".join(str(company or "").split())[:80] or "This site"
    what = " ".join(str(title or "").split())[:80] or "terms"
    return f"{who} needs you to accept its {what} to apply. Accept it?"


def terms_decision(choice: str) -> tuple[str, str]:
    """(decision, via) for a choice: ("accept", "all"), ("accept", "once") or ("stop", "")."""
    if choice == TERMS_ALL:
        return "accept", "all"
    if choice == TERMS_ONCE:
        return "accept", "once"
    return "stop", ""


def read_terms_reply(text: str) -> str | None:
    """The choice a typed reply makes, strictly: a choice's own words or its number, or a plain
    yes / no. Consent is never read by a model and never guessed: anything else is None (the
    person is asked to tap a choice)."""
    s = " ".join(str(text or "").split()).strip(" .!").lower()
    if not s:
        return None
    for k, c in enumerate(TERMS_CHOICES, start=1):
        if s in (c.lower(), str(k)):
            return c
    if s in ("accept for all", "accept all", "all", "always", "for all", "yes to all", "yes for all"):
        return TERMS_ALL
    if s in ("yes", "y", "ok", "okay", "accept", "accept it", "i accept", "yes, accept it", "this one",
             "just this once", "once", "only this one"):
        return TERMS_ONCE
    if s in ("no", "n", "stop", "don't", "do not", "no thanks", "decline", "cancel", "/stop"):
        return TERMS_STOP
    return None
