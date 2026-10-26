"""The Home chat agent (CLAUDE.md §4): the Home box takes a pasted job description, a link,
or a plain request ("Find 5 data analytics roles and apply for me"). A description or a link
keeps today's path (a tailoring session); a request is parsed into an intent and drives the
existing features, always stating a plan first. Nothing here touches the network, the records
or the submit path: the routes in ui/app.py do, through the same functions Auto-apply uses.

Routing is a cheap heuristic first (a URL, or long multi-line text with job-posting words),
then, for short text, a tiny LLM parse into one JSON object; a rule-based parse stands in when
no model answers (no key, offline, a test). The texts the person reads are built here so the
tests can pin them: plain words, no dashes, "resume" never "CV".
"""

from __future__ import annotations

import json
import re

INTENTS = ("find_and_apply", "status", "tailor_one", "other")

# Words a posting carries and a request almost never does.
_JD_WORDS = re.compile(
    r"\b(responsibilities|requirements|qualifications|what you.ll do|what you will do|"
    r"about the role|about the team|about us|benefits|we are looking for|we.re looking for|"
    r"minimum qualifications|preferred qualifications|equal opportunity|job description|"
    r"you will|the ideal candidate|nice to have|must have|apply now|salary|compensation)\b",
    re.IGNORECASE)
_URL = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)

# Rule-based parse: what each example says, in the words people use for it.
_STATUS = re.compile(r"\b(waiting on me|waiting for me|what is waiting|what's waiting|"
                     r"status|pending|progress|where are we|what did you do|update me|"
                     r"anything for me|what do i need to do|what needs me)\b", re.IGNORECASE)
_FIND = re.compile(r"\b(find|search|look for|apply|tailor|queue|get me|build)\b", re.IGNORECASE)
_TAILOR_ONE = re.compile(r"\b(tailor|resume|cv)\b.*\b(for|at)\b", re.IGNORECASE)
_NUM = re.compile(r"\b(\d{1,3})\b")
_WORDS_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "a few": 3, "some": 5, "a couple of": 2}
_TITLES = re.compile(
    r"(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|a few|some|a couple of)?"
    r"\s*([a-z][a-z /&+-]*?)\s+(?:roles?|jobs?|positions?|openings?)\b", re.IGNORECASE)
_LOCATION = re.compile(r"\b(?:in|near|around)\s+([A-Z][\w.]*(?:[ ,]+[A-Z][\w.]*){0,3})")
_AWAY = re.compile(r"\baway\s+(?:for\s+)?(\d{1,2}|an?)\s*(?:hours?|hrs?|h)\b", re.IGNORECASE)
_AT_COMPANY = re.compile(r"\bat\s+([A-Z][\w&.]*(?:\s+[A-Z][\w&.]*){0,2})")
_FOR_ROLE = re.compile(r"\bfor\s+(?:an?\s+)?([a-z][a-z /&+-]*?)\s+(?:role|job|position|opening)\b",
                       re.IGNORECASE)
_TITLE_NOISE = {"best", "good", "new", "more", "matching", "remote", "sponsored", "sponsoring",
                "sponsor", "visa", "fresh", "top", "great", "suitable", "open", "the", "me", "my",
                "some", "any", "a", "an", "and", "for", "of", "to", "with", "that", "find",
                "search", "tailor", "apply", "queue", "get", "build", "look", "resume", "cv",
                "please", "can", "you", "then", "now", "also", "hours", "hour", "away", "am", "i"}


# ----------------------------------------------------------------------------- routing

def looks_like_url(text: str) -> bool:
    return bool(_URL.match((text or "").strip()))


def looks_like_jd(text: str) -> bool:
    """A pasted posting: long or multi-line AND it uses posting words, or very long anyway."""
    t = (text or "").strip()
    if not t:
        return False
    lines = [ln for ln in t.splitlines() if ln.strip()]
    long_ish = len(t) > 500 or len(lines) >= 3
    if len(t) > 1500:
        return True
    return long_ish and bool(_JD_WORDS.search(t))


def route(text: str) -> str:
    """'url' or 'jd' keeps today's path (a tailoring session); 'request' goes to the agent."""
    if looks_like_url(text):
        return "url"
    if looks_like_jd(text):
        return "jd"
    return "request"


# ----------------------------------------------------------------------------- the parse

def empty_intent(intent: str = "other") -> dict:
    return {"intent": intent, "titles": [], "count": None, "location": None,
            "telegram": False, "away_hours": None, "sponsors_only": False}


def _clean_title(raw: str) -> str:
    words = [w for w in re.split(r"\s+", raw.strip().lower()) if w]
    # Keep the words nearest the noun ("business analyst" from "find me 10 business analyst
    # roles"): a verb or filler word anywhere cuts off what came before it.
    kept: list[str] = []
    for w in words:
        if w in _TITLE_NOISE or w.isdigit():
            kept = []
        else:
            kept.append(w)
    return " ".join(kept[-3:])


def _count_in(text: str) -> int | None:
    m = _NUM.search(text)
    if m:
        return int(m.group(1))
    low = text.lower()
    for word, n in _WORDS_NUM.items():
        if re.search(rf"\b{word}\b", low):
            return n
    return None


def parse_request_rules(text: str) -> dict:
    """The offline parse: keyword rules over the sentence. Also what FakeLLM runs on."""
    t = " ".join((text or "").split())
    out = empty_intent()
    if not t:
        return out
    low = t.lower()
    out["telegram"] = "telegram" in low
    out["sponsors_only"] = bool(re.search(r"\bsponsor", low))
    m = _AWAY.search(t)
    if m:
        out["away_hours"] = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
    m = _LOCATION.search(t)
    if m:
        out["location"] = m.group(1).strip(" ,.")
    titles = []
    for m in _TITLES.finditer(t):
        title = _clean_title(m.group(1))
        if title and title not in titles:
            titles.append(title)
    m = _FOR_ROLE.search(t)
    if m:
        title = _clean_title(m.group(1))
        if title and title not in titles:
            titles.append(title)
    out["titles"] = titles
    if _STATUS.search(low):
        out["intent"] = "status"
    elif _AT_COMPANY.search(t) and _TAILOR_ONE.search(t) and not re.search(r"\bfind\b", low):
        out["intent"] = "tailor_one"
    elif _FIND.search(low) and (titles or re.search(r"\b(roles?|jobs?|positions?)\b", low)):
        out["intent"] = "find_and_apply"
        # "tailor 10 business analyst roles": the number is how many, never a year or an hour
        body = _AWAY.sub(" ", t)
        out["count"] = _count_in(body)
    return out


INTENT_SYSTEM = (
    "You turn one short request from a job seeker into JSON for an app that finds roles, "
    "tailors resumes and applies. Answer with one JSON object only, no prose:\n"
    '{"intent": "find_and_apply" | "status" | "tailor_one" | "other", '
    '"titles": ["job title", ...], "count": integer or null, "location": string or null, '
    '"telegram": boolean, "away_hours": integer or null, "sponsors_only": boolean}\n'
    "find_and_apply: find roles, tailor, queue or apply (count = how many, titles = the roles "
    "named). status: what is waiting, pending, or done. tailor_one: tailor a resume for one "
    "named role or company with no job description given. other: anything else. "
    "telegram is true when the person wants a message on Telegram. away_hours is how long "
    "they said they will be away. sponsors_only is true when they ask for visa sponsors only."
)


def _json_object(raw: str):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def normalize_intent(d: dict | None, fallback: dict | None = None) -> dict:
    """One well-typed intent from a model answer: unknown intents become "other", lists and
    numbers are coerced, and anything unreadable takes the rule parse's value."""
    fb = fallback or empty_intent()
    if not isinstance(d, dict):
        return dict(fb)
    out = empty_intent()
    intent = str(d.get("intent") or "").strip().lower()
    out["intent"] = intent if intent in INTENTS else fb["intent"]
    titles = d.get("titles")
    if isinstance(titles, str):
        titles = [titles]
    out["titles"] = [str(x).strip() for x in (titles or []) if str(x).strip()][:5] or fb["titles"]
    for key in ("count", "away_hours"):
        v = d.get(key)
        try:
            out[key] = max(1, int(v)) if v not in (None, "", False) else None
        except (TypeError, ValueError):
            out[key] = fb[key]
    loc = d.get("location")
    out["location"] = str(loc).strip() if loc and str(loc).strip() else fb["location"]
    out["telegram"] = bool(d.get("telegram")) or fb["telegram"]
    out["sponsors_only"] = bool(d.get("sponsors_only")) or fb["sponsors_only"]
    return out


def parse_request(llm, text: str) -> dict:
    """The intent of one short request: the model's JSON when it answers (a tiny prompt, low
    effort), else the rule parse. A backend with no free-form call (FakeLLM) uses the rules."""
    rules = parse_request_rules(text)
    complete = getattr(llm, "_complete", None)
    if not callable(complete):
        return rules
    try:
        raw = complete(INTENT_SYSTEM, (text or "").strip()[:600], max_tokens=200, effort="low")
    except TypeError:                     # a backend without the effort knob
        raw = complete(INTENT_SYSTEM, (text or "").strip()[:600], max_tokens=200)
    except Exception:                     # noqa: BLE001 - the rules still answer
        return rules
    return normalize_intent(_json_object(raw), rules)


# ----------------------------------------------------------------------------- the texts

EXAMPLES = ("Find 5 data analytics roles and apply for me",
            "I am away for 3 hours: tailor 10 business analyst roles and message me on Telegram",
            "What is waiting on me?")

DEFAULT_COUNT = 5
RUN_CAP = 10          # the Auto-apply route's cap per run


def other_text() -> str:
    return ('I can find roles and apply, tailor a resume for a job description, or tell you '
            'what is waiting. Try "' + EXAMPLES[0] + '", "' + EXAMPLES[1] + '" or "'
            + EXAMPLES[2] + '".')


def tailor_one_text(intent: dict) -> str:
    what = ", ".join(intent.get("titles") or []) or "that role"
    return (f"To tailor your resume for {what} I need the job description or a link to it. "
            "Paste it here, or I can find matching roles for you instead.")


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def titles_phrase(titles: list[str]) -> str:
    t = [x for x in (titles or []) if x]
    if not t:
        return "best-fit"
    if len(t) == 1:
        return t[0]
    return ", ".join(t[:-1]) + " or " + t[-1]


def cap_count(wanted: int | None, packages_left: int | None) -> tuple[int, str]:
    """How many the run may do, and one sentence saying why when it is fewer than asked."""
    n = max(1, int(wanted or DEFAULT_COUNT))
    note = ""
    if n > RUN_CAP:
        note = f"Auto-apply does up to {RUN_CAP} a run, so I will do {RUN_CAP}."
        n = RUN_CAP
    if packages_left is not None and packages_left < n:
        n = max(0, packages_left)
        note = (f"Your pass has {packages_left} {plural(packages_left, 'package')} left, so I "
                f"will do {n}.")
    return n, note


def plan_text(*, n: int, titles: list[str], min_fit: int, auto: int, auto_companies: str,
              auto_reason: str, telegram: bool, telegram_connected: bool,
              packages_left: int | None, note: str = "") -> str:
    """The plan the person confirms, in exactly the approved shape. ``auto`` is computed by
    the app's own gate, never assumed; ``auto_reason`` says why it is 0."""
    lines = ["Here is the plan."]
    lines.append(f"1. Find the {n} best {titles_phrase(titles)} roles that fit you at {min_fit}% "
                 "or better, visa sponsors first.")
    lines.append("2. Tailor a resume and a cover letter for each, from your saved profile.")
    rest = max(0, n - auto)
    why = auto_companies if auto else auto_reason
    lines.append(f"3. Queue them in To review. {auto} can be sent for you ({why}); {rest} will be "
                 "filled in and ready for your click.")
    if telegram and telegram_connected:
        lines.append("4. Message you on Telegram when they are done.")
    elif telegram:
        lines.append("4. Telegram is not connected yet, so I will show the results here. Connect "
                     "it in Auto-apply to get them on your phone.")
    tail = f"This uses {n} of your {packages_left} packages." if packages_left is not None \
        else f"This uses {n} {plural(n, 'package')}."
    if note:
        tail = note + " " + tail
    lines.append(tail + " Go ahead?")
    return "\n".join(lines)


def result_text(tailored: int, sent: int) -> str:
    rest = max(0, tailored - sent)
    return (f"Done. Tailored {tailored}. {sent} sent for you. {rest} ready for your click in "
            "Auto-apply.")


def status_text(counts: dict, newest: list[str]) -> str:
    """One bubble from the records: the four tiles and the newest items by name."""
    bits = [f"To review: {int(counts.get('review') or 0)}.",
            f"Waiting on you: {int(counts.get('waiting') or 0)}.",
            f"Needs you: {int(counts.get('needs') or 0)}.",
            f"Submitted today: {int(counts.get('submitted_today') or 0)}."]
    text = " ".join(bits)
    names = [x for x in (newest or []) if x][:5]
    if names:
        text += " Newest: " + "; ".join(names) + "."
    elif not any(counts.get(k) for k in ("review", "waiting", "needs")):
        text += " Nothing is waiting on you."
    return text


def no_match_text(titles: list[str], location: str | None) -> str:
    where = f" in {location}" if location else ""
    return (f"No {titles_phrase(titles)} roles{where} match right now. Try broader titles, "
            "another location, or drop the sponsors-only filter.")


def change_text() -> str:
    return "What should I change? The titles, how many, or where."


def batch_message(tailored: list[dict], sent: list[dict], companies_fn) -> str:
    """The one Telegram message after a Go ahead, with the same batch buttons as Autopilot
    so Approve all / Review / Skip work from the phone for what is still waiting."""
    n = len(tailored)
    rest = [it for it in tailored if it not in sent]
    parts = [f"Done. Tailored {n} {plural(n, 'role')} from Home ({companies_fn(tailored)})."]
    if sent:
        parts.append(f"{len(sent)} sent for you ({companies_fn(sent)}).")
    if rest:
        verb = "are" if len(rest) != 1 else "is"
        parts.append(f"{len(rest)} {verb} ready and waiting for your click ({companies_fn(rest)}).")
    return " ".join(parts)
