"""The key registry of the applicant brain (docs/applicant-brain.md, section 5.2).

A list in code, not a table. Each key says what kind of value it holds, how sensitive it is
(``normal``, ``legal`` or ``eeo``), whether a person has one value or several, the allowed
choices, how long a value stays fresh, how it is asked, and the older names it maps from.

Sensitivity is decided HERE, never by a caller: ``applicant/store.py`` sets the column from
``sensitivity()`` and ignores anything a caller passes. Canonical keys reuse the answer
library's names (``@salary``, ``@gender``), so a library row and a brain fact share one key.
"""
from __future__ import annotations

from dataclasses import dataclass, field

NORMAL, LEGAL, EEO = "normal", "legal", "eeo"
SENSITIVITIES = (NORMAL, LEGAL, EEO)

# Where a fact came from (docs section 5.1). ``you`` and ``drafted`` are the answer library's
# own two sources, kept as they are.
SOURCES = ("you", "card", "onboarding", "ask_bar", "ask_telegram", "late_telegram", "extension",
           "resume", "chat", "telegram_chat", "drafted", "derived", "migration")
# A self-identification value only ever comes from the person's own direct choice (section 9).
EEO_REFUSED_SOURCES = frozenset({"chat", "telegram_chat", "resume", "drafted", "derived", "model"})
# A legal value proposed by these waits as needs_check (unconfirmed_legal) and never fills a form.
LEGAL_PROPOSAL_SOURCES = frozenset({"resume", "chat", "telegram_chat"})

# Confidence by source (section 5.1): said or chosen by the person 1.0, read from their resume
# 0.9, drafted from the profile 0.8, extracted from chat 0.7.
CONFIDENCE = {"resume": 0.9, "drafted": 0.8, "chat": 0.7, "telegram_chat": 0.7}


def confidence_for(source: str) -> float:
    return CONFIDENCE.get(str(source or ""), 1.0)


@dataclass(frozen=True)
class Key:
    name: str
    type: str = "text"            # text, money, date, yes_no, choice, country, country_list, json
    sensitivity: str = NORMAL
    multi: bool = False           # several active values at once (citizenships, skills)
    choices: tuple = ()
    reconfirm_days: int = 0       # 0: never goes stale on its own
    ask: str = ""                 # how the question is put to the person
    legacy: tuple = field(default_factory=tuple)   # older names it maps from ("app_prefs.json:desired_salary")


def _k(name, type="text", sensitivity=NORMAL, *, multi=False, choices=(), reconfirm_days=0, ask="", legacy=()):
    return Key(name, type, sensitivity, multi, tuple(choices), reconfirm_days, ask, tuple(legacy))


_YES_NO = ("Yes", "No")

REGISTRY: dict[str, Key] = {k.name: k for k in (
    # ---- contact
    _k("name", ask="Your full name"),
    _k("email", ask="Your email address"),
    _k("phone", ask="Your phone number"),
    # The whole address line as the profile holds it; its parts are filled from Phase 3.
    _k("address", ask="Your address"),
    _k("location", ask="Where you live (city, state)"),
    _k("address.street"), _k("address.city"), _k("address.state"), _k("address.postal_code"),
    _k("address.country", "country"),
    _k("@linkedin", ask="Your LinkedIn profile"),
    _k("@portfolio", ask="Your website or portfolio"),
    _k("github", ask="Your GitHub profile"),
    _k("@preferred_name", ask="Your preferred name"),
    # ---- work and legal
    _k("workauth", "json", LEGAL, ask="Your work authorization", legacy=("app_prefs.json:work_auth",)),
    _k("@citizenship", "country", LEGAL, multi=True, ask="Country of citizenship"),
    # The answer library keeps a second citizenship under its own key; it folds into the
    # multi-valued @citizenship in Phase 3.
    _k("@citizenship2", "country", LEGAL, ask="Second country of citizenship"),
    _k("citizenship.complete", "yes_no", LEGAL, choices=_YES_NO),
    _k("permanent_resident", "yes_no", LEGAL, choices=_YES_NO),
    _k("@over_18", "yes_no", LEGAL, choices=_YES_NO, ask="Are you at least 18 years of age?",
       legacy=("app_prefs.json:over_18",)),
    _k("@previously_worked", "yes_no", LEGAL, ask="Have you worked for this company before?"),
    _k("security_clearance", sensitivity=LEGAL),
    # ---- preferences
    _k("@salary", "money", reconfirm_days=183, ask="Desired annual salary",
       legacy=("app_prefs.json:desired_salary",)),
    _k("@start_date", "date", ask="When can you start?", legacy=("app_prefs.json:earliest_start",)),
    _k("@notice_period", ask="Your notice period"),
    _k("relocate", "yes_no", choices=_YES_NO, ask="Are you willing to relocate?",
       legacy=("app_prefs.json:willing_to_relocate", "app_prefs.json:work_auth.relocate")),
    _k("remote", "yes_no", choices=_YES_NO),
    _k("@heard_about", ask="How did you hear about us?", legacy=("app_prefs.json:hear_about_us",)),
    _k("@years_experience", ask="How many years of experience do you have?",
       legacy=("app_prefs.json:years_experience",)),
    # Terms a site requires to apply (a privacy notice, an E-Verify acknowledgement): "always"
    # accepts them for every application, "ask" (the default, never stored) asks per site.
    _k("accept_required_terms", "choice", choices=("ask", "always"),
       ask="Accept the terms a site requires to apply?", legacy=("app_prefs.json:accept_required_terms",)),
    # ---- voluntary self-identification (EEO)
    _k("@gender", "choice", EEO, ask="Gender"),
    _k("@sex", "choice", EEO, ask="Sex"),
    _k("@pronouns", "choice", EEO, ask="Pronouns"),
    _k("@hispanic_latino", "choice", EEO, ask="Are you Hispanic or Latino?"),
    _k("@race", "choice", EEO, ask="Race"),
    _k("@ethnicity", "choice", EEO, ask="Ethnicity"),
    _k("@veteran", "choice", EEO, ask="Veteran status"),
    _k("@disability", "choice", EEO, ask="Disability status"),
    _k("@eeo_decline", "yes_no", EEO, legacy=("app_prefs.json:eeo_decline",)),
    # ---- career (open facts; subject = item id)
    _k("role", "json", multi=True), _k("school", "json", multi=True), _k("project", "json", multi=True),
    _k("skill", multi=True), _k("certification", multi=True), _k("language", multi=True),
    # ---- chat notes from memory_facts (subject = the key distill_memory gave)
    _k("preference", multi=True), _k("constraint", multi=True), _k("fact", multi=True),
)}

# Open facts: one row per item, the item id in ``subject``.
ITEM_KEYS = frozenset({"role", "school", "project", "skill", "certification", "language"})
QUESTION_PREFIX = "q:"


def get(key: str) -> Key | None:
    return REGISTRY.get(str(key or ""))


def is_multi(key: str) -> bool:
    k = get(key)
    return bool(k and k.multi)


def sensitivity(key: str, question: str = "") -> str:
    """How sensitive a key's value is. Fixed here for every registered key. A one-question key
    (``q:<wording>``) or an unregistered key is normal, unless its wording classifies as a
    self-identification question (EEO) or a work-authorization one (legal)."""
    k = get(key)
    if k is not None:
        return k.sensitivity
    words = str(question or "")
    if not words and str(key or "").startswith(QUESTION_PREFIX):
        words = str(key)[len(QUESTION_PREFIX):]
    if words:
        from autoapply import asks, workauth
        if asks.is_eeo(words):
            return EEO
        if workauth.classify(words):
            return LEGAL
    return NORMAL


def fact_key(library_key: str) -> str:
    """The brain key of an answer-library row: a canonical key as it is (``@salary``), a
    wording key under ``q:`` (``q:annual requirement salary``)."""
    k = str(library_key or "")
    return k if k.startswith("@") else QUESTION_PREFIX + k


def library_key(fact_key_: str) -> str:
    """The answer-library key of a brain fact key (the inverse of fact_key)."""
    k = str(fact_key_ or "")
    return k[len(QUESTION_PREFIX):] if k.startswith(QUESTION_PREFIX) else k


# The flat answers of app_prefs.json and the keys they become (section 11).
PREFS_FIELDS = (("desired_salary", "@salary"), ("earliest_start", "@start_date"),
                ("hear_about_us", "@heard_about"), ("years_experience", "@years_experience"),
                ("over_18", "@over_18"))
