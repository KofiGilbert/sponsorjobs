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

from autoapply import commonsense, consents, eeo, workauth
from autoapply.answers import normalize
from autoapply import countries
from autoapply.countries import country_option
from autoapply.fields import APPLICANT_KEYS, FillAction, FormField

_VALID_OPS = {"fill", "select", "check", "upload", "skip"}

# --------------------------------------------------------------------------- the fact gate
# A question about the PERSON: only their own saved material may answer it, never the model.
_FACT = re.compile(
    r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender|first[- ]generation"
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
    return bool(_FACT.search(lbl) or workauth.classify(lbl) or normalize(lbl).startswith("@")
                or consents.is_acknowledgement(lbl))


# --------------------------------------------------------------------------- profile fields
_NOT_ME = re.compile(r"reference|\breferr|emergency|manager|supervisor|company|employer|school|"
                     r"universit|recruiter|hiring|spouse|parent|guardian", re.I)
_CONTACT: list[tuple[str, re.Pattern]] = [
    ("current_company", re.compile(r"^\W*(current|present|most recent|latest) (company|employer|organi[sz]ation)( name)?\W*$", re.I)),
    ("current_title", re.compile(r"^\W*(current|present|most recent|latest) (job )?(title|position|role)\W*$", re.I)),
    ("email", re.compile(r"e-?mail", re.I)),
    ("first_name", re.compile(r"first name|given name|forename|^\W*first\W*$", re.I)),
    ("last_name", re.compile(r"last name|surname|family name|^\W*last\W*$", re.I)),
    ("full_name", re.compile(r"^\W*(full |legal |your )*name\W*$", re.I)),
    # never the KIND of phone ("Phone Device Type") or its extension: those are not the number
    ("phone", re.compile(r"^(?!.*\b(device type|phone type|telephone type|number type|type of (phone|telephone|device|number)"
                         r"|extension|ext)\b).*(phone|mobile|telephone|\bcell\b)", re.I)),
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
    if not lbl:
        return ""
    for key, rx in _CONTACT[:2]:                  # the person's own current job ("Current company")
        if rx.search(lbl):
            return key
    if _NOT_ME.search(lbl):
        return ""
    for key, rx in _CONTACT[2:]:
        if rx.search(lbl):
            return key
    return ""


_EEO = re.compile(r"gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation"
                  r"|transgender|lgbt|queer|pronoun|first[- ]generation (professional|college|student)", re.I)
# The "decline to self-identify" choice in its usual wordings (autoapply/eeo.py, where the
# self-identification matching lives). Mirrored in shell-electron/agent.js DECLINE_RE.
_DECLINE = eeo.DECLINE
decline_option = eeo.decline_option

# Country questions: a saved country is matched to the form's own list by name and alias.
_COUNTRY_KEYS = frozenset({"@citizenship", "@citizenship2"})
# "...country of citizenship (if you are a citizen of more than one, list each)": every country.
_EACH = re.compile(r"more than one|list (each|all|them)|all (countries|citizenships)|each (country|citizenship)"
                   r"|dual|multiple", re.I)


def _country_answer(library, question: str) -> str:
    """The saved country for a citizenship question, by the list's own name; "" when nothing is
    saved or the saved words are not a country."""
    try:
        hit = library.lookup(question) if library is not None else None
    except Exception:          # noqa: BLE001 - nothing saved then
        hit = None
    return countries.canonical(str((hit or {}).get("answer") or "").strip())


def citizenship_text(label: str, library, first: str) -> str:
    """The answer to a free-text citizenship question: the saved country, and the second one as
    well when the question asks for each ("Ghana, Canada"). Only a second answer that is a
    country counts (never a "Yes" to a "Dual citizenship?" question)."""
    first = str(first or "").strip()
    if not first or library is None or not _EACH.search(str(label or "")):
        return first
    second = _country_answer(library, "Second country of citizenship")
    return f"{first}, {second}" if second and second.lower() != first.lower() else first


# A question that asks about permanent residence too ("country/region of citizenship or legal
# permanent residence"): the country of citizenship alone may not be its answer.
_RESIDENCE = re.compile(r"permanent resid|lawful permanent|legal permanent|green card|residen(ce|cy)", re.I)


def residence_withheld(label: str, work_auth: dict | None) -> bool:
    """True when a saved country of citizenship must not answer this question: it also asks
    for a permanent residence, and the person holds one somewhere (a green card, settled
    status...). Lam's export-control question wants the MOST RECENT of the two; only the person
    knows which that is."""
    lbl = str(label or "")
    if normalize(lbl) != "@citizenship" or not _RESIDENCE.search(lbl):
        return False
    for e in ((work_auth or {}).get("countries") or {}).values():
        if isinstance(e, dict) and e.get("status") in workauth.PERMANENT:
            return True
    return False


_CITIZEN_OF = re.compile(r"\bcitizen|nationality|\bnational of\b|\bnationals?\b", re.I)
# Beyond citizenship: residence, birth, travel. A "No" to one of those is not the person's to give.
_BEYOND = re.compile(r"resid|green card|born|birth|travel|visit|located|live[ds]? in|ordinarily|employ", re.I)


def listed_citizenship(question: str, library, options=(), field_type: str = "text") -> str | None:
    """The answer to a sanctions or export screen that names countries ("Are you a citizen of
    Cuba, Iran, North Korea, Syria, or the Crimea region of Ukraine?"), from the countries of
    citizenship the person saved: Yes when one of them is named; No only when their saved
    countries are all known, none is named, and the question asks about citizenship alone.
    Otherwise None (theirs to answer). Never from the work-authorization profile, which is
    about the job's country (workauth.classify leaves these out)."""
    q = str(question or "")
    if workauth.classify(q):
        return None                                       # the job's country: the profile answers
    named = countries.named_in(q)
    if workauth._SCREENING.search(q):
        named = [n for n in named if n != "United States"]     # the country imposing the embargo
    if not named or not _CITIZEN_OF.search(q) or library is None:
        return None
    first = _country_answer(library, "Country of citizenship")
    if not first:
        return None
    mine = {first}
    try:
        raw2 = str((library.lookup("Second country of citizenship") or {}).get("answer") or "").strip()
    except Exception:          # noqa: BLE001 - treated as unknown
        raw2 = "?"
    if raw2:
        second = countries.canonical(raw2)
        if not second:
            return None                                   # a second answer we cannot read: ask
        mine.add(second)
    if mine & set(named):
        yes = True
    elif _BEYOND.search(q):
        return None
    else:
        yes = False
    opts = [str(o) for o in (options or []) if str(o).strip()]
    t = str(field_type or "text").lower()
    if t == "checkbox":
        return "Yes" if yes else None
    if opts or t in ("select", "radio"):
        return workauth.pick_yes_no(yes, opts)
    return "Yes" if yes else "No"


_RESUME = re.compile(r"r[ée]sum[ée]|\bcv\b|curriculum", re.I)
_LETTER = re.compile(r"cover\s*letter|letter of (interest|motivation)|motivation(al)? letter", re.I)

# "Have you ever, or are you currently working at Gusto or Symmetry in any capacity?": a worked-here
# question that names the company instead of saying "this company". Never one about someone else
# ("Do you have a relative working at ...", "Do you know anyone who works at ...").
_WORKED_AT = re.compile(r"\b(work(ed|ing)?|been employed|employed|interned|contract(ed|ing))\b[^?]{0,40}?"
                        r"\b(at|for|by|with)\s+(?P<who>[^?]{2,80})", re.I)
# (2026-11-17, Phoenix Contact on Greenhouse) The "employee" wordings: "Are you a current or former
# Phoenix Contact employee?", "Phoenix Contact employee (current or former)", "Are you a former
# employee of Acme?".
_WHEN = r"(?:current|former|past|previous|prior|ex)"
_EMPLOYEE_OF = re.compile(
    r"\b" + _WHEN + r"(?:\s*(?:or|/|and)\s*" + _WHEN + r")?[\s-]+(?P<who>[^?()]{2,80}?)\s+"
    r"(?:employee|employment|contractor|intern|staff member|team member)s?\b"
    r"|^\W*(?P<who2>[^?()]{2,80}?)\s+(?:employee|contractor|intern)s?\s*\(\s*" + _WHEN
    + r"|\b" + _WHEN + r"(?:\s*(?:or|/|and)\s*" + _WHEN + r")?\s+(?:employee|contractor|intern)s?\s+(?:of|at|with|for)\s+"
    r"(?P<who3>[^?()]{2,80})", re.I)
_SOMEONE_ELSE = re.compile(r"relative|related|family|friend|spouse|anyone|someone|know|refer|partner", re.I)
_CO_SUFFIX = re.compile(r"\b(inc|incorporated|llc|l l c|llp|l l p|pllc|lp|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa"
                        r"|group|holdings?|technologies|labs|usa|u s a|north america|americas|international|global)\b", re.I)
# "Why do you want to work at Acme?", "Would you like to work for Acme?": a wish, not a history.
_WISH = re.compile(r"\bwhy\b|\b(want|like|love|wish|hope|plan|excited|eager|interested in|choose|chose)\b[^?]{0,12}"
                   r"\b(to )?(work|join|working)\b", re.I)
# A question with no "you" that still asks the person ("Previously employed by Acme?").
_LEADS = re.compile(r"^\W*(previously|formerly|former|current|past|prior|ever|employed|worked|have|are|were)\b", re.I)


def _company_core(company: str) -> str:
    return _n(_CO_SUFFIX.sub(" ", _n(company)))


def names_company(label: str, company: str) -> bool:
    """True when ``label`` names ``company`` (its name without the Inc / LLC / USA) and is not
    about someone else (a relative, a referral)."""
    core = _company_core(company)
    return len(core) >= 3 and not _SOMEONE_ELSE.search(str(label or "")) \
        and bool(re.search(r"\b" + re.escape(core) + r"\b", _n(label)))


# "Have you worked for Acme or any of our companies in the past?" (a Workday form, 2026-11-18):
# "our companies" is the employer asking about itself and its group, whatever name the record holds.
_OURS = re.compile(r"\b(our|its|their) (family of |group of |affiliated |related |subsidiary |parent )?"
                   r"(compan(y|ies)|affiliates?|subsidiar(y|ies)|brands|entities|businesses|banks|firms?)\b", re.I)
# A first word that names no one ("First National Bank", "American Express"): never a match alone.
_COMMON_FIRST = frozenset("first american national general united new the bank north south east west global "
                          "international advanced applied pacific atlantic central capital".split())


def _names(core: str, who: str) -> bool:
    """Does ``who`` (the words after "worked for") name the company ``core``: its whole name, or
    its distinctive first words the way a form shortens it ("Acme" for "Acme Financial")."""
    w, parts = _n(who), core.split()
    if not core or not w:
        return False
    for n in range(len(parts), 0, -1):
        head = " ".join(parts[:n])
        if n == 1 and (len(head) < 4 or head in _COMMON_FIRST) and len(parts) > 1:
            break
        if n < len(parts) and len(head) < 4:
            break
        if re.search(r"\b" + re.escape(head) + r"\b", w):
            return True
    return False


def worked_here_question(label: str, company: str) -> bool:
    """True when ``label`` asks whether the person works or worked at ``company`` by its name:
    "Have you ever worked at Gusto", "Are you a current or former Phoenix Contact employee?",
    "Previously employed by Acme?", "Have you worked for Acme before?"; by its short name ("Acme"
    for a record that says "Acme Financial Corporation"); or as the employer's own group ("Have
    you worked for Acme or any of our companies in the past?"). Never a question about someone
    else ("Are you related to a current Acme employee?")."""
    lbl, core = str(label or ""), _company_core(company)
    if len(core) < 3 or _SOMEONE_ELSE.search(lbl) or _WISH.search(lbl):
        return False
    has = lambda who: bool(who) and (_names(core, who) or bool(_OURS.search(who)))  # noqa: E731
    for m in _EMPLOYEE_OF.finditer(lbl):
        if has(m.group("who") or m.group("who2") or m.group("who3")):
            return True
    if not (re.search(r"\b(have|are|were|did|do) you\b", lbl, re.I) or _LEADS.search(lbl)):
        return False
    return any(has(m.group("who")) for m in _WORKED_AT.finditer(lbl))


def asks_worked_here(label: str, company: str, confirm=None) -> bool:
    """"Have you worked here before?" for THIS company: a wording the rule knows, or one the label
    mapper matched to @previously_worked that names the company (then the person's answer for
    the company, or their work history, answers it without a one-tap confirm)."""
    if worked_here_question(label, company):
        return True
    return (confirm or {}).get(label) == "@previously_worked" and names_company(label, company)


# "Do you currently reside in one of the following locations?" with places as the choices
# ("Yes - Denver, CO", "Yes - New York, NY", "No - None of the above").
_RESIDE_Q = re.compile(r"\b(reside|live|living|located|based)\b.{0,40}\b(following|these|listed|any of|one of)\b", re.I)
_NONE_OF = re.compile(r"\bnone of (the|these)\b|^no\b|\bnot in (any|one)\b|\bother location\b")


def residence_option(label: str, options, applicant: dict) -> str | None:
    """The choice that says where the person lives, from the profile's city and state: the option
    naming their city, else the one "No / None of the above" when no option names their city or
    their state. None when the profile has no city or the choices are unclear (theirs to answer)."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if not opts or not _RESIDE_Q.search(str(label or "")):
        return None
    city = _n((applicant or {}).get("city") or "")
    state = _n((applicant or {}).get("state") or "")
    if not city:
        return None
    named = [o for o in opts if re.search(r"\b" + re.escape(city) + r"\b", _n(o)) and workauth.polarity(o) is not False]
    if named:
        return named[0] if len(named) == 1 else None
    if state and any(re.search(r"\b" + re.escape(state) + r"\b", _n(o)) for o in opts):
        return None                                   # their state is listed: only they know
    no = [o for o in opts if _NONE_OF.search(_n(o))]
    return no[0] if len(no) == 1 else None


# Where the person LIVES, in any wording: "Where in the US do you currently reside?" (Upgrade,
# Greenhouse, 2026-11-17), "Where are you located?", "Current location", "Which state do you live
# in?", "City and state of residence". It is on the profile, so it is never the person's to type
# again. Not an office pick (_OFFICE_Q), not "one of the following locations" (residence_option),
# never relocation, a willingness, a permanent residence status or a work location.
_LIVE_Q = re.compile(
    r"\bwhere\b.{0,30}\b(reside|live|living|located|based)\b"
    r"|\b(which|what) (city|state|metro|area|region|country)\b.{0,30}\b(reside|live|living|located|based)\b"
    r"|\bcurrent(ly)? (location|residence|city|state|address)\b"
    r"|\b(city|state|location),? (and|&|/) (state|country)\b.{0,20}\b(residence|you (live|reside))\b"
    r"|\b(city|state|place) of residence\b|\bresidential location\b", re.I)
_NOT_LIVE = re.compile(r"relocat|willing|open to|permanent resid|green card|citizen|visa|work (from|location)"
                       r"|office|applying|commut|travel|prefer", re.I)


def _state_codes(state: str) -> tuple[str, str]:
    """(full name, two-letter code) for a US state given either form, lowercased; ("", "") if unknown."""
    from tailoring.conform import _US_STATES
    st = _n(state)
    if not st:
        return "", ""
    for full, code in _US_STATES.items():
        if st == full or st == code.lower():
            return full, code.lower()
    return st, ""


def where_you_live(label: str, options, applicant: dict, field_type: str = "") -> str | None:
    """The answer to a question about where the person lives, from the profile's city and state.
    Free text: "City, ST". A list: the one choice naming their city, else the one naming their
    state (full name or code), else None (the choices are unclear: theirs). None when the label is
    not about where they live or the profile has no city."""
    lbl = str(label or "")
    if not _LIVE_Q.search(lbl) or _NOT_LIVE.search(lbl) or _RESIDE_Q.search(lbl):
        return None
    city_raw = str((applicant or {}).get("city") or "").strip()
    if not city_raw:
        return None
    full, code = _state_codes(str((applicant or {}).get("state") or ""))
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if not opts:
        if field_type in ("select", "radio", "checkbox", "combobox"):
            return None                                   # a list we could not read: theirs
        st = code.upper() if code else str((applicant or {}).get("state") or "").strip()
        return f"{city_raw}, {st}" if st else city_raw
    city = _n(city_raw)
    hit = [o for o in opts if re.search(r"\b" + re.escape(city) + r"\b", _n(o))]
    if len(hit) == 1:
        return hit[0]
    if hit:
        return None
    hit = [o for o in opts if (full and re.search(r"\b" + re.escape(full) + r"\b", _n(o)))
           or (code and re.search(r"(^|[\s,(-])" + re.escape(code) + r"($|[\s,)-])", _n(o)))]
    return hit[0] if len(hit) == 1 else None


# "Which location are you applying for?", "...a hybrid schedule from one of the locations below?
# If so, please select one." (Lever at CrossCountry, 2026-11-16): an office pick, answered from the
# profile's city when one choice names it.
_OFFICE_Q = re.compile(
    r"\bwhich (office|location|city|site)\b|\blocation (are you|you are|you're) applying\b"
    r"|\bpreferred (office|work )?location\b|\b(office|location) (preference|you would like)\b"
    r"|\bfrom one of (the|these|our) (locations|offices|cities|sites)\b"
    r"|\bselect (one|a|your)( preferred)? (location|office)\b", re.I)
_CITY_ABBR = {"new york": ("nyc", "ny"), "washington": ("dc", "d c"), "los angeles": ("la",),
              "san francisco": ("sf",)}


def office_option(label: str, options, applicant: dict) -> str | None:
    """The office choice that names the person's own city (profile), else the one "None of these"
    choice when the question offers it and no choice names their city. None when the profile has
    no city or the choices are unclear (theirs to answer)."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    if not opts or not _OFFICE_Q.search(str(label or "")):
        return None
    city = _n((applicant or {}).get("city") or "")
    if not city:
        return None
    names = (city,) + _CITY_ABBR.get(city, ())
    named = [o for o in opts if any(re.search(r"\b" + re.escape(c) + r"\b", _n(o)) for c in names)
             and not _NONE_OF.search(_n(o))]
    if len(named) == 1:
        return named[0]
    if named:
        return None
    no = [o for o in opts if _NONE_OF.search(_n(o))]
    return no[0] if len(no) == 1 else None


# A free-text box asking for the visa or sponsorship details ("please specify your current visa
# type and any future sponsorship needs"): the profile's one-sentence status, workauth.note.
_VISA_DETAIL = re.compile(
    r"\b(specify|describe|explain|details?|provide|tell us|elaborate|list)\b.{0,80}\b(visa|sponsorship|immigration|work (authori[sz]ation|permit|status))\b"
    r"|\b(visa|sponsorship|immigration)\b.{0,60}\b(details?|specify|explain|describe)\b", re.I)


def visa_detail_text(label: str, t: str, wa: dict | None, job_country: str = "US") -> str | None:
    """The person's own status sentence for a visa-details text box, else None."""
    if (t or "text").lower() not in ("text", "textarea") or not _VISA_DETAIL.search(str(label or "")):
        return None
    return workauth.note(wa, job_country) or None


# --------------------------------------------------------------------------- option matching
def _n(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#$ ]+", " ", str(s or "").lower())).strip()


# Words that add nothing to a choice: "Online job board" says what "Job board" says, and
# "Bachelor's degree" what "Bachelor" says. Any other word an option has beyond the answer's is a
# qualifier the answer did not give ("College" in "College Job Board"). Mirrored in
# extension/autofill_core.js NEUTRAL_WORDS and extension/agent_view.js.
_NEUTRAL = frozenset("a an the and or s job jobs online internet web website websites site sites posting postings "
                     "general etc eg ie degree".split())

# "How did you hear about us?": choices that name no particular source, best first. A saved
# answer no choice says the same as lands on one of these, never on a choice that adds a source
# the person did not give ("College Job Board" for "Job board", Phoenix Contact on Greenhouse,
# 2026-11-05). The job board ones only for an answer that is itself a board or the web.
_GENERIC_BOARD = re.compile(r"^((online|internet|web) )?job (board|site|website|posting)s?( (site|website))?$"
                            r"|^(online|internet|web) (job )?(board|site|posting|search)s?$|^(the )?(internet|online|web|websites?)$")
# The catch-all board of a list that names boards one by one ("Job Board: Indeed", ..., "Other Job
# Board"; a Workday form, 2026-11-18): "other" names no source, so it is generic too, after a plain one.
_OTHER_BOARD = re.compile(r"^other ((online|internet|web) )?job (board|site|website|posting)s?$"
                          r"|^((online|internet|web) )?job (board|site|website|posting)s? other$")
_GENERIC_OTHER = re.compile(r"^other( source| sources| please specify)?$")
# Job boards by name, the general ones first (generic_source's last resort for a "Job board" answer).
_NAMED_BOARDS = ("indeed", "glassdoor", "ziprecruiter", "monster", "careerbuilder", "simplyhired", "dice",
                 "handshake", "wellfound", "angellist", "built in", "linkedin", "linkedin jobs")
_BOARDISH = frozenset("job jobs board boards online internet web website websites site sites posting postings "
                      "search engine".split())


def adds_qualifier(answer: str, option: str) -> bool:
    """Does ``option`` carry a word ``answer`` does not, other than a neutral one? ("College Job
    Board" for "Job board": yes. "Online job board": no.)"""
    have = set(_n(answer).split())
    return any(w not in have and w not in _NEUTRAL for w in _n(option).split())


def generic_sources(answer: str, options) -> list:
    """For a how-did-you-hear question: the choices that name no particular source, best first,
    when no choice says the same as the saved answer. For an answer that is itself a job board
    or the web: 1. a generic job board or "Internet" choice ("Job board", "Job Sites"), 2. the
    list's catch-all board ("Other Job Board"), 3. "Other", 4. a general job board the list
    names by its exact name (never "Built In Colorado"). For any other answer: "Other" only.
    Never a choice that adds a source the person did not give ("College Job Board"). Mirrored in
    extension/autofill_core.js genericSources and extension/agent_view.js."""
    opts = [str(o) for o in (options or []) if str(o).strip()]
    words = set(_n(answer).split())
    boardish = bool(words) and words <= _BOARDISH
    out = []
    if boardish:
        out += [o for o in opts if _GENERIC_BOARD.match(_n(o))]
        out += [o for o in opts if _OTHER_BOARD.match(_n(o))]
    out += [o for o in opts if _GENERIC_OTHER.match(_n(o))]
    if boardish:
        # (2026-11-16, Gusto on Greenhouse: LinkedIn, Glassdoor, Indeed, Facebook, ... and no "Job
        # board" or "Other") A job-board answer on a list of named sources only: the closest is a
        # job board the list names, the general boards first.
        for board in _NAMED_BOARDS:
            out += [o for o in opts if _n(o) == board]
    seen, uniq = set(), []
    for o in out:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq


def generic_source(answer: str, options) -> str | None:
    """The first of generic_sources, None when the list has none (the person is asked)."""
    got = generic_sources(answer, options)
    return got[0] if got else None


def match_option(answer: str, options, *, strict: bool = False) -> str | None:
    """The option a saved text answer names: equal after normalizing, else the one option the
    answer and the option share as a prefix; a Yes/No answer picks the option that says it.
    None when no single option fits (the field is then left for the person).

    ``strict`` (a SAVED answer meeting the page's choices, literal_for): a shared prefix counts
    only when the option adds no qualifier ("Bachelor" is "Bachelor's degree"; "Job board" is
    not "Job board for students"), and an option that holds the answer's words plus only
    neutral ones counts too ("Online job board"). Never an option that adds a qualifier the
    answer did not have, the way "College Job Board" was picked for "Job board"."""
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
    if strict:
        pre = [o for o in pre if not adds_qualifier(a, o)]
    if len(pre) == 1:
        return pre[0]
    if strict and not pre:
        same = [o for o in opts if set(a.split()) <= set(_n(o).split()) and not adds_qualifier(a, o)]
        if len(same) == 1:
            return same[0]
    return None


def eeo_option(answer: str, options, key: str = "", answers: dict | None = None, question: str = "") -> str | None:
    """The option a saved self-identification answer names: the same words, else (for a known
    question ``key``) the option that makes the same choice in the form's own words ("Male" is
    "Man", "No" is "No, I do not have a disability..."; autoapply/eeo.py), else the page's own
    decline choice for a saved decline ("I don't wish to answer" is Workday's "I do not wish to
    answer" and Lever's "Decline to self-identify"). Never the Yes/No or shared-prefix guesses of
    match_option: a plain "Yes" is not "I identify as a protected veteran", and "Yes, I have a
    disability" is not "Yes, I need an accommodation". ``answers`` ({key: answer}) carries the
    other self-identification answers a combined race and ethnicity list needs, and
    ``question`` the words of the question (a plain No only answers "Are you a protected
    veteran?")."""
    got = dict(answers or {})
    if key and str(answer or "").strip():
        got[key] = str(answer).strip()
    if key:
        return eeo.choose(key, options, got, question=question)
    opts = [str(o) for o in (options or []) if str(o).strip()]
    a = _n(answer)
    if not opts or not a:
        return None
    eq = [o for o in opts if _n(o) == a]
    if eq:
        return eq[0]
    if _DECLINE.search(str(answer)):
        return decline_option(opts)
    return None


def literal_for(key: str, text: str, options) -> str | None:
    """A saved answer on a choice field: a country by name or alias on a country question,
    else the option that says the same (match_option, strict: never one that adds a qualifier),
    else, on a how-did-you-hear question, the choice that names no particular source
    (generic_source). None leaves the field for the person."""
    if key in _COUNTRY_KEYS:
        v = country_option(text, options)
        if v:
            return v
    v = match_option(text, options, strict=True)
    if v is None and key == "@heard_about":
        v = generic_source(text, options)
    return v


def _op_for(f: FormField) -> str:
    t = (f.type or "text").lower()
    if t in ("select", "radio"):
        return "select"
    if t == "checkbox":
        return "check"
    return "fill"


def _literal(f: FormField, text: str, key: str = "") -> str | None:
    """Fit a known answer to this field: an exact option for a choice field (a country by name
    or alias on a country question), Yes for a checkbox the answer affirms, the text itself for a
    text box."""
    text = str(text or "").strip()
    if not text:
        return None
    t = (f.type or "text").lower()
    if f.options or t in ("select", "radio"):
        return literal_for(key, text, f.options)
    if t == "checkbox":
        return "Yes" if workauth.polarity(text) is True else None
    return text


def _used(key: str, rows: dict) -> list:
    """The library rows an EEO choice came from (both halves for a race or ethnicity list)."""
    keys = eeo.RACE_KEYS if key in eeo.RACE_KEYS else (key,)
    return [rows[k] for k in keys if k in rows]


def _saved_for(library, question: str, options) -> tuple:
    """eeo.saved for a question, or, when its words name no self-identification topic, for the
    topic its CHOICES name. The federal disability form (CC-305) asks "Please check one of the
    boxes below:" over "Yes, I have a disability...", "No, I do not have a disability..." and "I do
    not want to answer": the saved disability answer picks the box (Uline on Workday, 2026-11-16)."""
    key, answers, rows = eeo.saved(library, question)
    if key in eeo.KEYS:
        return key, answers, rows
    topics = {eeo.topic_key(str(o)) for o in (options or []) if str(o).strip()} & set(eeo.KEYS)
    if len(topics) == 1:
        tk = topics.pop()
        _k, answers, rows = eeo.saved(library, eeo.QUESTIONS[tk])
        return tk, answers, rows
    return key, answers, rows


def _eeo_fill(f: FormField, label: str, t: str, ctx, groups: dict | None = None) -> tuple | None:
    """(FillAction, [library keys]) for a self-identification field from the person's standing
    decline or their own saved answers, else None (the field is theirs). Never a guess. The
    standing "decline to self-identify" choice comes first and ALONE: while it is on, a saved
    answer is never used. A field with no decline choice (or whose choices are not read yet) is
    left open, and the agent's ask, which reads the page's choices, picks the decline there."""
    if t == "checkbox":
        # One box of a native group, labelled "Question: Option" (the engine's checkboxLabel):
        # the group's ONE chosen option is worked out from all its boxes, and only that box is
        # ticked. Never from the answer's Yes/No polarity, which would tick every box at once.
        q, sep, opt = label.partition(": ")
        if not sep or not opt.strip():
            return None
        group = list((groups or {}).get(q) or [opt])
        if ctx.eeo_decline:
            v = decline_option(group)
            return (FillAction(f.ref, "check", value="Yes", origin="eeo", sensitive=True), []) if v and v == opt else None
        key, answers, rows = _saved_for(ctx.library, q, group)
        v = eeo_option(answers.get(key, ""), group, key, answers, question=q) if answers else None
        if v and v == opt:
            return FillAction(f.ref, "check", value="Yes", origin="library", sensitive=True), _used(key, rows)
        if not v and decline_option(group) == opt:          # nothing saved fits: the decline
            return FillAction(f.ref, "check", value="Yes", origin="eeo_decline", sensitive=True), []
        return None
    if f.options or t in ("select", "radio"):
        if ctx.eeo_decline:
            v = decline_option(f.options)
            return (FillAction(f.ref, _op_for(f), value=v, origin="eeo", sensitive=True), []) if v else None
        key, answers, rows = _saved_for(ctx.library, label, f.options)
        v = eeo_option(answers.get(key, ""), f.options, key, answers, question=label) if answers else None
        if v:
            return FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True), _used(key, rows)
        # (2026-11-16, Gusto on Greenhouse) Nothing saved fits this question (one with no saved
        # answer, such as a first-generation college student, or a saved answer none of the
        # choices says; transgender, sexual orientation and first-generation professional are
        # saved on the memory page and answered above like the other five):
        # self-identification is voluntary, so the page's own decline choice, never a question
        # per application. The Ready to send summary says it was declined.
        v = decline_option(f.options)
        return (FillAction(f.ref, _op_for(f), value=v, origin="eeo_decline", sensitive=True), []) if v else None
    # No choices read (a text box, or a dropdown whose list opens later)
    if ctx.eeo_decline:
        return None
    hit = ctx.library.lookup(label) if ctx.library is not None else None
    ans = str((hit or {}).get("answer") or "").strip()
    if not ans:
        return None
    if normalize(label) in ("@race", "@ethnicity") and not both_halves(ans):
        # "Race/Ethnicity" or Workday's combined "Ethnicity" list: a bare "Asian" or "No" says
        # one half only. Left open; the ask reads the page's choices and takes both halves.
        return None
    return FillAction(f.ref, "fill", value=ans, origin="library", sensitive=True), [hit["key"]]


def both_halves(words: str) -> bool:
    """True when saved words state a race and the Hispanic or Latino answer together ("Asian
    (Not Hispanic or Latino)"), or a decline that covers both."""
    w = str(words or "")
    if _DECLINE.search(w):
        return True
    return bool(eeo.race_class(w)) and eeo.hispanic_class(w, bare=False) is not None


def _checkbox_groups(fields: list) -> dict:
    """{question: [option, ...]} for the checkbox groups a form lists as "Question: Option"."""
    out: dict = {}
    for f in fields:
        if (f.type or "").lower() == "checkbox":
            q, sep, opt = (f.label or "").partition(": ")
            if sep and opt.strip():
                out.setdefault(q, []).append(opt)
    return out


# --------------------------------------------------------------------------- the known pass
@dataclass
class KnownContext:
    """Everything the deterministic pass may answer from. All local; none of it reaches the model."""

    work_auth: dict = field(default_factory=dict)
    library: object = None                       # an AnswerLibrary (or anything with .lookup)
    drafted: list = field(default_factory=list)  # [{"question","answer"}] for THIS application
    cover_letter: str = ""
    cover_letter_path: str = ""                  # the drafted cover letter as a PDF, for a file box
    applicant: dict = field(default_factory=dict)
    resume_path: str = ""
    job_country: str = "US"
    company: str = ""
    eeo_decline: bool = False                    # pick "Decline to self-identify" on EEO questions
    # (Phase 3, docs/applicant-brain.md) With the applicant brain on, "Have you worked here
    # before?" is answered per company (``worked_here``: ("Yes"|"No", origin) or None to ask),
    # never from an answer given for another employer; and a key in ``hold`` (a salary
    # conflict not asked yet) is left open on a REQUIRED field, so the person is asked once.
    per_company: bool = False
    worked_here: tuple | None = None
    hold: frozenset = frozenset()
    # (Phase 4) New wordings and expiry (applicant/mapper.py, applicant/expiry.py), brain on only:
    # ``mapped`` {label: key} answers a wording no rule knows from that key; ``confirm`` {label:
    # key} is a legal or self-identification mapping the person has not confirmed yet (left
    # open, asked once); ``withhold`` keys are never filled (a start date that passed, a salary
    # in the wrong currency for this job); ``job_salary`` is the person's salary for a job
    # outside the US; ``wa_ended`` the date the job country's time-limited status ended.
    mapped: dict = field(default_factory=dict)
    confirm: dict = field(default_factory=dict)
    withhold: frozenset = frozenset()
    job_salary: str = ""
    wa_ended: object = None
    # (2026-11-17) The person's standing consent to the notices a site requires (Your answers'
    # accept_required_terms "always"): an acknowledgement question gets its affirmative choice
    # (autoapply/consents.py). Marketing opt-ins are declined unless @marketing_optin says yes.
    accept_notices: bool = False
    # (2026-11-17, autoapply/commonsense.py) ``adult``: the saved profile proves the person is
    # at least 18 (a degree, or a job or school date 4 years back); ``applied_here``: ("Yes" | "No",
    # "tracker") for "have you applied to this company before?" from the person's own records,
    # None when they could not be read (then it stays a question).
    adult: bool = False
    applied_here: tuple | None = None
    # (2026-11-18) ``education``: the highest degree level the saved profile shows as completed
    # ("masters", "bachelors", ...; commonsense.highest_education), "" when it shows none.
    education: str = ""


@dataclass
class KnownResult:
    actions: list = field(default_factory=list)  # FillActions decided without the model
    rest: list = field(default_factory=list)     # FormFields still open (the model may plan these)
    left: list = field(default_factory=list)     # FormFields only the person can answer
    used_library: list = field(default_factory=list)   # library keys used (to mark last used)
    drafted_used: list = field(default_factory=list)   # (question, answer) drafted answers used


def _newer(hit: dict | None, ended) -> bool:
    """True when a saved answer was given after the date a status ended (it is still theirs)."""
    import datetime as _dt
    try:
        return _dt.date.fromtimestamp(float((hit or {}).get("updated_at") or 0)) > ended
    except (TypeError, ValueError, OSError):
        return False


def _ended_for(label: str, ctx: KnownContext):
    """The end date that makes this work-authorization question unsafe to fill, or None."""
    if ctx.wa_ended is None:
        return None
    from applicant.expiry import expired_kind
    return ctx.wa_ended if expired_kind(label, ctx.wa_ended) else None


def keyed(ctx: KnownContext, key: str, hit: dict | None) -> dict | None:
    """The saved row a key may fill from: the person's salary for this country on a job
    outside the US; nothing for a withheld key; else the library's own row."""
    if key == "@salary" and ctx.job_salary:
        return {"answer": ctx.job_salary, "key": "", "updated_at": None}
    if key in ctx.withhold:
        return None
    return hit


def mapped_value(key: str, f: FormField, ctx: KnownContext) -> tuple | None:
    """(value, origin, [library keys]) for a field whose wording the label mapper matched to
    ``key`` (applicant/mapper.py), by the same rules the key's own wording gets. None when the
    key holds nothing usable here (the field is then the person's)."""
    label, t = f.label or "", (f.type or "text").lower()
    if key.startswith("workauth:"):
        kind = key.split(":", 1)[1]
        from applicant.expiry import EXPIRED_KINDS
        if ctx.wa_ended is not None and kind in EXPIRED_KINDS:
            return None
        v = workauth.answer(label, ctx.work_auth, options=f.options, field_type=t, job_country=ctx.job_country,
                            kind=kind)
        return (v, "workauth", []) if v else None
    if key in eeo.KEYS:
        if ctx.eeo_decline:
            v = decline_option(f.options) if f.options else None
            return (v, "eeo", []) if v else None
        _k, answers, rows = eeo.saved(ctx.library)
        own = answers.get(key, "")
        if not own:
            return None
        if f.options or t in ("select", "radio"):
            v = eeo_option(own, f.options, key, answers, question=label)
            return (v, "library", _used(key, rows)) if v else None
        if t == "checkbox" or (key in ("@race", "@ethnicity") and not both_halves(own)):
            return None
        return own, "library", _used(key, rows)
    if key == "@previously_worked" and ctx.per_company:
        wh = ctx.worked_here
        if not wh or wh[1] == "confirm":
            return None
        v = _literal(f, wh[0])
        return (v, wh[1] or "library", []) if v else None
    if key in ctx.hold and f.required:
        return None
    hit = keyed(ctx, key, ctx.library.best(key) if ctx.library is not None else None)
    if not hit:
        return None
    v = _literal(f, hit["answer"], key)
    return (v, "library", [hit["key"]] if hit.get("key") else []) if v else None


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
    groups = _checkbox_groups(fields)
    for f in fields:
        label = f.label or ""
        t = (f.type or "text").lower()

        if t == "file":                                    # the tailored resume, nothing else
            if _LETTER.search(label):
                # (2026-11-16) a cover letter box gets the cover letter drafted for this job, never
                # the resume; with none, an optional box stays empty (never asked), a required one
                # is the person's.
                if ctx.cover_letter_path:
                    out.actions.append(FillAction(f.ref, "upload", origin="letter"))
                elif f.required:
                    out.left.append(f)
            elif _RESUME.search(label) and ctx.resume_path:
                out.actions.append(FillAction(f.ref, "upload", origin="resume"))
            else:
                out.rest.append(f)
            continue

        # 0. EEO / demographics: the person's standing "decline" choice, else their own saved
        #    answers (Your answers, or an earlier "ask") on the form's own wording, never a guess
        # (a box of a checkbox group is a self-identification question when any of the group's
        #  choices is: CC-305's "I do not want to answer" sits beside "Yes, I have a disability")
        cq, csep, _co = label.partition(": ")
        eeo_group = t == "checkbox" and csep and any(_EEO.search(o) for o in (groups.get(cq) or []))
        if (_EEO.search(label) or eeo_group) and not workauth.classify(label):
            got = _eeo_fill(f, label, t, ctx, groups)
            if got:
                out.actions.append(got[0])
                out.used_library.extend(k for k in got[1] if k not in out.used_library)
            else:
                out.left.append(f)
            continue

        # 0b. a sanctions or export screen naming countries: from the saved citizenships only
        v = listed_citizenship(label, ctx.library, f.options, t)
        if v:
            out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True))
            continue

        # 0c. an office / location pick ("Which location are you applying for?", "a hybrid schedule
        #     from one of the locations below?"): the profile's city, before the work-authorization
        #     rules, which read "work ... from one of the locations" as a relocation question
        v = office_option(label, f.options, ctx.applicant)
        if v:
            out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="profile", sensitive=True))
            continue

        # 1. the visa / sponsorship DETAILS box (text that asks to specify or explain): the
        #    profile's own status sentence, never a bare Yes or No typed into a text box
        v = visa_detail_text(label, t, ctx.work_auth, ctx.job_country)
        if v:
            out.actions.append(FillAction(f.ref, "fill", value=v, origin="workauth", sensitive=True))
            continue

        # 1. work authorization
        if workauth.classify(label):
            # (Phase 4) a status whose end date passed (OPT, an EAD) no longer answers it
            ended = _ended_for(label, ctx)
            v = None if ended else workauth.answer(label, ctx.work_auth, options=f.options, field_type=t,
                                                   job_country=ctx.job_country)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="workauth", sensitive=True))
                continue
            # The profile does not say, but the person answered this exact question before (on
            # the agent's bar, or in Your answers): their own words, not a guess.
            hit = ctx.library.lookup(label) if ctx.library is not None else None
            if ended and not _newer(hit, ended):
                hit = None                                 # answered before the status ended
            v = _literal(f, hit["answer"]) if hit else None
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True))
                out.used_library.append(hit["key"])
            else:
                out.left.append(f)                         # theirs to answer
            continue

        # 1a. "Do you currently reside in one of the following locations?": the profile's city;
        #     any other question about where they live: the profile's city and state
        v = residence_option(label, f.options, ctx.applicant) or where_you_live(label, f.options, ctx.applicant, t)
        if v:
            out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="profile", sensitive=True))
            continue

        # 1a'. (2026-11-17, autoapply/commonsense.py) "Have you applied to / interviewed with this
        #      company before?": the person's own answer to this wording, else their records (No
        #      when the Tracker holds no earlier application here, Yes when it does); a question
        #      that also asks "or worked for" needs the worked-here verdict too
        qkey0 = normalize(label)
        ak = commonsense.applied_question(label, ctx.company) if ctx.applied_here else ""
        if ak:
            hit = ctx.library.lookup(label) if ctx.library is not None and not qkey0.startswith("@") else None
            v, origin = (_literal(f, hit["answer"]) if hit else None), "library"
            if not v:
                hit = None
                got = commonsense.applied_answer(label, ak, ctx.applied_here,
                                                 ctx.worked_here if ctx.per_company else None)
                v, origin = (_literal(f, got[0]), got[1]) if got else (None, "")
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin=origin, sensitive=True))
                if hit and hit.get("key"):
                    out.used_library.append(hit["key"])
            else:
                out.left.append(f)
            continue

        # 1a+. (2026-11-18) Common sense, before anything reads these labels as something else:
        #      "Phone Device Type" is the KIND of phone (it used to read as the phone number): the
        #      saved answer when it is one of the choices, else the mobile choice. "List any
        #      aliases or other names ... or indicate N/A": the standing answer from Apply setup,
        #      never a guess. "List all ... connections employed by <company> ... or indicate
        #      N/A": what the question asks for when there is none, under a standing No on relatives.
        if commonsense.phone_type_question(label) and commonsense.fast_path("phone_type"):
            hit = ctx.library.lookup(label) if ctx.library is not None else None
            got = commonsense.phone_type_answer(label, f.options, (hit or {}).get("answer") or "", t)
            if got:
                out.actions.append(FillAction(f.ref, _op_for(f), value=got[0], origin=got[1], sensitive=True))
                if got[1] == "library" and hit and hit.get("key"):
                    out.used_library.append(hit["key"])
            else:
                out.left.append(f)
            continue
        if commonsense.other_names_question(label):
            row = ctx.library.best(commonsense.OTHER_NAMES_KEY) if ctx.library is not None and hasattr(ctx.library, "best") else None
            own = ctx.library.lookup(label) if ctx.library is not None else None
            got = commonsense.other_names_answer(label, (own or row or {}).get("answer") or "", f.options, t)
            if got:
                out.actions.append(FillAction(f.ref, _op_for(f), value=got[0], origin=got[1], sensitive=True))
                out.used_library.append((own or row).get("key") or commonsense.OTHER_NAMES_KEY)
            else:
                out.left.append(f)
            continue
        if commonsense.relatives_list_question(label):
            own = ctx.library.lookup(label) if ctx.library is not None else None
            got = ((own["answer"], "library") if own and str(own.get("answer") or "").strip() and t in ("text", "textarea")
                   else commonsense.relatives_list_answer(label, ctx.library, t))
            if got:
                out.actions.append(FillAction(f.ref, "fill", value=got[0], origin=got[1], sensitive=True))
                out.used_library.append((own or {}).get("key") or commonsense.RELATIVES_KEY)
            else:
                out.left.append(f)
            continue

        # 1a''. "Are any of your relatives employed by this company?": No when the person saved a
        #       standing No in Apply setup (@relatives_at_employer); unsaved or "ask me each time"
        #       falls through to their own answer to this exact wording, else it is theirs
        if commonsense.relatives_question(label):
            got = commonsense.relatives_answer(ctx.library)
            v = _literal(f, got[0]) if got else None
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin=got[1], sensitive=True))
                out.used_library.append(commonsense.RELATIVES_KEY)
                continue

        # 1b. "Have you worked here before?": for THIS company (applicant/facts.py worked_here),
        #     also when the question names the company ("... working at Gusto or Symmetry ...")
        if ctx.per_company and qkey0 != "@previously_worked" and asks_worked_here(label, ctx.company, ctx.confirm):
            qkey0 = "@previously_worked"
        if ctx.per_company and qkey0 == "@previously_worked":
            # (Phase 4) a Yes read from the resume waits for the person's one tap per company
            wh = ctx.worked_here if ctx.worked_here and ctx.worked_here[1] != "confirm" else None
            if wh and wh[1] == "history" and not commonsense.fast_path("worked_here"):
                wh = None                                  # the rule is off: the brain answers it
            v = _literal(f, wh[0]) if wh else None
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin=wh[1] or "library",
                                              sensitive=True))
            else:
                out.left.append(f)
            continue

        # 1b'. a marketing opt-in (declined unless the person saved a yes) and an acknowledgement
        #      of a notice or condition (the affirmative choice, under the person's standing
        #      consent to required notices): never asked per application (autoapply/consents.py)
        got = consents.standing_answer(label, f.options, t, f.required, library=ctx.library,
                                       accept_notices=ctx.accept_notices)
        if got is not None:
            kind, v, origin = got
            if kind == consents.PICK:
                out.actions.append(FillAction(f.ref, "check" if t == "checkbox" else _op_for(f), value=v,
                                              origin=origin, sensitive=True))
            elif kind == consents.THEIRS:
                hit = ctx.library.lookup(label) if ctx.library is not None else None
                v = _literal(f, hit["answer"]) if hit else None
                if v:                                      # their own answer to this exact wording
                    out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True))
                    out.used_library.append(hit["key"])
                else:
                    out.left.append(f)
            continue

        # 1c. one box of a checkbox group ("How did you hear about this opportunity?: Indeed"):
        #     the group's ONE choice from the saved answer, only that box ticked (2026-11-16: every
        #     box was ticked from the answer's Yes/No polarity)
        q, sep, opt = label.partition(": ")
        if t == "checkbox" and sep and opt.strip() and len(groups.get(q) or []) > 1:
            hit = ctx.library.lookup(q) if ctx.library is not None else None
            v = literal_for(normalize(q), hit["answer"], groups[q]) if hit and str(hit.get("answer") or "").strip() else None
            if v == opt:
                out.actions.append(FillAction(f.ref, "check", value="Yes", origin="library", sensitive=True))
                if hit.get("key") and hit["key"] not in out.used_library:
                    out.used_library.append(hit["key"])
            elif v is None and f.required and not any(x.label.partition(": ")[0] == q for x in out.left):
                out.left.append(f)                         # one line for the group: theirs
            continue

        # 2. the answer library (a free-text citizenship question that asks for each country
        #    gets the second one too)
        hit = ctx.library.lookup(label) if ctx.library is not None else None
        if qkey0.startswith("@"):
            hit = keyed(ctx, qkey0, hit)                   # (Phase 4) withheld, or this country's
            if not hit and qkey0 in ctx.withhold:
                out.left.append(f)                         # a stale value: theirs to give again
                continue
        if not hit and ctx.education and commonsense.fast_path("education") and commonsense.education_question(label):
            # (2026-11-18) the highest completed degree on the resume: the list's choice for it
            got = commonsense.education_answer(label, f.options, ctx.education, t)
            if got:
                out.actions.append(FillAction(f.ref, _op_for(f), value=got[0], origin=got[1], sensitive=True))
                continue
        if not hit and qkey0 == commonsense.HEARD_KEY and commonsense.fast_path("heard"):
            # (2026-11-18) nothing saved: someone applying through a job app found it on a job board
            v = _literal(f, commonsense.HEARD_DEFAULT, qkey0)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin=commonsense.HEARD_ORIGIN,
                                              sensitive=True))
                continue
        if not hit and ctx.adult and commonsense.is_adult_question(label):
            # (2026-11-17) no saved answer, and the profile shows it (a degree, or a date 4 years back)
            v = _literal(f, "Yes", qkey0)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="history", sensitive=True))
                continue
        if not hit and label in ctx.confirm:
            out.left.append(f)                             # a new wording of a legal question: asked once
            continue
        if not hit and label in ctx.mapped:
            got = mapped_value(ctx.mapped[label], f, ctx)
            if got:
                out.actions.append(FillAction(f.ref, _op_for(f), value=got[0], origin=got[1], sensitive=True))
                out.used_library.extend(k for k in got[2] if k not in out.used_library)
            else:
                out.left.append(f)
            continue
        if hit and qkey0 in ctx.hold and f.required:
            out.left.append(f)                             # two saved values disagree: asked once
            continue
        if hit and residence_withheld(label, ctx.work_auth):
            out.left.append(f)                             # residence too: theirs to say
            continue
        if hit:
            ans, qkey = hit["answer"], normalize(label)
            if qkey == "@citizenship" and not f.options and t in ("text", "textarea"):
                ans = citizenship_text(label, ctx.library, ans)
            v = _literal(f, ans, qkey)
            if v:
                out.actions.append(FillAction(f.ref, _op_for(f), value=v, origin="library", sensitive=True))
                if hit.get("key"):
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
