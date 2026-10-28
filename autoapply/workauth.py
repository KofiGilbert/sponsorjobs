"""The person's work authorization, and the ONE function that answers a form's question about it.

Every application asks some version of "Are you authorized to work here?" and "Will you need
sponsorship?", worded a hundred ways, as Yes/No radios, as dropdowns whose options say "Yes, I am
authorized and do not require sponsorship", or as a free-text box. A wrong or inconsistent answer
is an automatic rejection, so this is deterministic, never the model's guess:

* the person states their status ONCE, per country (US first; UK, Canada, Australia and the EU are
  data here too): current status, authorized yes/no, sponsorship now / in the future, an optional
  work permit end date, and willingness to relocate;
* ``answer(question, wa, options=...)`` maps any work-authorization / sponsorship / visa /
  citizenship question to the consistent answer, picking the exact option text for selects and
  radios;
* when the profile does not say (no entry for that country, an unanswered yes/no, options that
  do not fit), it returns ``None`` and the field is left for the person. Nothing is invented.

The only values implied rather than stated are the unambiguous ones: a citizen or permanent
resident of a country is authorized there and needs no sponsorship there.

Stable API for other modules (the job picker hides citizen-only roles with it):
``load``, ``clean``, ``status``, ``is_authorized``, ``needs_sponsorship``, ``is_citizen``,
``is_citizen_or_pr``, ``classify``, ``answer``, ``legacy_prefs``.

Pure: no I/O, no model. The saved shape (in the app's prefs store, key ``work_auth``)::

    {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                          "sponsor_future": "Yes", "auth_end": "2027-06-30"}},
     "relocate": "Yes"}
"""
from __future__ import annotations

import re

COUNTRIES: tuple[str, ...] = ("US", "UK", "CA", "AU", "EU")
COUNTRY_NAMES = {"US": "United States", "UK": "United Kingdom", "CA": "Canada",
                 "AU": "Australia", "EU": "European Union"}

# (code, label). Labels are what a free-text "What is your visa status?" box receives.
STATUSES: dict[str, list[tuple[str, str]]] = {
    "US": [("us_citizen", "U.S. citizen"), ("green_card", "Green card holder (permanent resident)"),
           ("h1b", "H-1B"), ("h4_ead", "H-4 EAD"), ("f1_opt", "F-1 OPT"),
           ("f1_stem_opt", "F-1 STEM OPT"), ("f1_cpt", "F-1 CPT"), ("tn", "TN"), ("e3", "E-3"),
           ("o1", "O-1"), ("l1", "L-1"), ("j1", "J-1"), ("daca", "DACA"), ("other", "Other")],
    "UK": [("uk_citizen", "British or Irish citizen"), ("uk_settled", "Settled status or ILR"),
           ("uk_pre_settled", "Pre-settled status"), ("uk_skilled_worker", "Skilled Worker visa"),
           ("uk_graduate", "Graduate visa"), ("uk_hpi", "High Potential Individual visa"),
           ("uk_global_talent", "Global Talent visa"), ("uk_student", "Student visa"),
           ("other", "Other")],
    "CA": [("ca_citizen", "Canadian citizen"), ("ca_pr", "Permanent resident"),
           ("ca_pgwp", "Post-graduation work permit"), ("ca_open_wp", "Open work permit"),
           ("ca_closed_wp", "Employer-specific work permit"), ("ca_study", "Study permit"),
           ("other", "Other")],
    "AU": [("au_citizen", "Australian citizen"), ("au_pr", "Permanent resident"),
           ("au_nz", "New Zealand citizen"), ("au_485", "Temporary Graduate visa (subclass 485)"),
           ("au_482", "Skills in Demand visa (subclass 482)"), ("au_whv", "Working Holiday visa"),
           ("au_student", "Student visa"), ("other", "Other")],
    "EU": [("eu_citizen", "EU, EEA or Swiss citizen"), ("eu_pr", "Long-term resident"),
           ("eu_blue_card", "EU Blue Card"), ("eu_work_permit", "National work permit"),
           ("eu_job_seeker", "Job seeker visa"), ("eu_student", "Student residence permit"),
           ("other", "Other")],
}
CITIZEN = frozenset({"us_citizen", "uk_citizen", "ca_citizen", "au_citizen", "eu_citizen"})
PERMANENT = frozenset({"green_card", "uk_settled", "ca_pr", "au_pr", "au_nz", "eu_pr"})
# Statuses with an end date worth stating (OPT, STEM OPT, EADs, time-limited permits).
DATED = frozenset({"f1_opt", "f1_stem_opt", "f1_cpt", "h4_ead", "daca", "j1", "uk_graduate",
                   "uk_pre_settled", "ca_pgwp", "ca_open_wp", "ca_closed_wp", "au_485", "au_whv",
                   "eu_job_seeker"})

_YN = ("", "Yes", "No")
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _yn(v) -> str:
    s = str(v or "").strip().lower()
    return {"yes": "Yes", "y": "Yes", "true": "Yes", "no": "No", "n": "No", "false": "No"}.get(s, "")


def status_label(code: str, country: str = "US") -> str:
    return dict(STATUSES.get(country, [])).get(code, "")


# --------------------------------------------------------------------------- the stored shape
def clean(raw) -> dict:
    """Validate a saved or posted work-authorization profile into the stored shape. Unknown
    countries, statuses and values are dropped; empty country entries are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {"countries": {}, "relocate": _yn(raw.get("relocate"))}
    for cc, e in (raw.get("countries") or {}).items():
        cc = str(cc or "").upper()
        if cc not in COUNTRIES or not isinstance(e, dict):
            continue
        codes = {c for c, _ in STATUSES[cc]}
        st = str(e.get("status") or "").strip()
        end = str(e.get("auth_end") or "").strip()
        ent = {"status": st if st in codes else "",
               "authorized": _yn(e.get("authorized")),
               "sponsor_now": _yn(e.get("sponsor_now")),
               "sponsor_future": _yn(e.get("sponsor_future")),
               "auth_end": end if _DATE.match(end) else ""}
        if any(ent.values()):
            out["countries"][cc] = ent
    return out


def from_legacy(prefs: dict) -> dict:
    """The older flat answers (work_auth_us Yes/No, needs_sponsorship Yes/No/Future,
    willing_to_relocate) as a US entry, so a person who answered before keeps their answers."""
    p = prefs or {}
    wa = _yn(p.get("work_auth_us") or p.get("work_authorized"))
    ns = str(p.get("needs_sponsorship") or "")
    now, fut = {"Yes": ("Yes", "Yes"), "No": ("No", "No"), "Future": ("No", "Yes")}.get(ns, ("", ""))
    raw = {"countries": {"US": {"authorized": wa, "sponsor_now": now, "sponsor_future": fut}},
           "relocate": p.get("willing_to_relocate")}
    return clean(raw)


def load(prefs: dict | None) -> dict:
    """The person's work-authorization profile from the app's prefs store: the structured
    ``work_auth`` when saved, else built from the older flat answers."""
    p = prefs or {}
    if isinstance(p.get("work_auth"), dict) and (p["work_auth"].get("countries") or p["work_auth"].get("relocate")):
        return clean(p["work_auth"])
    return from_legacy(p)


def apply_legacy(wa: dict | None, prefs: dict) -> dict:
    """An older surface (the extension popup, a form correction) wrote the flat answers: carry
    the ones it actually set into the US entry, keeping everything else in the profile."""
    out = clean(wa or {})
    p = prefs or {}
    us = dict(out["countries"].get("US") or {"status": "", "authorized": "", "sponsor_now": "",
                                              "sponsor_future": "", "auth_end": ""})
    wa_v = _yn(p.get("work_auth_us") or p.get("work_authorized"))
    if wa_v:
        us["authorized"] = wa_v
    ns = str(p.get("needs_sponsorship") or "")
    if ns in ("Yes", "No", "Future"):
        us["sponsor_now"], us["sponsor_future"] = {"Yes": ("Yes", "Yes"), "No": ("No", "No"),
                                                   "Future": ("No", "Yes")}[ns]
    out["countries"]["US"] = us
    rel = _yn(p.get("willing_to_relocate"))
    if rel:
        out["relocate"] = rel
    return clean(out)


def legacy_prefs(wa: dict) -> dict:
    """The flat answers the browser extension and older code read, derived from the US entry,
    so every surface fills the same answer."""
    us = entry(wa, "US")
    now, fut = us["sponsor_now"], us["sponsor_future"]
    if now == "Yes":
        ns = "Yes"
    elif now == "No" and fut == "Yes":
        ns = "Future"
    elif now == "No" and fut == "No":
        ns = "No"
    else:
        ns = ""
    return {"work_auth_us": us["authorized"], "work_authorized": us["authorized"],
            "needs_sponsorship": ns, "willing_to_relocate": (wa or {}).get("relocate", "")}


def entry(wa: dict | None, country: str = "US") -> dict:
    """One country's answers with the unambiguous implications filled in (a citizen or permanent
    resident is authorized and needs no sponsorship there). Missing stays ""."""
    e = dict(((wa or {}).get("countries") or {}).get(str(country or "US").upper()) or {})
    e = {k: e.get(k, "") for k in ("status", "authorized", "sponsor_now", "sponsor_future", "auth_end")}
    if e["status"] in CITIZEN or e["status"] in PERMANENT:
        e["authorized"] = e["authorized"] or "Yes"
        e["sponsor_now"] = e["sponsor_now"] or "No"
        e["sponsor_future"] = e["sponsor_future"] or "No"
    if e["sponsor_now"] == "Yes" and not e["sponsor_future"]:
        e["sponsor_future"] = "Yes"              # needing it now means needing it going forward
    return e


def _b(v: str):
    return True if v == "Yes" else False if v == "No" else None


def status(wa: dict | None, country: str = "US") -> str:
    """The person's status code in that country, or "" when not given."""
    return entry(wa, country)["status"]


def is_authorized(wa: dict | None, country: str = "US"):
    """True / False, or None when the profile does not say."""
    return _b(entry(wa, country)["authorized"])


def needs_sponsorship(wa: dict | None, country: str = "US", when: str = "any"):
    """Does the person need visa sponsorship in that country? ``when``: "now", "future", or
    "any" (now or in the future, how most forms mean it). None when the profile does not say."""
    e = entry(wa, country)
    now, fut = _b(e["sponsor_now"]), _b(e["sponsor_future"])
    if when == "now":
        return now
    if when == "future":
        return fut
    if now is True or fut is True:
        return True
    if now is False and fut is False:
        return False
    return None


def is_citizen(wa: dict | None, country: str = "US"):
    """True for a citizen, False for any other stated status, None when no status is given."""
    st = status(wa, country)
    return (st in CITIZEN) if st else None


def is_citizen_or_pr(wa: dict | None, country: str = "US"):
    st = status(wa, country)
    return (st in CITIZEN or st in PERMANENT) if st else None


# --------------------------------------------------------------------------- reading a question
_SPONSOR = re.compile(
    r"(requir|need|will you|do you|would you|seek|petition).{0,80}\bsponsor"
    r"|\bsponsor\w*.{0,50}(visa|employment|work|immigration|h-?1b|status|permit)"
    r"|(require|need)s? (a |an )?(work |employment |new )?(visa|work permit)\b"
    r"|visa (support|transfer)", re.I)
_AUTH = re.compile(
    r"(authori[sz]ed|eligible|entitled|permitted|allowed|able|right|clearance) (to|for) "
    r"(legally |lawfully )?(work|be employed|employment|accept employment)"
    r"|work authori[sz]ation|right to work|eligib\w* (for|to) (work|employment)"
    r"|employment eligibility|legally (work|able to work|authori[sz]ed|eligible|permitted)"
    r"|lawfully (work|employed)|permission to work|legal(ly)? (right|entitled)", re.I)
_NO_SPONSOR = re.compile(r"without (the )?(need (for|of) |needing |requiring |requirement (for|of) |any )?"
                         r"(visa |employer |company )?sponsor|not (need|require)\w* (visa |any )?sponsor"
                         r"|no (visa )?sponsorship", re.I)
_UNRESTRICTED = re.compile(r"any employer|without (any )?restriction|unrestricted|no restriction", re.I)
_STATUS = re.compile(
    r"(visa|immigration|work authori[sz]ation|citizenship|residency|residence|work permit|employment authori[sz]ation) "
    r"(status|type|category|classification)"
    r"|(what|which) (type of |kind of )?(visa|work permit|immigration)"
    r"|current (visa|immigration|work authori[sz]ation)"
    r"|describes your (current )?(work|visa|immigration|citizenship|legal)"
    r"|status in the (us|u\.s\.|united states|uk|canada|australia)", re.I)
_NATIONALITY = re.compile(r"country of (citizenship|birth|origin)|nationalit|which country", re.I)
_CITIZEN_PR = re.compile(r"citizen.{0,40}(permanent resident|green card|lawful permanent|national)"
                         r"|(permanent resident|green card).{0,40}citizen", re.I)
_CITIZEN = re.compile(r"\b(are|is) (you|applicant) (a|an) .{0,30}citizen|\bcitizen(ship)?\b.{0,40}\?|\bcitizen\b", re.I)
_PR = re.compile(r"green card|permanent resident|lawful permanent|indefinite leave|settled status", re.I)
_END = re.compile(r"(opt|stem|ead|work permit|permit|authori[sz]ation|visa|status).{0,40}"
                  r"(expir|end date|\bends?\b|valid until|valid through|expiration)", re.I)
_RELOCATE = re.compile(r"relocat", re.I)
_NOW = re.compile(r"\bnow\b|\bcurrent(ly)?\b|at this time|at present|presently|today|to begin", re.I)
_FUTURE = re.compile(r"future|later|eventually|at any (point|time)|going forward|down the (road|line)"
                     r"|extend|extension", re.I)

_CC_RX = [
    ("US", re.compile(r"united states|\bu\.\s?s\.?(a\.?)?(?=\W|$)|\bamerica\b", re.I)),
    ("US", re.compile(r"\bUSA?\b")),             # case-sensitive: "US", never the pronoun "us"
    ("UK", re.compile(r"united kingdom|\bu\.k\.|britain|\bengland\b|\bscotland\b|\bwales\b"
                      r"|northern ireland", re.I)),
    ("UK", re.compile(r"\bUK\b")),
    ("CA", re.compile(r"\bcanad(a|ian)\b", re.I)),
    ("AU", re.compile(r"\baustralia(n)?\b", re.I)),
    ("EU", re.compile(r"european union|\beea\b|\beurope(an)?\b|schengen|\bgermany\b|\bfrance\b"
                      r"|netherlands|\bireland\b|\bspain\b|\bitaly\b|\bportugal\b|\bbelgium\b"
                      r"|\baustria\b|\bpoland\b|\bsweden\b|\bdenmark\b|\bfinland\b|\bswitzerland\b"
                      r"|\bnorway\b|\bluxembourg\b|\bczech", re.I)),
    ("EU", re.compile(r"\bEU\b")),
]


def question_country(question: str, job_country: str = "US") -> str:
    """The country a question asks about: the one it names, else the job's country."""
    q = str(question or "")
    for cc, rx in _CC_RX:
        if rx.search(q):
            return cc
    return str(job_country or "US").upper()


def classify(question: str) -> str | None:
    """What a form question asks about the person's work authorization, or None when it is not
    such a question. Kinds: authorized, authorized_no_sponsor, authorized_unrestricted,
    sponsor_now, sponsor_future, sponsor_any, citizen, citizen_or_pr, pr, status, auth_end,
    relocate."""
    q = re.sub(r"\s+", " ", str(question or "")).strip()
    if not q:
        return None
    if _NATIONALITY.search(q):
        return None                               # nationality is not in the profile
    if _END.search(q) and re.search(r"opt|stem|ead|permit|authori[sz]ation|visa", q, re.I) \
            and re.search(r"date|when|expir|until|through", q, re.I):
        return "auth_end"
    sponsor = bool(_SPONSOR.search(q)) or bool(re.fullmatch(
        r"(do you (need|require) )?(visa |immigration |employment )?sponsorship( required| needed| status)?\s*\??\*?", q, re.I))
    auth = bool(_AUTH.search(q))
    if auth and _NO_SPONSOR.search(q):
        return "authorized_no_sponsor"
    if auth and sponsor:
        ms, ma = _SPONSOR.search(q), _AUTH.search(q)
        if not (ms and ma and ms.start() < ma.start()):
            return None                           # "authorized AND sponsorship?" in one: theirs
    if sponsor:
        now, fut = bool(_NOW.search(q)), bool(_FUTURE.search(q))
        if now and not fut:
            return "sponsor_now"
        if fut and not now and not re.search(r"\b(do|are) you\b", q, re.I):
            return "sponsor_future"
        if fut and not now:
            return "sponsor_any" if re.search(r"\bnow\b|or", q, re.I) else "sponsor_future"
        return "sponsor_any"
    if _STATUS.search(q):
        return "status"
    if auth:
        return "authorized_unrestricted" if _UNRESTRICTED.search(q) else "authorized"
    if _CITIZEN_PR.search(q):
        return "citizen_or_pr"
    if re.search(r"\bcitizen\b", q, re.I) and _CITIZEN.search(q):
        return "citizen"
    if _PR.search(q) and re.search(r"\b(are|is|do) you\b|\?", q, re.I):
        return "pr"
    if _RELOCATE.search(q) and re.search(r"willing|open|able|consider|would you|are you|relocation", q, re.I):
        return "relocate"
    return None


def fact(kind: str, wa: dict | None, country: str):
    """The profile's answer to one kind: True/False for yes/no kinds, a code for "status", a
    date for "auth_end"; None when the profile does not say."""
    e = entry(wa, country)
    if kind == "authorized":
        return _b(e["authorized"])
    if kind == "authorized_unrestricted":
        a, n = _b(e["authorized"]), _b(e["sponsor_now"])
        if a is False:
            return False
        if a is True and n is not None:
            return not n
        return None
    if kind == "authorized_no_sponsor":
        a, s = _b(e["authorized"]), needs_sponsorship(wa, country, "any")
        if a is False or s is True:
            return False
        if a is True and s is False:
            return True
        return None
    if kind == "sponsor_now":
        return needs_sponsorship(wa, country, "now")
    if kind == "sponsor_future":
        return needs_sponsorship(wa, country, "future")
    if kind == "sponsor_any":
        return needs_sponsorship(wa, country, "any")
    if kind == "citizen":
        return is_citizen(wa, country)
    if kind == "citizen_or_pr":
        return is_citizen_or_pr(wa, country)
    if kind == "pr":
        st = e["status"]
        return (st in PERMANENT) if st else None
    if kind == "status":
        return e["status"] or None
    if kind == "auth_end":
        return e["auth_end"] or None
    if kind == "relocate":
        return _b((wa or {}).get("relocate", ""))
    return None


# --------------------------------------------------------------------------- choosing an option
def _norm(s: str) -> str:
    s = str(s or "").lower().replace("’", "'")
    s = re.sub(r"(?<=\w)[.\-](?=\w)", "", s)      # u.s. -> us, h-1b -> h1b
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NEG = re.compile(r"\b(not|no|don't|dont|do not|won't|wont|will not|never|cannot|can't|cant|"
                  r"am not|i'm not|im not|isn't|aren't|neither|none)\b")
_POSITIVE_WITHOUT = re.compile(r"without (the )?(need (for|of) |needing |requiring |any )?"
                               r"(visa |employer |company )?(sponsorship|restriction|restrictions|a visa)")


def polarity(option: str):
    """Does an option say yes (True) or no (False)? None for options that are neither, like
    "Other" or "Prefer not to say"."""
    n = _norm(option)
    if not n or re.search(r"\b(prefer not|decline|rather not|not sure|unsure|not applicable)\b"
                          r"|^other\b|^n a$", n):
        return None
    if re.match(r"^(yes|y|true)\b", n):
        return True
    if re.match(r"^(no|n|false)\b", n):
        return False
    body = _POSITIVE_WITHOUT.sub(" ", n)
    return not _NEG.search(body)


def _sponsor_claim(option: str):
    """When an option also states sponsorship ("Yes, but I will need sponsorship"), what it
    says: True needs it, False does not, None says nothing."""
    n = _norm(option)
    if "sponsor" not in n and "visa" not in n:
        return None
    if _POSITIVE_WITHOUT.search(n):
        return False
    tail = n.split(" but ", 1)[1] if " but " in n else re.sub(r"^(yes|no)\b", "", n)
    return not _NEG.search(tail)


def pick_yes_no(want: bool, options, *, sponsor_any=None):
    """The option that says ``want``. When several do (multi-part options), the one consistent
    with the person's sponsorship need; None when it stays ambiguous or nothing fits."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    cands = [o for o in opts if polarity(o) is want]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    plain = [o for o in cands if _norm(o) in ("yes", "no", "y", "n", "true", "false")]
    claims = [(o, _sponsor_claim(o)) for o in cands]
    if sponsor_any is not None and any(c is not None for _, c in claims):
        fit = [o for o, c in claims if c is sponsor_any] or [o for o, c in claims if c is None]
        if len(fit) == 1:
            return fit[0]
        return None
    if len(plain) == 1:
        return plain[0]
    return None


# Status synonyms, most specific first (STEM OPT before OPT, H-4 EAD before EAD).
_STATUS_SYN: list[tuple[str, tuple[str, ...]]] = [
    ("f1_stem_opt", ("stem opt", "opt stem", "stem extension", "stem")),
    ("f1_opt", ("f1 opt", "opt", "optional practical training")),
    ("f1_cpt", ("f1 cpt", "cpt", "curricular practical training")),
    ("h4_ead", ("h4 ead", "h4")),
    ("h1b", ("h1b", "h1 b", "h1")),
    ("green_card", ("green card", "permanent resident", "lawful permanent", "lpr")),
    ("us_citizen", ("us citizen", "united states citizen", "american citizen", "citizen")),
    ("tn", ("tn",)), ("e3", ("e3",)), ("o1", ("o1",)), ("l1", ("l1",)), ("j1", ("j1",)),
    ("daca", ("daca", "deferred action")),
    ("uk_citizen", ("british citizen", "irish citizen", "uk citizen", "citizen")),
    ("uk_settled", ("settled status", "indefinite leave", "ilr")),
    ("uk_pre_settled", ("pre settled",)),
    ("uk_skilled_worker", ("skilled worker", "tier 2")),
    ("uk_graduate", ("graduate visa", "graduate route", "psw")),
    ("uk_hpi", ("high potential",)), ("uk_global_talent", ("global talent",)),
    ("uk_student", ("student visa", "tier 4", "student")),
    ("ca_citizen", ("canadian citizen", "citizen")), ("ca_pr", ("permanent resident",)),
    ("ca_pgwp", ("post graduation work permit", "pgwp")), ("ca_open_wp", ("open work permit",)),
    ("ca_closed_wp", ("employer specific", "closed work permit", "lmia")),
    ("ca_study", ("study permit", "student")),
    ("au_citizen", ("australian citizen", "citizen")), ("au_pr", ("permanent resident",)),
    ("au_nz", ("new zealand",)), ("au_485", ("485", "temporary graduate")),
    ("au_482", ("482", "tss", "skills in demand", "temporary skill shortage")),
    ("au_whv", ("working holiday",)), ("au_student", ("student visa", "student")),
    ("eu_citizen", ("eu citizen", "eea citizen", "swiss citizen", "eu eea", "citizen")),
    ("eu_pr", ("long term resident", "permanent resident")), ("eu_blue_card", ("blue card",)),
    ("eu_work_permit", ("work permit",)), ("eu_job_seeker", ("job seeker",)),
    ("eu_student", ("student",)),
]
_GENERIC = {  # a broader option the status still truthfully falls under
    "f1_opt": ("f1", "f 1", "student visa", "ead", "employment authorization"),
    "f1_stem_opt": ("f1", "f 1", "student visa", "ead", "employment authorization"),
    "f1_cpt": ("f1", "f 1", "student visa"),
    "h4_ead": ("ead", "employment authorization"),
    "daca": ("ead", "employment authorization"),
}


def _has(n: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", n) is not None


def pick_status(code: str, options, country: str = "US"):
    """The option naming this status (or, failing that, the broader category it falls under);
    None when no option fits. "Other" is chosen only for the "other" status."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if not code:
        return None
    if code == "other":
        return next((o for o in opts if _norm(o).startswith("other")), None)
    syn = dict(_STATUS_SYN).get(code, ())
    for phrase in syn:
        hits = []
        for o in opts:
            n = _norm(o)
            if not _has(n, phrase):
                continue
            if _NEG.search(n) or n.startswith("other"):
                continue                          # "Not a U.S. citizen" never names the status
            # a more specific status must not claim the general one's option (OPT vs STEM OPT)
            if code == "f1_opt" and _has(n, "stem"):
                continue
            if code == "h1b" and _has(n, "h4"):
                continue
            if code.endswith("citizen") and re.search(r"permanent|green card", n) and \
                    not re.search(r"\bor\b|/", o.lower()):
                continue
            hits.append(o)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return min(hits, key=len)
    for phrase in _GENERIC.get(code, ()):
        hits = [o for o in opts if _has(_norm(o), phrase) and not _NEG.search(_norm(o))]
        if len(hits) == 1:
            return hits[0]
    return None


_CHOICE_TYPES = ("select", "radio")


def answer(question: str, wa: dict | None, *, options=(), field_type: str = "text",
           job_country: str = "US"):
    """The consistent answer to one work-authorization question, as the text to put in the
    field: the exact option label for a select/radio, "Yes"/"No" for a checkbox or a text box,
    the status label or date for those questions. None when it is not such a question or the
    profile does not say: the field is then left for the person, never guessed."""
    kind = classify(question)
    if not kind:
        return None
    country = question_country(question, job_country)
    val = fact(kind, wa, country)
    if val is None:
        return None
    opts = [str(o) for o in (options or []) if str(o).strip()]
    ftype = str(field_type or "text").lower()
    if kind == "status":
        if opts:
            return pick_status(val, opts, country)
        return status_label(val, country) or None
    if kind == "auth_end":
        return None if opts else val
    # yes/no kinds
    if ftype == "checkbox":
        return "Yes" if val else None             # an unticked box already says no
    if opts or ftype in _CHOICE_TYPES:
        sponsor_any = needs_sponsorship(wa, country, "any")
        if kind.startswith("sponsor"):
            sponsor_any = val
        picked = pick_yes_no(bool(val), opts, sponsor_any=sponsor_any)
        if picked is None and kind in ("citizen", "citizen_or_pr", "pr") and opts:
            st = entry(wa, country)["status"]  # a "Yes/No" citizen question may list statuses
            picked = pick_status(st, opts, country) if st else None
        return picked
    return "Yes" if val else "No"


def note(wa: dict | None, job_country: str = "US") -> str:
    """One honest sentence about the person's status for the "please explain / provide more
    information" box many forms show after a sponsorship question. Built only from the saved
    profile; "" when it holds nothing for that country."""
    wa = wa or {}
    cc = (job_country or "US").upper()
    ent = (wa.get("countries") or {}).get(cc) or {}
    if not ent:
        return ""
    label = dict(STATUSES.get(cc, [])).get(str(ent.get("status") or ""), "")
    parts = []
    if label:
        parts.append(f"I am currently on {label}" if cc == "US" and label not in ("U.S. citizen", "Green card holder (permanent resident)")
                     else f"I am a {label.lower()}" if label == "U.S. citizen" else f"I am a {label.lower()}" if label.startswith("Green card") else f"My status is {label}")
    auth = str(ent.get("authorized") or "")
    now, fut = str(ent.get("sponsor_now") or ""), str(ent.get("sponsor_future") or "")
    if auth == "Yes" and now == "No":
        parts.append("authorized to work without sponsorship at this time")
    elif auth == "Yes":
        parts.append("authorized to work")
    elif auth == "No":
        parts.append("not yet authorized to work")
    first = " and ".join(parts) + "." if parts else ""
    second = ""
    if fut == "Yes":
        second = " I will need sponsorship in the future."
    elif fut == "No" and now == "No":
        second = " I will not need sponsorship."
    return (first + second).strip()
