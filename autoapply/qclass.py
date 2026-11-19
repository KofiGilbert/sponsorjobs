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
    r"|drug (test|screen)|terminat|dismiss|\bfired\b|discharg|resign(ed)? in lieu|disciplinar|disciplined|lawsuit|litigation|bankrupt"
    r"|involuntar|separated from|reasons? for (leaving|separation|departure)|why (did|are|do) you (leave|leaving|left|want to leave)"
    r"|date of birth|birth ?date|\bdob\b|\bage\b|years? (old|of age)|social security|\bssn\b|national (id|insurance)"
    r"|passport|tax (id|identification)|government[- ]issued|driver'?s? licen[cs]e number"
    r"|accommodation|essential functions"
    r"|suspended|sanctioned|debarred|excluded from|precluded from|exclusion list|\boig\b|medicare|medicaid|federal health ?care program"
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
    "@other_names", "@forced_consent", "@marketing_optin", "@criminal_record", "@dismissed", "@excluded_programs"})

# How the person heard about the job; the kind of phone; a follow-up that only applies to a Yes.
_HEARD = re.compile(r"how did you (hear|find|learn)|where did you (hear|find|learn|see)|hear about (us|this)"
                    r"|learn(ed)? (about|of) (us|this)|source of (application|referral|this application)", re.I)
_FOLLOW_UP = re.compile(r"^\W*if (yes|so|applicable|you (answered|selected|chose|checked))\b", re.I)

_PREFERENCE = re.compile(r"relocat|willing|open to|comfortable (with|working)|travel|\bshifts?\b|weekend|overtime|commut"
                         r"|on-?site|hybrid|remote|in[- ]office|work (from|location|arrangement|schedule)"
                         r"|prefer|desired|interested in|which (office|location|site)|full[- ]time|part[- ]time", re.I)
_FACTUAL = re.compile(r"employ|worked (at|for|with)|company|organi[sz]ation|\btitle\b|position|years|experience|degree|diploma"
                      r"|\bgpa\b|graduat|school|universit|college|education|major|field of study|\bdiscipline\b|skill|proficien|familiar"
                      r"|language|speak|fluent|certification|certified|licen[cs]e|located|reside|live in|address"
                      r"|\bcountry\b|\bstate\b|\bcity\b|location|linkedin|github|portfolio|website", re.I)


# (2026-11-18, the owner: common sense first) Three kinds of question the agent may answer with a
# stated common-sense DEFAULT (autoapply/protocol.py checks each against the job posting and the
# person's place): the type of employment, which office or location, and a routine condition of
# the job (overtime, days on site, travel, a shift) that the posting itself states.
EMPLOYMENT, OFFICE, CONDITION = "employment_type", "office", "condition"
_EMPLOYMENT_Q = re.compile(r"type of employment|employment type|type of (position|role|work) (are you|you are)|seeking (full|part)"
                           r"|(full|part)[- ]time (or|and|/) (full|part)[- ]time|full[- ]time or|are you (seeking|looking for) "
                           r"(a )?(full|part)[- ]time", re.I)
_OFFICE_Q = re.compile(r"\bwhich (of our )?(offices?|locations?|sites?)\b|\b(offices?|locations?) (are you|you are|you're) "
                       r"(open|willing|interested|applying|able)|\bin which of our (offices|locations)\b"
                       r"|\bpreferred (office|work )?locations?\b|\b(office|location) (preference|you would like)\b"
                       r"|\boffice locations? do you prefer\b|\bselect (one|a|your)( preferred)? (location|office)\b", re.I)
CONDITIONS = (("overtime", re.compile(r"overtime|extra hours|additional hours", re.I)),
              ("on site", re.compile(r"on-?site|in[- ](the )?office|in person|hybrid", re.I)),
              ("travel", re.compile(r"\btravel", re.I)),
              ("shift", re.compile(r"\bshifts?\b|weekend|evening|night|holiday|schedule|work hours|hours per week|on[- ]call", re.I)))
_YES_NO_Q = re.compile(r"^\W*(are|can|will|would|do|is) (you|this)\b|\b(able|available|willing|comfortable|open) (to|for)\b", re.I)
_WA_WORDS = re.compile(r"authori[sz]|legal|eligib|permit|visa|sponsor|citizen|relocat|resid", re.I)
# An open question answered in the person's own words (a draft, autoapply/drafts.py): why this
# role, what interests them, their experience with something, a strength, a project.
_ESSAY = re.compile(r"\bwhy\b|\bdescribe\b|tell us|\bexplain\b|elaborate|share (an?|your|with)|what (excites|motivates|interests|attracts)"
                    r"|what (do|did) you (like|enjoy|dislike)|liked and disliked|interest(s|ed)? (you )?(most )?(in|about)"
                    r"|strengths?\b|proud of|example of|how would you|walk us through|in your own words|tell me about"
                    r"|what makes you|what would you bring|greatest (achievement|accomplishment)", re.I)
_NOT_ESSAY = re.compile(r"cover letter|anything else|additional (information|comments)|\bsummary\b|comments?\W*$", re.I)


# (2026-11-19, Auto never stops to ask) What is NEVER guessed, in any mode, because a wrong value
# there is not a judgement call: a government ID number, an SSN, a date of birth, bank or payment
# details, a password or code, and a typed legal signature. A required field of this kind with
# nothing saved means the agent cannot do this application: it is skipped, never guessed.
_NEVER = re.compile(
    r"social security|\bssn\b|\bsin\b number|national (id|insurance|identification)|\bnino\b|tax (id|identification|file) ?(number|no)?"
    r"|\btin\b|\bitin\b|passport (number|no)|driver'?s? licen[cs]e (number|no)|licen[cs]e number|government[- ]issued (id|identification)"
    r"|\bid number\b|identification number|alien (registration )?number|\buscis\b number|visa number"
    r"|date of birth|birth ?date|\bdob\b|birthday|year of birth"
    r"|bank (account|details|name)|account number|routing number|\biban\b|\bswift\b|sort code|credit card|debit card|card number"
    r"|\bcvv\b|payment (details|method|information)"
    r"|password|passcode|verification code|one[- ]time (code|password)|security (question|answer)|\bpin\b"
    r"|signature|\bsign(ed)? (here|below|your name)|type your (full )?(legal )?name (to|as|below|here)|electronic(ally)? sign|\be-?sign", re.I)


def never_guess(label: str) -> bool:
    """True for a field whose value is never guessed, in any mode (see _NEVER)."""
    lbl = _squash(label)
    return bool(lbl) and bool(_NEVER.search(lbl))


def is_consent(label: str) -> bool:
    """A consent, an acknowledgement of a notice or the terms, or a marketing opt-in: these
    follow the person's consent settings (autoapply/consents.py), never a guess."""
    lbl = _squash(label)
    return bool(lbl) and (consents.is_acknowledgement(lbl) or consents.is_optin(lbl)
                          or bool(re.search(r"\bterms\b|privacy (notice|policy|statement)|\bi agree\b|\bconsent\b", lbl, re.I)))


def is_self_id(label: str, options=()) -> bool:
    """A voluntary self-identification question (by its words or its choices)."""
    from autoapply import eeo
    from autoapply.asks import is_eeo
    lbl = _squash(label)
    return bool(lbl) and (is_eeo(lbl) or bool(_SELF_ID.search(lbl))
                          or any(eeo.topic_key(str(o)) in eeo.KEYS for o in (options or []) if str(o).strip()))


def default_kind(label: str) -> str:
    """employment_type | office | condition for a question with a common-sense default, else ""."""
    lbl = _squash(label)
    if not lbl or len(lbl) > 300:
        return ""
    if _EMPLOYMENT_Q.search(lbl):
        return EMPLOYMENT
    if _OFFICE_Q.search(lbl) and not re.search(r"relocat", lbl, re.I):
        return OFFICE
    if _YES_NO_Q.search(lbl) and any(rx.search(lbl) for _n, rx in CONDITIONS) and not _WA_WORDS.search(lbl):
        return CONDITION
    return ""


def condition_names(text: str) -> set:
    """The job conditions a text names: overtime, on site, travel, shift."""
    return {n for n, rx in CONDITIONS if rx.search(str(text or ""))}


def is_essay(label: str, options=(), *, company: str = "", employers=()) -> bool:
    """An open-ended, non-legal question the drafter may answer for the person to read (never a
    legal or saved-only one, a follow-up that only applies to a Yes, a cover letter box or an
    "anything else" box)."""
    lbl = _squash(label)
    if not lbl or len(lbl) < 12 or _FOLLOW_UP.search(lbl) or _NOT_ESSAY.search(lbl) or [o for o in (options or []) if str(o).strip()]:
        return False
    return bool(_ESSAY.search(lbl)) and not is_legal(lbl, company=company, employers=employers)


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
    # (workauth.classify reads "able to work weekends" and an office pick as its own; a question
    # with a common-sense default that names no legal word is not one)
    wa = workauth.classify(lbl) and not (default_kind(lbl) and not _WA_WORDS.search(lbl))
    if is_eeo(lbl) or _SELF_ID.search(lbl) or wa or _LEGAL.search(lbl):
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
    if default_kind(lbl):
        return PREFERENCE
    if _PREFERENCE.search(lbl):
        return PREFERENCE
    if _FACTUAL.search(lbl):
        return FACTUAL
    return ""
