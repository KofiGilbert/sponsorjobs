"""Turn a form's fields into a fill plan: the person's own answers first, the model last.

For each field the planner tries, in order:

1. **work authorization** (autoapply/workauth.py): any work-auth / sponsorship / visa /
   citizenship question gets the one consistent answer from the person's profile;
2. **the answer library** (autoapply/answers.py): an answer the person saved or fixed once;
3. **the record's drafted screening answers** (drafting/): answers drafted for THIS application;
4. **the cover letter** for a "cover letter" text box;
5. **profile fields**: contact details by label (name, email, phone, links...) as placeholder
   KEYS, resolved to real values locally;
6. **the model** (through our broker), only for what is still left. It sees field structure and
   placeholder keys only, never an answer or a personal value, and its output passes a FACT GATE:
   it may not answer a question about the person (work authorization, salary, years, degree,
   demographics, consents...) or type free text, so nothing is invented. Those fields are left
   for the person.

A question the person's material cannot answer is left for them, never guessed. ``parse_plan``
hard-validates the model's output against the ACTUAL fields and allowed keys, so a hallucinated
field or key is dropped rather than acted on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from autoapply import workauth
from autoapply.answers import normalize
from autoapply.fields import APPLICANT_KEYS, FillAction, FormField

_VALID_OPS = {"fill", "select", "check", "upload", "skip"}

# --------------------------------------------------------------------------- the fact gate
# A question about the PERSON: only their own saved material may answer it, never the model.
_FACT = re.compile(
    r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender"
    r"|pronoun|date of birth|birth ?date|\bage\b|criminal|convict|felony|background check|drug"
    r"|salary|compensation|\bpay\b|\brate\b|start|notice|years|experience|degree|\bgpa\b|graduat"
    r"|school|universit|education|clearance|citizen|visa|sponsor|authori[sz]|eligib|relocat"
    r"|\brefer|hear about|how did you|previously|ever (worked|been)|related to|relative"
    r"|non-?compete|agreement|consent|acknowledg|certif|attest|signature|\bsign\b|agree|terms|privacy"
    r"|language|speak|fluent|licen[cs]e|military|travel|shift|weekend|overtime|commut|located"
    r"|reside|live in|address|country|state|city", re.I)


def is_fact_question(label: str) -> bool:
    """True for a question only the person's own material may answer."""
    lbl = str(label or "")
    return bool(_FACT.search(lbl) or workauth.classify(lbl) or normalize(lbl).startswith("@"))


# --------------------------------------------------------------------------- profile fields
_NOT_ME = re.compile(r"reference|\breferr|emergency|manager|supervisor|company|employer|school|"
                     r"universit|recruiter|hiring|spouse|parent|guardian", re.I)
_CONTACT: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"e-?mail", re.I)),
    ("first_name", re.compile(r"first name|given name|forename|^\W*first\W*$", re.I)),
    ("last_name", re.compile(r"last name|surname|family name|^\W*last\W*$", re.I)),
    ("full_name", re.compile(r"^\W*(full |legal |your )*name\W*$", re.I)),
    ("phone", re.compile(r"phone|mobile|telephone|\bcell\b", re.I)),
    ("linkedin", re.compile(r"linked\s?in", re.I)),
    ("github", re.compile(r"git\s?hub", re.I)),
    ("website", re.compile(r"website|portfolio|personal (site|url|page)|\bblog\b", re.I)),
    ("postal_code", re.compile(r"\bzip\b|postal|post code|postcode", re.I)),
    ("city", re.compile(r"^\W*(current )?city\b|\bcity\W*$|\btown\b", re.I)),
    ("state", re.compile(r"^\W*(state|province|region)\b|state\s*/\s*province", re.I)),
    ("country", re.compile(r"^\W*(current )?country( of residence)?\W*$", re.I)),
    ("address", re.compile(r"^\W*(street |home |mailing |current )?address( line 1)?\W*$|street", re.I)),
]


def contact_key(label: str) -> str:
    """The placeholder key a contact field asks for ("" when it is not one)."""
    lbl = str(label or "")
    if not lbl or _NOT_ME.search(lbl):
        return ""
    for key, rx in _CONTACT:
        if rx.search(lbl):
            return key
    return ""


_EEO = re.compile(r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation"
                  r"|transgender|pronoun", re.I)
_DECLINE = re.compile(r"decline|prefer not|do not wish|don't wish|choose not|rather not|not to (say|answer|disclose)", re.I)


def decline_option(options) -> str | None:
    """The one "decline to self-identify" option, or None."""
    hits = [str(o) for o in (options or []) if _DECLINE.search(str(o))]
    return hits[0] if len(hits) == 1 else None


_RESUME = re.compile(r"r[ée]sum[ée]|\bcv\b|curriculum", re.I)
_LETTER = re.compile(r"cover\s*letter|letter of (interest|motivation)|motivation(al)? letter", re.I)


# --------------------------------------------------------------------------- option matching
def _n(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#$ ]+", " ", str(s or "").lower())).strip()


def match_option(answer: str, options) -> str | None:
    """The option a saved text answer names: equal after normalizing, else the one option the
    answer and the option share as a prefix; a Yes/No answer picks the option that says it.
    None when no single option fits (the field is then left for the person)."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    a = _n(answer)
    if not opts or not a:
        return None
    eq = [o for o in opts if _n(o) == a]
    if eq:
        return eq[0]
    yn = {"yes": True, "no": False, "y": True, "n": False, "true": True, "false": False}.get(a)
    if yn is not None:
        return workauth.pick_yes_no(yn, opts)
    pre = [o for o in opts if _n(o) and (_n(o).startswith(a) or a.startswith(_n(o)))]
    if len(pre) == 1:
        return pre[0]
    return None


def _op_for(f: FormField) -> str:
    t = (f.type or "text").lower()
    if t in ("select", "radio"):
        return "select"
    if t == "checkbox":
        return "check"
    return "fill"


def _literal(f: FormField, text: str) -> str | None:
    """Fit a known answer to this field: an exact option for a choice field, Yes for a checkbox
    the answer affirms, the text itself for a text box."""
    text = str(text or "").strip()
    if not text:
        return None
    t = (f.type or "text").lower()
    if f.options or t in ("select", "radio"):
        return match_option(text, f.options)
    if t == "checkbox":
        return "Yes" if workauth.polarity(text) is True else None
    return text


# --------------------------------------------------------------------------- the known pass
@dataclass
class KnownContext:
    """Everything the deterministic pass may answer from. All local; none of it reaches the model."""

    work_auth: dict = field(default_factory=dict)
    library: object = None                       # an AnswerLibrary (or anything with .lookup)
    drafted: list = field(default_factory=list)  # [{"question","answer"}] for THIS application
    cover_letter: str = ""
    applicant: dict = field(default_factory=dict)
    resume_path: str = ""
    job_country: str = "US"
    company: str = ""
    eeo_decline: bool = False                    # pick "Decline to self-identify" on EEO questions


@dataclass
class KnownResult:
    actions: list = field(default_factory=list)  # FillActions decided without the model
    rest: list = field(default_factory=list)     # FormFields still open (the model may plan these)
    left: list = field(default_factory=list)     # FormFields only the person can answer
    used_library: list = field(default_factory=list)   # library keys used (to mark last used)
    drafted_used: list = field(default_factory=list)   # (question, answer) drafted answers used


def plan_known(fields: list[FormField], ctx: KnownContext) -> KnownResult:
    """Answer every field the person's own material answers, in the planner's order; return the
    rest for the model and the fact questions nothing answered for the person."""
    out = KnownResult()
    drafted = {}
    for qa in ctx.drafted or []:
        if isinstance(qa, dict) and str(qa.get("answer") or "").strip():
            k = normalize(qa.get("question") or "")
            if k and k not in drafted:
                drafted[k] = (str(qa.get("question") or ""), str(qa.get("answer")).strip())
    for f in fields:
        label = f.label or ""
        t = (f.type or "text").lower()

        if t == "file":                                    # the tailored resume, nothing else
            if _LETTER.search(label):
                out.left.append(f)
            elif _RESUME.search(label) and ctx.resume_path:
                out.actions.append(FillAction(f.ref, "upload", origin="resume"))
            else:
                out.rest.append(f)
            continue

        # 0. EEO / demographics: only the person's standing "decline" choice, never a guess
        if _EEO.search(label) and not workauth.classify(label):
            v = decline_option(f.options) if ctx.eeo_decline and f.options else None
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="eeo", sensitive=True))
            else:
                out.left.append(f)
            continue

        # 1. work authorization
        if workauth.classify(label):
            v = workauth.answer(label, ctx.work_auth, options=f.options, field_type=t,
                                job_country=ctx.job_country)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="workauth", sensitive=True))
            else:
                out.left.append(f)                         # the profile does not say: theirs to answer
            continue

        # 2. the answer library
        hit = ctx.library.lookup(label) if ctx.library is not None else None
        if hit:
            v = _literal(f, hit["answer"])
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True))
                out.used_library.append(hit["key"])
            else:
                out.left.append(f)
            continue

        # 3. this application's drafted screening answers
        key = normalize(label)
        if key and key in drafted:
            q, a = drafted[key]
            v = _literal(f, a)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="drafted", sensitive=True))
                out.drafted_used.append((q, a))
            else:
                out.left.append(f)
            continue

        # 4. the cover letter
        if _LETTER.search(label) and t in ("textarea", "text"):
            if ctx.cover_letter.strip():
                out.actions.append(FillAction(f.ref, "fill", value=ctx.cover_letter.strip(),
                                              origin="letter", sensitive=True))
            else:
                out.left.append(f)
            continue

        # 5. profile fields (placeholder keys, resolved locally)
        ck = contact_key(label)
        if ck:
            if str(ctx.applicant.get(ck) or "").strip():
                out.actions.append(FillAction(f.ref, _op_for(f), source=ck, origin="profile"))
            else:
                out.left.append(f)
            continue

        # 6. a question about the person that none of their material answers: theirs
        if is_fact_question(label) or t == "textarea":
            out.left.append(f)
            continue
        out.rest.append(f)
    return out


# --------------------------------------------------------------------------- the model pass
def parse_plan(raw: dict, fields: list[FormField]) -> list[FillAction]:
    """Validate the model's raw plan against the real fields. Keeps only actions that target a known
    field ref and, when they reference personal data, a real APPLICANT_KEY. The fact gate drops any
    literal the model wrote into a question about the person, and any free text (the model has no
    material to write it from). Everything else is dropped, so a hallucinated field/key/answer can
    never drive the browser."""
    by_ref = {f.ref: f for f in fields}
    keys = set(APPLICANT_KEYS)
    out: list[FillAction] = []
    seen: set[str] = set()
    for item in (raw or {}).get("actions") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        op = str(item.get("op") or "fill").strip().lower()
        if ref not in by_ref or op not in _VALID_OPS or ref in seen:
            continue
        f = by_ref[ref]
        source = str(item.get("source") or "").strip()
        value = str(item.get("value") or "").strip()
        if source and source not in keys:        # a made-up personal key -> ignore the source
            source = ""
        if source:
            value = ""
        elif value:
            t = (f.type or "text").lower()
            if t not in ("select", "radio", "checkbox") or is_fact_question(f.label):
                value = ""                       # fact gate: never the model's guess about the person
            elif f.options and value not in f.options:
                value = ""                       # only an option that really exists
        if op == "upload" and (f.type or "").lower() != "file":
            continue
        if op in ("fill", "select", "check") and not source and not value:
            continue                             # nothing to put in the field -> skip it
        seen.add(ref)
        out.append(FillAction(ref=ref, op=op, source=source, value=value, origin="ai"))
    return out


def build_fill_plan(fields: list[FormField], llm, *, role: str = "", company: str = "",
                    jd: str = "", available_keys: tuple[str, ...] = APPLICANT_KEYS) -> list[FillAction]:
    """Ask the broker LLM for a fill plan for these fields, then validate it. Returns [] on any model
    error (the caller then drops to assisted apply, never guesses). The prompt carries NO personal
    data -- only field structure + the placeholder keys the plan may reference."""
    if not fields:
        return []
    public = [f.public() for f in fields]
    try:
        raw = llm.plan_form_fill(public, list(available_keys), role=role, company=company, jd=jd)
    except Exception:                            # noqa: BLE001 - model/broker blip -> no plan
        return []
    return parse_plan(raw or {}, fields)


def plan_form(fields: list[FormField], ctx: KnownContext, llm=None, *, role: str = "",
              jd: str = "") -> KnownResult:
    """The whole planner: the known pass, then the model for what is left (if any and if a model
    is given). Returns the KnownResult with the model's validated actions appended."""
    res = plan_known(fields, ctx)
    if res.rest and llm is not None:
        ai = build_fill_plan(res.rest, llm, role=role, company=ctx.company, jd=jd)
        planned = {a.ref for a in ai}
        res.actions.extend(ai)
        res.rest = [f for f in res.rest if f.ref not in planned]
    return res
