"""Common-sense screeners: five required Yes / No selects a Workday "Application Questions" page
asked after everything else was filled (2026-11-17). Four need no person:

1. "Are you at least 18 years old?": Yes when the saved profile shows it (a degree finished or
   started a year ago, or a job or school date 4 years back; or the saved answer); else no guess;
2. "Have you previously applied for employment with <company> ...?": from the person's own
   records (No when the Tracker holds no earlier application there, Yes when it does);
3. an acknowledgement worded as a Yes / No choice ("Please be aware that ...", "Please select
   "Yes" to acknowledge ..."): the affirmative choice under the standing consent to required
   notices; never a real question, a marketing opt-in or a covenant;
4. "Are any of your relatives presently employed by <company>?": only the person knows. Apply
   setup asks once; a saved No answers No everywhere, anything else leaves it a question.
Made-up people and companies. No model: the planner and the ask route only.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
from pathlib import Path

import pytest

from applicant.mapper import rule_known
from autoapply import commonsense as CS
from autoapply import consents as C
from autoapply import grounded
from autoapply import setup as S
from autoapply.answers import AnswerLibrary, normalize
from autoapply.asks import known_answer, neutral_option
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known, worked_here_question

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
CO = "Globex Hale LLP"
YN = ["Yes", "No"]
TODAY = dt.date(2026, 11, 17)

ADULT = "Are you at least 18 years old?"
RELATIVES = "Are any of your relatives presently employed by Globex Hale LLP?"
APPLIED = "Have you previously applied for employment with Globex Hale LLP or any of its predecessor firms?"
OUTSIDE = ("Please be aware that Globex Hale does not generally permit outside employment while you are employed "
           "with the Firm. Any outside business activities must be approved in advance by the Firm.")
PIIA = ("As a condition of hire, candidates who receive an offer of employment to a role that may be involved in AI "
        "invention or development activities must sign the Proprietary Information, Inventions, Employment, and AI "
        "Development Agreement (PIIA). Please select \"Yes\" to acknowledge that you have received and understand "
        "this requirement.")
PAGE = [ADULT, RELATIVES, APPLIED, OUTSIDE, PIIA]

OLD = {"experience": [{"org": "Initech", "roles": [{"title": "Analyst", "dates": "March 2019 - June 2022"}]}],
       "education": [{"school": "Example State University", "degree": "BSc Economics", "date": "2018"}]}


@pytest.fixture()
def lib(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    yield lib
    lib.close()


def _ctx(lib, **kw):
    base = dict(library=lib, company=CO, per_company=True, worked_here=("No", "history"), accept_notices=True,
                adult=True, applied_here=("No", "tracker"))
    base.update(kw)
    return KnownContext(**base)


def _plan(label, ctx, options=YN, kind="select", required=True):
    res = plan_known([FormField("f", label, kind, list(options or []), required=required)], ctx)
    return [(a.value, a.origin) for a in res.actions], [f.ref for f in res.left]


# --------------------------------------------------------------------------- 1. at least 18
@pytest.mark.parametrize("label", [
    ADULT,
    "Are you 18 years of age or older?",
    "Are you over 18?",
    "Are you at least 18 years of age?",
    "Are you 18 or older?",
    "Are you 18+?",
    "Are you of legal age to work?",
    "Are you of legal working age?",
])
def test_the_adult_wordings_are_one_question(label, lib):
    assert CS.is_adult_question(label) and normalize(label) == "@over_18"
    assert rule_known(label, CO) and not grounded.eligible(label, YN, company=CO)
    assert known_answer(label, YN, library=lib, adult=True) == ("Yes", "history")
    assert _plan(label, _ctx(lib)) == ([("Yes", "history")], [])


@pytest.mark.parametrize("label", [
    "Are you under 18 years of age?",
    "If you are under 18 years of age, do you have a work permit?",
    "Are you between 18 and 21 years old?",
    "Are you at least 21 years of age?",
    "What is the legal age of employment in your state?",
    "Date of birth",
])
def test_the_inverse_or_another_age_is_never_answered_from_dates(label, lib):
    assert not CS.is_adult_question(label)
    assert known_answer(label, YN, library=lib, adult=True) is None
    acts, _left = _plan(label, _ctx(lib))
    assert not acts


def _job(dates):
    return {"experience": [{"org": "Initech", "roles": [{"title": "Analyst", "dates": dates}]}]}


def _school(degree, date, school="Example State University"):
    return {"education": [{"school": school, "degree": degree, "date": date}]}


def test_a_finished_or_year_old_degree_proves_it():
    assert CS.adult_proof(_school("Bachelor of Science in Economics", "2021 - 2025"), TODAY)    # finished last year
    assert CS.adult_proof(_school("BSc Economics", "May 2026"), TODAY)                          # one date, past
    assert CS.adult_proof(_school("MBA", "September 2025 - Present"), TODAY)                    # started a year ago
    assert CS.adult_proof(_school("Associate of Arts", "2024 - 2027"), TODAY)
    assert CS.adult_proof(_school("College Diploma in Accounting", "2025"), TODAY)
    assert not CS.adult_proof(_school("Bachelor of Science", "September 2026 - Present"), TODAY)   # just started
    assert not CS.adult_proof(_school("Bachelor of Science", "Expected May 2027"), TODAY)
    assert not CS.adult_proof(_school("Bachelor of Science", ""), TODAY)                        # no date


def test_high_school_alone_with_a_recent_date_stays_a_question():
    assert not CS.adult_proof(_school("High School Diploma", "2025", "Example High"), TODAY)
    # the school's name alone is never a degree
    assert not CS.adult_proof(_school("", "2025", "Example State University"), TODAY)
    assert not CS.adult_proof(_school("Certificate", "2025", "Example University College"), TODAY)


def test_a_first_job_four_years_back_proves_it_and_two_years_does_not():
    assert CS.adult_proof(_job("October 2021 - Present"), TODAY)                # 5 years ago
    assert CS.adult_proof(_job("November 2022 - Present"), TODAY)               # exactly 4
    assert not CS.adult_proof(_job("December 2022 - Present"), TODAY)           # a month short of 4
    assert not CS.adult_proof(_job("November 2024 - Present"), TODAY)           # 2 years ago, no degree
    assert not CS.adult_proof(_job("2022 - Present"), TODAY)                    # no month: December
    assert CS.adult_proof(_job("2021 - Present"), TODAY)
    assert not CS.adult_proof({}, TODAY) and not CS.adult_proof({"experience": [{"org": "Initech"}]}, TODAY)
    assert CS.JOB_YEARS >= 4


def test_without_proof_it_stays_a_question_and_a_saved_answer_wins(lib):
    assert known_answer(ADULT, YN, library=lib) is None
    assert _plan(ADULT, _ctx(lib, adult=False)) == ([], ["f"])
    assert neutral_option(ADULT, YN + ["Prefer not to say"]) is None        # a fact: never a neutral pick
    lib.save(S.QUESTIONS["over_18"], "No", "you")                          # their own saved answer first
    assert known_answer(ADULT, YN, library=lib, adult=True) == ("No", "library")
    assert _plan(ADULT, _ctx(lib)) == ([("No", "library")], [])


# --------------------------------------------------------------------------- 2. applied here before
@pytest.mark.parametrize("label,kind", [
    (APPLIED, "applied"),
    ("Have you ever applied to Globex Hale before?", "applied"),
    ("Have you applied to this company in the past?", "applied"),
    ("Have you previously applied for a position with us?", "applied"),
    ("Have you interviewed with Globex Hale before?", "applied"),
    ("Have you previously applied?", "applied"),
    ("Have you ever applied to or worked for Globex Hale?", "both"),
])
def test_the_applied_wordings(label, kind):
    assert CS.applied_question(label, CO) == kind
    assert rule_known(label, CO) and not grounded.eligible(label, YN, company=CO)


@pytest.mark.parametrize("label", [
    "Have you previously applied for employment with Initrode Partners?",        # another employer
    "Has a relative of yours previously applied to Globex Hale?",
    "Were you referred by someone who previously applied to Globex Hale?",
    "If yes, when did you previously apply to Globex Hale?",
    "Have you previously applied for a work visa?",
    "Have you ever applied for a security clearance?",
    "Have you applied to other companies in the past month?",
    "Why did you apply to Globex Hale?",
    "Are you a current or former Globex Hale employee?",                        # the worked-here rule's
])
def test_another_question_is_not_the_applied_question(label):
    assert CS.applied_question(label, CO) == ""


def test_the_records_answer_no_or_yes_and_unreadable_records_answer_nothing():
    assert CS.applied_before(CO, []) == ("No", "tracker")
    assert CS.applied_before(CO, ["Initech", "Initrode Partners"]) == ("No", "tracker")
    assert CS.applied_before(CO, ["Initech", "Globex Hale"]) == ("Yes", "tracker")          # the name, normalised
    assert CS.applied_before(CO, None) is None and CS.applied_before("", ["Globex Hale"]) is None


def test_applied_here_is_answered_from_the_tracker(lib):
    assert not worked_here_question(APPLIED, CO)
    assert known_answer(APPLIED, YN, library=lib, company=CO, applied_here=("No", "tracker")) == ("No", "tracker")
    assert known_answer(APPLIED, YN, library=lib, company=CO, applied_here=("Yes", "tracker")) == ("Yes", "tracker")
    assert _plan(APPLIED, _ctx(lib)) == ([("No", "tracker")], [])
    assert _plan(APPLIED, _ctx(lib, applied_here=("Yes", "tracker"))) == ([("Yes", "tracker")], [])
    # records that could not be read: the person's
    assert known_answer(APPLIED, YN, library=lib, company=CO) is None
    assert _plan(APPLIED, _ctx(lib, applied_here=None)) == ([], ["f"])


def test_a_yes_about_a_time_window_or_this_position_is_the_persons(lib):
    recent = "Have you applied to Globex Hale in the last 12 months?"
    same = "Have you previously applied for this position at Globex Hale?"
    for q in (recent, same):
        assert known_answer(q, YN, library=lib, company=CO, applied_here=("No", "tracker")) == ("No", "tracker")
        assert known_answer(q, YN, library=lib, company=CO, applied_here=("Yes", "tracker")) is None
        assert _plan(q, _ctx(lib, applied_here=("Yes", "tracker"))) == ([], ["f"])


def test_applied_or_worked_needs_both_verdicts(lib):
    q = "Have you ever applied to or worked for Globex Hale?"
    no, yes = ("No", "tracker"), ("Yes", "tracker")
    assert CS.applied_answer(q, "both", no, ("No", "history")) == ("No", "tracker")
    assert CS.applied_answer(q, "both", yes, ("No", "history")) == ("Yes", "tracker")
    assert CS.applied_answer(q, "both", no, ("Yes", "library")) == ("Yes", "library")
    assert CS.applied_answer(q, "both", no, ("Yes", "confirm")) is None        # read from the resume: one tap first
    assert CS.applied_answer(q, "both", no, None) is None
    assert _plan(q, _ctx(lib)) == ([("No", "tracker")], [])
    assert _plan(q, _ctx(lib, worked_here=("Yes", "confirm"))) == ([], ["f"])


def test_their_own_answer_to_this_wording_comes_before_the_records(lib):
    lib.save(APPLIED, "Yes", "you")                    # they applied by hand before using the app
    assert known_answer(APPLIED, YN, library=lib, company=CO, applied_here=("No", "tracker")) == ("Yes", "library")
    assert _plan(APPLIED, _ctx(lib)) == ([("Yes", "library")], [])


# --------------------------------------------------------------------------- 3. acknowledgements
@pytest.mark.parametrize("label,options,want", [
    (OUTSIDE, YN, "Yes"),
    (PIIA, YN, "Yes"),
    (PIIA, ["Yes, I acknowledge", "No"], "Yes, I acknowledge"),
    ("I understand that this position requires working on site five days a week.", YN, "Yes"),
    ("I acknowledge that I have read the job description.", ["I acknowledge", "I do not acknowledge"], "I acknowledge"),
    ("Please be aware that this role is on site. Do you understand and agree?", YN, "Yes"),
])
def test_a_yes_no_acknowledgement_takes_the_affirmative_choice_under_the_standing_consent(lib, label, options, want):
    assert C.is_acknowledgement(label) and rule_known(label, CO)
    assert known_answer(label, options, library=lib, accept_notices=True) == (want, "consent")
    assert known_answer(label, options, library=lib) is None                  # the preference is off: asked
    assert _plan(label, _ctx(lib), options) == ([(want, "consent")], [])
    assert _plan(label, _ctx(lib, accept_notices=False), options) == ([], ["f"])


@pytest.mark.parametrize("label", [
    "Are you aware of any conflicts of interest?",
    "Are you aware of any reason you could not perform the duties of this role?",
    "Do you understand English?",
    "Please be aware that this role requires travel. Are you willing to travel 50% of the time?",
    "Are you bound by a non-compete agreement?",
    "I understand that I may be required to sign a non-compete agreement.",
    "I acknowledge that I am at least 18 years of age.",
    "I understand that I am authorized to work in the United States.",
    "I acknowledge that I have been convicted of a felony.",
])
def test_a_real_question_or_a_fact_is_never_an_acknowledgement(lib, label):
    assert not C.is_acknowledgement(label)
    assert (known_answer(label, YN, library=lib, accept_notices=True) or ("", ""))[1] != "consent"
    acts, _left = _plan(label, _ctx(lib, adult=False))
    assert not [a for a in acts if a[1] == "consent"]


def test_a_marketing_opt_in_keeps_its_own_rule(lib):
    sms = "I understand and agree to receive text messages about new jobs. SMS Policy"
    assert C.is_optin(sms) and not C.is_acknowledgement(sms)
    assert known_answer(sms, YN, library=lib, accept_notices=True) == ("No", "optin")
    assert _plan(sms, _ctx(lib)) == ([("No", "optin")], [])
    # a required opt-in with no way to decline is still the person's (a forced consent)
    assert C.standing_answer(sms, ["Yes"], "select", True, library=lib, accept_notices=True) == (C.THEIRS, "", "")


def test_a_covenant_question_keeps_its_own_rule(lib):
    q = "Are you bound by a non-compete agreement?"
    assert normalize(q) == "@restrictive_covenants"
    assert _plan(q, _ctx(lib)) == ([], ["f"])                                  # asked once, then saved
    lib.save(S.QUESTIONS["covenants"], "No", "you")
    assert _plan(q, _ctx(lib)) == ([("No", "library")], [])


# --------------------------------------------------------------------------- 4. relatives
@pytest.mark.parametrize("label", [
    RELATIVES,
    "Do you have any relatives currently working at Globex Hale?",
    "Do you have family members who work at Globex Hale?",
    "Are you related to anyone employed by Globex Hale?",
    "Is your spouse or domestic partner employed by Globex Hale?",
])
def test_the_relatives_wordings(label):
    assert CS.relatives_question(label)
    assert rule_known(label, CO) and not grounded.eligible(label, YN, company=CO)
    assert not worked_here_question(label, CO) and not CS.applied_question(label, CO)


@pytest.mark.parametrize("label", [
    "Are you related to a current or former government official?",
    "Do you have any relatives who are public officials or work for a government agency?",
    "Do you have relatives working for a competitor of Globex Hale?",
    "Were you referred by a relative who works at Globex Hale?",
    "If yes, please list the names of your relatives employed by Globex Hale.",
    "Name and relationship of your emergency contact",
    "Are you a current or former Globex Hale employee?",
])
def test_another_question_is_not_the_relatives_question(label):
    assert not CS.relatives_question(label)


def test_relatives_is_answered_only_from_a_saved_no(lib):
    # never answered: the person's, and never a neutral pick or a default
    assert CS.relatives_saved(lib) == ""
    assert known_answer(RELATIVES, YN, library=lib, company=CO) is None
    assert _plan(RELATIVES, _ctx(lib)) == ([], ["f"])
    assert neutral_option(RELATIVES, YN + ["Prefer not to say"]) is None
    # "Yes, ask me each time": still the person's
    lib.save(S.QUESTIONS["relatives"], "Yes", "you")
    assert CS.relatives_saved(lib) == "ask"
    assert known_answer(RELATIVES, YN, library=lib, company=CO) is None
    assert _plan(RELATIVES, _ctx(lib)) == ([], ["f"])
    # a saved No answers No everywhere
    lib.save(S.QUESTIONS["relatives"], "No", "you")
    assert CS.relatives_saved(lib) == "No"
    assert known_answer(RELATIVES, YN, library=lib, company=CO) == ("No", "library")
    assert _plan(RELATIVES, _ctx(lib)) == ([("No", "library")], [])
    other = "Do you have family members who work at Initrode Partners?"
    assert known_answer(other, YN, library=lib, company="Initrode Partners") == ("No", "library")
    # and never a question about a public official
    official = "Are you related to a current or former government official?"
    assert known_answer(official, YN, library=lib, company=CO) is None


def test_one_companys_answer_never_becomes_the_standing_answer(lib):
    assert S.KEYS["relatives"] == CS.RELATIVES_KEY == normalize(S.QUESTIONS["relatives"])
    assert normalize(RELATIVES) != CS.RELATIVES_KEY
    lib.save(RELATIVES, "No", "you")                   # answered on the bar for this company
    assert CS.relatives_saved(lib) == ""
    other = "Do you have family members who work at Initrode Partners?"
    assert known_answer(other, YN, library=lib, company="Initrode Partners") is None


# --------------------------------------------------------------------------- the page, end to end
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Business Analyst", CO, 70, "", data={"status": "approved"})
    recs.close()
    return mod, rid


def _post(mod, path, body):
    return mod.app.test_client().post(path, json=body, headers=H).get_json()


def _add(mod, company, data):
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        return recs.add("Analyst", company, 70, "", data=data)
    finally:
        recs.close()


def test_earlier_applications_come_from_the_records_and_leave_out_this_one(app):
    mod, rid = app
    rec = mod._get_record(rid)
    assert mod._earlier_applications(rec) == []                               # only this application
    assert mod._common_sense(CO, rec)["applied_here"] == ("No", "tracker")
    _add(mod, "Initech", {"status": "applied", "applied_at": "2026-10-01T10:00:00"})
    _add(mod, "Globex Hale", {"status": "approved"})                          # tailored, never sent
    assert mod._common_sense(CO, rec)["applied_here"] == ("No", "tracker")
    _add(mod, "Globex Hale", {"status": "applied", "applied_at": "2026-09-01T10:00:00"})
    assert mod._common_sense(CO, rec)["applied_here"] == ("Yes", "tracker")
    assert mod._common_sense("", rec)["applied_here"] is None                 # no company known


def test_this_application_marked_applied_is_not_an_earlier_one(app):
    mod, rid = app
    recs = mod._records()
    try:
        recs.merge_data(rid, {"status": "applied", "applied_at": "2026-11-17T09:00:00"})
    finally:
        recs.close()
    rec = mod._get_record(rid)
    mod.app.test_client().get("/api/autoapply/board", headers=H)             # the Tracker catches up
    assert mod._common_sense(CO, rec)["applied_here"] == ("No", "tracker")


def test_unreadable_records_leave_it_a_question(app, monkeypatch):
    mod, rid = app

    def boom():
        raise RuntimeError("locked")
    rec = mod._get_record(rid)
    monkeypatch.setattr(mod, "_records", boom)
    assert mod._earlier_applications(rec) is None
    assert mod._common_sense(CO, rec)["applied_here"] is None


def test_the_profile_dates_reach_the_engine(app, monkeypatch):
    mod, _rid = app
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: OLD)
    assert mod._common_sense(CO, None)["adult"] is True
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: {})
    assert mod._common_sense(CO, None)["adult"] is False


def test_the_page_leaves_one_question_for_the_person(app, monkeypatch):
    """The ask route's known_only check on the five questions: four answered by the rules before
    the grounded call, only the relatives question left (nothing saved for it)."""
    mod, rid = app
    seen = []
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: OLD)
    monkeypatch.setattr(mod, "_grounded_answers", lambda qs, *a, **k: seen.extend(q["question"] for q in qs) or {})
    monkeypatch.setattr(mod, "_label_maps", lambda items, company="", library=None: ({}, {}, set()))
    monkeypatch.setattr(mod, "_terms_pref", lambda: "always")
    qs = [{"question": q, "options": YN} for q in PAGE]
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": qs, "record_id": rid, "known_only": True,
                                                 "fill_through": True})
    piia = PIIA[:300]                                   # the route keeps a question's first 300 characters
    assert r["answers"] == {ADULT: "Yes", APPLIED: "No", OUTSIDE: "Yes", piia: "Yes"}
    assert r["origins"] == {ADULT: "history", APPLIED: "tracker", OUTSIDE: "consent", piia: "consent"}
    assert r["open"] == [RELATIVES] and seen == [RELATIVES]     # (the grounded call's own gate drops it: above)
    # with the standing No from Apply setup, nothing is left
    c = mod.app.test_client()
    assert c.post("/api/apply-setup/save", json={"step": "forms", "fields": {"relatives": "No"}}, headers=H).status_code == 200
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": qs, "record_id": rid, "known_only": True,
                                                 "fill_through": True})
    assert r["ok"] and r["open"] == [] and r["answers"][RELATIVES] == "No" and r["origins"][RELATIVES] == "library"
    # and with the acknowledgement preference off, the two notices are the person's again
    monkeypatch.setattr(mod, "_terms_pref", lambda: "ask")
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": qs, "record_id": rid, "known_only": True})
    assert r["open"] == [OUTSIDE, piia]


def test_a_long_notice_is_read_whole_and_a_view_cut_statement_is_not_taken(app, monkeypatch, lib):
    mod, rid = app
    monkeypatch.setattr(mod, "_grounded_answers", lambda qs, *a, **k: {})
    monkeypatch.setattr(mod, "_label_maps", lambda items, company="", library=None: ({}, {}, set()))
    monkeypatch.setattr(mod, "_terms_pref", lambda: "always")
    # the instruction to pick Yes comes after the route's 300 characters
    long = ("As a condition of hire, every candidate who receives an offer must complete the onboarding paperwork "
            "that the Firm sends, including the handbook receipt, the equipment policy, the travel policy, the "
            "expense policy, the remote work policy and the code of conduct, each within five business days of the "
            "start date that the offer letter names. Please select \"Yes\" to acknowledge that you have received and "
            "understand this requirement.")
    assert len(long) > 340 and not C.is_acknowledgement(long[:300]) and C.is_acknowledgement(long)
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": [{"question": long, "options": YN}], "record_id": rid,
                                                 "known_only": True})
    assert r["answers"] == {long[:300]: "Yes"} and r["origins"] == {long[:300]: "consent"}
    # a statement the page view cut at 80 characters hides its end: the person's
    cut = OUTSIDE[:80]
    assert not C.is_acknowledgement(cut) and C.is_acknowledgement(OUTSIDE)
    assert known_answer(cut, YN, library=lib, accept_notices=True) is None
    # the brain's own shortening (an ellipsis) is still read
    assert C.is_acknowledgement(OUTSIDE[:120].rstrip() + " ...")


def test_apply_setup_asks_relatives_once_with_no_default(app):
    mod, _rid = app
    c = mod.app.test_client()
    get = lambda: c.get("/api/apply-setup", headers=H).get_json()["forms"]["relatives"]     # noqa: E731
    save = lambda v: c.post("/api/apply-setup/save", json={"step": "forms", "fields": {"relatives": v}}, headers=H)  # noqa: E731
    assert get() == ""                                                        # unanswered: no default
    assert save("ask").status_code == 200 and get() == "ask"
    assert save("No").status_code == 200 and get() == "No"
    assert save("").status_code == 200 and get() == ""                        # cleared again


# --------------------------------------------------------------------------- copy, shell
def test_the_new_strings_are_in_all_five_locales_with_no_dashes():
    for lang in ("de", "en", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("setup.relatives", "setup.relativesAsk", "aa.ans.from.tracker", "aa.ans.from.commonSense"):
            assert d.get(k), (lang, k)
            assert "—" not in d[k] and "–" not in d[k]
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'suT("setup.relatives"' in js and 'T("aa.ans.from.tracker"' in js and 'T("aa.ans.from.commonSense"' in js


def test_the_shell_keeps_the_rule_origins_for_review_answers():
    js = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    # (2026-11-18) "drafted" joined them: a written answer the engine drafted (autoapply/drafts.py)
    assert 'new Set(["grounded", "neutral", "history", "tracker", "consent", "common_sense", "drafted"])' in js
    assert 'tracker: "from your tracker"' in js and 'common_sense: "common sense"' in js


# --------------------------------------------------------------------------- how did you hear
HEARD = "How Did You Hear About Us?"
# The choices under "Job Sites" on a Workday form's two-level source list (2026-11-18).
JOB_SITES = ["BuiltIn", "College / University", "Compliance_HireAHero", "Compliance_JOFDAV", "Compliance_recruitABILITY",
             "Compliance_USDiversity", "Financial Careers Training", "Glassdoor", "HACE", "Handshake", "IDES",
             "Job Board: CareerBuilder", "Job Board: Indeed", "Job Board: Monster", "Other Job Board"]


def test_a_job_board_answer_lands_on_the_lists_generic_board_choice():
    from autoapply.planner import generic_source, generic_sources, literal_for
    assert generic_source("Job board", JOB_SITES) == "Other Job Board"
    assert generic_sources("Job board", JOB_SITES) == ["Other Job Board", "Glassdoor", "Handshake"]
    assert literal_for("@heard_about", "Job board", JOB_SITES) == "Other Job Board"
    # the first list of the same form: the board-like category, then nothing else
    assert generic_sources("Job board", ["Career Fair", "Employee Referral", "Job Sites", "Social Media"]) == ["Job Sites"]
    # a plain generic board before the catch-all one, and both before "Other"
    assert generic_sources("Job board", ["Other", "Other Job Board", "Job Board"]) == ["Job Board", "Other Job Board", "Other"]
    # never a choice that adds a source the person did not give
    for bad in (["College Job Board"], ["Compliance_HireAHero", "University Career Center", "Staffing Agency"],
                ["Job Board: Indeed", "Job Board: Monster"]):
        assert literal_for("@heard_about", "Job board", bad) is None
    # only on a how-did-you-hear question
    assert literal_for("", "Job board", JOB_SITES) is None


def test_python_and_the_page_side_share_the_how_did_you_hear_cases():
    # the same file drives extension/autofill_core.js heardChoices in tests/test_agent_view.js
    from autoapply.planner import literal_for
    cases = json.loads((ROOT / "tests" / "fixtures" / "heard_about_cases.json").read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 15
    for c in cases:
        assert literal_for("@heard_about", c["answer"], c["options"]) == c["want"], c


def test_with_nothing_saved_how_did_you_hear_is_a_job_board(lib):
    assert (CS.HEARD_KEY, CS.HEARD_DEFAULT, CS.HEARD_ORIGIN) == ("@heard_about", "Job board", "common_sense")
    assert normalize(HEARD) == CS.HEARD_KEY
    assert _plan(HEARD, _ctx(lib), options=JOB_SITES) == ([("Other Job Board", "common_sense")], [])
    assert _plan(HEARD, _ctx(lib), options=[], kind="text") == ([("Job board", "common_sense")], [])
    assert known_answer(HEARD, JOB_SITES, library=lib) == ("Other Job Board", "common_sense")
    assert known_answer(HEARD, [], library=lib) == ("Job board", "common_sense")
    # a list with no choice for it stays the person's
    assert _plan(HEARD, _ctx(lib), options=["College Job Board", "Staffing Agency"]) == ([], ["f"])
    # a saved answer always wins
    lib.save(HEARD, "LinkedIn")
    assert _plan(HEARD, _ctx(lib), options=["Indeed", "LinkedIn", "Other"]) == ([("LinkedIn", "library")], [])
    assert known_answer(HEARD, ["Indeed", "LinkedIn", "Other"], library=lib) == ("LinkedIn", "library")
    assert _plan(HEARD, _ctx(lib), options=[], kind="text") == ([("LinkedIn", "library")], [])


# --------------------------------------------------------------------------- worked here, by the short name
WORKED_GROUP = "Have you worked for Globex or any of our companies in the past?"


@pytest.mark.parametrize("label,company", [
    (WORKED_GROUP, "Globex"),
    (WORKED_GROUP, "Globex Financial Corporation"),          # the record's long name, the form's short one
    (WORKED_GROUP, "Hale Community Bank"),                   # a company of the group: "our companies"
    ("Have you worked for Globex before?", "Globex Hale LLP"),
    ("Have you previously been employed by Globex or any of its affiliates?", "Globex Financial Group"),
    ("Have you ever worked for Globex or one of its subsidiaries?", "Globex Hale LLP"),
    ("Previously employed by Globex?", "Globex Financial Corp."),
    ("Are you a current or former Globex employee?", "Globex Financial Corporation"),
    ("Are you a former employee of Globex?", "Globex Hale LLP"),
])
def test_worked_here_is_recognised_by_the_short_name_and_the_group(label, company, lib):
    assert worked_here_question(label, company)
    ctx = _ctx(lib, company=company)
    assert _plan(label, ctx) == ([("No", "history")], [])
    assert _plan(label, ctx, kind="radio") == ([("No", "history")], [])
    assert known_answer(label, YN, library=lib, per_company=True, worked_here=("No", "history"),
                        company=company) == ("No", "history")


@pytest.mark.parametrize("label,company", [
    ("Have you worked for Initech in the past?", "Globex Financial Corporation"),       # another employer
    ("Have you worked for First Republic before?", "First National Bank"),             # "First" names no one
    ("Have you worked for any other companies in the past?", "Globex"),
    ("Do you know anyone who has worked for Globex or any of our companies?", "Globex"),
    ("Has a relative worked for Globex or any of our companies in the past?", "Globex"),
    ("Why do you want to work for Globex or one of our companies?", "Globex"),
])
def test_worked_here_is_never_read_into_another_question(label, company):
    assert not worked_here_question(label, company)


def test_a_worked_here_yes_from_the_resume_still_waits_and_a_saved_answer_wins(lib):
    # the company is in the work history: the Yes is confirmed once by the person, never sent unasked
    assert _plan(WORKED_GROUP, _ctx(lib, company="Globex Financial Corporation", worked_here=("Yes", "confirm"))) == ([], ["f"])
    # their own answer for this company
    assert _plan(WORKED_GROUP, _ctx(lib, company="Globex Financial Corporation",
                                    worked_here=("Yes", "library"))) == ([("Yes", "library")], [])


# --------------------------------------------------------------------------- highest education
EDU = "What is the highest level of education you have completed?"
EDU_OPTS = ["No High School Diploma", "High School Diploma or GED", "Some College, No Degree", "Associate Degree (AA, AS)",
            "Bachelor's Degree (BA, BS)", "Master's Degree (MA, MS)", "Doctorate (PhD)"]
EDU_DAY = dt.date(2026, 11, 18)


def _edu(*entries):
    return {"education": [{"school": "Example State University", "degree": d, "date": w} for d, w in entries]}


def test_the_highest_completed_degree_is_read_from_the_profile():
    hi = lambda *e: CS.highest_education(_edu(*e), EDU_DAY)      # noqa: E731
    assert hi(("MSc Data Science", "2021"), ("BSc Economics", "2018")) == "masters"
    assert hi(("BSc Economics", "2014 - 2018")) == "bachelors"
    assert hi(("Associate of Arts", "2012")) == "associates"
    assert hi(("PhD Physics", "May 2020"), ("MS Physics", "2016")) == "doctorate"
    assert hi(("High School Diploma", "2010")) == "high school"
    assert hi(("MBA", "")) == "masters"                           # no date: on the resume as held
    # a degree still in progress or expected is not completed: the one below it counts
    assert hi(("MSc Data Science", "Expected 2027"), ("BSc Economics", "2018")) == "bachelors"
    assert hi(("Master of Science", "Sept 2025 - Present"), ("BA History", "2015 - 2019")) == "bachelors"
    assert hi(("PhD Candidate, Physics", "2022"), ("MS Physics", "2021")) == "masters"
    assert hi(("MSc Data Science", "2025 - 2027"), ("BSc Economics", "2018")) == "bachelors"
    assert hi(("BSc Economics", "2024 - Present")) == ""
    assert hi(("Data Bootcamp", "2020")) == "" and CS.highest_education({}, EDU_DAY) == ""


@pytest.mark.parametrize("level,want", [
    ("doctorate", "Doctorate (PhD)"), ("masters", "Master's Degree (MA, MS)"), ("bachelors", "Bachelor's Degree (BA, BS)"),
    ("associates", "Associate Degree (AA, AS)"), ("high school", "High School Diploma or GED"),
])
def test_the_exact_workday_list_takes_the_choice_for_the_level(level, want, lib):
    assert CS.education_option(level, EDU_OPTS) == want
    assert _plan(EDU, _ctx(lib, education=level), options=EDU_OPTS) == ([(want, "history")], [])
    assert known_answer(EDU, EDU_OPTS, library=lib, education=level) == (want, "history")


def test_a_choice_is_matched_loosely_and_never_overstates():
    opt = CS.education_option
    assert opt("masters", ["High school", "Undergraduate degree", "Graduate degree"]) == "Graduate degree"
    assert opt("masters", ["GED", "Bachelors", "Masters", "PhD"]) == "Masters"
    assert opt("masters", ["Bachelor's", "Master's Degree (MA, MS, MBA)", "Professional degree (JD, MD)"]) == "Master's Degree (MA, MS, MBA)"
    assert opt("bachelors", ["High school", "2-year degree", "4-year degree", "Postgraduate"]) == "4-year degree"
    assert opt("bachelors", ["Some college", "College degree", "Some graduate school", "Graduate degree"]) == "College degree"
    assert opt("doctorate", ["Bachelor's", "Master's", "Doctorate (PhD)"]) == "Doctorate (PhD)"
    assert opt("associates", ["High School", "Associate Degree (AA, AS)", "Bachelor's"]) == "Associate Degree (AA, AS)"
    # no choice for the level: the nearest lower one, never a higher one
    assert opt("masters", ["High School Diploma or GED", "Bachelor's Degree (BA, BS)", "Doctorate (PhD)"]) == "Bachelor's Degree (BA, BS)"
    assert opt("bachelors", ["High School Diploma", "Some College, No Degree", "Master's Degree"]) == "Some College, No Degree"
    assert opt("doctorate", ["Bachelor's", "Master's", "Professional degree (JD, MD)"]) == "Master's"
    assert opt("high school", ["No High School Diploma", "Bachelor's Degree"]) is None      # never "no diploma", never more
    assert opt("bachelors", ["Master's Degree", "Doctorate", "Other"]) is None
    assert opt("", EDU_OPTS) is None


@pytest.mark.parametrize("label", [
    EDU, "Highest level of education completed", "What is your highest level of education attained?",
    "Highest level of education achieved", "Highest degree earned", "What is the highest degree you have obtained?",
    "Education level", "Please select your highest education level",
])
def test_the_common_wordings_are_one_question(label, lib):
    assert CS.education_question(label) == "level"
    assert _plan(label, _ctx(lib, education="masters"), options=EDU_OPTS) == ([("Master's Degree (MA, MS)", "history")], [])
    # a text box, and a list whose choices were not read: the level in words
    assert _plan(label, _ctx(lib, education="masters"), options=[], kind="text") == ([("Master's degree", "history")], [])
    assert _plan(label, _ctx(lib, education="bachelors"), options=[]) == ([("Bachelor's degree", "history")], [])
    assert known_answer(label, [], library=lib, education="masters") == ("Master's degree", "history")


def test_do_you_have_a_degree_is_yes_or_no_from_the_level(lib):
    q = "Do you have a bachelor's degree (or higher)?"
    assert CS.education_question(q) == "bachelors"
    for level, want in (("doctorate", "Yes"), ("masters", "Yes"), ("bachelors", "Yes"), ("associates", "No"), ("high school", "No")):
        assert _plan(q, _ctx(lib, education=level)) == ([(want, "history")], [])
        assert _plan(q, _ctx(lib, education=level), kind="radio") == ([(want, "history")], [])
        assert known_answer(q, YN, library=lib, education=level) == (want, "history")
    assert _plan("Have you earned a master's degree?", _ctx(lib, education="bachelors")) == ([("No", "history")], [])
    assert _plan("Do you have a high school diploma or GED?", _ctx(lib, education="masters")) == ([("Yes", "history")], [])
    assert _plan("Do you have a 4-year college degree?", _ctx(lib, education="masters")) == ([("Yes", "history")], [])


@pytest.mark.parametrize("label", [
    "Do you have a bachelor's degree in Accounting?",            # the field decides
    "What is the minimum education level required for this role?",
    "What is your mother's highest level of education?",
    "Are you currently pursuing a degree?",
    "Field of study", "Which university did you attend?", "What is your GPA?",
])
def test_another_education_question_is_never_answered_from_the_level(label, lib):
    assert CS.education_question(label) == ""
    acts, _left = _plan(label, _ctx(lib, education="masters"), options=EDU_OPTS)
    assert not [a for a in acts if a[1] == "history"]


def test_no_completed_degree_stays_a_question_and_a_saved_answer_wins(lib):
    assert CS.education_answer(EDU, EDU_OPTS, "") is None
    acts, _left = _plan(EDU, _ctx(lib, education=""), options=EDU_OPTS)
    assert acts == []                                             # nothing known: never a guess
    assert known_answer(EDU, EDU_OPTS, library=lib, education="") is None
    lib.save(EDU, "Bachelor's Degree (BA, BS)")
    assert _plan(EDU, _ctx(lib, education="masters"), options=EDU_OPTS) == ([("Bachelor's Degree (BA, BS)", "library")], [])
    assert known_answer(EDU, EDU_OPTS, library=lib, education="masters") == ("Bachelor's Degree (BA, BS)", "library")


def test_a_page_whose_only_field_is_the_education_question_is_planned(app):
    # (2026-11-18) the plan route said "Could not plan this form confidently" three times: no
    # rule knew the question, so the plan was empty. It is now one known action.
    mod, _rid = app
    prof = {"identity": {"name": "Sam Example"},
            "education": [{"school": "Example State University", "degree": "MSc Data Science", "date": "2021"},
                          {"school": "Example State University", "degree": "BSc Economics", "date": "2018"}]}
    rid = _add(mod, CO, {"status": "approved", "render_profile": prof})
    assert mod._common_sense(CO, mod._get_record(rid))["education"] == "masters"
    d = _post(mod, "/api/autoapply/plan", {"url": "https://example.wd1.myworkdayjobs.com/x/job/1/apply", "record_id": rid,
                                           "fields": [{"ref": "w1", "label": EDU, "type": "select", "widget": "combobox",
                                                       "options": EDU_OPTS, "required": True}]})
    assert d["ok"] is True and d["left"] == [], d
    assert [(o["ref"], o["op"], o["text"], o["from"]) for o in d["ops"]] == [("w1", "select", "Master's Degree (MA, MS)", "history")]
    # the model path: the answer placeholder for the open label (a text answer, picked from the
    # list by the page script's strict match), and the heard-about and worked-here ones beside it
    rec = mod._get_record(rid)
    got = mod._known_answers_for([EDU, "How Did You Hear About Us?"], rec, mod._record_data(rec), {})
    assert got.get(EDU) == "Master's degree" and got.get("How Did You Hear About Us?") == "Job board", got
    assert mod._common_sense(CO, None)["education"] == ""         # no profile, no record: nothing known


# --------------------------------------------------------------------------- phone device type
PHONE_TYPE = "Phone Device Type"


@pytest.mark.parametrize("options,want", [
    (["Home", "Mobile", "Telephone"], "Mobile"),                     # one Workday tenant
    (["Fax", "Landline", "Mobile"], "Mobile"),                       # another
    (["Home", "Work", "Cell"], "Cell"),
    (["Landline", "Cellular", "Fax"], "Cellular"),
    (["Home Phone", "Cell Phone", "Work Phone"], "Cell Phone"),
    (["Home", "Personal Cell", "Work Cell"], "Personal Cell"),
    (["Landline", "Mobile Phone"], "Mobile Phone"),
    (["Work Mobile", "Mobile", "Home"], "Mobile"),                   # the plain one before a work line
])
def test_phone_device_type_takes_the_mobile_choice(options, want, lib):
    assert CS.phone_type_question(PHONE_TYPE) and CS.mobile_option(options) == want
    assert _plan(PHONE_TYPE, _ctx(lib), options=options) == ([(want, "common_sense")], [])
    assert known_answer(PHONE_TYPE, options, library=lib) == (want, "common_sense")


def test_phone_type_is_never_the_phone_number_and_a_saved_answer_wins_only_when_listed(lib):
    from autoapply.planner import contact_key
    # the cause (2026-11-18): the label read as the phone NUMBER, so the number was the "answer"
    for label in (PHONE_TYPE, "Phone Type", "Type of phone", "Telephone type", "Phone Extension"):
        assert contact_key(label) == "", label
    for label in ("Phone", "Phone Number", "Mobile phone", "Cell"):
        assert contact_key(label) == "phone", label
    for label in (PHONE_TYPE, "Phone Type", "Type of phone", "Phone Device Type*"):
        assert CS.phone_type_question(label), label
    for label in ("Phone Number", "Phone", "Country Phone Code", "Device", "Type of employment"):
        assert not CS.phone_type_question(label), label
    ctx = _ctx(lib, applicant={"phone": "555-010-0000"})
    acts, left = _plan(PHONE_TYPE, ctx, options=["Home", "Mobile", "Telephone"])
    assert acts == [("Mobile", "common_sense")] and not left
    # a list with no mobile choice at all stays a question; a text box says Mobile
    assert _plan(PHONE_TYPE, _ctx(lib), options=["Home", "Work"]) == ([], ["f"])
    assert _plan(PHONE_TYPE, _ctx(lib), options=[], kind="text") == ([("Mobile", "common_sense")], [])
    assert _plan(PHONE_TYPE, _ctx(lib), options=[]) == ([("Mobile", "common_sense")], [])     # choices not read yet
    # a saved answer that is one of the choices wins; one that is not falls back to mobile, never asked
    lib.save(PHONE_TYPE, "Home")
    assert _plan(PHONE_TYPE, _ctx(lib), options=["Home", "Mobile", "Telephone"]) == ([("Home", "library")], [])
    assert _plan(PHONE_TYPE, _ctx(lib), options=["Fax", "Landline", "Mobile"]) == ([("Mobile", "common_sense")], [])
    assert known_answer(PHONE_TYPE, ["Fax", "Landline", "Mobile"], library=lib) == ("Mobile", "common_sense")


def test_the_page_engine_never_offers_the_phone_number_to_a_phone_type_list():
    js = (ROOT / "extension" / "autofill_core.js").read_text(encoding="utf-8")
    assert 'if (c.key === "phone" && (PHONE_KIND_RE.test(sig) || PHONE_EXT_RE.test(sig))) continue;' in js


# --------------------------------------------------------------------------- other names, connections
ALIASES = "List any aliases or other name(s) you have used or currently use or indicate N/A."
CONNECTIONS = ("List all close personal and professional connections employed by Globex Hale and their relationship "
               "to you, or indicate N/A.")


def test_other_names_are_never_guessed_and_filled_once_saved(lib):
    assert CS.other_names_question(ALIASES) and normalize(S.QUESTIONS["other_names"]) == CS.OTHER_NAMES_KEY == "@other_names"
    assert S.KEYS["other_names"] == "@other_names"
    # nothing saved: the person's, never "N/A" by guess
    assert _plan(ALIASES, _ctx(lib), options=[], kind="textarea") == ([], ["f"])
    assert _plan(ALIASES, _ctx(lib), options=[], kind="text") == ([], ["f"])
    assert known_answer(ALIASES, [], library=lib) is None
    # the person said none in Apply setup: exactly what the question asks for
    lib.save(S.QUESTIONS["other_names"], "None")
    assert _plan(ALIASES, _ctx(lib), options=[], kind="textarea") == ([("N/A", "library")], [])
    assert known_answer(ALIASES, [], library=lib) == ("N/A", "library")
    assert _plan("Other names used. If none, write None.", _ctx(lib), options=[], kind="text") == ([("None", "library")], [])
    assert _plan("Former or maiden name", _ctx(lib), options=[], kind="text") == ([("N/A", "library")], [])
    assert _plan("Have you ever used another name?", _ctx(lib)) == ([("No", "library")], [])
    # names they gave are written as given
    lib.save(S.QUESTIONS["other_names"], "Sam Sample")
    assert _plan(ALIASES, _ctx(lib), options=[], kind="textarea") == ([("Sam Sample", "library")], [])
    assert _plan("Have you ever used another name?", _ctx(lib)) == ([("Yes", "library")], [])
    for other in ("Preferred name", "First name", "Company name", "Name of your reference", "Emergency contact name"):
        assert not CS.other_names_question(other), other


def test_what_a_question_asks_for_when_there_is_nothing():
    assert CS.none_word(ALIASES) == "N/A" and CS.none_word(CONNECTIONS) == "N/A"
    assert CS.none_word("List any other names. If none, write None.") == "None"
    assert CS.none_word('Please enter "N/A" if not applicable') == "N/A"
    assert CS.none_word("List your previous names (write n/a if none)") == "n/a"
    assert CS.none_word("Other names used") == "N/A"                  # it names no word: N/A
    for said in ("None", "none.", "N/A", "n/a", "No", "Not applicable"):
        assert CS.said_none(said), said
    assert not CS.said_none("Sam Sample") and not CS.said_none("")


def test_connections_employed_here_as_a_list_follow_the_standing_no_on_relatives(lib):
    assert CS.relatives_list_question(CONNECTIONS) and not CS.relatives_question(CONNECTIONS)
    # nothing saved, or "Yes, ask me each time": theirs to list
    assert _plan(CONNECTIONS, _ctx(lib), options=[], kind="textarea") == ([], ["f"])
    assert known_answer(CONNECTIONS, [], library=lib) is None
    lib.save(S.QUESTIONS["relatives"], "Yes")
    assert _plan(CONNECTIONS, _ctx(lib), options=[], kind="textarea") == ([], ["f"])
    # a standing No: what the question asks for
    lib.save(S.QUESTIONS["relatives"], "No")
    assert _plan(CONNECTIONS, _ctx(lib), options=[], kind="textarea") == ([("N/A", "library")], [])
    assert _plan(CONNECTIONS, _ctx(lib), options=[], kind="text") == ([("N/A", "library")], [])
    assert known_answer(CONNECTIONS, [], library=lib) == ("N/A", "library")
    assert _plan("Please list any relatives who work at Globex Hale. If none, write None.", _ctx(lib), options=[],
                 kind="textarea") == ([("None", "library")], [])
    # the Yes / No form still gets its No, as before
    assert _plan(RELATIVES, _ctx(lib)) == ([("No", "library")], [])
    # never a follow-up to a Yes, a reference, a client, a public official
    for other in ("If yes, please list the names of your relatives employed by Globex Hale.",
                  "List your references and their relationship to you",
                  "List all clients of Globex Hale you have worked with",
                  "List any relatives who are government officials"):
        assert not CS.relatives_list_question(other), other
        acts, _left = _plan(other, _ctx(lib), options=[], kind="textarea")
        assert acts == [], other


def test_apply_setup_asks_other_names_once_in_five_locales(app):
    mod, _rid = app
    c = mod.app.test_client()
    get = lambda: c.get("/api/apply-setup", headers=H).get_json()["forms"]["other_names"]     # noqa: E731
    save = lambda v: c.post("/api/apply-setup/save", json={"step": "forms", "fields": {"other_names": v}}, headers=H)  # noqa: E731
    assert get() == ""                                                        # unanswered: no default
    assert save("None").status_code == 200 and get() == "None"
    assert save("Sam Sample").status_code == 200 and get() == "Sam Sample"
    assert save("").status_code == 200 and get() == ""                        # cleared again
    for lang in ("de", "en", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("setup.otherNames", "setup.otherNamesPh"):
            assert d.get(k), (lang, k)
            assert "—" not in d[k] and "–" not in d[k]
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'suInput("other_names", suT("setup.otherNames"' in js
