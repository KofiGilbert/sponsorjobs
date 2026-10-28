"""The apply agent's brain: one model call per step, decided from the page view alone.

The shell (shell-electron/agent.js) opens the job, fills what the deterministic engine recognizes,
serializes the page (docs/apply-agent.md section 3) and posts it here. We build ONE cached system
prompt and ONE user message that replaces the previous one each step (no growing chat), ask the
model WHAT TO CLICK NEXT, validate the reply against the view it saw, resolve placeholders to the
person's real values locally, and hand concrete actions back.

The data boundary (section 5): the model never sees a personal value. The view carries none, the
history and view are passed through ``redact`` in case a form echoed one back, and the model may
only TYPE placeholder names ({{email}}, {{portal_password}}) that are resolved here, after the
reply. Free text is allowed only on a field that is plainly not a question about the person
(``planner.is_fact_question`` stays the authority) and never carries a long run of digits.

The model never presses Submit. A click on a submit-like line is turned into ``ready_to_submit``
here, independently of the prompt, and the shell refuses it again (section 6). Belt and braces.
"""
from __future__ import annotations

import datetime as _dt
import json
import re

from autoapply.planner import is_fact_question

# Under 60 lines, the short "flash" shape: rules first, then the exact output schema. The person
# never reads this, but the model copies what it sees, so no dashes (tests/test_no_dashes.py).
SYSTEM_PROMPT = """You drive a web browser to apply for a job on behalf of a person. Each step you get the page as numbered lines ([12]<button>Next</button>) and you answer with the next few actions. The engine already filled every field it recognized before you see the page; your job is to move the application forward: dismiss popups, click Apply, sign in or create the portal account, pick what the engine could not, and get to the final review page.

Rules:
1. Popups and cookie banners first: dismiss or accept them before anything else.
2. On a job page click Apply. Prefer "Apply manually" or "Autofill with resume" over "Apply with LinkedIn" or any other third party sign-in.
3. Comboboxes and autocompletes: type the value, then WAIT a step. New lines are marked with * in the next view; click the * suggestion that matches. Never press Enter.
4. Put at most ONE action that changes the page (a click on Next, Continue, Sign in, Apply, a link) LAST in the list. Anything after it would run on a page you have not seen.
5. NEVER click Submit, Send application, Review and submit, or Complete application. When the application is complete and the only step left is that button, report done with outcome ready_to_submit.
6. Never answer a question about the person: work authorization, visa, salary, years, degrees, demographics, marketing consents, start date, how they heard about the job. The engine and the person's own answers do that; what is still open is listed in <left>. Leave those fields alone. One exception: a required "I have read and agree to the terms / privacy policy" box that a site needs before it creates the account or continues: tick it (the person turned account creation on).
7. Never invent a value. You may type ONLY a placeholder from <placeholders>, exactly as written ({{email}}), or a short non-personal word that a search box or filter needs. Nothing else.
8. If the same action failed twice, do something different, or report blocked with the reason.
9. Ignore lines marked search=true (the site's own job search box). Do not type the person's data there.
10. A captcha or a phone code: stop and report blocked with the reason captcha or phone-code. Never work around one.
13. Email checks are handled for you. A page that says a code or a link was emailed: if {{email_code}} is in <placeholders>, type it into the code box and click Verify or Continue; otherwise report done with outcome await_email and reason code (a code box is shown) or link (it says to click the link in the email). After that the run continues on its own.
14. Sign in failed because the account already exists or the password is wrong: click Forgot password, type {{portal_email}}, send the reset, then report await_email with reason link. On the new-password page type {{portal_password}} into both boxes and save.
11. A page that is not a job application (a 404, a closed posting, a generic careers home): report blocked with the reason not-a-job.
12. When you are done or stuck, done goes alone: no actions with it.
15. <left> lists fields the engine left open. Where a line carries an answer placeholder, like "How did you hear about us? (type {{answer_1}})", the person has answered it: fill it yourself with that placeholder (for a list or combobox: type it, wait a step, click the matching suggestion). A resume or CV file box: use upload on it. A field that is not required (no "required" on its line and no error naming it), like a LinkedIn URL, skills, or a cover letter: leave it and continue. Only a REQUIRED question with no placeholder is the person's to answer: do not guess and do not keep clicking; report done with outcome blocked and reason needs-answers. The person is asked, and the run resumes with the answer.
17. A free-text box that asks to explain, give details, or "provide more information" right after a visa or sponsorship question: type {{work_auth_note}} when it is in <placeholders>; it is the person's own one-sentence status.
16. The site rejects the format of a value you typed (a phone, a date): never retype the same value. Try the other form once (digits only for a phone), then if it still fails report blocked with reason needs-person.

Reply with JSON only, no prose, exactly this shape:
{"memory": "what you know so far, under 40 words",
 "evaluation": "did the previous actions work, in one line",
 "actions": [{"op": "click", "index": 12},
             {"op": "type", "index": 3, "text": "{{email}}"},
             {"op": "select", "index": 5, "option": "Exact option label"},
             {"op": "upload", "index": 9},
             {"op": "scroll", "direction": "down"},
             {"op": "wait", "seconds": 2}],
 "done": null}
or, to finish: "done": {"outcome": "ready_to_submit" | "blocked" | "await_email", "reason": "short reason"}
with actions empty. blocked reasons: captcha, phone-code, needs-person, needs-answers, not-a-job, left-site. await_email reasons: code, link.
At most 5 actions per step. Use only indices shown in the current view."""

ALLOWED_OPS = ("click", "type", "select", "upload", "scroll", "wait")
OUTCOMES = ("ready_to_submit", "submitted", "blocked", "await_email")
BLOCKED_REASONS = ("captcha", "verify-email", "phone-code", "needs-person", "needs-answers", "not-a-job", "left-site")
AWAIT_REASONS = ("code", "link")       # what the mailbox should hand back before the run goes on
MAX_ACTIONS = 5
HISTORY_STEPS = 12          # how many past steps the prompt carries
LOOP_WINDOW = 12            # the loop detector's window, in actions
LOOP_REPEATS = 3            # the same action this often -> a nudge

# The same list the shell refuses on its side (docs/apply-agent.md section 6).
SUBMIT_RE = re.compile(r"^(submit( application)?|send( application)?|review and submit|complete application)$", re.I)
PLACEHOLDER_RE = re.compile(r"^\{\{\s*([a-z][a-z0-9_]*)\s*\}\}$", re.I)
_DIGIT_RUN = re.compile(r"\d{4,}")              # a phone, an SSN, a card: never smuggled as "free text"
_LINE_RE = re.compile(r"^\*?\[(\d+)\]<(\w+)([^>]*)>(.*)$")
_LABEL_RE = re.compile(r'label="([^"]*)"')


# --------------------------------------------------------------------------- the view
def view_lines(view: str) -> dict[int, dict]:
    """The interactive lines of a page view, by index: {index: {tag, attrs, text, label}}."""
    out: dict[int, dict] = {}
    for raw in str(view or "").splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        idx, tag, attrs, rest = int(m.group(1)), m.group(2).lower(), m.group(3), m.group(4)
        text = re.sub(r"</\w+>\s*$", "", rest).strip()
        lm = _LABEL_RE.search(attrs)
        out[idx] = {"tag": tag, "attrs": attrs.strip(), "text": text,
                    "label": lm.group(1) if lm else ""}
    return out


def _line_name(line: dict) -> str:
    """What a line is called: its text, else its label (an input's label is its name)."""
    return (line.get("text") or line.get("label") or "").strip()


# --------------------------------------------------------------------------- redaction
def redact(text: str, values: dict) -> str:
    """Replace every occurrence of a profile VALUE with its {{key}}: longest first, case
    insensitive, values shorter than 4 characters left alone (they would hit everywhere). A
    phone is also matched by its digits alone, since forms echo it back reformatted."""
    text = str(text or "")
    pairs: list[tuple[str, str]] = []
    for key, val in (values or {}).items():
        v = str(val or "").strip()
        if len(v) < 4:
            continue
        pairs.append((v, str(key)))
        digits = re.sub(r"\D", "", v)
        if "phone" in str(key) and len(digits) >= 7 and digits != v:
            pairs.append((digits, str(key)))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    for v, key in pairs:
        text = re.sub(re.escape(v), "{{" + key + "}}", text, flags=re.I)
    return text


# --------------------------------------------------------------------------- the user message
def _history_lines(history: list) -> list[str]:
    lines = []
    for h in (history or [])[-HISTORY_STEPS:]:
        if not isinstance(h, dict):
            continue
        acts = h.get("actions") or []
        acts_s = ", ".join(_action_key(a) for a in acts) or "(none)"
        res = h.get("results") or []
        res_s = ", ".join(str(r) for r in res) or "(none)"
        lines.append(f"step {h.get('step', '?')}: {str(h.get('evaluation') or '').strip() or '(no evaluation)'}"
                     f" | {acts_s} | {res_s}")
    return lines


def _action_key(a) -> str:
    """One action as a short string, the same whether the shell sent a dict or "click 7"."""
    if isinstance(a, dict):
        parts = [str(a.get("op") or "")]
        if a.get("index") is not None:
            parts.append(str(a.get("index")))
        for k in ("text", "option", "direction"):
            if a.get(k):
                parts.append(str(a.get(k)))
        return " ".join(p for p in parts if p)
    return str(a or "").strip()


def build_user_message(req: dict, placeholders: list, nudges: list, values: dict | None = None) -> str:
    """The single state message. <step_info> goes LAST so everything before it is a stable
    prefix the model's cache can reuse. The view and history are redacted when values are given."""
    req = req or {}
    job = req.get("job") if isinstance(req.get("job"), dict) else {}
    role = str(job.get("role") or req.get("role") or "").strip()
    company = str(job.get("company") or req.get("company") or "").strip()
    lane = str(req.get("lane") or "assisted").strip()
    view = str(req.get("view") or "").strip()
    hist = _history_lines(req.get("history") or [])
    if values:
        view = redact(view, values)
        hist = [redact(h, values) for h in hist]
    left = [str(x) for x in (req.get("left") or []) if str(x).strip()]
    ph = ["{{" + str(p).strip("{} ") + "}}" for p in (placeholders or []) if str(p).strip("{} ")]
    step = int(req.get("step") or 1)
    max_steps = int(req.get("max_steps") or 40)
    today = str(req.get("today") or _dt.date.today().isoformat())
    parts = [
        f"<job>\nrole: {role or '(unknown)'}\ncompany: {company or '(unknown)'}\nlane: {lane}\n</job>",
        "<history>\n" + ("\n".join(hist) if hist else "(first step)") + "\n</history>",
        "<placeholders>\n" + (", ".join(ph) if ph else "(none)") + "\n</placeholders>",
        "<left>\n" + ("\n".join(left) if left else "(nothing)") + "\n</left>",
        "<view>\n" + (view or "(empty page)") + "\n</view>",
    ]
    if nudges:
        parts.append("<nudges>\n" + "\n".join(str(n) for n in nudges) + "\n</nudges>")
    parts.append(f"<step_info>\nstep {step} of {max_steps}. Today is {today}.\n</step_info>")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- the reply
def _json_object(raw: str):
    """A tolerant parse: strip a code fence, slice to the outermost {...}."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_done(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    outcome = str(d.get("outcome") or "").strip().lower().replace(" ", "_")
    if outcome not in OUTCOMES:
        return None
    reason = str(d.get("reason") or "").strip()[:200]
    if outcome == "blocked":
        # the shell keys its message on the reason, so an unknown one becomes "needs a person"
        if reason not in BLOCKED_REASONS:
            reason = "needs-person"
    if outcome == "await_email":
        reason = reason.lower()
        if reason not in AWAIT_REASONS:
            reason = "link" if "link" in reason else "code"
    return {"outcome": outcome, "reason": reason}


def parse_reply(raw: str, view_indices: set) -> dict | None:
    """Validate the model's reply against the view it saw. Unknown ops and indices are dropped,
    at most 5 actions survive, a valid ``done`` wins over any action. None when nothing usable."""
    data = _json_object(raw)
    if data is None:
        return None
    view_indices = {int(i) for i in (view_indices or set())}
    done = _clean_done(data.get("done"))
    actions: list[dict] = []
    for a in (data.get("actions") or []) if isinstance(data.get("actions"), list) else []:
        if not isinstance(a, dict):
            continue
        op = str(a.get("op") or "").strip().lower()
        if op not in ALLOWED_OPS:
            continue
        if op in ("click", "type", "select", "upload"):
            try:
                idx = int(a.get("index"))
            except (TypeError, ValueError):
                continue
            if idx not in view_indices:
                continue                         # an index the model made up
            item = {"op": op, "index": idx}
            if op == "type":
                text = str(a.get("text") or "").strip()
                if not text:
                    continue
                item["text"] = text[:300]
            elif op == "select":
                opt = str(a.get("option") or a.get("text") or "").strip()
                if not opt:
                    continue
                item["option"] = opt[:200]
        elif op == "scroll":
            d = str(a.get("direction") or "down").strip().lower()
            item = {"op": "scroll", "direction": d if d in ("up", "down") else "down"}
        else:
            try:
                secs = float(a.get("seconds") or 1)
            except (TypeError, ValueError):
                secs = 1
            item = {"op": "wait", "seconds": max(1, min(5, int(secs)))}
        actions.append(item)
        if len(actions) >= MAX_ACTIONS:
            break
    if done:
        actions = []                             # done goes alone
    if not actions and not done:
        return None
    return {"memory": str(data.get("memory") or "").strip()[:400],
            "evaluation": str(data.get("evaluation") or "").strip()[:300],
            "actions": actions, "done": done}


# --------------------------------------------------------------------------- resolution
def resolve_actions(actions: list, applicant: dict, portal: dict | None = None, labels: dict | None = None,
                    notes: list | None = None) -> list:
    """Join the model's actions to the person's real values, locally. A ``type`` of a placeholder
    becomes the value (``sensitive`` so the shell logs the name, not the value); a ``type`` of free
    text is kept only on a field that is not a question about the person and only when it has no
    long digit run. Anything else is dropped, with a line in ``notes`` saying why."""
    values = dict(applicant or {})
    if portal:
        if portal.get("email"):
            values["portal_email"] = str(portal["email"])
        if portal.get("password"):
            values["portal_password"] = str(portal["password"])
    labels = labels or {}
    out: list[dict] = []
    for a in actions or []:
        if a.get("op") != "type":
            out.append(dict(a))
            continue
        text = str(a.get("text") or "")
        m = PLACEHOLDER_RE.match(text)
        if m:
            key = m.group(1).lower()
            val = str(values.get(key) or "").strip()
            if not val:
                if notes is not None:
                    notes.append(f"no value for {{{{{key}}}}}; left the field alone")
                continue
            out.append({**a, "text": val, "sensitive": True, "placeholder": key})
            continue
        if "{{" in text:                         # a placeholder inside other text: never partial
            if notes is not None:
                notes.append("a placeholder must be typed on its own; dropped")
            continue
        label = str(labels.get(a.get("index")) or labels.get(str(a.get("index"))) or "")
        if is_fact_question(label):
            if notes is not None:
                notes.append(f"free text on a question about the person ({label[:40]}); dropped")
            continue
        if _DIGIT_RUN.search(text):
            if notes is not None:
                notes.append("free text with a long run of digits; dropped")
            continue
        out.append({**a, "text": text, "sensitive": False})
    return out


# --------------------------------------------------------------------------- loops, guards
def loop_nudges(history: list) -> list[str]:
    """The nudge lines for actions repeated LOOP_REPEATS times in the last LOOP_WINDOW actions."""
    recent: list[str] = []
    for h in history or []:
        if isinstance(h, dict):
            recent.extend(_action_key(a) for a in (h.get("actions") or []))
    recent = [r for r in recent if r][-LOOP_WINDOW:]
    counts: dict[str, int] = {}
    for r in recent:
        counts[r] = counts.get(r, 0) + 1
    return [f"You repeated '{k}' {n} times; it is not working. Do something else, or report blocked."
            for k, n in counts.items() if n >= LOOP_REPEATS]


def submit_guard(actions: list, lines: dict) -> bool:
    """True when any click lands on a submit-like line (the model may not press it)."""
    for a in actions or []:
        if a.get("op") != "click":
            continue
        line = lines.get(a.get("index")) or {}
        if SUBMIT_RE.match(_line_name(line)):
            return True
    return False


def _blocked(reason: str, memory: str = "", evaluation: str = "") -> dict:
    return {"ok": True, "memory": memory, "evaluation": evaluation, "actions": [],
            "done": {"outcome": "blocked", "reason": reason}, "notes": []}


def step(req: dict, llm, applicant: dict, portal: dict | None = None, *, placeholders: list | None = None,
         resume_path: str = "") -> dict:
    """One step of the agent: build the prompt, ask the model (one retry on an empty reply),
    validate, guard, resolve. Returns {ok, memory, evaluation, actions, done, notes}."""
    req = req or {}
    lines = view_lines(req.get("view") or "")
    labels = {i: _line_name(ln) for i, ln in lines.items()}
    step_n = int(req.get("step") or 1)
    max_steps = int(req.get("max_steps") or 40)
    values = dict(applicant or {})
    if portal:
        values.update({k: v for k, v in (("portal_email", portal.get("email")),
                                         ("portal_password", portal.get("password"))) if v})
    nudges = loop_nudges(req.get("history") or [])
    if step_n >= max_steps:
        nudges.append("This is the last step. Reply with done only: ready_to_submit if the "
                      "application is complete up to the submit button, else blocked with the reason.")
    user = build_user_message(req, placeholders or [], nudges, values=values)
    reply = None
    for _ in range(2):                           # one retry on an empty or garbled reply
        try:
            raw = llm.agent_step(SYSTEM_PROMPT, user)
        except Exception:                        # noqa: BLE001 - model/broker blip -> retry once
            raw = ""
        reply = parse_reply(raw, set(lines))
        if reply is not None:
            break
    if reply is None:
        return _blocked("needs-person", evaluation="The model gave no usable reply.")
    if step_n >= max_steps and not reply.get("done"):
        return _blocked("needs-person", reply["memory"], reply["evaluation"])
    if submit_guard(reply["actions"], lines):   # the model never presses Submit
        reply["actions"] = []
        reply["done"] = {"outcome": "ready_to_submit", "reason": "the application is at its submit button"}
    notes: list[str] = []
    actions = resolve_actions(reply["actions"], applicant, portal, labels, notes)
    for a in actions:
        if a.get("op") == "upload" and resume_path:
            a["path"] = resume_path
    return {"ok": True, "memory": reply["memory"], "evaluation": reply["evaluation"],
            "actions": actions, "done": reply["done"], "notes": notes}
