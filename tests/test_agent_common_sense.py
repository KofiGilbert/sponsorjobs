"""Common-sense screeners: five required Yes / No selects a Workday "Application Questions" page
asked after everything else was filled (2026-11-17). Four need no person:

1. "Are you at least 18 years old?": Yes when a job or school date in the saved profile lies 18
   years back (or the saved answer says so); no dates, no guess;
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

OLD = {"experience": [{"org": "Initech", "roles": [{"title": "Analyst", "dates": "March 2006 - June 2010"}]}],
       "education": [{"school": "Example State University", "degree": "BSc Economics", "date": "2005"}]}
YOUNG = {"experience": [{"org": "Initech", "roles": [{"title": "Intern", "dates": "June 2024 - Present"}]}],
         "education": [{"school": "Example State University", "degree": "BSc Economics", "date": "2021 - 2025"}]}


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


def test_a_date_eighteen_years_back_proves_it_and_no_dates_prove_nothing():
    assert CS.adult_proof(OLD, TODAY)
    assert not CS.adult_proof(YOUNG, TODAY)                     # nothing 18 years back: no guess
    assert not CS.adult_proof({}, TODAY) and not CS.adult_proof({"experience": [{"org": "Initech"}]}, TODAY)
    # the month matters at the edge: November 2008 is not yet 18 full years before 17 November 2026
    edge = lambda d: {"education": [{"school": "Example High", "date": d}]}     # noqa: E731
    assert CS.adult_proof(edge("October 2008"), TODAY) and not CS.adult_proof(edge("November 2008"), TODAY)
    assert CS.adult_proof(edge("2007"), TODAY) and not CS.adult_proof(edge("2008"), TODAY)   # no month: December


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
        for k in ("setup.relatives", "setup.relativesAsk", "aa.ans.from.tracker"):
            assert d.get(k), (lang, k)
            assert "—" not in d[k] and "–" not in d[k]
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'suT("setup.relatives"' in js and 'T("aa.ans.from.tracker"' in js


def test_the_shell_keeps_the_rule_origins_for_review_answers():
    js = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert 'new Set(["grounded", "neutral", "history", "tracker", "consent"])' in js
    assert 'tracker: "from your tracker"' in js
