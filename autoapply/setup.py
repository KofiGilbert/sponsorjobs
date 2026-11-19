"""Apply setup: the short, skippable setup that answers up front what the apply agent would
otherwise stop to ask (docs/apply-agent.md, "Apply setup").

Seven steps, each pre-filled from what the app already knows, each saved into the store that
already owns the answer (there is no setup store of its own):

1. contact   the profile identity (name, email, phone, address parts, links) and the brain's
             contact facts; a link the person has none of goes on the profile's ``declined`` list
2. workauth  the work-authorization profile (autoapply/workauth.py) and the country of citizenship
3. wants     the Auto-apply focus (``aa_focus``: titles, places), the "Never apply to" list,
             relocation (work_auth.relocate), start date, notice period, the desired salary, and
             the rest of what the person wants (``apply_wants``)
4. forms     the answer library's standing answers (how you heard, at least 18, non-compete,
             relatives at a company you apply to, marketing messages, forced consents, clearance,
             license, GPA, related degree, current pay) and the standing consent to required notices
5. selfid    the eight self-identification answers (autoapply/basics.py)
6. apply     review every application, or send where the site allows it (the verified
             allowlist), the daily limit and Autopilot's daily number
7. finish    "The agent can now answer N of the M questions forms usually ask", from real keys

This module is the pure half: the questions the answers are saved under, the checks behind the
count, and the cleaning of what the person typed. The routes in ui/app.py read and write the
stores. Nothing here leaves the machine.
"""
from __future__ import annotations

import re

from autoapply import workauth
from autoapply.answers import normalize

STEPS: tuple[str, ...] = ("contact", "workauth", "wants", "forms", "selfid", "apply", "finish")

# The questions the standing answers are saved under, worded as forms ask them. Each normalizes
# to its canonical key (autoapply/answers.py _CANON), so the planner, the agent's known answers
# and every other wording of the same question find it (tests pin each key).
QUESTIONS: dict[str, str] = {
    "start_date": "When can you start?",
    "notice": "What is your notice period?",
    "heard": "How did you hear about us?",
    "over_18": "Are you at least 18 years of age?",
    "covenants": "Do you have a non-compete or other restrictive covenant with a current or past employer?",
    # (2026-11-19) the standard declarations Auto would otherwise answer by best guess
    "criminal": "Have you ever been convicted of a crime, or do you have a pending charge?",
    "dismissed": "Have you ever been dismissed or asked to resign from a job?",
    "excluded": "Have you ever been excluded, suspended or sanctioned from a health care or government program?",
    "marketing": "May job sites send you marketing texts and emails?",
    "forced": "Accept a required consent whose only choice is yes?",
    # (2026-11-17, autoapply/commonsense.py) "No" answers No on every form; "Yes" means some do:
    # the agent asks each time. Unanswered: the question stays the person's.
    "relatives": "Do any of your relatives work at companies you apply to?",
    # (2026-11-18) Aliases, a maiden or former name: asked once. "None" fills "N/A" (or what the
    # form asks for); unanswered, the form's question stays the person's.
    "other_names": "What other names have you used?",
    "clearance": "Do you have an active security clearance?",
    "license": "Do you have a valid driver's license?",
    "gpa": "What is your GPA?",
    "related_degree": "Do you have a degree in a related field?",
    "current_salary": "What is your current annual salary?",
    "current_bonus": "What is your current annual bonus?",
    "current_equity": "What is your current equity?",
}
KEYS: dict[str, str] = {f: normalize(q) for f, q in QUESTIONS.items()}
YES_NO = ("over_18", "covenants", "criminal", "dismissed", "excluded", "marketing", "forced", "license", "related_degree")
CLEARANCES = ("None", "Public Trust", "Confidential", "Secret", "Top Secret", "TS/SCI")

LEVELS = ("internship", "entry", "mid", "senior", "lead")
JOB_TYPES = ("full_time", "part_time", "contract", "internship")
WORK_STYLES = ("remote", "hybrid", "onsite")
MAX_LIST = 20


def _words(v, n: int = 120) -> str:
    return " ".join(str(v or "").split())[:n]


def split_list(v) -> list[str]:
    """A list from a list or a comma or line separated string, trimmed, de-duplicated."""
    items = v if isinstance(v, (list, tuple)) else re.split(r"[,\n;]", str(v or ""))
    out, seen = [], set()
    for x in items:
        s = _words(x, 80)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:MAX_LIST]


def clean_wants(raw) -> dict:
    """What the person wants beyond the fields other stores own: {level, job_types, work_style,
    radius, industries}. Unknown values are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        radius = int(float(str(raw.get("radius") or 0)))
    except (TypeError, ValueError):
        radius = 0
    return {"level": [x for x in split_list(raw.get("level")) if x in LEVELS],
            "job_types": [x for x in split_list(raw.get("job_types")) if x in JOB_TYPES],
            "work_style": [x for x in split_list(raw.get("work_style")) if x in WORK_STYLES],
            "radius": max(0, min(500, radius)),
            "industries": split_list(raw.get("industries"))}


def yes_no(v) -> str:
    """"Yes", "No", or "" for anything else."""
    s = str(v or "").strip().lower()
    return {"yes": "Yes", "y": "Yes", "true": "Yes", "no": "No", "n": "No", "false": "No"}.get(s, "")


def gpa_text(gpa, scale) -> str:
    """The GPA as forms take it: "3.7 / 4.0", or "3.7" with no scale; "" when there is no figure.
    Raises ValueError for a GPA that is not a number or is above its scale."""
    g, s = _words(gpa, 10), _words(scale, 10)
    if not g:
        return ""
    try:
        gv = float(g)
        sv = float(s) if s else 0.0
    except ValueError:
        raise ValueError("Write the GPA as a number, like 3.7.") from None
    if gv < 0 or (sv and gv > sv) or gv > 100:
        raise ValueError("The GPA is above its scale.")
    return f"{g} / {s}" if s else g


def gpa_parts(text: str) -> tuple[str, str]:
    m = re.match(r"^\s*([\d.]+)\s*(?:/|out of|on a)?\s*([\d.]+)?", str(text or ""))
    return (m.group(1), m.group(2) or "") if m else ("", "")


def clearance_text(level: str) -> str:
    """"No" for none, "Yes, Secret" for a level: a yes/no form gets the yes or no, a text box the
    level too. "" when nothing was picked."""
    lv = _words(level, 40)
    if not lv:
        return ""
    if lv not in CLEARANCES:
        raise ValueError("Pick a clearance from the list.")
    return "No" if lv == "None" else f"Yes, {lv}"


def clearance_level(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return ""
    if workauth.polarity(t) is False or t.lower() in ("no", "none"):
        return "None"
    for lv in sorted(CLEARANCES, key=len, reverse=True):
        if lv.lower() in t.lower():
            return lv
    return ""


# ------------------------------------------------------------------------- the count
# The questions forms usually ask, in the order the finish step lists what is open, each with the
# step that answers it. The seven of autoapply/basics.py (citizenship, salary, five
# self-identification) are counted exactly as the Form answers card counts them.
ITEMS: tuple[tuple[str, str, str], ...] = (
    ("name", "Your name", "contact"),
    ("email", "Email", "contact"),
    ("phone", "Phone", "contact"),
    ("address", "Where you live", "contact"),
    ("linkedin", "LinkedIn", "contact"),
    ("links", "GitHub or portfolio", "contact"),
    ("authorized", "Authorized to work in the US", "workauth"),
    ("sponsorship", "Visa sponsorship, now and later", "workauth"),
    ("citizenship", "Country of citizenship", "workauth"),
    ("relocate", "Willing to relocate", "wants"),
    ("start_date", "When you can start", "wants"),
    ("salary", "Desired salary", "wants"),
    ("over_18", "At least 18", "forms"),
    ("heard", "How you heard about the job", "forms"),
    ("covenants", "Non-compete or restrictive covenant", "forms"),
    ("gender", "Gender", "selfid"),
    ("hispanic", "Hispanic or Latino", "selfid"),
    ("race", "Race", "selfid"),
    ("veteran", "Veteran status", "selfid"),
    ("disability", "Disability", "selfid"),
)


def answered(snap: dict) -> dict:
    """{item: bool} for ITEMS, from a snapshot of the stores:

    applicant  the applicant map a fill resolves against (autoapply.fields.applicant_from_record)
    declined   the profile's declined list ("linkedin", "github", "blog")
    work_auth  the work-authorization profile
    basics     autoapply.basics.read(...) (its ``missing``)
    saved      {library key: answer} for the standing answers (KEYS values), with the prefs
               store's flat answers folded in"""
    ap = snap.get("applicant") or {}
    dec = set(snap.get("declined") or [])
    wa = snap.get("work_auth") or {}
    saved = snap.get("saved") or {}
    missing = set((snap.get("basics") or {}).get("missing") or [])
    has = lambda k: bool(str(ap.get(k) or "").strip())        # noqa: E731
    out = {
        "name": has("full_name"),
        "email": has("email"),
        "phone": has("phone"),
        "address": has("city") or has("address"),
        "linkedin": has("linkedin") or "linkedin" in dec,
        "links": has("github") or has("website") or bool({"github", "blog"} & dec),
        "authorized": workauth.is_authorized(wa, "US") is not None,
        "sponsorship": workauth.needs_sponsorship(wa, "US", "now") is not None
        and workauth.needs_sponsorship(wa, "US", "future") is not None,
        "relocate": bool(workauth.clean(wa).get("relocate")),
        "start_date": bool(saved.get(KEYS["start_date"])),
        "over_18": bool(saved.get(KEYS["over_18"])),
        "heard": bool(saved.get(KEYS["heard"])),
        "covenants": bool(saved.get(KEYS["covenants"])),
    }
    # A citizen or permanent resident needs no sponsorship: implied, never asked.
    if workauth.is_citizen_or_pr(wa, "US"):
        out["authorized"] = out["sponsorship"] = True
    for f in ("citizenship", "salary", "gender", "hispanic", "race", "veteran", "disability"):
        out[f] = f not in missing
    return out


def coverage(snap: dict) -> dict:
    """{answered, total, open: [{id, label, step}]}: what the finish step says."""
    got = answered(snap)
    open_ = [{"id": i, "label": label, "step": step} for i, label, step in ITEMS if not got.get(i)]
    return {"answered": len(ITEMS) - len(open_), "total": len(ITEMS), "open": open_}


# ------------------------------------------------------------------------- Auto's checklist
# (2026-11-19, the owner: Auto never stops to ask.) The standard declarations Auto would otherwise
# answer by best guess, each answered once in this setup and saved under its own key. Shown as a
# checklist where Auto is switched on, with how many are still unanswered. It never blocks Auto:
# what is left blank gets a flagged best guess (autoapply/guess.py).
AUTO_SENTENCE = ("Auto never stops to ask. Answer these once so it never has to guess. "
                 "Anything left blank, I answer with my best guess and flag for you.")
AUTO_TERMS = ("In Auto I may answer a form's questions with my best judgement when you have not saved an answer. "
              "Those answers are flagged, and you are responsible for checking them before an application is sent.")
# (id, label, the setup step that answers it, the saved key or what holds it)
AUTO_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("authorized", "Authorized to work", "workauth", "work authorization profile"),
    ("sponsorship", "Visa sponsorship, now and later", "workauth", "work authorization profile"),
    ("other_names", "Other names you have used", "forms", "@other_names"),
    ("relatives", "Relatives at a company you apply to", "forms", "@relatives_at_employer"),
    ("covenants", "Non-compete or other restrictive agreement", "forms", "@restrictive_covenants"),
    ("criminal", "Conviction or pending charge", "forms", "@criminal_record"),
    ("dismissed", "Ever dismissed or asked to resign", "forms", "@dismissed"),
    ("excluded", "Excluded or sanctioned from a health care or government program", "forms", "@excluded_programs"),
    ("background", "Background check and drug screen", "forms", "accept_required_terms"),
    ("relocate", "Willing to relocate", "wants", "work authorization profile (relocate)"),
    ("start", "Notice period or earliest start date", "wants", "@notice_period / @start_date"),
    ("current_salary", "Current salary", "forms", "@current_salary"),
    ("salary", "Wanted salary", "wants", "@salary"),
    ("clearance", "Security clearance", "forms", "@security_clearance"),
)


def auto_checklist(snap: dict) -> dict:
    """{items: [{id, label, step, answered}], open, total, sentence, terms} from the same snapshot
    ``answered`` reads, plus ``terms_decided`` (the person chose how required notices and
    background checks are handled)."""
    got = answered(snap)
    saved = snap.get("saved") or {}
    has = lambda k: bool(str(saved.get(k) or "").strip())           # noqa: E731
    done = {
        "authorized": got.get("authorized"), "sponsorship": got.get("sponsorship"),
        "other_names": has(KEYS["other_names"]), "relatives": has(KEYS["relatives"]),
        "covenants": has(KEYS["covenants"]), "criminal": has(KEYS["criminal"]), "dismissed": has(KEYS["dismissed"]),
        "excluded": has(KEYS["excluded"]),
        "background": bool(snap.get("terms_decided")),
        "relocate": got.get("relocate"),
        "start": has(KEYS["start_date"]) or has(KEYS["notice"]),
        "current_salary": has(KEYS["current_salary"]), "salary": got.get("salary"),
        "clearance": has(KEYS["clearance"]),
    }
    items = [{"id": i, "label": label, "step": step, "answered": bool(done.get(i))} for i, label, step, _k in AUTO_ITEMS]
    return {"items": items, "open": sum(1 for x in items if not x["answered"]), "total": len(items),
            "sentence": AUTO_SENTENCE, "terms": AUTO_TERMS}
