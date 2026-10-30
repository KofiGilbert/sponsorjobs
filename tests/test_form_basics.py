"""Applicant memory, phase 1: the answers every form asks.

Live failure this pins (Lam Research, 2026-10-30): the apply agent asked country of citizenship,
gender and ethnicity again, and missed a saved salary because the form's label was "Error: The
field What is your annual salary requirement? is required and must have a value.". Now:

* labels lose their form furniture before any matching (answers.clean_label, and the shell's
  cleanQuestion), and the library's re-key stays non-destructive;
* Your answers has an "Answers every form asks" card (/api/answers/basics) that saves under the
  canonical keys, the salary in the prefs store too, and prefills from usable answers only;
* a saved self-identification answer lands on each form's own wording, in code (autoapply/eeo.py),
  combined race and ethnicity lists included, never a guess;
* before the agent's first run of a session, missing answers are named once on the bar.

The option sets below are the wordings those systems use on their US self-identification pages.
"""
from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply.answers import AnswerLibrary, clean_label, normalize
from autoapply.countries import NAMES, canonical, country_option
from autoapply.eeo import choose
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known

ROOT = Path(__file__).resolve().parents[1]
LAM_SALARY = "Error: The field What is your annual salary requirement? is required and must have a value."
LAM_REPLY = "Yes, I will need H1B\n\nMale\n\nDon't you have this information on my profile already ?"

# --------------------------------------------------------------------------- real option sets
WD_ETHNICITY = ["American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)",
                "Asian (Not Hispanic or Latino) (United States of America)",
                "Black or African American (Not Hispanic or Latino) (United States of America)",
                "Hispanic or Latino (United States of America)",
                "Native Hawaiian or Other Pacific Islander (Not Hispanic or Latino) (United States of America)",
                "Two or More Races (Not Hispanic or Latino) (United States of America)",
                "White (Not Hispanic or Latino) (United States of America)",
                "I do not wish to answer. (United States of America)"]
WD_GENDER = ["Male", "Female", "Not Declared"]
WD_VETERAN = ["I am not a veteran", "I identify as one or more of the classifications of protected veterans",
              "I do not wish to self-identify"]
WD_DISABILITY = ["Yes, I have a disability, or have had one in the past",
                 "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
GH_GENDER = ["Male", "Female", "Decline To Self Identify"]
GH_HISPANIC = ["Yes", "No", "Decline To Self Identify"]
GH_RACE = ["American Indian or Alaskan Native", "Asian", "Black or African American", "Hispanic or Latino", "White",
           "Native Hawaiian or Other Pacific Islander", "Two or More Races", "Decline To Self Identify"]
GH_VETERAN = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
              "I don't wish to answer"]
LV_GENDER = ["Male", "Female", "Decline to self-identify"]
LV_RACE = ["Hispanic or Latino", "White (Not Hispanic or Latino)", "Black or African American (Not Hispanic or Latino)",
           "Native Hawaiian or Other Pacific Islander (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)",
           "American Indian or Alaska Native (Not Hispanic or Latino)", "Two or More Races (Not Hispanic or Latino)",
           "Decline to self-identify"]
LV_VETERAN = ["I am a veteran", "I am not a veteran", "Decline to self-identify"]
LV_DISABILITY = ["Yes, I have a disability (or previously had a disability)", "No, I don't have a disability",
                 "I don't wish to answer"]
# Phenom (careers.lamresearch.com) and iCIMS: Hispanic or Latino and race as separate questions.
PH_GENDER = ["Man", "Woman", "Non-binary", "I choose not to disclose"]
PH_ETHNICITY = ["Hispanic or Latino", "Not Hispanic or Latino", "I choose not to disclose"]
PH_RACE = ["American Indian or Alaska Native", "Asian", "Black or African American",
           "Native Hawaiian or Other Pacific Islander", "White", "Two or More Races", "I choose not to disclose"]
PH_VETERAN = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
              "I choose not to self-identify"]
IC_GENDER = ["M", "F", "I do not wish to self-identify"]
IC_HISPANIC = ["Yes, I am Hispanic or Latino", "No, I am not Hispanic or Latino", "I do not wish to self-identify"]
IC_VETERAN = ["I identify as one or more of the classifications of protected veteran listed above",
              "I am not a protected veteran", "I do not wish to self-identify"]
IC_DISABILITY = ["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability",
                 "I don't wish to answer"]
YES_NO = ["Yes", "No", "Prefer not to say"]


# --------------------------------------------------------------------------- 1. label cleaning
@pytest.mark.parametrize("label", [
    LAM_SALARY,
    "Error: The field What is your annual salary requirement? is required",
    # the fill engine had already cut "required" out of the label
    "Error: The field What is your annual salary requirement? is  and must have a value.",
    "Error-What is your annual salary requirement?",
    "Errors Found Error-What is your annual salary requirement? The field What is your annual salary "
    "requirement? is required",
    "What is your annual salary requirement? (required)",
    "* What is your annual salary requirement? *",
    "What is your annual salary requirement?*Required",
    "What is your annual salary requirement? is required",
])
def test_error_and_required_furniture_is_cleaned_before_matching(label):
    assert clean_label(label) == "What is your annual salary requirement?"
    assert normalize(label) == normalize("What is your annual salary requirement?") == "@salary"


@pytest.mark.parametrize("label,want", [
    ("2 Errors Found Error: Gender is required", "Gender"),
    ("Gender (Required)", "Gender"), ("Salary - Required", "Salary"), ("Required: Salary", "Salary"),
    ("What is your salary? This field is required.", "What is your salary?"),
    # a question's own words stay
    ("Visa sponsorship required", "Visa sponsorship required"),
    ("Is visa sponsorship required?", "Is visa sponsorship required?"),
    ("Which of the following describes the field you work in?", "Which of the following describes the field you work in?"),
    ("Error-free code?", "Error-free code?"),
    # nothing but furniture: no question, no key
    ("Errors Found", ""), ("This field is required", ""), ("Error: The field is required", ""),
])
def test_clean_label_keeps_the_question(label, want):
    assert clean_label(label) == want
    if not want:
        assert normalize(label) == ""


def test_an_error_wrapped_eeo_label_is_the_eeo_question():
    assert normalize("Error: The field Gender is required") == "@gender"
    assert normalize("Error-Country of citizenship") == "@citizenship"


@pytest.mark.parametrize("q,key", [
    ("Are you Hispanic or Latino?", "@hispanic_latino"), ("Are you Hispanic/Latino?", "@hispanic_latino"),
    ("Hispanic or Latino", "@hispanic_latino"), ("Do you identify as Hispanic or Latino?", "@hispanic_latino"),
    ("Are you of Hispanic or Latino origin?", "@hispanic_latino"), ("Ethnicity", "@ethnicity"),
    ("What is your ethnic group?", "@ethnicity"),
    ("Second country of citizenship", "@citizenship2"), ("Other citizenship", "@citizenship2"),
    ("Dual citizenship country", "@citizenship2"), ("Country of dual citizenship", "@citizenship2"),
    ("Additional nationality", "@citizenship2"), ("Country of citizenship", "@citizenship"),
    ("Desired annual salary (USD)", "@salary"), ("Veteran status", "@veteran"), ("Disability status", "@disability"),
])
def test_canonical_keys_of_the_card(q, key):
    assert normalize(q) == key


@pytest.mark.parametrize("q,not_key", [
    ("Race: Hispanic or Latino", "@ethnicity"), ("Ethnicity: Hispanic or Latino", "@ethnicity"),
    ("Race: Hispanic or Latino", "@hispanic_latino"), ("Ethnicity", "@hispanic_latino"),
    ("Do you hold dual citizenship?", "@citizenship2"), ("Race", "@ethnicity"),
    # legal-5: a bare "Dual citizenship" is a yes/no question, never the second country
    ("Dual citizenship", "@citizenship2"), ("Dual Citizenship?", "@citizenship2"), ("Dual nationality", "@citizenship2"),
])
def test_one_box_of_a_group_is_not_the_question(q, not_key):
    assert normalize(q) != not_key


def test_saved_question_text_is_the_clean_question(tmp_path):
    with AnswerLibrary(tmp_path / "a.db") as lib:
        row = lib.save(LAM_SALARY, "130000", "you")
        assert row["key"] == "@salary" and row["question"] == "What is your annual salary requirement?"


# --------------------------------------------------------------------------- the re-key
def _old_db(path, rows):
    AnswerLibrary(path).close()
    con = sqlite3.connect(path)
    for key, q, a, at in rows:
        con.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                    " VALUES (?,?,?,'you',?,?,0)", (key, q, a, at, at))
    con.execute("UPDATE meta SET v='old' WHERE k='keys_version'")
    con.commit()
    con.close()


OLD_KEY = "and annual error field have must requirement salary value"   # the key that label had before


def test_rekey_moves_an_error_label_salary_to_the_salary_key_when_it_is_free(tmp_path):
    path = tmp_path / "a.db"
    _old_db(path, [(OLD_KEY, LAM_SALARY, "130000", 1.0)])
    with AnswerLibrary(path) as lib:
        assert [r["key"] for r in lib.list()] == ["@salary"]
        hit = lib.lookup("What are your salary expectations?")
        assert hit["answer"] == "130000" and hit["question"] == "What is your annual salary requirement?"


def test_rekey_leaves_the_row_in_place_when_the_salary_key_is_taken(tmp_path):
    """Never a DELETE: both rows stay. The newer one (the agent's answer on the error label) is
    what the library gives, and what Your answers shows."""
    path = tmp_path / "a.db"
    _old_db(path, [("@salary", "What are your salary expectations?", "120000", 1.0),
                   (OLD_KEY, LAM_SALARY, "130000", 2.0)])
    with AnswerLibrary(path) as lib:
        rows = {r["key"]: r["answer"] for r in lib.list()}
        assert rows == {"@salary": "120000", OLD_KEY: "130000"}          # nothing deleted, nothing moved
        assert lib.lookup("Desired compensation")["answer"] == "130000"   # the most recent
        assert lib.count() == 2
    # a later save under the canonical key is the newest, and wins
    with AnswerLibrary(path) as lib:
        lib.save("Desired annual salary (USD)", "125000", "you")
        assert lib.lookup(LAM_SALARY)["answer"] == "125000" and lib.count() == 2


def test_a_drafted_newer_salary_never_beats_the_persons_own(tmp_path):
    path = tmp_path / "a.db"
    _old_db(path, [(OLD_KEY, LAM_SALARY, "130000", 1.0)])
    con = sqlite3.connect(path)
    con.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                " VALUES ('@salary', 'Expected salary', '$1', 'drafted', 5, 5, 0)")
    con.commit()
    con.close()
    with AnswerLibrary(path) as lib:
        assert lib.lookup("Salary requirements")["answer"] == "130000"


# --------------------------------------------------------------------------- 3. EEO on each form's wording
@pytest.mark.parametrize("opts", [GH_GENDER, LV_GENDER, WD_GENDER, PH_GENDER, IC_GENDER])
def test_gender_lands_on_each_forms_wording(opts):
    assert choose("@gender", opts, {"@gender": "Male"}) == opts[0]
    assert choose("@gender", opts, {"@gender": "Female"}) == opts[1]
    assert choose("@gender", opts, {"@gender": "Prefer not to say"}) == opts[-1]   # the page's own decline


def test_non_binary_and_a_list_without_it():
    assert choose("@gender", PH_GENDER, {"@gender": "Non-binary"}) == "Non-binary"
    assert choose("@gender", ["Woman", "Man", "Non-binary/non-conforming", "Prefer not to say"],
                  {"@gender": "Non-binary"}) == "Non-binary/non-conforming"
    assert choose("@gender", ["Gender non-conforming", "Male", "Female"], {"@gender": "Non-binary"}) == "Gender non-conforming"
    assert choose("@gender", GH_GENDER, {"@gender": "Non-binary"}) is None        # not offered: theirs to answer


@pytest.mark.parametrize("opts,yes,no", [
    (GH_HISPANIC, "Yes", "No"), (PH_ETHNICITY, "Hispanic or Latino", "Not Hispanic or Latino"),
    (IC_HISPANIC, "Yes, I am Hispanic or Latino", "No, I am not Hispanic or Latino"),
])
def test_hispanic_or_latino(opts, yes, no):
    assert choose("@ethnicity", opts, {"@ethnicity": "Yes"}) == yes
    assert choose("@ethnicity", opts, {"@ethnicity": "No"}) == no
    assert choose("@ethnicity", opts, {"@ethnicity": "Prefer not to say"}) == opts[-1]
    assert choose("@ethnicity", opts, {}) is None


@pytest.mark.parametrize("race,want", [
    ("American Indian or Alaska Native", "American Indian or Alaska Native"), ("Asian", "Asian"),
    ("Black or African American", "Black or African American"),
    ("Native Hawaiian or Other Pacific Islander", "Native Hawaiian or Other Pacific Islander"),
    ("White", "White"), ("Two or more races", "Two or More Races"), ("Prefer not to say", "I choose not to disclose"),
])
def test_race_on_a_race_list(race, want):
    assert choose("@race", PH_RACE, {"@race": race}) == want


def test_greenhouse_race_alaskan_spelling():
    assert choose("@race", GH_RACE, {"@race": "American Indian or Alaska Native", "@ethnicity": "No"}) == \
        "American Indian or Alaskan Native"


@pytest.mark.parametrize("opts", [WD_ETHNICITY, LV_RACE])
def test_combined_race_and_ethnicity_lists(opts):
    """Workday's "Ethnicity" and Lever's "Race": the Hispanic choice for a Hispanic or Latino
    person, else the saved race's own option (with its "(Not Hispanic or Latino)"), the decline
    for a decline on either, and nothing while the other half is unknown."""
    key = "@ethnicity" if opts is WD_ETHNICITY else "@race"
    hisp = next(o for o in opts if o.startswith("Hispanic"))
    dec = next(o for o in opts if "wish" in o or "Decline" in o)
    assert choose(key, opts, {"@race": "Asian", "@ethnicity": "Yes"}) == hisp
    assert choose(key, opts, {"@race": "Asian", "@ethnicity": "No"}).startswith("Asian (Not Hispanic or Latino)")
    assert choose(key, opts, {"@race": "Two or more races", "@ethnicity": "No"}).startswith("Two or More Races")
    assert choose(key, opts, {"@race": "Black or African American", "@ethnicity": "No"}).startswith("Black or African American")
    assert choose(key, opts, {"@race": "White", "@ethnicity": "Prefer not to say"}) == dec
    assert choose(key, opts, {"@race": "Prefer not to say", "@ethnicity": "No"}) == dec
    assert choose(key, opts, {"@race": "Asian"}) is None                   # Hispanic or Latino unknown: ask
    assert choose(key, opts, {"@ethnicity": "No"}) is None                 # race unknown: ask


def test_greenhouse_race_list_with_hispanic_is_combined_too():
    assert choose("@race", GH_RACE, {"@race": "Asian", "@ethnicity": "Yes"}) == "Hispanic or Latino"
    assert choose("@race", GH_RACE, {"@race": "Asian", "@ethnicity": "No"}) == "Asian"


def test_an_answer_picked_on_a_combined_list_counts_for_both_halves():
    picked = "Asian (Not Hispanic or Latino) (United States of America)"
    assert choose("@ethnicity", WD_ETHNICITY, {"@ethnicity": picked}) == picked
    assert choose("@race", LV_RACE, {"@ethnicity": picked}) == "Asian (Not Hispanic or Latino)"
    assert choose("@ethnicity", PH_ETHNICITY, {"@ethnicity": picked}) == "Not Hispanic or Latino"


@pytest.mark.parametrize("opts,not_,protected,question", [
    (GH_VETERAN, "I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
     "Veteran Status"),
    # legal-8: "not a PROTECTED veteran" is not "not a veteran" (a veteran who is not protected
    # served): an any-service list is left open, and the person is asked once
    (WD_VETERAN, None, "I identify as one or more of the classifications of protected veterans", "Veteran Status"),
    (PH_VETERAN, "I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
     "Veteran Status"),
    (IC_VETERAN, "I am not a protected veteran",
     "I identify as one or more of the classifications of protected veteran listed above", "Veteran Status"),
    (LV_VETERAN, None, "I am a veteran", "Veteran Status"),
    (YES_NO, "No", "Yes", "Are you a protected veteran?"),
    (YES_NO, None, "Yes", "Veteran"),                       # a bare No could mean "never served"
])
def test_veteran_status(opts, not_, protected, question):
    assert choose("@veteran", opts, {"@veteran": "I am not a protected veteran"}, question=question) == not_
    assert choose("@veteran", opts, {"@veteran": "I am a protected veteran"}, question=question) == protected
    assert choose("@veteran", opts, {"@veteran": "Prefer not to say"}, question=question) == opts[-1]


def test_a_saved_never_served_answer_fits_every_veteran_list():
    """A reply "I am not a veteran" (asked once on an any-service list) is also "not a protected
    veteran", and a plain No to either question."""
    saved = {"@veteran": "I am not a veteran"}
    assert choose("@veteran", WD_VETERAN, saved) == "I am not a veteran"
    assert choose("@veteran", GH_VETERAN, saved) == "I am not a protected veteran"
    assert choose("@veteran", YES_NO, saved, question="Veteran") == "No"


def test_veteran_never_guessed():
    # data-2: a plain "Yes" is not a protected-veteran self-identification
    assert choose("@veteran", GH_VETERAN, {"@veteran": "Yes"}) is None
    # two "not" choices (a veteran who is not protected): which one is the person's? Ask.
    both = ["I am not a veteran", "I am a veteran but not a protected veteran",
            "I identify as one or more of the classifications of protected veteran", "I do not wish to self-identify"]
    assert choose("@veteran", both, {"@veteran": "I am not a protected veteran"}) is None


@pytest.mark.parametrize("opts", [WD_DISABILITY, LV_DISABILITY, IC_DISABILITY, YES_NO])
def test_disability(opts):
    assert choose("@disability", opts, {"@disability": "No"}) == opts[1]
    assert choose("@disability", opts, {"@disability": "Yes"}) == opts[0]
    assert choose("@disability", opts, {"@disability": "Prefer not to say"}) == opts[-1]


def test_nothing_matches_and_nothing_is_guessed():
    assert choose("@gender", ["Option A", "Option B"], {"@gender": "Male"}) is None
    assert choose("@race", ["Group 1", "Group 2"], {"@race": "Asian", "@ethnicity": "No"}) is None
    assert choose("@disability", ["Yes, I need an accommodation", "No"], {"@disability": "Yes"}) is None
    assert choose("@gender", GH_GENDER, {}) is None
    # a decline with no single decline choice on the page
    assert choose("@gender", ["Male", "Female"], {"@gender": "Prefer not to say"}) is None


# --------------------------------------------------------------------------- the planner and the asks
def test_the_planner_fills_a_workday_combined_list_from_two_saved_answers(tmp_path):
    with AnswerLibrary(tmp_path / "a.db") as lib:
        lib.save("Race", "Asian", "you")
        lib.save("Are you Hispanic or Latino?", "No", "you")
        lib.save("Gender", "Male", "you")
        lib.save("Disability status", "No", "you")
        fields = [FormField("e", "Ethnicity", "select", WD_ETHNICITY),
                  FormField("g", "Gender", "select", PH_GENDER),
                  FormField("v", "Veteran Status", "select", WD_VETERAN)]
        fields += [FormField(f"d{i}", "Disability Status: " + o, "checkbox") for i, o in enumerate(WD_DISABILITY)]
        res = plan_known(fields, KnownContext(library=lib))
        by = {a.ref: a.value for a in res.actions}
        assert by == {"e": WD_ETHNICITY[1], "g": "Man", "d1": "Yes"}
        assert {f.ref for f in res.left} == {"v", "d0", "d2"}          # veteran not saved: theirs
        assert sorted(res.used_library) == ["@disability", "@gender", "@hispanic_latino", "@race"]


def test_the_standing_decline_wins_over_a_saved_answer(tmp_path):
    """legal-1: while the decline is on, a saved self-identification answer is never used. A
    field with its decline choice gets it; any other (no decline choice, a dropdown whose list
    is not read yet, a text box) stays open, and the agent's ask reads the page's choices."""
    with AnswerLibrary(tmp_path / "a.db") as lib:
        for q, a in (("Gender", "Male"), ("Race", "Asian"), ("Are you Hispanic or Latino?", "No"),
                     ("Veteran status", "I am a protected veteran"), ("Disability status", "Yes")):
            lib.save(q, a, "you")
        fields = [FormField("g", "Gender", "select", GH_GENDER),
                  FormField("m", "Gender", "select", ["Male", "Female"]),
                  FormField("c", "Gender", "combobox"),
                  FormField("r", "Race", "combobox", []),
                  FormField("t", "Ethnicity", "text"),
                  FormField("v", "Veteran Status", "radio", ["Yes", "No"])]
        fields += [FormField(f"d{i}", "Disability Status: " + o, "checkbox") for i, o in enumerate(["Yes", "No"])]
        res = plan_known(fields, KnownContext(library=lib, eeo_decline=True))
        assert [(a.ref, a.value, a.origin) for a in res.actions] == [("g", "Decline To Self Identify", "eeo")]
        assert {f.ref for f in res.left} == {"m", "c", "r", "t", "v", "d0", "d1"}
        assert AK.known_answer("Gender", GH_GENDER, library=lib, eeo_decline=True) == ("Decline To Self Identify", "eeo_decline")
        for q, opts, partial in (("Gender", [], False), ("Gender", ["Male", "Female"], True), ("Race", [], False),
                                 ("Veteran Status", ["Yes", "No"], False)):
            assert AK.known_answer(q, opts, partial=partial, library=lib, eeo_decline=True) is None


def test_known_answer_uses_the_forms_wording(tmp_path):
    with AnswerLibrary(tmp_path / "a.db") as lib:
        lib.save("Veteran status", "I am not a protected veteran", "you")
        lib.save("Race", "White", "you")
        lib.save("Are you Hispanic or Latino?", "No", "you")
        assert AK.known_answer("Veteran Status", GH_VETERAN, library=lib) == ("I am not a protected veteran", "library")
        assert AK.known_answer("Veteran Status", WD_VETERAN, library=lib) is None      # any service: asked
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib) == (WD_ETHNICITY[6], "library")
        assert AK.known_answer("Are you Hispanic/Latino?", GH_HISPANIC, library=lib) == ("No", "library")
        assert AK.known_answer("Are you a protected veteran?", ["Yes", "No"], library=lib) == ("No", "library")
        # an error-wrapped label of the same question
        assert AK.known_answer("Error: The field Veteran Status is required", GH_VETERAN, library=lib) == \
            ("I am not a protected veteran", "library")


def test_countries_match_the_forms_list():
    wd = ["United States Minor Outlying Islands", "United States of America", "Korea, Republic of", "Viet Nam", "Ghana"]
    assert country_option("United States", wd) == "United States of America"
    assert country_option("South Korea", wd) == "Korea, Republic of"
    assert country_option("Vietnam", wd) == "Viet Nam"
    assert country_option("Atlantis", wd) is None
    assert canonical("USA") == "United States" and canonical("Czechia") == "Czech Republic"
    assert canonical("Congo") == ""                             # two countries: never a guess
    from autoapply import countries
    assert list(NAMES) == sorted(NAMES, key=countries._key)     # the list reads in order, accents aside
    assert all(canonical(n) == n for n in NAMES)


def test_citizenship_on_a_select_and_a_list_each_text_box(tmp_path):
    lam = ("For the sole purpose of determining export licensing requirements, please provide your most recent "
           "country/region of citizenship or legal permanent residence (if you are a citizen of more than one, "
           "list each)")
    with AnswerLibrary(tmp_path / "a.db") as lib:
        lib.save("Country of citizenship", "United States", "you")
        lib.save("Second country of citizenship", "Ghana", "you")
        res = plan_known([FormField("s", "Country of citizenship", "select", ["Canada", "United States of America"]),
                          FormField("t", lam, "text"), FormField("u", "Nationality", "text")], KnownContext(library=lib))
        by = {a.ref: a.value for a in res.actions}
        assert by == {"s": "United States of America", "t": "United States, Ghana", "u": "United States"}
        assert AK.known_answer(lam, [], library=lib) == ("United States, Ghana", "library")


# --------------------------------------------------------------------------- 2. the routes
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def _db_rows(A):
    con = sqlite3.connect(A._ANSWERS_DB)
    try:
        return {k: a for k, a in con.execute("SELECT key, answer FROM answers")}
    finally:
        con.close()


def test_get_basics_empty(app):
    A, c = app
    d = c.get("/api/answers/basics").get_json()
    assert d["ok"] and d["total"] == 7 and d["answered"] == 0
    assert d["missing"] == ["citizenship", "salary", "gender", "hispanic", "race", "veteran", "disability"]
    assert d["values"]["gender"] == "" and len(d["countries"]) > 190 and "United States" in d["countries"]
    assert [c["code"] for c in d["choices"]["gender"]] == ["male", "female", "nonbinary", "decline"]


def test_post_basics_saves_canonical_keys_and_the_salary_pref(app):
    A, c = app
    r = c.post("/api/answers/basics", json={"fields": {
        "citizenship": "Ghana", "citizenship2": "Canada", "salary": "$130,000", "gender": "male", "hispanic": "no",
        "race": "black", "veteran": "not", "disability": "no"}}).get_json()
    assert r["answered"] == 7 and r["missing"] == []
    assert r["values"]["salary"] == "130000" and r["values"]["race"] == "black"
    rows = _db_rows(A)
    assert rows == {"@citizenship": "Ghana", "@citizenship2": "Canada", "@salary": "130000", "@gender": "Male",
                    "@hispanic_latino": "Not Hispanic or Latino", "@race": "Black or African American",
                    "@veteran": "I am not a protected veteran", "@disability": "No"}
    assert A._load_prefs()["desired_salary"] == "130000"                 # the browser extension fills it too
    lst = c.get("/api/answers").get_json()["answers"]
    assert {x["key"]: x["source"] for x in lst}["@gender"] == "you"
    # the agent's own planner reads them
    lib = A._answer_lib()
    try:
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib) == (WD_ETHNICITY[2], "library")
    finally:
        lib.close()


def test_post_only_changes_what_is_sent_and_clears_on_empty(app):
    A, c = app
    c.post("/api/answers/basics", json={"fields": {"gender": "female", "salary": "120k"}})
    r = c.post("/api/answers/basics", json={"fields": {"gender": ""}}).get_json()
    assert r["values"]["gender"] == "" and r["values"]["salary"] == "120000"
    assert "@gender" not in _db_rows(A) and _db_rows(A)["@salary"] == "120000"
    c.post("/api/answers/basics", json={"fields": {"salary": ""}})
    assert "@salary" not in _db_rows(A) and A._load_prefs()["desired_salary"] == ""


def test_a_bad_salary_is_refused_and_nothing_is_written(app):
    A, c = app
    r = c.post("/api/answers/basics", json={"fields": {"gender": "male", "salary": "lots"}})
    assert r.status_code == 400 and "number" in r.get_json()["error"]
    assert _db_rows(A) == {}


def test_prefill_ignores_unusable_rows_and_takes_the_newest_salary(app):
    """The Lam run's raw replies show as empty (the agent asks again); of two salaries that
    disagree, the most recently updated one shows."""
    A, c = app
    c.get("/api/answers/basics")                                         # the library exists
    now = time.time()
    con = sqlite3.connect(A._ANSWERS_DB)
    for key, q, a, at in [("@gender", "Gender", LAM_REPLY, now - 50),
                          ("@ethnicity", "Ethnicity", "What are the options under ethnicity ?", now - 40),
                          ("@salary", "What are your salary expectations?", "$120,000", now - 30),
                          (OLD_KEY, LAM_SALARY, "130000", now - 10),
                          ("@citizenship", "Country of citizenship", "USA", now - 5)]:
        con.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                    " VALUES (?,?,?,'you',?,?,0)", (key, q, a, at, at))
    con.commit()
    con.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["gender"] == "" and d["values"]["hispanic"] == ""
    assert d["values"]["salary"] == "130000"
    assert d["values"]["citizenship"] == "United States"
    assert d["answered"] == 2 and "gender" in d["missing"]
    assert len(_db_rows(A)) == 5                                         # shown empty, never deleted


def test_a_combined_list_pick_shows_both_halves_and_survives_an_edit(app):
    """An answer picked on Workday's combined list says race AND Hispanic or Latino: the card
    shows both, and changing one keeps the other as the person's own answer."""
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Ethnicity", WD_ETHNICITY[1], "you")                 # Asian (Not Hispanic or Latino) ...
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["race"] == "asian" and d["values"]["hispanic"] == "no"
    r = c.post("/api/answers/basics", json={"fields": {"hispanic": "yes"}}).get_json()
    assert r["values"]["race"] == "asian" and r["values"]["hispanic"] == "yes"
    rows = _db_rows(A)
    assert rows["@hispanic_latino"] == "Hispanic or Latino" and rows["@ethnicity"] == WD_ETHNICITY[1]
    # and the agent reads the person's newer Hispanic or Latino answer first
    lib = A._answer_lib()
    try:
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib) == (WD_ETHNICITY[3], "library")
    finally:
        lib.close()


def test_own_words_are_never_replaced_by_a_derived_value(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Race", "Asian Indian", "you")                         # a subgroup some forms offer
        lib.save("Ethnicity", WD_ETHNICITY[6], "you")                   # White (Not Hispanic or Latino)
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["race"] == "Asian Indian" and "race" in d["other"]
    assert choose("@race", LV_RACE, {"@race": "Asian Indian", "@ethnicity": WD_ETHNICITY[6]}) is None   # ask


def test_unclassified_saved_words_show_as_they_are(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Veteran Status", "I am a veteran", "you")              # Lever's wording: any service
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["veteran"] == "I am a veteran" and "veteran" in d["other"] and "veteran" not in d["missing"]


def test_eeo_decline_counts_the_five_as_answered(app):
    A, c = app
    r = c.post("/api/answers/basics", json={"fields": {"citizenship": "Ghana"}, "eeo_decline": True}).get_json()
    assert r["eeo_decline"] is True and r["answered"] == 6 and r["missing"] == ["salary"]
    assert A._load_prefs()["eeo_decline"] is True
    r = c.post("/api/answers/basics", json={"fields": {}, "eeo_decline": False}).get_json()
    assert r["answered"] == 1 and len(r["missing"]) == 6


def test_basics_routes_are_guarded_like_the_other_answers_routes(app):
    A, _ = app
    import inspect
    src = inspect.getsource(A)
    for route in ('@app.get("/api/answers/basics")\n@_guard', '@app.post("/api/answers/basics")\n@_guard'):
        assert route in src


# --------------------------------------------------------------------------- the page
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")


def _section(html, start, end_marker="</section>\n      <!-- Why was this skipped?"):
    return html[html.index(start):html.index(end_marker)]


def test_the_card_sits_between_work_authorization_and_saved_answers_with_one_accent():
    view = _section(HTML, 'id="view-answers"')
    wa, basics, saved = view.index('id="ansWaCard"'), view.index('id="ansBasicsCard"'), view.index('id="ansLibH"')
    assert wa < basics < saved
    assert view.count("btn-primary") == 1                                # Work authorization's Save
    card = view[basics:saved]
    assert 'id="ansBasicsSave"' in card and "btn btn-ghost btn-sm\" id=\"ansBasicsSave\"" in card
    assert 'id="ansEeo"' in card and 'id="ansEeo"' not in view[wa:basics]   # the decline sits by its rows
    assert card.index('id="ansEeo"') < card.index('id="ansEeoRows"')          # above them: the box never moves
    for f in ("gender", "hispanic", "disability"):
        assert f'data-basic="{f}"' in card
    assert 'id="ansRace"' in card and 'id="ansVet"' in card and 'id="ansCit2"' in card


def test_the_prompt_is_local_and_once_per_session():
    i = APP_JS.index("async function aaBasicsGate(go)")
    gate = APP_JS[i:APP_JS.index("\n  }\n", i)]
    assert "sessionStorage" in gate and "/api/answers/basics" in gate
    assert "telegram" not in gate.lower()
    fill = APP_JS[APP_JS.index("async function aaFill("):APP_JS.index("const BAS_ASKED_KEY")]
    assert "await aaBasicsGate(" in fill and "basicsDone: true" in fill


def test_the_shell_asks_the_clean_question():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const A = require(%r);
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " != " + JSON.stringify(b)); };
const raw = %r, q = "What is your annual salary requirement?";
eq(A.cleanQuestion(raw), q);
eq(A.cleanQuestion("Error: The field What is your annual salary requirement? is  and must have a value."), q);
eq(A.cleanQuestion("Errors Found Error-" + q + " The field " + q + " is required"), q);
eq(A.cleanQuestion("Gender (Required)"), "Gender");
eq(A.cleanQuestion("Visa sponsorship required"), "Visa sponsorship required");
eq(A.cleanQuestion("Error-free code?"), "Error-free code?");
eq(A.cleanQuestion("Errors Found"), "");
eq(A.sameLabel(raw, q), true);
const view = '[3]<input label="' + raw + '" value="" required>\n';
eq(A.questionsFor(view, [raw], false), [q]);                 // asked without the error round it
eq(A.indexFor(view, q), 3);                                  // and still found on the page
console.log("ok");
""" % (str(ROOT / "shell-electron" / "agent.js"), LAM_SALARY)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_english_strings_exist_and_every_locale_has_them():
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    for k in ("ans.basics", "ans.basicsSub", "ans.citizenship", "ans.salary", "ans.eeoNote", "aa.basics.ask",
              "aa.basics.answer", "aa.basics.anyway"):
        assert k in en
    assert en["ans.basicsSub"] == "{n} of {m} form answers saved"
    assert en["aa.basics.ask"] == "Before I apply: {n} questions every form asks."


# =========================================================================== review fixes
# --------------------------------------------------------------------------- label cleaning gaps
@pytest.mark.parametrize("label,want,key", [
    ("Gender is required", "Gender", "@gender"),
    ("Gender is", "Gender", "@gender"),                       # the engine cut "required" out
    ("Veteran Status is required", "Veteran Status", "@veteran"),
    ("Ethnicity is required and must have a value.", "Ethnicity", "@ethnicity"),
    ("Errors Found Error-Gender The field Gender is required", "Gender", "@gender"),
    ("Errors Found Error-Veteran Status The field Veteran Status is required", "Veteran Status", "@veteran"),
])
def test_labels_without_a_question_mark_are_cleaned_too(label, want, key):
    assert clean_label(label) == want and normalize(label) == key


def test_the_shell_cleans_the_same_shapes():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const A = require(%r);
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " != " + JSON.stringify(b)); };
eq(A.cleanQuestion("Gender is required"), "Gender");
eq(A.cleanQuestion("Gender is"), "Gender");
eq(A.cleanQuestion("Errors Found Error-Gender The field Gender is required"), "Gender");
eq(A.cleanQuestion("What is your salary? This field is required."), "What is your salary?");
eq(A.cleanQuestion("Visa sponsorship required"), "Visa sponsorship required");
console.log("ok");
""" % str(ROOT / "shell-electron" / "agent.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_the_views_80_character_cut_of_an_error_label_is_still_the_same_field():
    """data-samelabel-80-cut: the view cleans a label (cutting the word "required") and cuts it
    at 80 characters; the engine keeps it whole. Lam's salary box, already filled under a stale
    error, is never re-asked, and a select under that label still gives its choices."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const A = require(%r);
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " != " + JSON.stringify(b)); };
const cl = (t) => String(t || "").replace(/\s+/g, " ").replace(/\b(select one|required)\b/ig, "").replace(/[*✱]+/g, "").trim();
const raw = %r, q = "What is your annual salary requirement?";
const engine = cl(raw), view = engine.slice(0, 80);
eq(view.length, 80);
eq(A.sameLabel(view, engine), true);
eq(A.sameLabel(view, q), true);
eq(A.questionsFor('[3]<input type=text label="' + view + '" value="(filled)" required>', [engine]), []);
const sel = '[5]<select label="' + view + '" value="" options=3 first="Select...|100000|150000" required>';
eq(A.questionsFor(sel, [engine]), [q]);
eq(A.indexFor(sel, q), 5);
eq(A.optionsFor(sel, q).options, ["100000", "150000"]);
eq(A.sameLabel("Gender", "Gender is a protected category"), false);
console.log("ok");
""" % (str(ROOT / "shell-electron" / "agent.js"), LAM_SALARY)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_an_answered_self_identification_radio_group_is_not_re_asked():
    if not shutil.which("node"):
        pytest.skip("needs node")
    js = r"""
const A = require(%r);
const v = '[4]<radio label="Gender" option="Male" checked=hidden answered=true required>\n'
        + '[5]<radio label="Gender" option="Female" checked=hidden answered=true required>\n'
        + '[6]<radio label="Disability" option="Yes" checked=hidden answered=false required>';
const got = JSON.stringify(A.questionsFor(v, ["Gender", "Disability"]));
if (got !== JSON.stringify(["Disability"])) throw new Error(got);
console.log("ok");
""" % str(ROOT / "shell-electron" / "agent.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# --------------------------------------------------------------------------- dual citizenship (legal-5)
def test_a_yes_no_dual_citizenship_reply_never_becomes_the_second_country(tmp_path):
    lam = ("For the sole purpose of determining export licensing requirements, please provide your most recent "
           "country/region of citizenship or legal permanent residence (if you are a citizen of more than one, "
           "list each)")
    with AnswerLibrary(tmp_path / "a.db") as lib:
        from autoapply import basics
        basics.write(lib, {"citizenship": "Ghana", "citizenship2": "Canada"})
        res = plan_known([FormField("d", "Dual Citizenship?", "radio", ["Yes", "No"])], KnownContext(library=lib))
        assert [f.ref for f in res.left] == ["d"]
        lib.save("Dual Citizenship?", "Yes", "you")                     # the person's reply on the bar
        assert basics.read(lib)["values"]["citizenship2"] == "Canada"
        assert AK.known_answer(lam, [], library=lib) == ("Ghana, Canada", "library")
        # a "Yes" that did land on the second-country key is never a country
        lib.conn.execute("UPDATE answers SET answer='Yes' WHERE key='@citizenship2'")
        lib.conn.commit()
        assert basics.read(lib)["values"]["citizenship2"] == ""
        assert AK.known_answer(lam, [], library=lib) == ("Ghana", "library")
        assert lib.save("Second country of citizenship", "No", "you") is None


def test_an_old_dual_citizenship_answer_moves_off_the_second_country_key(tmp_path):
    path = tmp_path / "a.db"
    _old_db(path, [("@citizenship2", "Dual citizenship", "No", 1.0)])
    with AnswerLibrary(path) as lib:
        assert lib.get_key("@citizenship2") is None and lib.lookup("Dual citizenship")["answer"] == "No"
        assert lib.count() == 1


# --------------------------------------------------------------------------- sanctions screens (legal-3)
CUBA_Q = "Are you a citizen of Cuba, Iran, North Korea, Syria, or the Crimea region of Ukraine?"
EMBARGO_Q = "Are you a citizen or national of a country subject to U.S. embargo or sanctions (Cuba, Iran, North Korea, Syria)?"
US_CITIZEN = {"countries": {"US": {"status": "us_citizen"}}}
IRAN_H1B = {"countries": {"US": {"status": "h1b"}}}


@pytest.mark.parametrize("q", [CUBA_Q, EMBARGO_Q])
def test_a_sanctions_screen_is_never_answered_from_the_work_authorization_profile(tmp_path, q):
    from autoapply import workauth
    assert workauth.classify(q) is None
    assert workauth.answer(q, US_CITIZEN, options=["Yes", "No"]) is None
    with AnswerLibrary(tmp_path / "a.db") as lib:
        # nothing saved about citizenship: the person answers it
        assert AK.known_answer(q, ["Yes", "No"], library=lib, work_auth=US_CITIZEN) is None
        res = plan_known([FormField("s", q, "radio", ["Yes", "No"])], KnownContext(library=lib, work_auth=US_CITIZEN))
        assert [f.ref for f in res.left] == ["s"]


@pytest.mark.parametrize("q", [CUBA_Q, EMBARGO_Q])
def test_a_sanctions_screen_is_answered_from_the_saved_countries_of_citizenship(tmp_path, q):
    from autoapply import basics
    with AnswerLibrary(tmp_path / "us.db") as lib:
        basics.write(lib, {"citizenship": "United States"})
        assert AK.known_answer(q, ["Yes", "No"], library=lib, work_auth=US_CITIZEN) == ("No", "library")
        res = plan_known([FormField("s", q, "select", ["Yes", "No"])], KnownContext(library=lib, work_auth=US_CITIZEN))
        assert [(a.ref, a.value) for a in res.actions] == [("s", "No")]
    with AnswerLibrary(tmp_path / "ir.db") as lib:
        basics.write(lib, {"citizenship": "Ghana", "citizenship2": "Iran"})
        assert AK.known_answer(q, ["Yes", "No"], library=lib, work_auth=IRAN_H1B) == ("Yes", "library")
        assert AK.known_answer(q, [], library=lib, work_auth=IRAN_H1B) == ("Yes", "library")


def test_a_screen_about_residence_too_gets_a_yes_but_never_a_no(tmp_path):
    from autoapply import basics
    q = "Are you a citizen or permanent resident of Cuba, Iran, North Korea or Syria?"
    with AnswerLibrary(tmp_path / "a.db") as lib:
        basics.write(lib, {"citizenship": "Ghana"})
        assert AK.known_answer(q, ["Yes", "No"], library=lib) is None
        basics.write(lib, {"citizenship": "Syria"})
        assert AK.known_answer(q, ["Yes", "No"], library=lib) == ("Yes", "library")


def test_the_job_countrys_citizen_question_still_comes_from_the_profile(tmp_path):
    from autoapply import workauth
    assert workauth.classify("Are you a US citizen?") == "citizen"
    assert workauth.classify("Are you legally authorized to work in Germany?") == "authorized"
    assert workauth.classify("Are you legally authorized to work in India?") is None     # not a profile country
    with AnswerLibrary(tmp_path / "a.db") as lib:
        assert AK.known_answer("Are you a US citizen?", ["Yes", "No"], library=lib, work_auth=US_CITIZEN) == ("Yes", "workauth")


# --------------------------------------------------------------------------- residence (legal-9)
LAM_EXPORT = ("For the sole purpose of determining export licensing requirements, please provide your most recent "
              "country/region of citizenship or legal permanent residence (if you are a citizen of more than one, "
              "list each)")


def test_the_lam_export_question_is_left_to_a_permanent_resident(tmp_path):
    from autoapply import basics
    gc = {"countries": {"US": {"status": "green_card"}}}
    with AnswerLibrary(tmp_path / "a.db") as lib:
        basics.write(lib, {"citizenship": "Ghana"})
        assert AK.known_answer(LAM_EXPORT, [], library=lib, work_auth=gc) is None
        res = plan_known([FormField("t", LAM_EXPORT, "text")], KnownContext(library=lib, work_auth=gc))
        assert [f.ref for f in res.left] == ["t"]
        # without a permanent residence anywhere, the citizenship answers it
        assert AK.known_answer(LAM_EXPORT, [], library=lib, work_auth=IRAN_H1B) == ("Ghana", "library")
        # the card's own question never mentions residence
        assert AK.known_answer("Country of citizenship", [], library=lib, work_auth=gc) == ("Ghana", "library")


def test_the_step_route_leaves_the_lam_export_question_to_a_permanent_resident(app, monkeypatch):
    A, c = app
    c.post("/api/answers/basics", json={"fields": {"citizenship": "Ghana"}})
    monkeypatch.setattr(A, "_work_auth", lambda: {"countries": {"US": {"status": "green_card"}}})
    assert A._known_answers_for([LAM_EXPORT, "Country of citizenship"], None, {}, {}) == {"Country of citizenship": "Ghana"}


# --------------------------------------------------------------------------- self-identification on the step route
def _seed_card(c, **extra):
    fields = {"gender": "male", "race": "asian", "hispanic": "no", "veteran": "protected", "disability": "yes"}
    return c.post("/api/answers/basics", json={"fields": fields, **extra}).get_json()


def test_the_step_route_never_hands_the_brain_a_self_identification_answer(app):
    """legal-1, legal-4: EEO fields left to the brain arrive as text with no choices, where a
    saved "No" or "Asian" is half an answer and the decline cannot be honored: they go to the
    ask instead, which reads the page's choices (and picks the decline while it is on)."""
    A, c = app
    labels = ["Gender", "Race", "Ethnicity", "Are you Hispanic or Latino?", "Veteran Status", "Disability Status",
              "Race/Ethnicity"]
    for decline in (False, True):
        _seed_card(c, eeo_decline=decline)
        assert A._known_answers_for(labels, None, {}, {}) == {}
    lib = A._answer_lib()
    try:
        # the ask (known_only) reads the choices: the decline while it is on
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib, eeo_decline=True) == (WD_ETHNICITY[-1], "eeo_decline")
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib) == (WD_ETHNICITY[1], "library")
    finally:
        lib.close()


class _SpyLLM:
    def __init__(self):
        self.user = []

    def agent_step(self, system, user):
        self.user.append(user)
        return json.dumps({"memory": "", "evaluation": "", "actions": [], "done": {"outcome": "blocked", "reason": "needs-person"}})


def test_choices_the_engine_made_on_self_identification_never_reach_the_model(app, monkeypatch):
    """legal-2: the engine filled Workday's Voluntary Disclosures page; the view the shell sends
    still carries the choices (an older view), and the brain's prompt must not."""
    A, c = app
    _seed_card(c)
    spy = _SpyLLM()
    monkeypatch.setattr(A, "_make_llm", lambda: spy)
    view = ("URL: https://acme.wd1.myworkdayjobs.com/apply\nTitle: Apply\n[Start of page]\n"
            "[3]<combobox label=\"Gender\" value=\"Male\" expanded=false required>\n"
            "[4]<select label=\"Ethnicity\" value=\"Asian (Not Hispanic or Latino) (United States of America)\" options=8 first=\"A|B|C|D\" required>\n"
            "[5]<radio label=\"Please select the veteran status which most accurately describes your relationship\" "
            "option=\"I identify as one or more of the classifications of protected veterans\" checked=true required>\n"
            "[6]<radio label=\"Please select the veteran status which most accurately describes your relationship\" "
            "option=\"I am not a veteran\" checked=false required>\n"
            "[7]<checkbox label=\"Disability: Yes, I have a disability, or have had one in the past\" checked=true>\n"
            "[8]<select label=\"Country\" value=\"Canada\" options=3 first=\"A|B|C\">\n"
            "[9]<button>Save and Continue</button>\n[End of page]")
    r = c.post("/api/autoapply/agent/step", json={"record_id": None, "step": 2, "max_steps": 40,
                                                   "url": "https://acme.wd1.myworkdayjobs.com/apply", "view": view,
                                                   "left": ["Gender", "Race"], "history": []},
               headers={"X-Tailor-Extension": "1"})
    assert r.status_code == 200 and spy.user, r.get_data(as_text=True)
    prompt = spy.user[0]
    for word in ('value="Male"', 'value="Asian', "checked=true", "{{answer_"):
        assert word not in prompt, word
    assert 'value="(filled)"' in prompt and "answered=true" in prompt and 'value="Canada"' in prompt


def test_mask_self_id_keeps_the_choices_and_hides_the_choice():
    from autoapply.brain import mask_self_id
    v = ('[1]<radio label="Gender" option="Male" checked=false>\n[2]<radio label="Gender" option="Female" checked=true>\n'
         '[3]<radio label="Remote?" option="Yes" checked=true>\n*[4]<select label="Race" value="" options=3 first="A|B|C">')
    out = mask_self_id(v)
    assert 'option="Male" checked=hidden answered=true' in out and 'option="Female" checked=hidden answered=true' in out
    assert '[3]<radio label="Remote?" option="Yes" checked=true>' in out
    assert '*[4]<select label="Race" value=""' in out


# --------------------------------------------------------------------------- combined halves (legal-4)
def test_a_half_answer_never_fills_a_combined_race_field_without_its_choices(tmp_path):
    from autoapply import basics
    with AnswerLibrary(tmp_path / "a.db") as lib:
        basics.write(lib, {"race": "asian", "hispanic": "yes"})
        fields = [FormField("re", "Race/Ethnicity", "text"), FormField("r", "Race", "combobox"),
                  FormField("e", "Ethnicity", "combobox"), FormField("h", "Are you Hispanic or Latino?", "text")]
        res = plan_known(fields, KnownContext(library=lib))
        assert [(a.ref, a.value) for a in res.actions] == [("h", "Hispanic or Latino")]
        assert {f.ref for f in res.left} == {"re", "r", "e"}
        assert AK.known_answer("Race/Ethnicity", [], library=lib) is None
        # with the page's choices, both halves: the Hispanic choice
        assert AK.known_answer("Race", LV_RACE, library=lib) == ("Hispanic or Latino", "library")
        # words that say both halves fill a text box
        lib.save("Ethnicity", WD_ETHNICITY[1], "you")
        res = plan_known([FormField("e", "Ethnicity", "text")], KnownContext(library=lib))
        assert [(a.ref, a.value) for a in res.actions] == [("e", WD_ETHNICITY[1])]


# --------------------------------------------------------------------------- clearing a derived half (legal-7)
def test_clearing_a_race_read_from_a_combined_pick_sticks(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Ethnicity", WD_ETHNICITY[1], "you")                  # Asian (Not Hispanic or Latino) ...
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["race"] == "asian" and d["values"]["hispanic"] == "no"
    r = c.post("/api/answers/basics", json={"fields": {"race": ""}}).get_json()
    assert r["values"]["race"] == "" and r["values"]["hispanic"] == "no"           # the other half stays
    rows = _db_rows(A)
    assert "@ethnicity" not in rows and rows["@hispanic_latino"] == "Not Hispanic or Latino"
    lib = A._answer_lib()
    try:
        assert AK.known_answer("Race", LV_RACE, library=lib) is None                # race unknown: asked
        assert AK.known_answer("Ethnicity", WD_ETHNICITY, library=lib) is None
        res = plan_known([FormField("r", "Race", "select", LV_RACE)], KnownContext(library=lib))
        assert [f.ref for f in res.left] == ["r"]
    finally:
        lib.close()


def test_clearing_a_hispanic_answer_read_from_a_race_pick_keeps_the_race(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("Race", LV_RACE[4], "you")                            # Asian (Not Hispanic or Latino)
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["race"] == "asian" and d["values"]["hispanic"] == "no"
    r = c.post("/api/answers/basics", json={"fields": {"hispanic": ""}}).get_json()
    assert r["values"]["hispanic"] == "" and r["values"]["race"] == "asian"
    assert _db_rows(A)["@race"] == "Asian" and "@hispanic_latino" not in _db_rows(A)


# --------------------------------------------------------------------------- the UK's ethnic group (ui-4)
def test_another_ethnicity_answer_is_not_the_hispanic_answer_and_is_never_overwritten(app):
    A, c = app
    lib = A._answer_lib()
    try:
        lib.save("What is your ethnic group?", "Asian or Asian British: Indian", "you")
    finally:
        lib.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["hispanic"] == "" and "hispanic" in d["missing"]
    c.post("/api/answers/basics", json={"fields": {"hispanic": "no"}})
    rows = _db_rows(A)
    assert rows["@ethnicity"] == "Asian or Asian British: Indian" and rows["@hispanic_latino"] == "Not Hispanic or Latino"


def test_an_older_hispanic_reply_moves_to_its_own_key(tmp_path):
    path = tmp_path / "a.db"
    _old_db(path, [("@ethnicity", "Are you Hispanic or Latino?", "No", 1.0)])
    with AnswerLibrary(path) as lib:
        assert lib.get_key("@hispanic_latino")["answer"] == "No" and lib.get_key("@ethnicity") is None


# --------------------------------------------------------------------------- one salary (ui-2, ui-3)
def test_saving_the_card_salary_replaces_the_older_wording_and_an_edit_stays_on_its_row(tmp_path):
    from autoapply import basics
    path = tmp_path / "a.db"
    _old_db(path, [("@salary", "What are your salary expectations?", "120000", 1.0),
                   (OLD_KEY, LAM_SALARY, "130000", 2.0)])
    with AnswerLibrary(path) as lib:
        # an unchanged Edit and Save on the stuck row changes that row only
        row = lib.update(OLD_KEY, question="What is your annual salary requirement?", answer="130000")
        assert row["key"] == OLD_KEY and lib.get_key("@salary")["answer"] == "120000" and lib.count() == 2
        basics.write(lib, {"salary": "140000"})
        assert [(r["key"], r["answer"]) for r in lib.list()] == [("@salary", "140000")]
        assert lib.lookup(LAM_SALARY)["answer"] == "140000"


def test_saving_any_field_brings_the_extension_salary_in_line(app):
    A, c = app
    A._merge_prefs({"desired_salary": "95000"})
    c.get("/api/answers/basics")
    con = sqlite3.connect(A._ANSWERS_DB)
    con.execute("INSERT INTO answers (key, question, answer, source, created_at, updated_at, uses)"
                " VALUES (?,?,?,'you',?,?,0)", (OLD_KEY, LAM_SALARY, "130000", time.time(), time.time()))
    con.commit()
    con.close()
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["salary"] == "130000"
    c.post("/api/answers/basics", json={"fields": {"gender": "female"}})
    assert A._load_prefs()["desired_salary"] == "130000"


def test_a_salary_only_the_extension_had_shows_and_is_saved_with_the_card(app):
    A, c = app
    lib = A._answer_lib()                                                 # opened (and seeded) first
    lib.close()
    A._merge_prefs({"desired_salary": "$110,000"})
    d = c.get("/api/answers/basics").get_json()
    assert d["values"]["salary"] == "110000" and "salary" not in d["missing"]
    c.post("/api/answers/basics", json={"fields": {"gender": "male"}})
    assert _db_rows(A)["@salary"] == "110000" and A._load_prefs()["desired_salary"] == "110000"


# --------------------------------------------------------------------------- salary formats
@pytest.mark.parametrize("text,want", [
    ("120000", 120000), ("$120,000", 120000), ("120k", 120000), ("120K USD", 120000), ("120.000", 120000),
    ("120.000,00", 120000), ("120 000", 120000), ("120 000", 120000), ("100 000 USD", 100000),
    ("1.5M", 1500000), ("1,200,000", 1200000), ("120,000.50", 120000), ("$120,000 - $140,000", 120000),
    ("95/hour", None), ("95 per hour", None), ("45", None), ("120", None), ("lots", None), ("", None),
])
def test_parse_salary(text, want):
    from autoapply.basics import parse_salary
    assert parse_salary(text) == want


# --------------------------------------------------------------------------- the page
def test_the_decline_box_saves_itself_and_the_note_shows_the_saved_choice():
    i = APP_JS.index('$("#ansEeo")?.addEventListener("change"')
    handler = APP_JS[i:APP_JS.index('$("#ansBasicsSave")', i)]
    assert '"/api/answers/basics"' in handler and "eeo_decline: want" in handler and "box.checked = !want" in handler
    paint = APP_JS[APP_JS.index("function basPaint()"):APP_JS.index("function basFocusFirstOpen()")]
    assert "const dec = !!d.eeo_decline;" in paint and '$("#ansEeo").checked' not in paint


def test_the_card_follows_saved_answers_and_the_card_save_keeps_an_open_editor():
    lst = APP_JS[APP_JS.index('$("#ansList")?.addEventListener("click"'):APP_JS.index("/* ---- Up next")]
    assert lst.count("await basRefresh();") == 2
    save = APP_JS[APP_JS.index('$("#ansBasicsSave")?.addEventListener'):APP_JS.index("function ansWhen(")]
    assert "ansRefresh({ keepEditor: true })" in save


def test_the_prompt_takes_the_keyboard():
    i = APP_JS.index('} else if (info.state === "basics") {')
    branch = APP_JS[i:APP_JS.index('} else if (info.state === "running")', i)]
    assert "setTimeout(() => answer.focus(), 50)" in branch
