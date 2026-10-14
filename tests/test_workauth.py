"""Work authorization: one profile, one consistent answer to every phrasing of the question.

Real question and option wordings from Greenhouse, Lever, Ashby, Workday, iCIMS and SmartRecruiters
forms. The rule under test: the answer always agrees with the person's stated profile, the exact
option text is picked for selects and radios, and anything the profile does not say is left
(None), never guessed.
"""
from __future__ import annotations

import pytest

from autoapply import workauth as W

OPT = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                            "sponsor_future": "Yes", "auth_end": "2027-06-30"}}, "relocate": "Yes"}
STEM = {"countries": {"US": {"status": "f1_stem_opt", "authorized": "Yes", "sponsor_now": "No",
                             "sponsor_future": "Yes"}}}
CIT = {"countries": {"US": {"status": "us_citizen"}}}
GC = {"countries": {"US": {"status": "green_card"}}}
H1B = {"countries": {"US": {"status": "h1b", "authorized": "Yes", "sponsor_now": "Yes"}}}
EMPTY: dict = {}
UK_GRAD = {"countries": {"UK": {"status": "uk_graduate", "authorized": "Yes", "sponsor_now": "No",
                                "sponsor_future": "Yes"}}}

YN = ["Yes", "No"]

# (question, options, type, {profile_name: expected})
CASES = [
    # Greenhouse
    ("Are you legally authorized to work in the United States?", YN, "select",
     {"OPT": "Yes", "CIT": "Yes", "H1B": "Yes", "EMPTY": None}),
    ("Will you now or in the future require sponsorship for employment visa status (e.g. H-1B visa status)?",
     YN, "select", {"OPT": "Yes", "CIT": "No", "GC": "No", "H1B": "Yes", "EMPTY": None}),
    ("Will you now, or in the future, require sponsorship for employment visa status?*", YN, "radio",
     {"OPT": "Yes", "CIT": "No"}),
    # Workday
    ("Are you legally authorized to work in the country in which the job is located?", YN, "select",
     {"OPT": "Yes", "CIT": "Yes"}),
    ("Will you now or in the future require the company's sponsorship to obtain or maintain work authorization?",
     YN, "select", {"OPT": "Yes", "GC": "No"}),
    # Lever (free text and radios)
    ("Are you authorized to work lawfully in the US?", [], "text", {"OPT": "Yes", "CIT": "Yes"}),
    ("Do you currently require visa sponsorship?", YN, "radio", {"OPT": "No", "H1B": "Yes", "CIT": "No"}),
    ("Do you need visa sponsorship at this time?", YN, "radio", {"OPT": "No", "H1B": "Yes"}),
    ("Will you require sponsorship in the future?", YN, "radio", {"OPT": "Yes", "CIT": "No"}),
    ("Will you require visa sponsorship?", ["Yes", "No"], "radio", {"OPT": "Yes", "CIT": "No"}),
    # Ashby, combined question
    ("Are you authorized to work in the U.S. without sponsorship?", YN, "radio",
     {"OPT": "No", "CIT": "Yes", "H1B": "No", "EMPTY": None}),
    ("Are you legally eligible to work in the US without the need for visa sponsorship now or in the future?",
     YN, "select", {"OPT": "No", "GC": "Yes"}),
    ("Are you authorized to work for any employer in the US?", YN, "radio",
     {"OPT": "Yes", "H1B": "No", "CIT": "Yes"}),
    # Varied option wordings
    ("Sponsorship", ["I will require sponsorship", "I will not require sponsorship"], "select",
     {"OPT": "I will require sponsorship", "CIT": "I will not require sponsorship"}),
    ("Do you require sponsorship?", ["Yes, I require sponsorship", "No, I do not require sponsorship"],
     "radio", {"OPT": "Yes, I require sponsorship", "GC": "No, I do not require sponsorship"}),
    ("Are you legally authorized to work in the US?",
     ["Yes, I am authorized and do not require sponsorship",
      "Yes, but I will require sponsorship in the future", "No"], "select",
     {"OPT": "Yes, but I will require sponsorship in the future",
      "CIT": "Yes, I am authorized and do not require sponsorship"}),
    ("Work authorization",
     ["I am authorized to work in the US for any employer",
      "I am authorized but will require sponsorship now or in the future",
      "I am not authorized to work in the US"], "select",
     {"OPT": "I am authorized but will require sponsorship now or in the future",
      "CIT": "I am authorized to work in the US for any employer"}),
    ("Are you legally authorized to work in the United States?", ["True", "False"], "select",
     {"OPT": "True"}),
    # Status selects (iCIMS, SmartRecruiters)
    ("What is your current work authorization status?",
     ["U.S. Citizen", "Green Card Holder", "H-1B", "F-1 OPT", "F-1 STEM OPT", "Other"], "select",
     {"OPT": "F-1 OPT", "STEM": "F-1 STEM OPT", "CIT": "U.S. Citizen", "GC": "Green Card Holder",
      "H1B": "H-1B", "EMPTY": None}),
    ("Which of the following best describes your current work authorization?",
     ["US Citizen or National", "Lawful Permanent Resident", "F-1 Student (OPT/CPT)", "H1B", "Other"],
     "select", {"OPT": "F-1 Student (OPT/CPT)", "H1B": "H1B", "GC": "Lawful Permanent Resident"}),
    ("Visa status", [], "text", {"OPT": "F-1 OPT", "EMPTY": None}),
    ("What is your current visa status?", [], "text", {"OPT": "F-1 OPT", "H1B": "H-1B"}),
    # Citizenship
    ("Are you a U.S. citizen?", YN, "radio", {"OPT": "No", "CIT": "Yes", "EMPTY": None}),
    ("Are you a US citizen or permanent resident?", YN, "radio", {"OPT": "No", "GC": "Yes", "CIT": "Yes"}),
    ("Are you a green card holder?", YN, "radio", {"GC": "Yes", "OPT": "No"}),
    ("What is your country of citizenship?", [], "text", {"OPT": None, "CIT": None}),
    # Dates and relocation
    ("When does your OPT end?", [], "date", {"OPT": "2027-06-30", "STEM": None, "CIT": None}),
    ("Are you willing to relocate?", YN, "radio", {"OPT": "Yes", "CIT": None}),
    # Other countries: the question names the country, the profile must have that country
    ("Do you have the right to work in the UK?", YN, "radio", {"UK_GRAD": "Yes", "OPT": None}),
    ("Will you require a visa to work in the UK?", YN, "radio", {"UK_GRAD": "Yes"}),
    ("Are you legally eligible to work in Canada?", YN, "radio", {"OPT": None}),
    ("Are you authorized to work in the US and will you require sponsorship?", YN, "radio", {"OPT": None}),
    # Not work-auth questions at all
    ("Have you led sponsor relationships for events?", [], "text", {"OPT": None}),
    ("Why do you want to work with us?", [], "textarea", {"OPT": None}),
    ("Describe your experience with visa processing systems", [], "textarea", {"OPT": None}),
]
PROFILES = {"OPT": OPT, "STEM": STEM, "CIT": CIT, "GC": GC, "H1B": H1B, "EMPTY": EMPTY, "UK_GRAD": UK_GRAD}


@pytest.mark.parametrize("question,options,ftype,expect", CASES, ids=[c[0][:50] for c in CASES])
def test_answer_matches_profile(question, options, ftype, expect):
    for name, want in expect.items():
        got = W.answer(question, PROFILES[name], options=options, field_type=ftype)
        assert got == want, f"{name}: {question!r} -> {got!r}, want {want!r}"
        if got is not None and options:
            assert got in options                      # always an option that really exists


def test_checkbox_only_ticks_a_true_statement():
    q = "I am legally authorized to work in the United States"
    assert W.answer("Are you legally authorized to work in the United States?", OPT, field_type="checkbox") == "Yes"
    assert W.answer(q + " without sponsorship", OPT, field_type="checkbox") is None


def test_ambiguous_options_are_left_for_the_person():
    opts = ["Yes, I am authorized", "Yes, I have a work permit", "No"]
    assert W.answer("Are you authorized to work in the US?", OPT, options=opts, field_type="select") is None


def test_answers_are_consistent_across_phrasings():
    """However the sponsorship question is worded, an OPT holder gives the same answer."""
    qs = ["Will you now or in the future require sponsorship?",
          "Do you now, or will you in the future, need visa sponsorship?",
          "Will you require employer sponsorship for a work visa now or in the future?",
          "Will you need us to sponsor a visa for you?"]
    assert {W.answer(q, OPT, options=YN, field_type="radio") for q in qs} == {"Yes"}


def test_needs_sponsorship_and_status_getters():
    assert W.needs_sponsorship(OPT) is True
    assert W.needs_sponsorship(OPT, when="now") is False
    assert W.needs_sponsorship(CIT) is False
    assert W.needs_sponsorship(EMPTY) is None
    assert W.is_citizen(CIT) is True and W.is_citizen(OPT) is False and W.is_citizen(EMPTY) is None
    assert W.is_citizen_or_pr(GC) is True
    assert W.status(OPT) == "f1_opt" and W.status(EMPTY) == ""


def test_clean_drops_unknowns_and_legacy_round_trip():
    wa = W.clean({"countries": {"US": {"status": "bogus", "authorized": "maybe", "sponsor_now": "yes",
                                       "auth_end": "June"}, "XX": {"status": "x"}}, "relocate": "no"})
    assert wa == {"countries": {"US": {"status": "", "authorized": "", "sponsor_now": "Yes",
                                       "sponsor_future": "", "auth_end": ""}}, "relocate": "No"}
    old = {"work_auth_us": "Yes", "needs_sponsorship": "Future", "willing_to_relocate": "Yes"}
    wa = W.load(old)
    assert W.needs_sponsorship(wa, when="now") is False and W.needs_sponsorship(wa, when="future") is True
    assert W.legacy_prefs(wa)["needs_sponsorship"] == "Future"
    wa2 = W.apply_legacy(OPT, {"needs_sponsorship": "No"})
    assert wa2["countries"]["US"]["status"] == "f1_opt" and W.needs_sponsorship(wa2) is False
