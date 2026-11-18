"""What kind of question is this? ONE function, used wherever the apply agent decides whether it
may answer a form question itself (docs/apply-agent.md section 29, "The agent decides").

``question_class(label, options, company=, employers=)`` returns one of:

* ``legal``        a legal or attestation question. Answered ONLY from a saved answer or a saved
                   profile field for exactly that question (hard limit 5): work authorization, visa
                   sponsorship, citizenship, criminal history or background, security clearance,
                   non-compete and other agreements, disability, veteran status, race, ethnicity,
                   gender and other self-identification, date of birth and age, a government ID or
                   SSN, relatives and conflicts of interest, other names used, previous employment
                   AT THIS employer when the employer is in the work history, salary expectations
                   and start date, references' details, signatures, consents, acknowledgements and
                   certifications that statements are true.
* ``common_sense`` a question with no fact about the person that matters legally: how they heard
                   about the job, the kind of phone, a follow-up that does not apply.
* ``preference``   what the person wants or is willing to do (relocate, travel, shifts, an office).
* ``factual``      a fact the resume or the saved profile states: employers, titles, dates,
                   degrees, schools, skills, where they live, years of experience.
* ``""``           nothing here recognises it: not legal by any pattern we hold.

When unsure, it is legal: the legal patterns are the union of every pattern the engine already
held for these questions (autoapply/grounded.py, eeo.py, workauth.py, consents.py, commonsense.py,
planner.py), and they are read before anything else. Pure: no store, no network.
"""
from __future__ import annotations

import re

from autoapply import commonsense, consents, workauth
from autoapply.answers import normalize

LEGAL, FACTUAL, PREFERENCE, COMMON = "legal", "factual", "preference", "common_sense"
CLASSES = (LEGAL, FACTUAL, PREFERENCE, COMMON)

# Self-identification, by the question's words (autoapply/brain.py _SELF_ID, planner._EEO).
_SELF_ID = re.compile(r"gender|\bsex\b|\brac(e|ial)\b|ethnic|hispanic|latin[oax]|veteran|military (service|status)|disab"
                      r"|sexual orientation|transgender|lgbt|queer|pronoun|self[- ]?identif|marital|religio"
                      r"|first[- ]generation", re.I)
# Legal standing, attestations, consents and secrets (autoapply/grounded.py _LEGAL and _LEGAL_TOPIC,
# autoapply/asks.py _FACT_Q's legal half, consents._DISCLOSE).
_LEGAL = re.compile(
    r"citizen|nationality|\bvisa\b|sponsor|immigra|permanent resid|green card|work permit|work authori[sz]"
    r"|authori[sz]ed to work|right to work|legally|eligib(le|ility) to work|export control|\bitar\b|\bear\b"
    r"|clearance|polygraph|restrictive covenant|non-?compet|non-?solicit|confidentiality agreement|bound by"
    r"|convict|criminal|felon|misdemeanor|arrest|pending charge|plead|guilty|offen[cs]e|background (check|screen|investigation)"
    r"|drug (test|screen)|terminat|dismiss|\bfired\b|discharg|resign(ed)? in lieu|disciplin|lawsuit|litigation|bankrupt"
    r"|date of birth|birth ?date|\bdob\b|\bage\b|years? (old|of age)|social security|\bssn\b|national (id|insurance)"
    r"|passport|tax (id|identification)|government[- ]issued|driver'?s? licen[cs]e number"
    r"|accommodation|essential functions"
    r"|conflicts? of interest|government official|public official|politically exposed|relative|related to|family member"
    r"|spouse|domestic partner|next of kin|household"
    r"|\bagree|consent|acknowledg|attest|certif(y|ication that)|signature|\bsign(ed)?\b|\bterms\b|privacy|polic(y|ies)"
    r"|\bdeclare|truthful|true and (complete|correct|accurate)|i understand|authori[sz]e (us|the company|you)"
    r"|password|passcode|verification code|one[- ]time|captcha|\bpin\b|bank (account|details)|routing number"
    r"|credit card|card number|payment", re.I)
# Salary and start date: the person's to state; only a saved answer gives them. ("Start date" by
# itself is read by answers.normalize, which knows the availability question from a work
# history section's "Work Experience 1: Start Date".)
_MONEY_DATE = re.compile(r"salary|compensation|\bpay\b|\b(hourly|daily|pay|bill|billing|day) rate\b|\brate (expectation|requirement)|remuneration|\bwage|earnings|\bctc\b|\bote\b|bonus"
                         r"|earliest start|when (can|could|would) you (start|begin|join)|available to start"
                         r"|availability (date|to start)|notice period", re.I)
# A reference's details (a name, a phone, an email of someone else).
_REFERENCE = re.compile(r"\breferences?\b|\breferee", re.I)
_LEGAL_KEYS = frozenset({
    "@gender", "@sex", "@race", "@ethnicity", "@hispanic_latino", "@veteran", "@disability", "@transgender",
    "@sexual_orientation", "@first_gen", "@citizenship", "@citizenship2", "@restrictive_covenants",
    "@security_clearance", "@over_18", "@salary", "@start_date", "@notice_period", "@relatives_at_employer",
    "@other_names", "@forced_consent", "@marketing_optin"})

# How the person heard about the job; the kind of phone; a follow-up that only applies to a Yes.
_HEARD = re.compile(r"how did you (hear|find|learn)|where did you (hear|find|learn|see)|hear about (us|this)"
                    r"|learn(ed)? (about|of) (us|this)|source of (application|referral|this application)", re.I)
_FOLLOW_UP = re.compile(r"^\W*if (yes|so|applicable|you (answered|selected|chose|checked))\b", re.I)

_PREFERENCE = re.compile(r"relocat|willing|open to|comfortable (with|working)|travel|\bshifts?\b|weekend|overtime|commut"
                         r"|on-?site|hybrid|remote|in[- ]office|work (from|location|arrangement|schedule)"
                         r"|prefer|desired|interested in|which (office|location|site)|full[- ]time|part[- ]time", re.I)
_FACTUAL = re.compile(r"employ|worked (at|for|with)|company|organi[sz]ation|\btitle\b|position|years|experience|degree|diploma"
                      r"|\bgpa\b|graduat|school|universit|college|education|major|field of study|skill|proficien|familiar"
                      r"|language|speak|fluent|certification|certified|licen[cs]e|located|reside|live in|address"
                      r"|\bcountry\b|\bstate\b|\bcity\b|location|linkedin|github|portfolio|website", re.I)


def _squash(label) -> str:
    return " ".join(str(label or "").split())


def worked_here(label: str, company: str = "") -> bool:
    """"Have you worked for <this employer> before?" in any wording the engine knows."""
    from autoapply.planner import worked_here_question
    lbl = _squash(label)
    return bool(lbl) and (normalize(lbl) == "@previously_worked" or worked_here_question(lbl, company))


def employer_in_history(company: str, employers=()) -> bool:
    """Is ``company`` one of the person's employers (their resume's roles)?"""
    from applicant.facts import same_company
    return bool(_squash(company)) and any(same_company(e, company) for e in (employers or ()) if _squash(e))


def is_legal(label: str, options=(), *, company: str = "", employers=()) -> bool:
    """True for a legal or attestation question (hard limit 5). When unsure, True."""
    lbl = _squash(label)
    if not lbl:
        return False
    from autoapply import eeo
    from autoapply.asks import is_eeo
    if is_eeo(lbl) or _SELF_ID.search(lbl) or workauth.classify(lbl) or _LEGAL.search(lbl):
        return True
    key = normalize(lbl)
    if key in _LEGAL_KEYS or key in eeo.KEYS:
        return True
    if any(eeo.topic_key(str(o)) in eeo.KEYS for o in (options or []) if str(o).strip()):
        return True                                    # a self-identification list, by its choices
    if consents.is_acknowledgement(lbl) or consents.is_optin(lbl):
        return True
    if commonsense.other_names_question(lbl) or commonsense.relatives_question(lbl) \
            or commonsense.relatives_list_question(lbl) or commonsense.is_adult_question(lbl):
        return True
    if _MONEY_DATE.search(lbl) or _REFERENCE.search(lbl):
        return True
    # Worked here before: a Yes is the person's to confirm, so with the employer in the work
    # history it is legal; with the employer not in it, the history itself answers No.
    if worked_here(lbl, company) and employer_in_history(company, employers):
        return True
    return False


def question_class(label: str, options=(), *, company: str = "", employers=()) -> str:
    """legal | common_sense | preference | factual | "" for one form question (see the module)."""
    lbl = _squash(label)
    if not lbl:
        return ""
    if is_legal(lbl, options, company=company, employers=employers):
        return LEGAL
    if worked_here(lbl, company):
        return FACTUAL                                 # the employer is not in the work history
    if normalize(lbl) == commonsense.HEARD_KEY or _HEARD.search(lbl) or commonsense.phone_type_question(lbl) \
            or _FOLLOW_UP.search(lbl):
        return COMMON
    if commonsense.education_question(lbl):
        return FACTUAL
    if _PREFERENCE.search(lbl):
        return PREFERENCE
    if _FACTUAL.search(lbl):
        return FACTUAL
    return ""
