"""Grounded answers before an ask: a question the person's saved facts already answer is never
put to them (2026-11-17).

The planner's rules answer a question about the person only in wordings someone wrote a rule
for, so every new wording ("Where in the US do you currently reside?", "Current company", "Do you
currently reside in one of the following locations?") went to the bar and Telegram although the
profile says it. Before a page's open questions are asked, ONE model call per page answers them
from the person's saved facts only, each answer citing the fact it used:

* the facts (``build_facts``): where they live (city, state, postal code, country), each job
  (organization, title, dates, current or not), each school (school, degree, field, dates),
  their skills, and the answer library's own saved answers (question and answer). Never a
  self-identification (EEO) value, never a work authorization or other legal value
  (docs/applicant-brain.md section 9), never a drafted answer, a password or a code, never the
  resume text beyond these facts;
* the call (``answer_page``) sees the open questions, their options and those facts, and
  returns per question {answer, fact} or {none}. Only questions ``eligible`` reach it: never a
  self-identification question, a work authorization one, a legal attestation, a signature, a
  consent or the terms, an essay or a why-this-job question, nor a salary question unless a
  salary is saved;
* code then checks every answer (``verify``), never trusting the model: the cited fact must
  exist and must state the answer (a place answer names the fact's city, state, postal code or
  country and no other state or country; a company is a saved organization; a degree or field
  is in that school's fact; a date's years are that fact's years; a Yes or No follows from the
  cited fact by a plain rule), and a list question's answer must be one of its options exactly.
  Anything unverifiable stays an ask.

A verified answer goes back with origin ``grounded`` and the kind of fact it came from, so the
Review answers list shows it with "Answered from your saved address. Check it." It is never
saved to the answer library (it is the fact's, not a new answer of the person's).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field

from autoapply import countries, workauth
from autoapply.answers import is_per_job, normalize, unusable
from autoapply.history import degree_level, form_history, form_skills

MAX_QUESTIONS = 8            # open questions in one call
MAX_ANSWERS = 30             # saved answers sent as facts (the most relevant to the page)
MAX_JOBS = 8
MAX_SCHOOLS = 4
MAX_SKILLS = 40
MAX_CHARS = 200              # one fact value, one question
MAX_OPTION_CHARS = 120
MAX_OPTIONS = 40
MAX_TEXT_ANSWER = 80         # a grounded free-text answer is a short fact, never a paragraph
TIMEOUT_S = 20

ORIGIN = "grounded"
# The kinds of fact an answer can cite (the Review answers note names it).
KINDS = ("location", "job", "school", "skills", "answer")


@dataclass
class Fact:
    id: str
    kind: str
    show: dict                        # what the model sees
    data: dict = field(default_factory=dict)   # what the checks read (local only)


def _n(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#$ ]+", " ", str(s or "").lower())).strip()


def _flat(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _has(text_n: str, token) -> bool:
    t = _n(token)
    return bool(t) and bool(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text_n))


def _short(s, n: int = MAX_CHARS) -> str:
    return " ".join(str(s or "").split())[:n]


# --------------------------------------------------------------------------- what may be asked
# Never grounded: legal attestations, signatures, consents, the terms, and anything secret.
_LEGAL = re.compile(r"\bagree|consent|acknowledg|attest|certif(y|ication that)|signature|\bsign(ed)?\b|\bterms\b"
                    r"|privacy|polic(y|ies)|\bdeclare|truthful|true and (complete|correct|accurate)"
                    r"|i understand|authori[sz]e (us|the company|you)|criminal|convict|felony|misdemeanor"
                    r"|background check|drug (test|screen)|date of birth|birth ?date|\bage\b|social security|\bssn\b"
                    r"|password|passcode|verification code|one[- ]time|captcha|\bpin\b|bank (account|details)|routing number", re.I)
# Legal by the brain's rules (citizenship, visa, residence status): their own path, never here.
_LEGAL_TOPIC = re.compile(r"citizen|nationality|\bvisa\b|sponsor|immigra|permanent resid|green card|work permit"
                          r"|authori[sz]ed to work|right to work|export control|security clearance"
                          r"|restrictive covenant|non-?compet|non-?solicit", re.I)
# An essay or a question whose answer is written for this job: never a fact.
_ESSAY = re.compile(r"\bwhy\b|describe|tell us|explain|elaborate|share (an?|your|with)|what (excites|motivates)"
                    r"|cover letter|anything else|additional (information|comments)|summary|example of"
                    r"|how would you|walk us through|in your own words", re.I)
_SALARY = re.compile(r"salary|compensation|\bpay\b|\brate\b|remuneration|\bwage|earnings|\bctc\b|\bote\b", re.I)


def eligible(question: str, options=(), *, company: str = "", has_salary: bool = False) -> bool:
    """May this open question go to the grounded call? Never a self-identification question,
    work authorization, a legal or consent question, an essay or one job's own question, nor a
    salary question unless a salary is saved."""
    from autoapply.asks import is_eeo
    from autoapply import eeo
    q = _short(question, 400)
    if len(q) < 3:
        return False
    if is_eeo(q) or workauth.classify(q) or _LEGAL.search(q) or _LEGAL_TOPIC.search(q):
        return False
    if normalize(q) in eeo.KEYS:
        return False
    if any(eeo.topic_key(str(o)) in eeo.KEYS for o in (options or []) if str(o).strip()):
        return False                                   # a self-identification list by its choices
    if _ESSAY.search(q) or is_per_job(q, company):
        return False
    if _SALARY.search(q) and not has_salary:
        return False
    return True


# --------------------------------------------------------------------------- the facts
_STOP = frozenset("a an the and or of to in on at for with by from is are was were be been do does did you your "
                  "yours have has had what which where when who how many much any this that these those please "
                  "currently current enter provide select list our us we i my me it its if so as".split())
_DEGREE_WORDS = frozenset("bachelor bachelors bachelor's master masters master's doctor doctorate phd degree of in "
                          "science arts ba bs bsc b sc ma ms msc m s a mba associate associates diploma "
                          "certificate the".split())
_SECRET_Q = re.compile(r"password|passcode|code|\bpin\b|social security|\bssn\b|bank (account|details)|routing|account number"
                       r"|date of birth|birth ?date", re.I)


def _words(s) -> set:
    return {w for w in _n(s).split() if w not in _STOP and len(w) > 1}


def _ym(p) -> str:
    if not p:
        return ""
    return f"{p['y']}-{p['m']:02d}" if p.get("m") else str(p["y"])


def _years(*points) -> set:
    return {int(p["y"]) for p in points if p and p.get("y")}


def _state(state: str) -> tuple[str, str]:
    """(full name, code) for a US state given either way, lowercase / uppercase; ("", "") else."""
    from tailoring.conform import _US_STATES
    s = _n(state)
    if not s:
        return "", ""
    for full, code in _US_STATES.items():
        if s == full or s == code.lower():
            return full, code
    return s, ""


def _country(c: str) -> str:
    c = str(c or "").strip()
    if not c:
        return ""
    if re.fullmatch(r"(?i)u\.?s\.?a?\.?|united states of america|america", c):
        return "United States"
    if re.fullmatch(r"(?i)u\.?k\.?|great britain|britain", c):
        return "United Kingdom"
    return countries.canonical(c) or c


def _library_rows(library) -> list[dict]:
    if library is None or not hasattr(library, "list"):
        return []
    try:
        return list(library.list() or [])
    except Exception:          # noqa: BLE001 - no saved answers then
        return []


def build_facts(applicant: dict, profile: dict, library=None, *, questions=(), skip_keys=frozenset()) -> list[Fact]:
    """The person's saved facts for the grounded call, each with an id. ``applicant`` is the fill
    map (city, state, postal_code, country, current_company...), ``profile`` the saved profile
    (experience, education, skills). ``questions``: the page's open questions (the saved
    answers most related to them are sent, at most MAX_ANSWERS). ``skip_keys``: answer library
    keys held or withheld for this job (a salary conflict, an expired start date)."""
    from applicant import keys as K
    from autoapply.asks import is_eeo
    out: list[Fact] = []
    ap = applicant or {}
    city = _short(ap.get("city"), 60)
    full, code = _state(ap.get("state"))
    country = _country(ap.get("country")) or ("United States" if code else "")
    postal = _short(ap.get("postal_code"), 12)
    if city or full or country:
        show = {"id": "loc", "about": "where the person lives now"}
        for k, v in (("city", city), ("state", (full.title() + (f" ({code})" if code else "")) if full else ""),
                     ("postal_code", postal), ("country", country)):
            if v:
                show[k] = v
        out.append(Fact("loc", "location", show, {"city": city, "state": full, "code": code,
                                                  "postal": postal, "country": country}))
    hist = form_history(profile or {}, max_jobs=MAX_JOBS, max_schools=MAX_SCHOOLS)
    jobs = hist["history"]
    latest = next((i for i, h in enumerate(jobs) if h.get("current")), 0 if jobs else None)
    for i, h in enumerate(jobs, start=1):
        cur = bool(h.get("current"))
        show = {"id": f"job{i}", "about": "the person's current job" if cur else "a job the person held",
                "organization": _short(h.get("company"), 80), "title": _short(h.get("title"), 80),
                "start": _ym(h.get("start")), "end": "present" if cur else _ym(h.get("end"))}
        out.append(Fact(f"job{i}", "job", {k: v for k, v in show.items() if v},
                        {"org": h.get("company") or "", "title": h.get("title") or "", "current": cur,
                         "latest": latest == i - 1, "years": _years(h.get("start"), h.get("end"))}))
    for i, e in enumerate(hist["education"], start=1):
        show = {"id": f"edu{i}", "about": "a school the person attended", "school": _short(e.get("school"), 100),
                "degree": _short(e.get("degree"), 100), "field": _short(e.get("field"), 80),
                "start": _ym(e.get("start")), "end": "present" if e.get("current") else _ym(e.get("end"))}
        out.append(Fact(f"edu{i}", "school", {k: v for k, v in show.items() if v},
                        {"school": e.get("school") or "", "degree": e.get("degree") or "",
                         "field": e.get("field") or "", "level": e.get("level") or degree_level(e.get("degree")),
                         "current": bool(e.get("current")), "years": _years(e.get("start"), e.get("end"))}))
    skills = form_skills(profile or {}, limit=MAX_SKILLS)
    if skills:
        out.append(Fact("skills", "skills", {"id": "skills", "about": "the person's skills", "skills": skills},
                        {"skills": skills}))
    # The person's own saved answers: never a self-identification, legal, drafted or secret one.
    qwords = set().union(*[_words(q) for q in questions]) if questions else set()
    rows = []
    for r in _library_rows(library):
        key, q, a = str(r.get("key") or ""), str(r.get("question") or ""), str(r.get("answer") or "").strip()
        if not a or not q or str(r.get("source") or "") in ("drafted", "derived") or key in skip_keys:
            continue
        if K.sensitivity(K.fact_key(key), q) != K.NORMAL or is_eeo(q) or workauth.classify(q):
            continue
        if _LEGAL_TOPIC.search(q) or _SECRET_Q.search(q) or unusable(key, a) or len(a) > 300:
            continue
        rows.append((len(_words(q) & qwords), r))
    rows.sort(key=lambda x: -x[0])                     # stable: the library's own order breaks ties
    for i, (_score, r) in enumerate(rows[:MAX_ANSWERS], start=1):
        out.append(Fact(f"ans{i}", "answer",
                        {"id": f"ans{i}", "about": "an answer the person saved", "question": _short(r["question"]),
                         "answer": _short(r["answer"], 300)},
                        {"key": str(r.get("key") or ""), "question": str(r["question"]), "answer": str(r["answer"]).strip()}))
    return out


# --------------------------------------------------------------------------- the model call
SYSTEM = (
    "You answer job application questions ONLY from the person's saved facts. Return ONLY JSON:\n"
    '{"answers": [{"n": <question number>, "answer": "<text>", "fact": "<fact id>"} or {"n": <question number>, "none": true}]}\n'
    "Rules:\n"
    "1. Answer a question only when ONE fact states the answer directly, and cite that fact's id.\n"
    "2. Never infer, guess, or use anything outside the facts. When unsure, none.\n"
    "3. A question with options: the answer is one option, copied exactly.\n"
    "4. A question without options: the shortest answer the fact supports, in the fact's own words "
    "(\"Chicago, IL\", the company name, the degree, a year). Never a sentence.\n"
    "5. Yes only when the fact states it. No only when the fact plainly rules it out (a person lives in one place).\n"
    "6. Never answer self-identification, work authorization, visa, citizenship, legal, consent, signature, "
    "essay or why-this-job questions: none.\n"
    "7. One entry per question number."
)


def build_payload(items: list[dict], facts: list[Fact]) -> dict:
    qs = []
    for n, it in enumerate(items, start=1):
        q = {"n": n, "question": _short(it.get("question"), 300)}
        opts = [_short(o, MAX_OPTION_CHARS) for o in (it.get("options") or []) if str(o).strip()][:MAX_OPTIONS]
        if opts:
            q["options"] = opts
        qs.append(q)
    return {"questions": qs, "facts": [f.show for f in facts]}


def user_text(payload: dict) -> str:
    return ("<questions>\n" + json.dumps(payload["questions"], ensure_ascii=False) + "\n</questions>\n"
            "<facts>\n" + json.dumps(payload["facts"], ensure_ascii=False) + "\n</facts>")


def parse(raw: str, n_items: int) -> dict[int, tuple[str, str]]:
    """{question number: (answer, fact id)} from the model's reply; a none, or anything
    unreadable, is left out."""
    text = str(raw or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except ValueError:
        return {}
    out: dict[int, tuple[str, str]] = {}
    got = obj.get("answers") if isinstance(obj, dict) else None
    for x in got if isinstance(got, list) else []:
        if not isinstance(x, dict) or x.get("none"):
            continue
        try:
            n = int(x.get("n"))
        except (TypeError, ValueError):
            continue
        a, fid = " ".join(str(x.get("answer") or "").split()), str(x.get("fact") or "").strip()
        if 1 <= n <= n_items and n not in out and a and fid:
            out[n] = (a, fid)
    return out


def _call(llm, system: str, user: str, timeout: float) -> str:
    """The model call, bounded: a stalled call never holds the page."""
    box: dict = {}

    def run():
        try:
            box["raw"] = llm.ground_answers(system, user)
        except Exception as exc:       # noqa: BLE001 - no answers then; asked as before
            box["err"] = exc
    t = threading.Thread(target=run, daemon=True, name="grounded")
    t.start()
    t.join(timeout)
    return "" if t.is_alive() or "err" in box else str(box.get("raw") or "")


# --------------------------------------------------------------------------- the checks
_RESIDE = re.compile(r"\b(reside|resides|residing|residence|live|lives|living|located|location|based|home)\b", re.I)
_NOT_RESIDE = re.compile(r"relocat|willing|open to|commut|travel|permanent resid|green card|citizen|visa"
                         r"|work (from|remote|on-?site|in the office)|office|prefer|hybrid", re.I)
_WORKED = re.compile(r"\b(work|worked|working|employed|employ|intern|interned|contract|contracted|held)\b", re.I)
_SOMEONE_ELSE = re.compile(r"relative|family|friend|spouse|anyone|someone|know|refer|partner", re.I)
_STUDIED = re.compile(r"\b(stud(y|ied|ying)|attend(ed)?|graduat\w*|degree|enrolled|school|universit\w*|college"
                      r"|bachelor\w*|master\w*|phd|doctora\w*|diploma)\b", re.I)
_SKILLED = re.compile(r"experience (with|in|using)|familiar|proficien|knowledge of|worked with|skilled|skills?\b"
                      r"|comfortable (with|using)|\buse\b|\busing\b", re.I)
_CURRENT = re.compile(r"\b(current(ly)?|present(ly)?|most recent|latest|now)\b", re.I)
_DONE = re.compile(r"\b(complet\w*|graduat\w*|earned|obtained|hold|have)\b", re.I)
_HIGHEST = re.compile(r"\bhighest\b", re.I)
_LEVEL_RANK = {"high school": 1, "associates": 2, "bachelors": 3, "masters": 4, "doctorate": 5}
_CO_SUFFIX = re.compile(r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa"
                        r"|group|holdings?|technologies|labs)\b")
_MONTHS = ("jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august "
           "september october november december present current now").split()
_PLACE_WORDS = frozenset("area metro greater region the of city county usa us u s and".split())
_CITY_ST = re.compile(r"\b[A-Z][a-zA-Z.]+(?: [A-Z][a-zA-Z.]+)*,\s*[A-Z]{2}\b")
_IN_PLACE = re.compile(r"\b(in|near|around|within|from)\s+(the\s+)?[A-Z][a-z]+")


def _yes_no(answer: str):
    """True / False when the answer is a Yes or a No (an option may say more: "Yes - Chicago, IL"),
    else None."""
    n = _n(answer)
    if re.match(r"^(yes|y)\b", n):
        return True
    if re.match(r"^(no|n)\b", n):
        return False
    return None


def _us_alias(text: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z])(US|U\.S\.(A\.)?|USA|United States( of America)?)(?![A-Za-z])", str(text or "")))


def _countries_in(text: str) -> set:
    found = set(countries.named_in(text))
    if _us_alias(text):
        found.add("United States")
    if re.search(r"(?<![A-Za-z])(UK|U\.K\.)(?![A-Za-z])", str(text or "")):
        found.add("United Kingdom")
    return {_country(c) for c in found}


def _states_in(text: str) -> set:
    """The US states a text names: by full name (any case), by code (capitals only, so "in" and
    "or" are never Indiana and Oregon)."""
    from tailoring.conform import _US_STATES
    tn, out = _n(text), set()
    for full, code in _US_STATES.items():
        if _has(tn, full) or re.search(r"(?<![A-Za-z])" + code + r"(?![A-Za-z])", str(text or "")):
            out.add(full)
    if _has(tn, "new york city") or _has(tn, "nyc"):
        out.add("new york")
    return out


def _loc_hit(text: str, d: dict) -> bool:
    tn = _n(text)
    if d["city"] and _has(tn, d["city"]):
        return True
    if d["state"] and d["state"] in _states_in(text):
        return True
    if d["postal"] and _has(tn, d["postal"]):
        return True
    return bool(d["country"]) and d["country"] in _countries_in(text)


def _loc_conflict(text: str, d: dict) -> bool:
    """The text names a US state or a country that is not the person's."""
    if d["state"] and _states_in(text) - {d["state"]}:
        return True
    cs = _countries_in(text)
    return bool(d["country"]) and bool(cs - {d["country"]})


def _names_place(text: str) -> bool:
    t = str(text or "")
    return bool(_states_in(t) or _countries_in(t) or _CITY_ST.search(t) or _IN_PLACE.search(t))


def _location_ok(q: str, answer: str, options: list, d: dict) -> bool:
    yn = _yes_no(answer)
    if yn is None:
        if not _loc_hit(answer, d) or _loc_conflict(answer, d):
            return False
        if options:
            return True                                    # one of the page's own choices
        allowed = set()
        for v in (d["city"], d["state"], d["code"], d["postal"], d["country"]):
            allowed |= set(_n(v).split())
        if d["country"] == "United States":
            allowed |= {"united", "states", "america"}
        return set(_n(answer).split()) <= allowed | _PLACE_WORDS
    if not _RESIDE.search(q) or _NOT_RESIDE.search(q):
        return False
    tail = re.sub(r"^\W*(yes|no|y|n)\b\W*", "", answer, flags=re.I)
    if yn:
        if _loc_hit(tail, d):
            return True                                    # "Yes - Chicago, IL"
        return _loc_hit(q, d) and not (tail and _loc_conflict(tail, d))
    # No: nothing on the question or its choices names where the person lives, and they name
    # some place (else "Do you live nearby?" would be a No).
    others = [o for o in options if o != answer]
    if _loc_hit(q, d) or _loc_hit(tail, d) or any(_loc_hit(o, d) for o in options):
        return False
    if d["country"] and _countries_in(q) and d["country"] not in _countries_in(q):
        return True                                        # "Are you located in Canada?" for a US resident
    if not (d["city"] and d["state"]):
        return False                                       # the place is not known well enough
    return _names_place(q) or any(_names_place(o) for o in others)


def _core(s) -> str:
    return _n(_CO_SUFFIX.sub(" ", _n(s)))


def _dates_ok(answer: str, years: set) -> bool:
    """A date answer whose years are all the fact's ("03/2021", "May 2019")."""
    ys = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", answer)}
    if not ys or not ys <= years:
        return False
    rest = re.sub(r"(?<!\d)\d{1,4}(?!\d)", " ", _n(answer))
    return all(w in _MONTHS for w in rest.split())


def _job_ok(q: str, answer: str, d: dict, facts: list) -> bool:
    if _CURRENT.search(q) and not (d["current"] or (d["latest"] and not any(
            f.kind == "job" and f.data.get("current") for f in facts))):
        return False                                       # "current company" is the current job
    yn = _yes_no(answer)
    if yn is not None:
        if not yn or not _WORKED.search(q) or _SOMEONE_ELSE.search(q):
            return False                                   # a No about history is never proven
        qn = _n(q)
        return (len(_core(d["org"])) >= 3 and _has(qn, _core(d["org"]))) or (len(_n(d["title"])) >= 3 and _has(qn, d["title"]))
    if _core(answer) and _core(answer) == _core(d["org"]):
        return True
    if _n(answer) and _n(answer) == _n(d["title"]):
        return True
    return _dates_ok(answer, d["years"])


def _school_ok(q: str, answer: str, d: dict, facts: list) -> bool:
    level = d["level"]
    if _HIGHEST.search(q):
        ranks = [_LEVEL_RANK.get(f.data.get("level"), 0) for f in facts if f.kind == "school"]
        if _LEVEL_RANK.get(level, 0) < max(ranks or [0]):
            return False                                   # the highest is another school's
    yn = _yes_no(answer)
    if yn is not None:
        if not yn or not _STUDIED.search(q):
            return False                                   # a No about education is never proven
        if d["current"] and _DONE.search(q):
            return False                                   # still studying: not completed
        qn, ql = _n(q), degree_level(q)
        if ql:
            return _LEVEL_RANK.get(level, 0) >= _LEVEL_RANK.get(ql, 99)
        return any(len(_n(v)) >= 3 and _has(qn, v) for v in (d["school"], d["field"]))
    an = _n(answer)
    if not an:
        return False
    if any(_n(v) and an == _n(v) for v in (d["school"], d["degree"], d["field"])):
        return True
    school = _n(d["school"])
    if school and len(school.split()) >= 2 and _has(an, school) and \
            set(an.split()) <= set(school.split()) | {"campus", "main", "the"}:
        return True                                        # "The University of Ghana, Main Campus"
    if level and degree_level(answer) == level:
        extra = set(an.split()) - _DEGREE_WORDS - {"s", "'s"}
        return extra <= set(_n(d["degree"] + " " + d["field"] + " " + d["school"]).split())
    return _dates_ok(answer, d["years"])


def _skills_ok(q: str, answer: str, d: dict) -> bool:
    mine = {_n(s) for s in d["skills"] if _n(s)}
    yn = _yes_no(answer)
    if yn is not None:
        if not yn or not _SKILLED.search(q) or re.search(r"\byears?\b", q, re.I):
            return False
        qn = _n(q)
        return any(_has(qn, s) for s in mine)
    parts = [_n(p) for p in re.split(r"[,;/]| and ", answer) if _n(p)]
    return bool(parts) and all(p in mine for p in parts)


_GENERIC_ASK = frozenset("years experience total professional relevant work many expected desired annual base "
                         "salary compensation pay range usd $ requirement requirements target targeting".split())
_RELATED = {"@salary": _SALARY,
            "@start_date": re.compile(r"\bstart\b|available|availability|begin|join", re.I),
            "@notice_period": re.compile(r"notice", re.I),
            "@heard_about": re.compile(r"hear|heard|learn(ed)? (about|of)|find (out|this)|source|referr", re.I)}


def _same_question(q: str, saved_q: str, key: str) -> bool:
    """The saved answer's question asks what this one asks: the same key, a canonical key's own
    topic, or this question's words are all in the saved one's (nothing new asked)."""
    k = normalize(q)
    if key and k == key:
        return True
    rx = _RELATED.get(key)
    if rx is not None and rx.search(q) and not (key == "@salary" and not _SALARY.search(q)):
        return True
    mine = _words(q)
    return bool(mine) and mine <= (_words(saved_q) | (_GENERIC_ASK if key == "@years_experience" else set()))


def _answer_ok(q: str, answer: str, options: list, d: dict) -> bool:
    if not _same_question(q, d["question"], d["key"]):
        return False
    saved = d["answer"]
    yn = _yes_no(answer)
    if yn is not None and len(_n(answer).split()) <= 2:
        return workauth.polarity(saved) is yn
    if _flat(answer) and _flat(answer) == _flat(saved):
        return True
    if options:
        from autoapply.planner import literal_for, match_option
        return answer in {literal_for(normalize(q), saved, options), match_option(saved, options, strict=True)}
    return False


def verify(question: str, options, answer: str, fact: Fact | None, facts: list) -> str | None:
    """The answer to use (a list's own option text) when the cited fact states it, else None."""
    if fact is None:
        return None
    a = " ".join(str(answer or "").split())
    opts = [" ".join(str(o).split()) for o in (options or []) if str(o).strip()]
    if not a:
        return None
    if opts:
        exact = [o for o in opts if o == a] or [o for o in opts if o.lower() == a.lower()]
        if len(exact) != 1:
            return None                                    # only one of the page's own choices
        a = exact[0]
    elif len(a) > MAX_TEXT_ANSWER or "\n" in a:
        return None
    q = " ".join(str(question or "").split())
    d = fact.data
    ok = {"location": lambda: _location_ok(q, a, opts, d),
          "job": lambda: _job_ok(q, a, d, facts),
          "school": lambda: _school_ok(q, a, d, facts),
          "skills": lambda: _skills_ok(q, a, d),
          "answer": lambda: _answer_ok(q, a, opts, d)}.get(fact.kind)
    try:
        return a if ok is not None and ok() else None
    except Exception:          # noqa: BLE001 - a check that fails never answers
        return None


# --------------------------------------------------------------------------- the page
def _cache_key(question: str, options, facts: list[Fact]) -> str:
    raw = json.dumps([_n(question), sorted(_n(o) for o in options or []), [f.show for f in facts]],
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def answer_page(llm, items: list[dict], facts: list[Fact], *, company: str = "", eeo_values=(),
                cache: dict | None = None, timeout: float = TIMEOUT_S) -> dict:
    """{question: {"answer", "fact", "kind"}} for the page's open questions the saved facts
    answer, with at most ONE model call. ``items`` = [{question, options}]. ``llm`` is a model,
    or a function that makes one (called only when a question needs it). ``eeo_values``: every
    stored self-identification value, withheld from the payload outside the form's own words
    (none should be there; this is the guard). ``cache`` {key: result or None} spares a second
    call for a question already tried with the same facts."""
    if not facts:
        return {}
    has_salary = any(f.kind == "answer" and f.data.get("key") == "@salary" for f in facts)
    by_id = {f.id: f for f in facts}
    out: dict = {}
    todo: list[dict] = []
    seen: set = set()
    for it in items or []:
        q = " ".join(str((it or {}).get("question") or "").split())
        opts = [str(o) for o in ((it or {}).get("options") or []) if str(o).strip()]
        if not q or q in seen or not eligible(q, opts, company=company, has_salary=has_salary):
            continue
        seen.add(q)
        ck = _cache_key(q, opts, facts)
        if cache is not None and ck in cache:
            if cache[ck]:
                out[q] = dict(cache[ck])
            continue
        todo.append({"question": q, "options": opts, "ck": ck})
    todo = todo[:MAX_QUESTIONS]
    if not todo or llm is None:
        return out
    if not hasattr(llm, "ground_answers") and callable(llm):
        try:
            llm = llm()                    # made only when a question needs it (a factory)
        except Exception:                  # noqa: BLE001 - no model: the person is asked
            return out
    if llm is None or not hasattr(llm, "ground_answers"):
        return out
    payload = build_payload(todo, facts)
    if eeo_values:
        from applicant import redact
        allowed = [it["question"] for it in payload["questions"]] + [o for it in payload["questions"]
                                                                     for o in it.get("options") or []]
        payload = redact.for_model(payload, list(eeo_values), allowed)
    raw = _call(llm, SYSTEM, user_text(payload), timeout)
    if not raw:
        return out                         # a blip: nothing cached, tried again next time
    got = parse(raw, len(todo))
    for n, it in enumerate(todo, start=1):
        res = None
        if n in got:
            a, fid = got[n]
            fact = by_id.get(fid)
            v = verify(it["question"], it["options"], a, fact, facts)
            if v:
                res = {"answer": v, "fact": fact.id, "kind": fact.kind}
        if cache is not None:
            cache[it["ck"]] = res
        if res:
            out[it["question"]] = dict(res)
    return out
