"""Five questions the person was asked after 15 fields were filled (Phoenix Contact on Greenhouse,
job-boards.greenhouse.io/phoenixcontact/jobs/8163614003, 2026-11-17), each now answered by a
category rule (never a site special case), on the page's exact labels:

1. "Are you a current or former Phoenix Contact employee?": the worked-here rule, from the
   person's work history (No unless the company is in it), deterministic and before any grounded
   call, never a "New wording" one-tap confirm;
2. "By selecting Yes, you agree to receive mobile messages ...": a marketing opt-in, declined
   unless the person saved @marketing_optin; a required yes-only box stays theirs;
3. the background check / drug screening notice (also worded as a willingness), "GDPR
   Acknowledgement", a privacy notice, at-will employment, "the information is true": the
   affirmative choice under the standing consent to required notices; never a disclosure;
4. "Do you have any restrictive covenants ...": a real fact, asked once, saved under
   @restrictive_covenants and reused on every wording;
5. the ask's context line never cut inside a bracket.
Also: a new run clears an earlier run's park reason on the Auto-apply row.
No model: the planner, the ask route and the brain run on the test double.
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from applicant import facts as F
from applicant import migrate as M
from applicant.library import BrainLibrary
from applicant.mapper import rule_known
from autoapply import consents as C
from autoapply import grounded
from autoapply.answers import AnswerLibrary, normalize
from autoapply.asks import known_answer, prompt_text, trim_label
from autoapply.fields import FormField
from autoapply.planner import KnownContext, asks_worked_here, plan_known, worked_here_question

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
CO = "Phoenix Contact"
YN = ["Yes", "No"]

EMPLOYEE = "Are you a current or former Phoenix Contact employee?"
SMS = ("By selecting Yes, you agree to receive mobile messages from us in relation to this job application. "
       "Message and data rates may apply. Message frequency varies. Reply STOP to opt out. SMS Policy")
SCREEN = ("If you are selected for this position, you will be required to complete a background check and/or "
          "drug screening as a condition of employment.")
WILLING = ("This question is intended only to provide advance notice of our hiring process. If an offer is extended "
           "and accepted, would you be willing to participate in and successfully complete any background check "
           "and/or drug screening required for this position and location?")
GDPR = "GDPR Acknowledgement"
COVENANTS = ("Do you have any restrictive covenants (non-compete, non-solicitation, confidentiality) with either "
             "your current or any prior employer?")
SPONSOR = "Will you now, OR IN THE FUTURE, require sponsorship for employment visa status (e.g., H-1B visa status)?"


@pytest.fixture()
def lib(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    yield lib
    lib.close()


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    d.mkdir()
    s = M.ensure(d)
    assert s is not None
    yield s
    M.forget_process_state(d)


def _ctx(lib, **kw):
    base = dict(library=lib, company=CO, per_company=True, worked_here=("No", "history"), accept_notices=True)
    base.update(kw)
    return KnownContext(**base)


# --------------------------------------------------------------------------- 1. worked here
@pytest.mark.parametrize("label", [
    EMPLOYEE,
    "Phoenix Contact employee (current or former)",
    "Have you been previously employed by Phoenix Contact?",
    "Were you previously employed with Phoenix Contact?",
    "Previously employed by Phoenix Contact?",
    "Have you worked for Phoenix Contact before?",
    "Are you a former employee of Phoenix Contact USA?",
])
def test_the_employee_wordings_are_the_worked_here_question(label):
    assert worked_here_question(label, CO)
    assert worked_here_question(label, "Phoenix Contact USA Inc")      # the record's name may carry the suffix
    assert rule_known(label, CO)                                       # the label mapper never sees it


@pytest.mark.parametrize("label", [
    "Are you related to a current or former Phoenix Contact employee?",
    "Were you referred by a current Phoenix Contact employee?",
    "Do you know anyone who is a current Phoenix Contact employee?",
    EMPLOYEE.replace("Phoenix Contact", "Siemens"),
    "Why do you want to work at Phoenix Contact?",
    "Would you like to work for Phoenix Contact?",
])
def test_someone_else_or_another_company_is_not_the_worked_here_question(label):
    assert not worked_here_question(label, CO)


def test_history_answers_no_unless_the_company_is_in_it(st):
    assert F.worked_here(st, CO, ["Stanbic Bank Ghana", "Viva"]) == ("No", "history")
    assert F.worked_here(st, CO, ["Phoenix Contact GmbH"]) == ("Yes", "confirm")      # theirs, one tap
    assert F.worked_here(st, CO, []) is None                                            # no history: asked
    F.save_worked_here(st, CO, "Yes", question=EMPLOYEE)
    assert F.worked_here(st, CO, ["Viva"]) == ("Yes", "library")                        # their own answer first


def test_the_phoenix_employee_question_is_answered_no_with_no_tap(lib):
    # the ask route's check (the shell's known_only), with the label mapper's waiting confirm
    assert known_answer(EMPLOYEE, YN, library=lib, per_company=True, worked_here=("No", "history"),
                        confirm={EMPLOYEE: "@previously_worked"}, company=CO) == ("No", "history")
    # the planner
    res = plan_known([FormField("w", EMPLOYEE, "select", YN, required=True)],
                     _ctx(lib, confirm={EMPLOYEE: "@previously_worked"}))
    assert [(a.value, a.origin) for a in res.actions] == [("No", "history")] and not res.left
    # a Yes read from the resume still waits for the one tap (never sent unconfirmed)
    assert known_answer(EMPLOYEE, YN, library=lib, per_company=True, worked_here=("Yes", "confirm"), company=CO) is None


def test_a_mapped_wording_that_names_the_company_needs_no_tap(lib):
    q = "Phoenix Contact alumni: have you held a position with us?"
    assert not worked_here_question(q, CO)
    assert asks_worked_here(q, CO, {q: "@previously_worked"})
    assert known_answer(q, YN, library=lib, per_company=True, worked_here=("No", "history"),
                        confirm={q: "@previously_worked"}, company=CO) == ("No", "history")
    # a mapping that does not name the company keeps its tap
    other = "Have you held a position with us?"
    assert not asks_worked_here(other, CO, {other: "@previously_worked"})


# --------------------------------------------------------------------------- 2. opt-ins
@pytest.mark.parametrize("label", [
    SMS, "I agree to receive newsletters and updates", "Text me updates about my application",
    "Subscribe to job alerts", "Would you like to join our talent community?",
    "I would like to receive marketing emails from Acme",
])
def test_marketing_opt_ins_are_recognised(label):
    assert C.is_optin(label) and rule_known(label, CO)


@pytest.mark.parametrize("label", ["Phone number", "Mobile phone", "Preferred method of contact",
                                   "How did you hear about us?", "Email address"])
def test_contact_fields_are_not_opt_ins(label):
    assert not C.is_optin(label)


def test_an_opt_in_is_declined_never_asked(lib):
    assert known_answer(SMS, YN, library=lib) == ("No", "optin")
    assert known_answer(SMS, ["Yes, I agree", "No, I do not agree"], library=lib) == ("No, I do not agree", "optin")
    fields = [FormField("sms", SMS, "select", YN, required=True),
              FormField("news", "Sign me up for the newsletter", "checkbox"),
              FormField("alerts", "Subscribe to job alerts", "checkbox", required=False)]
    res = plan_known(fields, _ctx(lib))
    assert [(a.ref, a.value, a.origin) for a in res.actions] == [("sms", "No", "optin")]
    assert not res.left and not res.rest                    # the boxes stay unticked, never the model's


def test_a_saved_yes_opts_in(lib):
    lib.save("@marketing_optin", "Yes", "you")
    assert lib.best("@marketing_optin")
    assert known_answer(SMS, YN, library=lib) == ("Yes", "library")
    res = plan_known([FormField("news", "Sign me up for the newsletter", "checkbox")], _ctx(lib))
    assert [(a.op, a.value) for a in res.actions] == [("check", "Yes")]


def test_a_required_yes_only_opt_in_is_the_persons(lib):
    assert known_answer(SMS, ["Yes"], library=lib) is None
    res = plan_known([FormField("sms", SMS, "select", ["Yes"], required=True),
                      FormField("box", "I agree to receive text messages about my application", "checkbox",
                                required=True)], _ctx(lib))
    assert not res.actions and [f.ref for f in res.left] == ["sms", "box"]


def test_a_box_of_an_opt_in_group_ticks_the_decline():
    assert C.standing_answer("Can we text you about this job?: No", [], "checkbox") == (C.PICK, "Yes", "optin")
    assert C.standing_answer("Can we text you about this job?: Yes", [], "checkbox") == (C.NOTHING, "", "")


# --------------------------------------------------------------------------- 3. acknowledgements
@pytest.mark.parametrize("label,options,want", [
    (SCREEN, YN, "Yes"),
    (SCREEN, ["Yes, I acknowledge", "No"], "Yes, I acknowledge"),
    (WILLING, YN, "Yes"),
    (GDPR, ["I acknowledge", "I do not acknowledge"], "I acknowledge"),
    ("I acknowledge that I have read and understand the Candidate Privacy Notice", ["Yes", "No"], "Yes"),
    ("I understand and acknowledge that employment with the company is at-will.", ["I agree", "I disagree"], "I agree"),
    ("I certify that the information I have provided in this application is true and complete.", YN, "Yes"),
])
def test_acknowledgements_take_the_affirmative_choice_under_the_standing_consent(lib, label, options, want):
    assert C.is_acknowledgement(label) and rule_known(label, CO)
    assert known_answer(label, options, library=lib, accept_notices=True) == (want, "consent")
    assert known_answer(label, options, library=lib) is None                      # no consent: asked as before
    res = plan_known([FormField("a", label, "select", options, required=True)], _ctx(lib))
    assert [(a.value, a.origin) for a in res.actions] == [(want, "consent")]
    res = plan_known([FormField("a", label, "select", options, required=True)], _ctx(lib, accept_notices=False))
    assert not res.actions and [f.ref for f in res.left] == ["a"]


def test_a_lone_acknowledgement_box_is_ticked():
    res = plan_known([FormField("c", "I certify that the information provided is true and accurate.", "checkbox",
                                required=True)], KnownContext(accept_notices=True))
    assert [(a.op, a.value, a.origin) for a in res.actions] == [("check", "Yes", "consent")]


@pytest.mark.parametrize("label", [
    "Have you ever been convicted of a felony?",
    "Have you ever failed a drug test or background check?",
    COVENANTS,
    "Are you subject to a non-compete agreement?",
    "Have you ever been terminated from a position?",
    "I certify that I am legally authorized to work in the United States.",
])
def test_a_disclosure_is_never_an_acknowledgement(lib, label):
    assert not C.is_acknowledgement(label)
    assert known_answer(label, YN, library=lib, accept_notices=True) is None


# --------------------------------------------------------------------------- 4. restrictive covenants
def test_restrictive_covenants_are_one_key_and_never_grounded():
    assert normalize(COVENANTS) == "@restrictive_covenants"
    assert normalize("Are you bound by a non-compete agreement?") == "@restrictive_covenants"
    assert normalize("Would you be willing to sign a non-compete agreement?") != "@restrictive_covenants"
    assert not grounded.eligible(COVENANTS, YN)
    assert C.is_acknowledgement(COVENANTS) is False and C.is_optin(COVENANTS) is False


def test_restrictive_covenants_are_asked_once_then_reused(lib, st):
    for library in (lib, BrainLibrary(st)):
        assert known_answer(COVENANTS, YN, library=library, accept_notices=True) is None    # asked the first time
        res = plan_known([FormField("rc", COVENANTS, "select", YN, required=True)], _ctx(library))
        assert [f.ref for f in res.left] == ["rc"]
        library.save(COVENANTS, "No", "you")                                               # their answer, saved
        assert known_answer(COVENANTS, YN, library=library) == ("No", "library")
        assert known_answer("Are you bound by a non-compete or non-solicitation agreement?", YN,
                            library=library) == ("No", "library")                            # every wording after
        res = plan_known([FormField("rc", COVENANTS, "select", YN, required=True)], _ctx(library))
        assert [(a.value, a.origin) for a in res.actions] == [("No", "library")]


# --------------------------------------------------------------------------- 5. the context line
def test_the_context_label_is_never_cut_inside_a_bracket():
    assert trim_label(SPONSOR[:90], 120) == "Will you now, OR IN THE FUTURE, require sponsorship for employment visa status…"
    assert trim_label(SPONSOR, 90) == "Will you now, OR IN THE FUTURE, require sponsorship for employment visa status…"
    assert trim_label("Gender (optional)", 90) == "Gender (optional)"
    assert trim_label("A short label", 90) == "A short label"
    long = "word " * 40
    t = trim_label(long, 50)
    assert t.endswith("…") and len(t) <= 51 and not t[:-1].endswith(" ")
    text = prompt_text([{"question": "Do you have any restrictive covenants?", "options": YN,
                         "context": SPONSOR[:82]}])
    assert "status (" not in text and "employment visa status…”" in text


def test_the_shells_context_line_is_trimmed_the_same_way():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = ("const A = require(%s);" % json.dumps(str(ROOT / "shell-electron" / "agent.js"))
          + "const s = %s;" % json.dumps(SPONSOR)
          + "const view = '[1]<input>Your name</input>\\n[2]<select>' + s + '</select>\\n[3]<select>Next question</select>';"
          + "process.stdout.write(JSON.stringify([A.trimLabel(s.slice(0, 82), 90), A.trimLabel(s, 90), A.trimLabel('Gender (optional)', 90)]));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60, check=True).stdout)
    want = "Will you now, OR IN THE FUTURE, require sponsorship for employment visa status…"
    assert out == [want, want, "Gender (optional)"]


# --------------------------------------------------------------------------- the stale park reason
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


def _detail(mod, rid):
    for t in mod.app.test_client().get("/api/autoapply/board", headers=H).get_json()["tiles"].values():
        for it in t:
            if it["id"] == rid:
                return it
    return None


def test_a_new_run_clears_the_earlier_runs_park_reason(app):
    mod, rid = app
    stale = ("I tried wait_for form, reload, reopen_job and could not get past this: the page did not answer "
             "(fill timed out while reading the form).")
    assert _post(mod, "/api/autoapply/agent/park", {"record_id": rid, "kind": "person", "message": stale,
                                                     "page_url": "https://job-boards.greenhouse.io/x", "run": {}})["ok"]
    assert stale in _detail(mod, rid)["stage_detail"]
    # the park file lost the entry (a failed write on a full disk), the row kept its copy
    mod._parked_save({})
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {"drop": True})["reason"] == "none"
    row = _detail(mod, rid)
    assert stale not in (row["stage_detail"] or "") and row["paused"] is None
    # and with the entry there, a new run's take clears it too
    assert _post(mod, "/api/autoapply/agent/park", {"record_id": rid, "kind": "person", "message": stale,
                                                     "page_url": "https://job-boards.greenhouse.io/x", "run": {}})["ok"]
    assert _post(mod, f"/api/autoapply/agent/parked/{rid}/take", {"drop": True})["ok"]
    assert _detail(mod, rid)["paused"] is None


def test_the_engine_reads_the_standing_consent(app, monkeypatch):
    mod, _rid = app
    monkeypatch.setattr(mod, "_terms_pref", lambda: "always")
    assert mod._accept_notices() is True
    monkeypatch.setattr(mod, "_terms_pref", lambda: "ask")
    assert mod._accept_notices() is False


def test_the_phoenix_page_asks_one_question_and_grounded_never_sees_the_others(app, monkeypatch):
    """The ask route's known_only check on the five questions the run asked: four answered by the
    rules before the grounded call, only the restrictive covenants left for the person."""
    mod, rid = app
    seen = []
    monkeypatch.setattr(mod, "_recall_ctx", lambda company, data=None: {
        "per_company": True, "worked_here": ("No", "history"), "hold": frozenset()})
    monkeypatch.setattr(mod, "_label_maps", lambda items, company="", library=None: (
        {}, {EMPLOYEE: "@previously_worked"}, set()))
    monkeypatch.setattr(mod, "_grounded_answers", lambda qs, *a, **k: seen.extend(q["question"] for q in qs) or {})
    monkeypatch.setattr(mod, "_terms_pref", lambda: "always")
    qs = [{"question": EMPLOYEE, "options": YN}, {"question": SMS, "options": YN},
          {"question": WILLING, "options": YN}, {"question": GDPR, "options": ["I acknowledge"]},
          {"question": COVENANTS, "options": YN}]
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": qs, "record_id": rid, "known_only": True})
    assert r["answers"] == {EMPLOYEE: "No", SMS: "No", WILLING: "Yes", GDPR: "I acknowledge"}
    assert r["origins"] == {EMPLOYEE: "history", SMS: "optin", WILLING: "consent", GDPR: "consent"}
    assert r["open"] == [COVENANTS] and seen == [COVENANTS]
