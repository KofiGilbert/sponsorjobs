"""The apply agent's brain: one model call per step, decided from the page view alone.

The shell (shell-electron/agent.js) opens the job, fills what the deterministic engine recognizes,
serializes the page (docs/apply-agent.md section 3) and posts it here. We build ONE cached system
prompt and ONE user message that replaces the previous one each step (no growing chat), ask the
model WHAT TO CLICK NEXT, validate the reply against the view it saw, resolve placeholders to the
person's real values locally, and hand concrete actions back.

The agent decides (section 29, 2026-11-18). The brain is given the person's own material
(``<person>``: autoapply/material.py, the facts of the resume and the saved profile, never a
legal or self-identification answer) and may answer a question itself under the answer protocol
(autoapply/protocol.py: source, evidence, class). Code checks each such answer against that
material before it runs; a refusal goes back to the brain as an observation with its reason,
never as a silent strip. A short list of hard limits stays in code whatever the model says: the
final Submit, captcha and identity checks, LinkedIn, consents, legal questions without a saved
answer, credentials, files, payment, the employer's site.

The data boundary (section 5): contact details, credentials and codes are never shown to the
model. The view carries none, the history and view are passed through ``redact`` in case a form
echoed one back, and the model TYPES them as placeholder names ({{email}}, {{portal_password}})
that are resolved here, after the reply.

The model never presses the application's Submit. A click on a submit-like line is refused here
with the limit named (alone, it becomes ``ready_to_submit``), independently of the prompt, and the
shell refuses it again (section 6). Belt and braces.
"""
from __future__ import annotations

import datetime as _dt
import json
import re

from autoapply import protocol
from llm import textguard
from llm.textguard import clean_text

# Under 60 lines, the short "flash" shape: rules first, then the exact output schema. The person
# never reads this, but the model copies what it sees, so no dashes (tests/test_no_dashes.py).
SYSTEM_PROMPT = """You drive a web browser to apply for a job on behalf of a person. Each step you get the page as numbered lines ([12]<button>Next</button>) and you answer with the next few actions. The engine already filled every field it recognized before you see the page; your job is to move the application forward: dismiss popups, click Apply, sign in or create the portal account, pick what the engine could not, and get to the final review page. Work like an engineer: when something does not work, look closer with the read-only tools, then try another way. The shell tries other ways too (a reload, reopening the job); giving up is the last resort.

Rules:
1. Popups and cookie banners first, before anything else: close the popup; on a cookie banner choose Reject all, Decline, Necessary only or Close (never Accept all, never opt in to marketing). If the only way past a banner is Accept, close it if it has a close button; otherwise leave it and continue.
2. On a job page click Apply. Prefer "Apply manually" (on Workday always "Apply Manually" over "Autofill with Resume": the site's resume parse puts wrong jobs and dates in the form), "Apply with Email" or "Autofill with resume". Never LinkedIn (Apply with LinkedIn, Sign in with LinkedIn: always refused), and no other social sign-in (Google, Indeed, Facebook) while an email or manual path exists. A chat-style apply (a chatbot that asks one question at a time; "Open Chatbot", "I'm interested"): open the chat, answer each question in its answer box (a placeholder, or your own answer under rule 6) and send it, one at a time; a question nothing answers is needs-answers.
3. Comboboxes and typeaheads (School, Location, a react-select): type the value on the combobox line itself. The tool opens the list, filters it, picks the best match and says what it picked ("picked ..."), or says nothing matched and lists the options: then select one of those, or the list's "Other". list_options opens a closed list for you. A plain typeahead that only shows suggestions (new lines marked * in the next view): click the * suggestion that matches. Never press Enter.
4. Put at most ONE action that changes the page (a click on Next, Continue, Sign in, Apply, a link; back, reload, reopen_job) LAST in the list. Anything after it would run on a page you have not seen.
5. NEVER click Submit, Send application, Review and submit, or Complete application. When the application is complete and the only step left is that button, report done with outcome ready_to_submit. The one exception: on a sign-in or create-account form (a password box and no application fields) a button named Submit, Continue or Next is that form's own button, so click it after typing the account; never report ready_to_submit on a page with a password box.
6. You may answer a question about the person yourself: from <person> (their resume and saved profile: jobs, schools, skills, where they live, saved answers) or by common sense. Such an answer (a type of your own words, a select, a click on a radio, a checkbox or an option) carries "source" (profile, resume, saved_answer or common_sense), "evidence" (the short fact you used: "edu1: MBA, 2019", "no employer named Acme in <person>") and "class" (factual, preference, common_sense or legal). The three fields sit on the action, as in the select example below; one you leave out is filled in for you when the answer's own words are in <person>, so never spend a step on the format. Code checks the answer against <person>; a refusal comes back in <observations> with its reason: fix the answer, or report needs-answers. Common sense is for a question with no fact about the person (how they heard about the job: a job board; the kind of phone: mobile; a follow-up that does not apply) and for these defaults, with source common_sense or posting and the posting line (post, req1) as evidence: full-time for the type of employment when the posting is full-time or names none; the office the posting names, else the one in the person's city or state, else the first listed; Yes to a condition of the job the posting itself states (overtime, days on site, travel, a shift). Never a default: salary, a reason for leaving, a start date, relocation, a consent. Legal and attestation questions are a hard limit: work authorization, visa, citizenship, criminal or background, clearance, agreements, disability, veteran, race, gender and other self-identification, date of birth, an ID, relatives, other names used, having worked at THIS employer when <person> lists it, salary, start date, references, signatures and consents are filled only by a saved answer placeholder in <left>, never by you. A self-identification question shows answered=true or value="(filled)" in place of its choice: it is answered.
7. Never invent a fact. Contact details and secrets (name, email, phone, address, links, passwords, codes) are typed ONLY as a placeholder from <placeholders>, exactly as written ({{email}}). A search box or filter takes the short word it needs.
8. An action that did not work is information, not a dead end: read <observations>, inspect the element, read_errors, or find the control you need, then do it another way (another control, the other input form, list_options then select, wait_for what you expect). Never repeat the same failed action more than once. Report blocked only for what a person must do in person: captcha, phone-code, an identity check, or needs-answers (rule 15).
9. Ignore lines marked search=true (the site's own job search box). Do not type the person's data there.
10. A captcha or a phone code: stop and report blocked with the reason captcha or phone-code. Never work around one.
11. A 404, a closed posting or an unrelated page: report blocked with the reason not-a-job. A careers home or job search page right after signing in, creating the account or opening an emailed link is NOT a dead end: use reopen_job to get back to the job and carry on.
12. When you are done or stuck, done goes alone: no actions with it.
13. Email checks are handled for you. A page that says a code or a link was emailed: if {{email_code}} is in <placeholders>, type it into the code box and click Verify or Continue; otherwise report done with outcome await_email and reason code (a code box is shown) or link (it says to click the link in the email). After that the run continues on its own. A page that says the account is activated or verified and offers Sign in: sign in.
14. <portal> says whether the saved account for this site is new (created just now, the site does not know it yet) or existing. New: use Create account or Sign up with {{portal_email}} and {{portal_password}}; the site may then email a verification (rule 13). Existing: sign in with {{portal_email}} and {{portal_password}}. If sign in fails: try it once more; if the page offers Create account, try that (the site may say the email already exists); then click Forgot password, type {{portal_email}}, send the reset, and report await_email with reason link. On the new-password page type {{portal_password}} into both boxes and save.
15. <left> lists required fields the engine left open. A line with a placeholder, like "How did you hear about us? (saved answer: type {{answer_1}})", is already answered: type it (for a list or combobox: type it, wait a step, click the * suggestion that says the same, never one that adds words to it such as "College Job Board" for "Job board"; if none says the same the list's options come back in <observations>: choose the closest one yourself under rule 6, or report needs-answers). "The engine suggests" on a line is advice: take it, or do better under rule 6. "Drafted for the person" on a line is a written answer made from their resume and the posting: type its placeholder into the box as it is. A line with no placeholder: answer it under rule 6 when <person> or common sense answers it; when nothing does, do not guess and do not keep clicking: report done with outcome blocked and reason needs-answers. A resume or CV file box: use upload on it. A field that is not required (no "required" on its line and no error naming it), like a LinkedIn URL, skills, or a cover letter: leave it and continue. A line marked "(left for the person: leave it empty)" is already noted for the person, who answers it at the end: leave it empty unless <person> answers it under rule 6, never type a stand-in such as N/A, fill everything else, and carry on (Next, Continue, or ready_to_submit at the Submit page); report needs-answers again only when the page will not move on without it.
16. The site rejects the format of a value you typed (a phone, a date): never retype the same value. inspect the box (its pattern, placeholder, inputmode) and try the other form (digits only for a phone, the format the box names for a date). A box that reads (filled) while the page's error still says it is required: the site did not register the value. Retype it once with its placeholder from <left> (the typing then happens for real), then Save.
17. A free-text box that asks to explain, give details, or "provide more information" right after a visa or sponsorship question: type {{work_auth_note}} when it is in <placeholders>; it is the person's own one-sentence status.
18. Privacy notices and terms are the person's to accept, and that is done for you before you see the page. Never click a link to a privacy policy, privacy notice, terms, or a poster: it opens a document or a dialog, not the next step. Never tick a box that accepts terms or a privacy notice, and never click Accept, I Agree, I Acknowledge or Cancel in a dialog that asks to accept one. If such a box or dialog still blocks the way, report done with outcome blocked and reason needs-consent. A marketing box (job alerts, newsletters, a talent community, texts) is never required: leave it unticked.
19. A page with nothing to do on it (a loading skeleton, "Loading", an empty form, grey boxes): wait_for the form once (up to 15 seconds); if it still shows nothing, reload; if it still shows nothing, reopen_job. Never answer with a plain wait more than twice in a row.
20. Read-only tools, use them freely, they never change the page: read_page (more of a long page, from an element index), find (where a label, a button or a word is), inspect (one element's tag, attributes, state and the error next to it), list_options (a dropdown's choices), read_errors (every error on the page), screenshot (a picture of the page comes with your next step), wait_for (a text or a CSS selector, up to 30 seconds). What they found comes back in <observations> next step.
21. Hard limits, checked in code whatever you answer: the application's final Submit, a captcha or identity check, LinkedIn, consents and terms, a legal question with no saved answer, credentials outside placeholders, files other than this application's resume and cover letter, payment or anything outside this application, leaving the employer's site. A refusal names its limit: never retry it, go another way or report blocked.
22. Keep <plan> up to date: the pages of this application as short lines, each starting with "done:", "now:" or "todo:" (Job page, Sign in, My information, Experience, Questions, Disclosures, Review). Change it when you learn more; it is kept for you.

Reply with JSON only, no prose, exactly this shape:
{"memory": "what you know so far, under 40 words",
 "evaluation": "did the previous actions work, in one line",
 "summary": "what these actions do, about 10 words, starting with an -ing verb (Clicking Next to open page 3); placeholders only, never a value",
 "plan": ["done: Job page", "now: Sign in", "todo: My information", "todo: Review"],
 "actions": [{"op": "click", "index": 12},
             {"op": "type", "index": 3, "text": "{{email}}"},
             {"op": "select", "index": 5, "option": "Exact option label", "source": "resume", "evidence": "edu1: MBA, 2019", "class": "factual"},
             {"op": "upload", "index": 9},
             {"op": "scroll", "direction": "down"},
             {"op": "wait", "seconds": 2},
             {"op": "read_page", "offset": 40},
             {"op": "find", "text": "Next"},
             {"op": "inspect", "index": 12},
             {"op": "list_options", "index": 5},
             {"op": "read_errors"},
             {"op": "screenshot"},
             {"op": "wait_for", "text": "My Information", "selector": "form input", "seconds": 15},
             {"op": "back"}, {"op": "reload"}, {"op": "reopen_job"}],
 "done": null}
or, to finish: "done": {"outcome": "ready_to_submit" | "blocked" | "await_email", "reason": "short reason"}
with actions empty. blocked reasons: captcha, phone-code, needs-person, needs-answers, needs-consent, not-a-job, left-site. await_email reasons: code, link.
At most 5 actions per step. Use only indices shown in the current view."""

ALLOWED_OPS = ("click", "type", "select", "upload", "scroll", "wait",
               "read_page", "find", "inspect", "list_options", "read_errors", "screenshot", "wait_for",
               "back", "reload", "reopen_job")
# Tools that only read the page (section 12): no index needed, nothing changes, an observation
# comes back next step. ``PAGE_OPS`` change the page and go last, like a click on Next.
READ_OPS = ("read_page", "find", "read_errors", "screenshot", "wait_for")
PAGE_OPS = ("back", "reload", "reopen_job")
MAX_PLAN = 12
MAX_OBSERVATION = 7000      # one tool's observation, as the model sees it
MAX_OBSERVATIONS = 12000    # all of a step's observations together
MAX_WAIT_FOR_S = 30
# The reflection call's strategy menu (section 13): the shell runs the one the model picks.
STRATEGIES = ("wait_for form", "reload", "back", "reopen_job", "sign in again", "create account", "reset password",
              "wait for the site's email", "fill the field myself", "read more of the page", "use the other input form")
RETRIES = 3                 # calls on a bad or empty model reply, with backoff
BACKOFF_S = (0.0, 0.5, 1.5)
_SAFE_SELECTOR = re.compile(r"^[A-Za-z0-9_\-\s\[\]=\"'#.:*,>+~()^$|]{1,120}$")
# Anthropic list prices per million tokens, by model family, for the per-application budget.
# Cache reads are cheaper; this is the plain estimate, on the high side on purpose.
PRICES = (("haiku-5", (0.10, 0.50)), ("haiku-4", (1.0, 5.0)), ("sonnet-5", (2.0, 10.0)), ("sonnet-4", (3.0, 15.0)),
          ("opus-4-8", (5.0, 25.0)), ("opus", (15.0, 75.0)))
DEFAULT_PRICE = (3.0, 15.0)
OUTCOMES = ("ready_to_submit", "submitted", "blocked", "await_email")
# (2026-11-17) The mark on a <left> line the run left for the person until its end (ui/app.py).
DEFERRED_MARK = "(left for the person: leave it empty)"

BLOCKED_REASONS = ("captcha", "verify-email", "phone-code", "needs-person", "needs-answers", "needs-consent",
                   "not-a-job", "left-site")
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


# --------------------------------------------------------------------------- self-identification
# A voluntary self-identification control (the planner's _EEO, by its label). Its chosen option
# is the person's gender, race, veteran or disability answer: it never leaves the machine. The
# view (extension/agent_view.js) already prints such a control as answered or not, never which
# choice; this is the same rule on the engine side, for any view that still carries the choice.
_SELF_ID = re.compile(r"gender|\bsex\b|\brac(e|ial)\b|ethnic|hispanic|latin[oax]|veteran|disab"
                      r"|sexual orientation|transgender|lgbt|queer|pronoun|self[- ]?identif|first[- ]generation (professional|college|student)", re.I)
# A cover letter upload box, by its line's words (autoapply/planner.py _LETTER).
_LETTER_BOX = re.compile(r"cover\s*letter|letter of (interest|motivation)|motivation(al)? letter", re.I)
_VIEW_LINE = re.compile(r'^(\*?\[\d+\]<)(select|combobox|radio|checkbox)\b([^\n]*)$', re.M)
_ATTR_LABEL = re.compile(r'label="((?:[^"\\]|\\.)*)"')


def mask_self_id(view: str) -> str:
    """The view with every self-identification control's CHOICE hidden: a chosen select or
    combobox reads value="(filled)", a radio reads checked=hidden with answered=true|false for
    its group, a checkbox checked=hidden. The choices themselves (the site's words) stay, so a
    question can still be read and asked."""
    text = str(view or "")
    answered: dict = {}
    for m in _VIEW_LINE.finditer(text):
        if m.group(2) == "radio":
            lab = _ATTR_LABEL.search(m.group(3))
            if lab and _SELF_ID.search(lab.group(1)):
                key = lab.group(1).lower()
                answered[key] = answered.get(key, False) or "checked=true" in m.group(3) or "answered=true" in m.group(3)

    def fix(m):
        kind, rest = m.group(2), m.group(3)
        lab = _ATTR_LABEL.search(rest)
        if not lab or not _SELF_ID.search(lab.group(1)):
            return m.group(0)
        if kind in ("select", "combobox"):
            rest = re.sub(r'value="(?:[^"\\]|\\.)+"', 'value="(filled)"', rest, count=1)
        elif kind == "radio" and re.search(r"checked=(true|false)", rest):
            on = "true" if answered.get(lab.group(1).lower()) else "false"
            rest = re.sub(r"checked=(true|false)", f"checked=hidden answered={on}", rest, count=1)
        elif kind == "checkbox":
            rest = re.sub(r"checked=(true|false)", "checked=hidden", rest, count=1)
        return m.group(1) + kind + rest
    return _VIEW_LINE.sub(fix, text)


def mask_values(view: str, withheld) -> str:
    """(Phase 4, docs/applicant-brain.md section 9) The view with the person's stored legal and
    self-identification values hidden where a form control holds them: a select or combobox whose
    chosen value is one of them reads value="(filled)", and a radio group or checkbox whose chosen
    option is one of them reads checked=hidden. Only the CHOICE is hidden: labels, the choices
    themselves (the site's words, "Ghana" in a country list) and the page's text stay, so the
    model can still read the page. ``withheld`` comes from applicant/redact.sensitive_values."""
    vals = [str(v) for v in (withheld or []) if str(v or "").strip()]
    text = str(view or "")
    if not vals or not text:
        return text
    rx = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", re.I) for v in vals]

    def hit(s: str) -> bool:
        return bool(s) and s != "(filled)" and any(r.search(s) for r in rx)

    groups: set = set()
    for m in _VIEW_LINE.finditer(text):
        if m.group(2) in ("radio", "checkbox") and "checked=true" in m.group(3):
            opt = re.search(r'option="((?:[^"\\]|\\.)*)"', m.group(3))
            lab = _ATTR_LABEL.search(m.group(3))
            words = opt.group(1) if opt else (lab.group(1).partition(": ")[2] if lab else "")
            if lab and hit(words):
                groups.add(lab.group(1).lower())

    def fix(m):
        kind, rest = m.group(2), m.group(3)
        if kind in ("select", "combobox"):
            v = re.search(r'value="((?:[^"\\]|\\.)*)"', rest)
            if v and hit(v.group(1)):
                rest = rest[:v.start()] + 'value="(filled)"' + rest[v.end():]
        else:
            lab = _ATTR_LABEL.search(rest)
            if lab and lab.group(1).lower() in groups:
                rest = re.sub(r"checked=(true|false)", "checked=hidden answered=true" if kind == "radio"
                              else "checked=hidden", rest, count=1)
        return m.group(1) + kind + rest
    return _VIEW_LINE.sub(fix, text)


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


def clean_plan(plan) -> list[str]:
    """The model's plan lines, kept only in the "done: | now: | todo: <page>" shape."""
    out: list[str] = []
    for p in (plan or []) if isinstance(plan, list) else []:
        t = " ".join(str(p or "").split())[:60]
        m = re.match(r"^(done|now|todo)\s*:\s*(.+)$", t, re.I)
        if not m:
            continue
        out.append(f"{m.group(1).lower()}: {m.group(2).strip()}")
        if len(out) >= MAX_PLAN:
            break
    return out


def observation_lines(observations) -> list[str]:
    """The last step's tool observations, each capped, all capped together."""
    out: list[str] = []
    total = 0
    for o in (observations or []) if isinstance(observations, list) else []:
        t = str(o or "").strip()[:MAX_OBSERVATION]
        if not t:
            continue
        if total + len(t) > MAX_OBSERVATIONS:
            out.append("(more observations cut)")
            break
        out.append(t)
        total += len(t)
    return out


def build_user_message(req: dict, placeholders: list, nudges: list, values: dict | None = None,
                       withheld=()) -> str:
    """The single state message. <step_info> goes LAST so everything before it is a stable
    prefix the model's cache can reuse. The view and history are redacted when values are given."""
    req = req or {}
    job = req.get("job") if isinstance(req.get("job"), dict) else {}
    role = str(job.get("role") or req.get("role") or "").strip()
    company = str(job.get("company") or req.get("company") or "").strip()
    lane = str(req.get("lane") or "assisted").strip()
    view = str(req.get("view") or "").strip()
    hist = _history_lines(req.get("history") or [])
    view = mask_values(mask_self_id(view), withheld)
    # The page is third-party text. agent_view.js drops what a person cannot SEE;
    # this drops what a person cannot see INSIDE text that is visible to them
    # (llm/textguard.py). It runs before redact so the prompt only ever carries
    # cleaned text, whatever path the caller took to get here.
    view = clean_text(view)
    hist = [clean_text(h) for h in hist]
    if values:
        view = redact(view, values)
        hist = [redact(h, values) for h in hist]
    left = [str(x) for x in (req.get("left") or []) if str(x).strip()]
    ph = ["{{" + str(p).strip("{} ") + "}}" for p in (placeholders or []) if str(p).strip("{} ")]
    step = int(req.get("step") or 1)
    max_steps = int(req.get("max_steps") or 40)
    today = str(req.get("today") or _dt.date.today().isoformat())
    plan = clean_plan(req.get("plan"))
    obs = [clean_text(o) for o in observation_lines(req.get("observations"))]
    if values:
        obs = [redact(o, values) for o in obs]
    portal = req.get("portal") if isinstance(req.get("portal"), dict) else {}
    if portal.get("account"):
        portal_s = "saved account for this site: " + ("new, created just now: the site does not know it yet, create it"
                                                     if portal.get("new") else "existing: sign in with it")
    else:
        portal_s = "no saved account for this site"
    parts = [
        f"<job>\nrole: {role or '(unknown)'}\ncompany: {company or '(unknown)'}\nlane: {lane}\n</job>",
        "<mode>\n" + (AUTO_MODE if req.get("auto") else "the person is here: a required question nothing answers is needs-answers") + "\n</mode>",
        "<portal>\n" + portal_s + "\n</portal>",
        "<plan>\n" + ("\n".join(plan) if plan else "(none yet: write one)") + "\n</plan>",
        "<history>\n" + ("\n".join(hist) if hist else "(first step)") + "\n</history>",
        "<placeholders>\n" + (", ".join(ph) if ph else "(none)") + "\n</placeholders>",
        "<left>\n" + ("\n".join(left) if left else "(nothing)") + "\n</left>",
        "<view>\n" + (view or "(empty page)") + "\n</view>",
    ]
    if obs:
        parts.append("<observations>\n" + "\n".join(obs) + "\n</observations>")
    if nudges:
        parts.append("<nudges>\n" + "\n".join(str(n) for n in nudges) + "\n</nudges>")
    if req.get("reflect"):
        parts.append(reflection_block(req.get("reflect")))
    parts.append(f"<step_info>\nstep {step} of {max_steps}. Today is {today}.\n</step_info>")
    return "\n\n".join(parts)


def reflection_block(r) -> str:
    """(Section 13) The deeper look the shell asks for after a nudge did not help: what went
    wrong and what was tried, with the strategy menu; a screenshot rides along when taken."""
    r = r if isinstance(r, dict) else {}
    problem = " ".join(str(r.get("problem") or "the last steps made no progress").split())[:400]
    tried = [str(t)[:80] for t in (r.get("tried") or []) if str(t).strip()]
    return ("<reflection>\nYou are stuck: " + problem + "\nWhat was tried: " + (", ".join(tried) if tried else "nothing yet")
            + "\nStep back. List 3 to 5 likely causes, rank them, pick the likeliest, and choose ONE strategy from this menu: "
            + ", ".join(STRATEGIES) + ".\nAdd to your JSON reply: \"causes\": [\"...\"], \"likeliest\": \"...\", \"strategy\": \"<one of the menu>\". "
            + "You may still give actions (read-only tools first) or done.\n</reflection>")


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
    dropped: list[str] = []                      # what did not parse, told back to the model
    for a in (data.get("actions") or []) if isinstance(data.get("actions"), list) else []:
        if not isinstance(a, dict):
            continue
        op = str(a.get("op") or "").strip().lower()
        if op not in ALLOWED_OPS:
            dropped.append(f"unknown op '{op[:20]}'")
            continue
        if op in ("click", "type", "select", "upload", "inspect", "list_options"):
            try:
                idx = int(a.get("index"))
            except (TypeError, ValueError):
                continue
            if idx not in view_indices:
                dropped.append(f"{op} on [{idx}]: no such index in this view")
                continue                         # an index the model made up
            item = {"op": op, "index": idx}
            if op in ("click", "type", "select"):
                # the answer protocol (autoapply/protocol.py): kept as written, checked in step()
                # (on the action or in a "proof" object: protocol.meta reads either)
                item.update({k: v for k, v in protocol.meta(a).items() if v})
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
        elif op == "wait":
            try:
                secs = float(a.get("seconds") or 1)
            except (TypeError, ValueError):
                secs = 1
            item = {"op": "wait", "seconds": max(1, min(5, int(secs)))}
        elif op == "read_page":
            try:
                off = int(a.get("offset") or 0)
            except (TypeError, ValueError):
                off = 0
            item = {"op": "read_page", "offset": max(0, off)}
        elif op == "find":
            text = " ".join(str(a.get("text") or "").split())[:80]
            if not text:
                continue
            item = {"op": "find", "text": text}
        elif op == "wait_for":
            text = " ".join(str(a.get("text") or "").split())[:80]
            sel = str(a.get("selector") or "").strip()
            if sel and not _SAFE_SELECTOR.match(sel):
                sel = ""
            if not text and not sel:
                continue
            try:
                secs = float(a.get("seconds") or 10)
            except (TypeError, ValueError):
                secs = 10
            item = {"op": "wait_for", "seconds": max(1, min(MAX_WAIT_FOR_S, int(secs)))}
            if text:
                item["text"] = text
            if sel:
                item["selector"] = sel
        else:
            item = {"op": op}                    # read_errors, screenshot, back, reload, reopen_job
        actions.append(item)
        if len(actions) >= MAX_ACTIONS:
            break
    if done:
        actions = []                             # done goes alone
    # A page-changing tool goes last, like a click on Next (rule 4); only one of them.
    page_at = [i for i, a in enumerate(actions) if a["op"] in PAGE_OPS]
    if page_at:
        actions = actions[:page_at[0] + 1]
    strategy = str(data.get("strategy") or "").strip().lower()
    out = {"memory": str(data.get("memory") or "").strip()[:400],
           "evaluation": str(data.get("evaluation") or "").strip()[:300],
           "summary": str(data.get("summary") or "").strip()[:200],
           "plan": clean_plan(data.get("plan")),
           "actions": actions, "done": done, "dropped": dropped[:MAX_ACTIONS]}
    if strategy or data.get("causes"):
        out["causes"] = [str(c)[:160] for c in (data.get("causes") or []) if str(c).strip()][:5] if isinstance(data.get("causes"), list) else []
        out["likeliest"] = str(data.get("likeliest") or "").strip()[:160]
        out["strategy"] = strategy if strategy in STRATEGIES else ""
    if not actions and not done and not out.get("strategy"):
        return None
    return out


# --------------------------------------------------------------------------- the live line
# The reply's ``summary`` is what the person reads on the agent bar while it works ("Clicking
# Next to open page 3", OpenHands V1's per-action summary). It goes to the screen, so it is held
# to the same boundary as everything else: no personal value, no self-identification choice.
_PH_ANY = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", re.I)
_PH_WORDS = {"portal_email": "your sign-in email", "portal_password": "your password",
             "email_code": "the emailed code", "work_auth_note": "your work status note",
             "current_company": "your current company", "current_title": "your current title"}
_EMAIL_RE = re.compile(r"\S+@\S+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
_SUMMARY_MAX = 90


def _ph_words(key: str) -> str:
    key = key.lower()
    if key in _PH_WORDS:
        return _PH_WORDS[key]
    if key.startswith("answer_"):
        return "your answer"
    return "your " + key.replace("_", " ")


def _short(text: str, limit: int = _SUMMARY_MAX) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


def derive_summary(actions: list, lines: dict, done: dict | None = None) -> str:
    """A plain summary built from the actions alone, when the model gave none (or one that
    could not be shown). Names the page's own button or field words, never a typed value and
    never a chosen option."""
    if done:
        oc = done.get("outcome")
        return {"ready_to_submit": "Stopping at the submit button for you",
                "await_email": "Waiting for the site's email",
                "submitted": "Checking the site's confirmation"}.get(oc, "Stopping here, this needs you")
    acts = list(actions or [])
    if not acts:
        return ""
    lead = next((a for a in reversed(acts) if a.get("op") == "click"), acts[0])
    name = _short(_line_name(lines.get(lead.get("index")) or {}), 40)
    op = lead.get("op")
    if op == "click":
        s = f"Clicking {name}" if name else "Clicking on the page"
    elif op == "type":
        m = PLACEHOLDER_RE.match(str(lead.get("text") or ""))
        key = str(lead.get("placeholder") or "") or (m.group(1) if m else "")
        s = f"Typing {_ph_words(key)}" if key else (f"Typing in {name}" if name else "Typing in a box")
    elif op == "select":
        s = f"Choosing an option for {name}" if name else "Choosing an option"
    elif op == "upload":
        s = "Uploading your resume"
    elif op == "scroll":
        s = "Scrolling to see more of the page"
    elif op in ("read_page", "find", "inspect", "list_options", "read_errors", "screenshot"):
        s = "Looking closer at the page"
    elif op == "wait_for":
        s = "Waiting for the page to show the form"
    elif op == "back":
        s = "Going back a page"
    elif op == "reload":
        s = "Reloading the page"
    elif op == "reopen_job":
        s = "Opening the job again"
    else:
        s = "Waiting for the page"
    if len(acts) > 1 and op != "click":
        s += f" and {len(acts) - 1} more"
    elif len(acts) > 1:
        s = f"Filling {len(acts) - 1} {'box' if len(acts) == 2 else 'boxes'}, then " + s[0].lower() + s[1:]
    return s


def clean_summary(summary: str, actions: list, lines: dict, done: dict | None = None, values: dict | None = None,
                  withheld=()) -> str:
    """The summary the bar may show. The model's own words when they pass: placeholders read as
    words ("your email"), any personal value (even one the model was never sent) or long run of
    digits drops it, and so does anything about self-identification or a chosen option. Else
    the summary built from the actions (``derive_summary``). Never a dash, about 10 words."""
    s = " ".join(str(summary or "").split())
    if s:
        s = _PH_ANY.sub(lambda m: _ph_words(m.group(1)), s)
        s = s.replace(chr(0x2014), ", ").replace(chr(0x2013), "-")
        bad = bool(_EMAIL_RE.search(s) or _URL_RE.search(s) or _DIGIT_RUN.search(s) or _SELF_ID.search(s))
        low = s.lower()
        for v in list((values or {}).values()) + [str(w) for w in (withheld or [])]:
            v = str(v or "").strip()
            if len(v) >= 3 and re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])", low):
                bad = True
                break
        for a in actions or []:
            opt = str(a.get("option") or "").strip().lower() if a.get("op") == "select" else ""
            if len(opt) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(opt) + r"(?![a-z0-9])", low):
                bad = True                       # the option chosen is the person's answer
            if a.get("op") == "type" and (a.get("sensitive") is False or a.get("proof")):
                t = str(a.get("text") or "").strip().lower()
                if len(t) >= 3 and t in low:
                    bad = True
        if bad:
            s = ""
    if not s:
        s = derive_summary(actions, lines, done)
    return _short(s[:1].upper() + s[1:]) if s else ""


# --------------------------------------------------------------------------- resolution
# (2026-11-26, Uline on Workday) A box's own kind decides the form of a contact value typed into
# it: a phone box takes the phone as plain digits (a saved answer "+1 (630) ..." next to the
# page's own country code control was "Enter a valid format" six steps in a row), and an email
# box takes the application email, whatever placeholder the model typed there (a saved answer
# holding the resume's email). Someone else's email (a referrer's, a reference's) is left as is.
_PHONE_BOX = re.compile(r"\b(phone|mobile|telephone|cell)\b(?!.*\b(ext|extension|type|device|code)\b)", re.I)
_EMAIL_BOX = re.compile(r"\be-?mail\b", re.I)
_EMAIL_OTHER = re.compile(r"referr|reference|recruiter|manager|supervisor|friend|emergency|employer|hiring|alternate|"
                          r"alternative|secondary|backup|colleague|contact person|parent|guardian|spouse|partner", re.I)
_CONTACT_KEYS_KEEP = {"portal_email", "portal_password", "email_code"}


def contact_form(key: str, value: str, label: str, applicant: dict) -> str:
    """The value as the box it goes into takes it (see above). ``key``: the placeholder."""
    label = str(label or "")
    if key in _CONTACT_KEYS_KEEP or not label:
        return value
    if _PHONE_BOX.search(label) and not _EMAIL_BOX.search(label):
        from autoapply.fields import phone_digits
        digits = phone_digits(value)
        return digits if len(digits) >= 7 else value
    if _EMAIL_BOX.search(label) and not _EMAIL_OTHER.search(label) and "@" in value:
        own = str((applicant or {}).get("email") or "").strip()
        return own or value
    return value


def resolve_actions(actions: list, applicant: dict, portal: dict | None = None, labels: dict | None = None,
                    notes: list | None = None) -> list:
    """Join the model's actions to the person's real values, locally. A ``type`` (or a ``select``)
    of a placeholder becomes the value (``sensitive`` so the shell logs the name, not the value;
    a select is then the saved answer's strict pick). Text of the brain's own goes on as it is:
    whether it may answer the field is the answer protocol's call (``step``), which says why when
    it refuses. Only what cannot be resolved is dropped here, with a line in ``notes``."""
    values = dict(applicant or {})
    if portal:
        if portal.get("email"):
            values["portal_email"] = str(portal["email"])
        if portal.get("password"):
            values["portal_password"] = str(portal["password"])
    out: list[dict] = []
    for a in actions or []:
        op = a.get("op")
        if op not in ("type", "select"):
            out.append(dict(a))
            continue
        field = "text" if op == "type" else "option"
        text = str(a.get(field) or "")
        m = PLACEHOLDER_RE.match(text)
        if m:
            key = m.group(1).lower()
            val = str(values.get(key) or "").strip()
            if not val:
                if notes is not None:
                    notes.append(f"no value for {{{{{key}}}}}; left the field alone")
                continue
            if op == "type":
                val = contact_form(key, val, (labels or {}).get(a.get("index")) or "", values)
            item = {**a, field: val, "sensitive": True, "placeholder": key}
            if op == "select":
                item["answer"] = True                # the saved answer's own strict pick (agent_view.js)
            out.append(item)
            continue
        if "{{" in text:                         # a placeholder inside other text: never partial
            if notes is not None:
                notes.append("a placeholder must be typed on its own; dropped")
            continue
        out.append({**a, field: text, "sensitive": False} if op == "type" else dict(a))
    return out


# --------------------------------------------------------------------------- the answer protocol
_ANSWER_TAGS = ("input", "textarea", "combobox")
_NOT_A_QUESTION = re.compile(r"\btype=(password|file|hidden|submit|button|search)\b|search=true")
_OPTION_ATTR = re.compile(r'option="((?:[^"\\]|\\.)*)"')
_FIRST_ATTR = re.compile(r'first="((?:[^"\\]|\\.)*)"')


def answer_target(action: dict, lines: dict) -> dict | None:
    """{label, options, value} when this action ANSWERS a form question with the brain's own
    choice (a type of text that is not a placeholder, a select, a click on a radio, a checkbox or
    an open list's option); None for anything else (a button, a link, a placeholder, the site's
    search box, a password box)."""
    op, idx = action.get("op"), action.get("index")
    line = lines.get(idx) or {}
    tag, attrs = line.get("tag") or "", line.get("attrs") or ""
    label = (line.get("label") or "").strip()
    if op in ("type", "select"):
        if action.get("placeholder") or _NOT_A_QUESTION.search(attrs) or not label:
            return None
        if op == "type" and tag not in _ANSWER_TAGS:
            return None
        if op == "select" and tag not in ("select", "combobox", "input"):
            return None
        first = _FIRST_ATTR.search(attrs)
        opts = [o for o in (first.group(1).split("|") if first else []) if o.strip()]
        return {"label": label, "options": opts, "value": str(action.get("text") if op == "type" else action.get("option") or "")}
    if op != "click":
        return None
    if tag == "radio" and label:
        opt = _OPTION_ATTR.search(attrs)
        group = [(_OPTION_ATTR.search(ln.get("attrs") or "") or [None, ""])[1] for ln in lines.values()
                 if ln.get("tag") == "radio" and (ln.get("label") or "") == label]
        return {"label": label, "options": [o for o in group if o], "value": opt.group(1) if opt else ""}
    if tag == "checkbox" and label:
        q, sep, opt = label.partition(": ")
        return {"label": label, "options": [], "value": opt if sep and opt.strip() else "Yes"}
    if tag == "option":
        for i in sorted((k for k in lines if k < idx), reverse=True):
            ln = lines[i]
            if ln.get("tag") == "combobox" and "expanded=true" in (ln.get("attrs") or ""):
                if _NOT_A_QUESTION.search(ln.get("attrs") or "") or not (ln.get("label") or "").strip():
                    return None
                return {"label": ln["label"].strip(), "options": [], "value": line.get("text") or ""}
    return None


def _typed_saved(history) -> bool:
    """Did the last step type a saved value (a placeholder, logged as "(secret)")?"""
    last = next((h for h in reversed(history or []) if isinstance(h, dict)), None)
    return bool(last) and any(re.match(r"^type \d+ \(secret\)", _action_key(a)) for a in (last.get("actions") or []))


def _guess_reason(label: str, reasons) -> str:
    """The engine's reason for a best guess on this field (``req["guess_reasons"]``), by its label."""
    if not reasons:
        return ""
    k = " ".join(str(label or "").split()).lower()
    return next((str(v) for q, v in reasons.items() if " ".join(str(q).split()).lower()[:75] == k[:75]), "")


def check_answers(actions: list, lines: dict, material, observations: list, rejected: list, history=None,
                  auto: bool = False, reasons=None, view: str = "") -> list:
    """The answer protocol over one step's actions (autoapply/protocol.py). An answer that passes
    keeps ``proof`` ({source, origin, evidence, class}) for the Review answers list; one that
    does not is taken out and comes back to the brain in ``observations`` with the reason.
    ``rejected`` gets {index, label, hard} for the shell, which counts them toward asking the
    person or setting the job aside. With no material at all (no profile on this machine) the
    protocol still runs: a legal question stays a hard limit, and nothing can be cited."""
    from autoapply import qclass
    out = []
    for a in actions or []:
        t = answer_target(a, lines)
        if t is None:
            out.append(a)
            continue
        if a.get("op") == "click" and lines.get(a.get("index"), {}).get("tag") == "option" and _typed_saved(history) \
                and not protocol.meta(a)["source"]:
            # A suggestion clicked right after a saved value was typed (its placeholder): the typed
            # value was the answer, and the brain cannot read it.
            out.append(a)
            continue
        if a.get("op") == "type" and protocol.signature_date(t["label"], t["value"], view):
            # (2026-11-19) today's date on a self-identification or signature form: not a fact
            # about the person, the day the form is signed
            sv = protocol.Verdict(True, klass="common_sense", source="common_sense", evidence=protocol.SIGN_DATE_EVIDENCE)
            out.append({**{k: x for k, x in a.items() if k not in ("source", "evidence", "class", "proof", "answer")},
                        "proof": protocol.record(a, sv)})
            continue
        v = protocol.check({**a, "value": t["value"]}, t["label"], t["options"], material, auto=auto,
                           reason=_guess_reason(t["label"], reasons))
        if v.ok:
            out.append({**{k: x for k, x in a.items() if k not in ("source", "evidence", "class", "proof", "answer")},
                        "proof": protocol.record(a, v)})
            continue
        observations.append(f"Your {a.get('op')} on [{a.get('index')}] ({t['label'][:60]}) was not done: {v.reason}")
        rejected.append({"index": a.get("index"), "label": t["label"], "hard": v.hard})
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


# The button of a sign-in or create-account form, whatever the site calls it (a Workday tenant
# labels its sign-in button "Submit", 2026-11-18).
_AUTH_BUTTON = re.compile(r"^(submit|continue|next|sign ?in|log ?in|create (an |my |your )?account|sign ?up|register)$", re.I)
_AUTH_EMAIL = re.compile(r"e-?mail|user ?name|user ?id|login", re.I)
_APP_FIELD_TAGS = {"textarea", "select", "combobox", "radio", "file"}


def auth_form(view: str, lines: dict | None = None) -> dict | None:
    """{email, passwords, button} (view indices) when this view is a sign-in or create-account
    form and nothing else: a password box, no application field (no file box, text area,
    dropdown or radio; at most five other boxes) and, after the last password box, a button
    that is the form's own. None otherwise, so the application's Submit stays guarded."""
    lines = lines if lines is not None else view_lines(view)
    passwords, others = [], []
    for i in sorted(lines):
        ln = lines[i]
        tag, attrs = ln.get("tag") or "", ln.get("attrs") or ""
        if tag in _APP_FIELD_TAGS or (tag == "input" and re.search(r"\btype=file\b", attrs)):
            return None
        if tag != "input" or "search=true" in attrs or re.search(r"\btype=(hidden|checkbox|submit|button)\b", attrs):
            continue
        (passwords if re.search(r"\btype=password\b", attrs) else others).append(i)
    if not passwords or len(others) > 5:
        return None
    button = next((i for i in sorted(lines) if i > passwords[-1] and lines[i].get("tag") == "button"
                   and _AUTH_BUTTON.match(_line_name(lines[i]))), None)
    if button is None:
        return None
    before = [i for i in others if i < passwords[0]]
    email = next((i for i in before if _AUTH_EMAIL.search(lines[i].get("label") or "")), before[-1] if before else None)
    return {"email": email, "passwords": passwords, "button": button}


def shell_signin(view: str, applicant: dict, portal: dict | None) -> dict:
    """The sign-in the shell does itself, with no model call: the saved account typed into the
    form's own boxes through the same placeholders the model uses, then the form's button."""
    form = auth_form(view)
    if not form:
        return {"ok": False, "reason": "not-a-sign-in-form", "cost": 0.0}
    if not (portal and portal.get("email") and portal.get("password")):
        return {"ok": False, "reason": "no-account", "cost": 0.0}
    acts = ([{"op": "type", "index": form["email"], "text": "{{portal_email}}"}] if form["email"] is not None else [])
    acts += [{"op": "type", "index": i, "text": "{{portal_password}}"} for i in form["passwords"][:2]]
    acts.append({"op": "click", "index": form["button"]})
    notes: list[str] = []
    actions = resolve_actions(acts, applicant, portal, {}, notes)
    return {"ok": True, "memory": "", "evaluation": "(shell) signing in with the saved account", "summary": "Signing in",
            "plan": [], "actions": actions, "done": None, "notes": notes, "cost": 0.0, "tries": 0}


def submit_guard(actions: list, lines: dict, view: str = "") -> bool:
    """True when any click lands on a submit-like line (the model may not press it). The button
    of a sign-in or create-account form is not one, whatever it is called (``auth_form``)."""
    form = auth_form(view, lines) if view else None
    for a in actions or []:
        if a.get("op") != "click":
            continue
        if form and a.get("index") == form["button"]:
            continue
        line = lines.get(a.get("index")) or {}
        if SUBMIT_RE.match(_line_name(line)):
            return True
    return False


def _blocked(reason: str, memory: str = "", evaluation: str = "") -> dict:
    return {"ok": True, "memory": memory, "evaluation": evaluation, "actions": [], "summary": "", "plan": [],
            "done": {"outcome": "blocked", "reason": reason}, "notes": []}


def price_of(model: str) -> tuple[float, float]:
    """(input, output) dollars per million tokens for a model name, the high plain estimate."""
    m = str(model or "").lower()
    for key, price in PRICES:
        if key in m:
            return price
    return DEFAULT_PRICE


def estimate_cost(usage: dict | None, prompt_chars: int, reply_chars: int, model: str = "") -> float:
    """Dollars for one call: from the real token counts when the client kept them
    (``llm.last_usage``), else about four characters a token. Never 0 for a real call."""
    pi, po = price_of(model)
    if isinstance(usage, dict) and (usage.get("input") or usage.get("output")):
        i, o = float(usage.get("input") or 0), float(usage.get("output") or 0)
        # a cached prefix: read at a tenth of the price, written at a quarter more
        i += 0.25 * float(usage.get("cache_write") or 0) - 0.9 * float(usage.get("cache_read") or 0)
    else:
        i, o = max(1, int(prompt_chars)) / 4.0, max(1, int(reply_chars)) / 4.0
    return round((i * pi + o * po) / 1_000_000, 5)


# (Section 14) The deterministic ready gate, the same rule as the shell's notReady(): a page
# with a password box, a sign-in or create-account form, or no Submit control is never the
# review page, whatever the model said (Gartner, 2026-11-02: "ready" on the sign-in page).
_SIGNIN_BTN = re.compile(r"^(sign ?in|log ?in|create (an )?account|sign ?up|register)$", re.I)
_SUBMIT_ANY = re.compile(r"\bsubmit\b|\bsend application\b|\breview and submit\b|\bcomplete application\b", re.I)


def not_ready(view: str, lines: dict | None = None) -> str:
    """Why this view cannot be handed over at Submit, or "" when it can."""
    text = str(view or "")
    if re.search(r"^\*?\[\d+\]<input type=password\b", text, re.M):
        return "the page has a password box"
    lines = lines if lines is not None else view_lines(text)
    names = [_line_name(ln) for ln in lines.values()]
    has_submit = any(_SUBMIT_ANY.search(n) for n in names if n)
    has_email = bool(re.search(r"^\*?\[\d+\]<input type=email\b", text, re.M))
    if not has_submit and has_email and any(_SIGNIN_BTN.match(n) for n in names if n):
        return "the page is a sign-in or create-account form"
    if not has_submit:
        return "the page shows no Submit control"
    return ""


# What a failed model call means (section 13): an account problem stops the job and the batch
# plainly ("fatal"); a passing one is retried with backoff; anything else is a bad reply.
# ``ai_company`` / ``ai_transient`` are the broker's own tags (llm/broker_client.py, PR #166):
# OUR key failed (stop plainly, nothing to buy) / the model is busy (retried there and here).
FATAL_REASONS = ("no_key", "no_credit", "upgrade_required", "access_off", "ai_company")
TRANSIENT_REASONS = ("rate_limited", "busy", "offline", "service_down", "ai_transient")


def classify_error(exc) -> str:
    """A reason code for a model call's exception, from its own tag or its words."""
    reason = str(getattr(exc, "reason", "") or "")
    if reason:
        return reason
    low = str(exc or "").lower()
    status = getattr(exc, "status_code", None)
    if status == 401 or "authentication" in low or "invalid x-api-key" in low or "api key" in low:
        return "no_key"
    if status in (402, 403) or "credit" in low or "billing" in low or "quota" in low or "turned off" in low or "usage limit" in low:
        return "no_credit"
    if status == 429 or "rate limit" in low or "rate_limit" in low:
        return "rate_limited"
    if status in (500, 502, 503, 529) or "overloaded" in low:
        return "busy"
    if "timed out" in low or "timeout" in low or "connection" in low or "network" in low or "getaddrinfo" in low:
        return "offline"
    return ""


def unavailable_message(reason: str) -> str:
    if reason == "ai_company":
        try:
            from llm.broker_client import AI_COMPANY_TEXT
            return AI_COMPANY_TEXT
        except Exception:                        # noqa: BLE001
            return "SponsorJobs' AI is temporarily unavailable. Nothing was changed; try again in a little while."
    if reason in ("no_credit", "upgrade_required"):
        return "SponsorJobs' AI is unavailable right now (credits). Nothing was changed; try again later."
    if reason == "no_key":
        return "SponsorJobs' AI is not set up (no working key). Nothing was changed; check Settings, then try again."
    return "SponsorJobs' AI is unavailable right now. Nothing was changed; try again later."


_LINKEDIN = re.compile(r"linked ?in", re.I)


def system_blocks(material=None) -> list[str]:
    """The system prompt as its stable parts, each one a cache breakpoint: the rules (the same
    for everyone) and, on a step with open questions, the person's material (the same text on
    every step of one application)."""
    block = getattr(material, "block", "") or ""
    return [SYSTEM_PROMPT] + ([PERSON_LEAD + "\n" + block] if block else [])


# (2026-11-19, the owner: "for auto apply, we can't stop to ask the user anything") What the brain
# is told on every step of an Auto run. It rides in the state message, so the cached system
# prompt is the same text in every mode.
AUTO_MODE = ("AUTO: nobody is here to ask, so never report needs-answers for a question. Every question gets an answer and "
             "you carry on to the Submit page. In this order: its saved-answer placeholder; <person> or the posting under "
             "rule 6; a common-sense default; and when none of those exists your BEST GUESS, with \"source\": \"best_guess\" "
             "and your reason as evidence: first a neutral choice the form offers (Prefer not to say, Decline to "
             "self-identify, I do not wish to answer, N/A where the question invites it), else the choice true for the "
             "great majority of applicants that commits the person to the least (No to relatives here, to a non-compete, "
             "to a conviction), else the first choice that commits to nothing. A self-identification question takes its "
             "decline choice. A legal question with no placeholder is answered this way too in AUTO. Never guessed, "
             "whatever the mode: an ID number, a date of birth, bank or payment details, a password, a typed signature, "
             "and consents: leave those.")

PERSON_LEAD = ("What you know about this person, for rule 6. These are facts from their resume and saved profile; "
               "cite one by its id as evidence. What is not here, you do not know.")


def _call(llm, user: str, image: str = "", material=None) -> str:
    """One model call: with a picture when the shell took one and the client can take it. The
    system prompt goes as cached blocks when the client marks them (``agent_step_blocks``)."""
    blocks = system_blocks(material)
    system = "\n\n".join(blocks)
    if image and hasattr(llm, "agent_step_image"):
        try:
            return llm.agent_step_image(system, user, image)
        except Exception:                        # noqa: BLE001 - the picture is a bonus, never a blocker
            pass
    if hasattr(llm, "agent_step_blocks"):
        return llm.agent_step_blocks(blocks, user)
    return llm.agent_step(system, user)


def step(req: dict, llm, applicant: dict, portal: dict | None = None, *, placeholders: list | None = None,
         resume_path: str = "", letter_path: str = "", withheld=(), sleep=None, material=None) -> dict:
    """One step of the agent: build the prompt, ask the model (up to RETRIES calls on an empty or
    garbled reply, with backoff and a nudge), validate, guard, resolve. Returns {ok, memory,
    evaluation, plan, actions, done, notes, cost, tries}; {ok: False, reason: "no-reply"} when
    the model gave nothing usable RETRIES times (the shell escalates, it never parks on that).

    ``material`` (autoapply/material.Material): what the agent knows about the person, sent in
    the cached part of the prompt, and what the answer protocol checks the brain's own answers
    against. ``observations`` in the reply: every refusal with its reason and the hard limit it
    names, for the brain's next step; ``rejected``: the answers the protocol refused."""
    import time as _time
    req = req or {}
    sleep = sleep or _time.sleep
    lines = view_lines(req.get("view") or "")
    labels = {i: _line_name(ln) for i, ln in lines.items()}
    step_n = int(req.get("step") or 1)
    max_steps = int(req.get("max_steps") or 40)
    values = dict(applicant or {})
    if portal:
        values.update({k: v for k, v in (("portal_email", portal.get("email")),
                                         ("portal_password", portal.get("password"))) if v})
    nudges = loop_nudges(req.get("history") or []) + [str(n) for n in (req.get("nudges") or []) if str(n).strip()]
    if step_n >= max_steps or req.get("last_step"):
        nudges.append("This is the last step. Reply with done only: ready_to_submit if the "
                      "application is complete up to the submit button, else blocked with the reason.")
    image = str(req.get("image") or "")
    reply = None
    cost = 0.0
    tries = 0
    errors: list[str] = []
    fatal = ""
    for n in range(RETRIES):
        if n:
            try:
                sleep(BACKOFF_S[min(n, len(BACKOFF_S) - 1)])
            except Exception:                    # noqa: BLE001 - a test's sleep may refuse
                pass
        extra = nudges + (["Your last reply was not valid JSON in the required shape (or named an index not "
                           "in the view). Reply with JSON only, exactly the shape given, indices from this view."] if n else [])
        user = build_user_message(req, placeholders or [], extra, values=values, withheld=withheld)
        tries += 1
        raw = ""
        try:
            raw = _call(llm, user, image if n == 0 else "", material)
        except Exception as exc:                 # noqa: BLE001 - the model or the broker failed
            kind = classify_error(exc)
            errors.append((kind + ": " if kind else "") + " ".join(str(exc).split())[:200])
            if kind in FATAL_REASONS:            # out of credits, no key: no retry can fix that
                fatal = kind
                break
            continue
        cost += estimate_cost(getattr(llm, "last_usage", None), sum(len(b) for b in system_blocks(material)) + len(user), len(raw or ""),
                              getattr(llm, "model", ""))
        reply = parse_reply(raw, set(lines))
        if reply is not None:
            break
        errors.append("bad reply: " + " ".join(str(raw or "").split())[:120] if raw else "empty reply")
    if reply is None:
        out = {"ok": False, "reason": "no-reply", "tries": tries, "cost": cost, "errors": errors,
               "error": errors[-1] if errors else "no reply",
               "evaluation": "The model gave no usable reply: " + (errors[-1] if errors else "nothing came back")}
        if fatal:
            out.update({"fatal": fatal, "message": unavailable_message(fatal)})
        return out
    if reply.get("done") and reply["done"].get("outcome") == "ready_to_submit":
        why = not_ready(req.get("view") or "", lines)
        if why:                                  # the gate, before anything else
            return {"ok": True, "memory": reply["memory"], "evaluation": reply["evaluation"], "summary": "",
                    "plan": reply.get("plan") or [], "actions": [], "done": None, "notes": ["ready_to_submit refused: " + why],
                    "refused": why, "cost": cost, "tries": tries}
    if (step_n >= max_steps or req.get("last_step")) and not reply.get("done"):
        out = _blocked("needs-person", reply["memory"], reply["evaluation"])
        out.update({"cost": cost, "tries": tries, "plan": reply.get("plan") or []})
        return out
    notes: list[str] = []
    observations: list[str] = [f"Dropped from your reply: {d}." for d in reply.get("dropped") or []]
    rejected: list[dict] = []
    # Hard limit 1: the model never presses the application's Submit. Alone, the click means the
    # application is ready; with other actions, those run and the click is refused with the reason.
    form = auth_form(req.get("view") or "", lines)
    is_submit = lambda a: (a.get("op") == "click" and not (form and a.get("index") == form["button"])   # noqa: E731
                           and bool(SUBMIT_RE.match(_line_name(lines.get(a.get("index")) or {}))))
    if submit_guard(reply["actions"], lines, req.get("view") or ""):
        rest = [a for a in reply["actions"] if not is_submit(a)]
        if rest:
            reply["actions"] = rest
            observations.append("Hard limit 1 (the final Submit): your click on Submit was not done. The person or the "
                                "send path presses it. Finish the page, then report ready_to_submit.")
        else:
            reply["actions"] = []
            reply["done"] = {"outcome": "ready_to_submit", "reason": "the application is at its submit button"}
    # What the guard took out of this page, so the person sees it rather than only
    # the model benefiting from it. It advises: nothing here stops the run.
    guard = textguard.clean(str(req.get("view") or ""))
    for finding in guard.findings:
        notes.append(f"hidden text on this page ({finding.kind}): {finding.detail}"
                     + (f' -> {finding.quote}' if finding.quote else ""))
    # Hard limit 3: LinkedIn is never driven (CLAUDE.md section 7): a click on it is dropped here
    # as well as refused in the shell.
    kept = []
    for a in reply["actions"]:
        if a.get("op") == "click" and _LINKEDIN.search(_line_name(lines.get(a.get("index")) or {})):
            notes.append("a click on LinkedIn was dropped; apply with email or manually")
            observations.append("Hard limit 3 (nothing on LinkedIn): your click on LinkedIn was not done. "
                                "Apply with email or manually.")
            continue
        kept.append(a)
    reply["actions"] = kept
    n_notes = len(notes)
    actions = resolve_actions(reply["actions"], applicant, portal, labels, notes)
    observations += ["Not done: " + n + "." for n in notes[n_notes:]]
    # (Section 29) The answer protocol: an answer the brain chose itself is checked against the
    # person's material before it runs; a refusal goes back with its reason.
    actions = check_answers(actions, lines, material, observations, rejected, req.get("history"), auto=bool(req.get("auto")),
                            reasons=req.get("guess_reasons") if isinstance(req.get("guess_reasons"), dict) else None,
                            view=str(req.get("view") or ""))
    # Nothing unrendered ever reaches the page: an action whose text, option or file still holds
    # "{{" or "}}" after the placeholders were resolved is dropped (2026-11-16, belt and braces).
    clean = []
    for a in actions:
        if any(("{{" in str(a.get(k) or "") or "}}" in str(a.get(k) or "")) for k in ("text", "option", "path")):
            notes.append("an unrendered placeholder was never typed; dropped")
            observations.append("Not done: an unrendered placeholder is never typed. Use a placeholder from <placeholders> on its own.")
            continue
        clean.append(a)
    actions = clean
    # An upload goes into the chosen field's own box: a cover letter box takes this job's cover
    # letter, never the resume (2026-11-16, Gusto: the resume landed in "Cover Letter"); with no
    # letter the upload is dropped and the box stays empty. The shell checks the box itself too.
    kept = []
    for a in actions:
        if a.get("op") == "upload":
            letter_box = bool(_LETTER_BOX.search(_line_name(lines.get(a.get("index")) or {})))
            path = letter_path if letter_box else resume_path
            if letter_box and not path:
                notes.append("the cover letter box was left empty (no cover letter yet); never the resume")
                observations.append("Hard limit 6 (only this application's own files): the cover letter box takes the "
                                    "cover letter, and there is none yet. It was left empty; carry on.")
                continue
            if path:
                a["path"] = path
                a["file"] = "cover_letter" if letter_box else "resume"
        kept.append(a)
    actions = kept
    summary = clean_summary(reply.get("summary", ""), actions, lines, reply["done"], values, withheld)
    out = {"ok": True, "memory": reply["memory"], "evaluation": reply["evaluation"],
           "summary": summary, "plan": reply.get("plan") or [], "actions": actions, "done": reply["done"],
           "notes": notes, "cost": cost, "tries": tries,
           "observations": [o[:400] for o in observations][:8], "rejected": rejected}
    for k in ("causes", "likeliest", "strategy"):
        if k in reply:
            out[k] = reply[k]
    return out


# --------------------------------------------------------------------------- the critic
CRITIC_PROMPT = """You check a job application just before it is handed to the person at its Submit button. You get the review page as numbered lines (a filled box reads value="(filled)", never its contents), the plan of pages, the fields the engine left open, the questions this run filled (<filled>: filled, even when a custom dropdown's box reads empty in the view) and the files it attached (<attached>: attached, even when the page shows a file name in place of an upload box). Never list a question under <filled> or a file under <attached> as empty or missing. List anything blank, wrong or suspicious: a required field still empty, an error shown on the page, no resume attached where one was asked, a page of the plan still todo, a box that reads (filled) under an error that says it is required. Do not list what is fine. A word in double braces like {{city}} or {{full_name}} is the person's own value, hidden from you on purpose (in text and file names alike): it is filled, never a template left unrendered, so never list it. When the view was cut, the rest of the page follows it under "[Rest of the page]". Never mention a personal value. Reply with JSON only: {"ok": true|false, "issues": ["short line", "..."]}. ok is true when nothing is wrong."""


def _label_key(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())).strip()


def _filled_label(label: str, filled) -> bool:
    k = _label_key(label)
    return bool(k) and any(f == k or (min(len(f), len(k)) >= 30 and (f.startswith(k) or k.startswith(f)))
                           for f in filled)


def mark_filled(view: str, filled) -> str:
    """The view with a dropdown the run filled read as filled: a react-select (Greenhouse) keeps
    its choice beside its input, so the input, and the view, read value="" although the choice
    is made (2026-11-17, Upgrade: "LGBTQ+, ethnicity ... are still empty")."""
    keys = [k for k in (_label_key(f) for f in (filled or [])) if k]
    if not keys:
        return str(view or "")

    def fix(m):
        lab = _ATTR_LABEL.search(m.group(3))
        if m.group(2) not in ("select", "combobox") or not lab or not _filled_label(lab.group(1), keys):
            return m.group(0)
        return m.group(1) + m.group(2) + m.group(3).replace('value=""', 'value="(filled)"', 1)
    return _VIEW_LINE.sub(fix, str(view or ""))


# An issue that says a file is missing, and one that says self-identification questions are empty.
_RESUME_WORD = re.compile(r"r[ée]sum[ée]|\bcv\b", re.I)
_LETTER_WORD = re.compile(r"cover\s*letter", re.I)
_FILE_MISSING = re.compile(r"\bno (r[ée]sum[ée]|cv|cover letter|upload|file|attachment)|not (been )?(attached|uploaded|present"
                           r"|found|appear)|missing|absent|without (a |an |the )?(r[ée]sum[ée]|cv|cover letter|file)", re.I)
_EMPTY = re.compile(r"empty|blank|unanswered|unselected|not (been )?(answered|selected|filled|chosen)|missing", re.I)
_DEMOGRAPHIC = re.compile(r"demographic|self[- ]?identif|voluntary|eeo\b|" + _SELF_ID.pattern, re.I)


def _self_id_open(view: str) -> bool:
    """A self-identification control in the (masked) view that is still empty."""
    for m in _VIEW_LINE.finditer(view or ""):
        lab = _ATTR_LABEL.search(m.group(3))
        if not lab or not _SELF_ID.search(lab.group(1)):
            continue
        rest = m.group(3)
        if m.group(2) in ("select", "combobox") and 'value=""' in rest:
            return True
        if m.group(2) == "radio" and "answered=false" in rest:
            return True
    return False


def false_alarm(issue: str, files, view: str) -> bool:
    """True for a critic line the run's own record contradicts: a resume or cover letter called
    missing while the run attached it (Greenhouse shows a file name where the upload box was),
    or self-identification questions called empty while none is."""
    i = str(issue or "")
    if _FILE_MISSING.search(i):
        if _LETTER_WORD.search(i):
            return "cover_letter" in files
        if _RESUME_WORD.search(i) or re.search(r"\bupload\b|\battach", i, re.I):
            return "resume" in files
    if _EMPTY.search(i) and _DEMOGRAPHIC.search(i):
        return not _self_id_open(view)
    return False


def critic(req: dict, llm, values: dict | None = None, withheld=()) -> dict:
    """(Section 14) One cheap call over the review page before ready_to_submit: {ok, issues,
    cost}. A failed call is ok with no issues: the critic never blocks a hand-over.
    ``req["filled"]``: the questions the run filled (labels only); ``req["files"]``: the kinds
    of file it attached ("resume", "cover_letter"). Both keep the check from calling a filled
    custom dropdown or an attached file (shown as a file name) missing."""
    req = req or {}
    filled = [str(x)[:200] for x in (req.get("filled") or []) if str(x).strip()][:120]
    files = {str(x) for x in (req.get("files") or [])} & {"resume", "cover_letter"}
    view = mask_values(mask_self_id(mark_filled(str(req.get("view") or ""), filled)), withheld)
    if values:
        view = redact(view, values)
        filled = [redact(f, values) for f in filled]
    plan = clean_plan(req.get("plan"))
    left = [str(x) for x in (req.get("left") or []) if str(x).strip()]
    attached = {"resume": "the resume", "cover_letter": "the cover letter"}
    user = ("<plan>\n" + ("\n".join(plan) if plan else "(none)") + "\n</plan>\n\n<left>\n"
            + ("\n".join(left) if left else "(nothing)") + "\n</left>\n\n<filled>\n"
            + ("\n".join(filled) if filled else "(nothing)") + "\n</filled>\n\n<attached>\n"
            + ("\n".join(attached[k] for k in sorted(files)) if files else "(nothing)") + "\n</attached>\n\n<view>\n"
            + (view or "(empty page)") + "\n</view>")
    try:
        raw = llm.agent_critic(CRITIC_PROMPT, user) if hasattr(llm, "agent_critic") else llm.agent_step(CRITIC_PROMPT, user)
    except Exception:                            # noqa: BLE001
        return {"ok": True, "issues": [], "cost": 0.0, "skipped": True}
    cost = estimate_cost(getattr(llm, "last_usage", None), len(CRITIC_PROMPT) + len(user), len(raw or ""),
                         getattr(llm, "model", ""))
    data = _json_object(raw) or {}
    issues = [" ".join(str(i).split())[:160] for i in (data.get("issues") or []) if str(i).strip()] if isinstance(data.get("issues"), list) else []
    issues = [i for i in issues if not (_EMAIL_RE.search(i) or _DIGIT_RUN.search(i))]
    kept = [i for i in issues if not false_alarm(i, files, view)]
    dropped = len(kept) < len(issues)
    issues = kept[:6]
    ok = (bool(data.get("ok")) or dropped) and not issues
    return {"ok": ok, "issues": issues, "cost": cost}
