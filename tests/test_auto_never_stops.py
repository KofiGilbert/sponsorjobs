"""Auto never stops to ask (2026-11-19, docs/apply-agent.md section 31).

The owner: "For auto apply, we can't stop to ask the user anything. ... if it does require, we
will use a common sense approach to pick the best option possible and proceed with the
application." Stopping to ask belongs to Assisted and Hybrid.

* the best guess and its order of preference (autoapply/guess.py): a neutral choice, else the
  majority answer, else the choice that commits to nothing; self-identification only declines;
* the check in Auto (autoapply/protocol.py): an unknown is a flagged best guess, also on a
  legal question; Assisted still refuses, so it still asks;
* what is never guessed, in any mode, and the skip that follows;
* an application holding a guessed legal answer is never sent by the agent alone, and says so;
* the setup's checklist round trip, and the plan's ordering by known forms;
* the shell under node (tests/js/test_auto_never_stops.js).

Everyone here is made up.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import brain, guess, modes, protocol, qclass
from autoapply import setup as S

# The apply agent is off by default (assisted apply only); these tests cover it, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
COMPANY = "Brightwater Freight"
GH = "https://boards.greenhouse.io/brightwater/jobs/"


# --------------------------------------------------------------------------- the best guess
def test_a_neutral_choice_comes_first():
    q = "Are you subject to a non-compete agreement?"
    assert guess.choose(q, ["Yes", "No", "Prefer not to say"])[0] == "Prefer not to say"
    assert guess.choose("Do you have a conflict of interest?", ["Yes", "No", "I do not wish to answer"])[0] == "I do not wish to answer"
    assert guess.choose("Which union are you a member of?", ["Local 1", "Local 2", "N/A"])[0] == "N/A"


@pytest.mark.parametrize("q, want", [
    ("Do you have relatives working at Brightwater Freight?", "No"),
    ("Are you subject to a non-compete or other restrictive agreement?", "No"),
    ("Have you ever been convicted of a felony?", "No"),
    ("Have you ever been dismissed or asked to resign?", "No"),
    ("Are you a current or former government official?", "No"),
    ("Are you at least 18 years old?", "Yes"),
    ("Can you perform the essential functions of this job with or without reasonable accommodation?", "Yes"),
])
def test_with_no_neutral_choice_the_majority_answer_that_commits_to_the_least(q, want):
    got = guess.choose(q, ["Yes", "No"])
    assert got and got[0] == want and len(got[1]) > 10, got


def test_else_the_first_choice_that_commits_to_nothing_else_the_brains_own():
    assert guess.choose("Which team are you applying to?", ["Alpha", "Beta", "Other"])[0] == "Other"
    assert guess.choose("Which team are you applying to?", ["Alpha", "Beta"]) is None        # the brain's own best guess
    assert guess.choose("Describe your ideal manager", []) is None                            # no choices to read


def test_self_identification_only_ever_declines():
    assert guess.choose("Gender", ["Male", "Female", "Decline To Self Identify"])[0] == "Decline To Self Identify"
    assert guess.choose("Veteran status", ["I am a veteran", "I am not a veteran", "I do not wish to answer"])[0] == "I do not wish to answer"
    assert guess.choose("Gender", ["Male", "Female"]) is None                                  # no decline: never a guess
    ok, why = guess.accept("Gender", "Male", ["Male", "Female", "Decline To Self Identify"])
    assert not ok and "decline" in why
    assert not guess.accept("Race", "White", ["White", "Asian"])[0]
    assert guess.accept("Gender", "Decline To Self Identify", ["Male", "Female", "Decline To Self Identify"])[0]


NEVER = ["Social Security Number", "SSN (last 4 digits)", "Date of birth", "Passport number", "Driver's license number",
         "Bank account number", "Routing number", "Credit card number", "Create a password", "National ID number",
         "Signature", "Type your full legal name to sign", "Electronic signature"]


@pytest.mark.parametrize("q", NEVER)
def test_what_is_never_guessed(q):
    assert qclass.never_guess(q)
    assert guess.choose(q, ["A", "B", "N/A"]) is None
    assert guess.accept(q, "N/A", [], typed=True) == (False, "never")


@pytest.mark.parametrize("q", ["Phone", "Design a sign for our team", "Notice period", "Desired salary", "City", "Signal experience"])
def test_ordinary_fields_are_not_on_the_never_list(q):
    assert not qclass.never_guess(q)


def test_consents_are_not_guessed():
    q = "I agree to the terms and conditions"
    assert qclass.is_consent(q) and guess.choose(q, ["Yes", "No"]) is None and guess.accept(q, "Yes", ["Yes", "No"]) == (False, "consent")
    # a background check or drug screen is a consent too: the consent settings decide, never a guess
    bg = "Are you willing to undergo a background check as a condition of employment?"
    assert qclass.is_consent(bg) and guess.choose(bg, ["Yes", "No"]) is None


def test_a_typed_guess_is_a_few_words_and_never_a_made_up_number():
    assert guess.accept("Notice period", "Two weeks", [], typed=True)[0]
    assert guess.accept("Salary expectations", "Negotiable", [], typed=True)[0]
    assert not guess.accept("What is your GPA?", "3.7", [], typed=True)[0]                     # a number about the person
    assert not guess.accept("Salary expectations", "120000", [], typed=True)[0]
    assert not guess.accept("Why us?", "word " * 12, [], typed=True)[0]


# --------------------------------------------------------------------------- the check, in Auto and not
LEGAL_Q = "Are you subject to a non-compete agreement?"


def test_in_auto_an_unknown_legal_question_is_a_flagged_best_guess():
    a = {"op": "select", "index": 4, "option": "No", "source": "best_guess", "evidence": "most people are not", "class": "legal"}
    v = protocol.check(a, LEGAL_Q, ["Yes", "No"], None, auto=True)
    assert v.ok and v.source == "best_guess" and v.klass == qclass.LEGAL and v.evidence == "most people are not"
    rec = protocol.record(a, v)
    assert rec == {"source": "best_guess", "origin": "best_guess", "evidence": "most people are not", "class": "legal"}
    # with nothing said, the engine's own reason rides along
    v = protocol.check({"op": "select", "index": 4, "option": "No"}, LEGAL_Q, ["Yes", "No"], None, auto=True)
    assert v.ok and v.source == "best_guess" and "no such agreement" in v.evidence
    # the form's neutral choice is the guess when it offers one
    v = protocol.check({"op": "select", "index": 4, "option": "Yes"}, "Which union are you in?", ["Local 1", "Prefer not to say", "Yes"], None, auto=True)
    assert not v.ok and "Prefer not to say" in v.reason
    # an unknown factual question too: no more "nothing in <person> supports this"
    v = protocol.check({"op": "select", "index": 4, "option": "Other"}, "Which team are you applying to?", ["Alpha", "Other"], None, auto=True)
    assert v.ok and v.source == "best_guess"


def test_assisted_and_hybrid_still_refuse_so_they_still_ask():
    v = protocol.check({"op": "select", "index": 4, "option": "No", "source": "best_guess", "evidence": "x", "class": "legal"},
                       LEGAL_Q, ["Yes", "No"], None)
    assert not v.ok and v.hard == "5"


@pytest.mark.parametrize("auto", [True, False])
def test_the_never_list_holds_in_every_mode(auto):
    v = protocol.check({"op": "type", "index": 4, "text": "01/02/1990", "source": "best_guess", "evidence": "x"}, "Date of birth", [], None, auto=auto)
    assert not v.ok and v.hard in ("5", "9")
    if auto:
        assert v.hard == "9" and "never guessed" in v.reason
    v = protocol.check({"op": "type", "index": 4, "text": "Dana Whitfield"}, "Signature", [], None, auto=auto)
    assert not v.ok


def test_filler_and_long_numbers_are_still_refused_in_auto():
    assert not protocol.check({"op": "type", "index": 4, "text": "TBD"}, "Team name", [], None, auto=True).ok
    assert not protocol.check({"op": "type", "index": 4, "text": "5550100199"}, "Team extension", [], None, auto=True).ok


PAGE = """URL: https://jobs.brightwater.example/apply/1
[3]<radio label="Are you subject to a non-compete agreement?" option="Yes" checked=false required>
[4]<radio label="Are you subject to a non-compete agreement?" option="No" checked=false required>
[5]<input type=text label="Social Security Number" value="" required>
[9]<button>Next</button>"""


class Scripted:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def agent_step(self, system, user):
        self.calls.append(user)
        return self.reply


def test_the_brain_is_told_its_mode_and_its_guess_runs_with_its_proof():
    reply = json.dumps({"memory": "", "evaluation": "", "summary": "", "done": None, "actions": [
        {"op": "click", "index": 4, "source": "best_guess", "evidence": "most applicants are under none", "class": "legal"},
        {"op": "type", "index": 5, "text": "000-00-0000", "source": "best_guess", "evidence": "x"}]})
    llm = Scripted(reply)
    out = brain.step({"view": PAGE, "step": 2, "left": [LEGAL_Q], "history": [], "auto": True}, llm, {}, None, placeholders=[])
    assert "<mode>\nAUTO: nobody is here to ask" in llm.calls[0] and "best_guess" in llm.calls[0]
    assert [(a["op"], a["index"]) for a in out["actions"]] == [("click", 4)]
    assert out["actions"][0]["proof"] == {"source": "best_guess", "origin": "best_guess", "evidence": "most applicants are under none",
                                          "class": "legal"}
    assert out["rejected"] == [{"index": 5, "label": "Social Security Number", "hard": "9"}]
    # the same reply with the person there: the legal one is refused (they are asked), and the mode says so
    llm2 = Scripted(reply)
    out2 = brain.step({"view": PAGE, "step": 2, "left": [LEGAL_Q], "history": []}, llm2, {}, None, placeholders=[])
    assert "AUTO" not in llm2.calls[0] and "the person is here" in llm2.calls[0]
    assert out2["actions"] == [] and {r["hard"] for r in out2["rejected"]} == {"5"}
    # the system prompt is the same text in both modes (it is the cached part)
    assert "AUTO" not in brain.SYSTEM_PROMPT


# --------------------------------------------------------------------------- never sent alone
def test_a_guessed_legal_answer_is_never_sent_by_the_agent_alone():
    assert modes.may_send_alone("auto", "auto", 90) is True
    assert modes.may_send_alone("auto", "auto", 90, guessed_legal=True) is False
    assert modes.may_send_alone("auto", "auto", 90, 80, guessed_legal=True) is False


# --------------------------------------------------------------------------- the routes
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    return mod


def _post(mod, path, body=None):
    return mod.app.test_client().post(path, json=body or {}, headers=H).get_json()


def _get(mod, path):
    return mod.app.test_client().get(path, headers=H).get_json()


def _add(mod, role, company, url, **data):
    recs = mod._records()
    try:
        return recs.add(role, company, 80, "", data={"status": "approved", "fit": 80, "source_job": {"url": url}, **data})
    finally:
        recs.close()


QS = [{"question": LEGAL_Q, "options": ["Yes", "No"]},
      {"question": "Which union are you in?", "options": ["Local 1", "Local 2", "Prefer not to say"]},
      {"question": "Which team are you applying to?", "options": ["Alpha", "Beta"]},
      {"question": "Social Security Number", "options": []}]


def test_the_known_only_check_guesses_only_when_auto_asks_it_to(A):
    mod = A
    r = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "fill_through": True, "auto_guess": True, "questions": QS})
    assert r["answers"][LEGAL_Q] == "No" and r["origins"][LEGAL_Q] == "best_guess" and r["classes"][LEGAL_Q] == "legal"
    assert "no such agreement" in r["reasons"][LEGAL_Q] and r["select"][LEGAL_Q] == "No"
    assert r["answers"]["Which union are you in?"] == "Prefer not to say"                 # the neutral choice (fill-through, as before)
    assert r["never"] == ["Social Security Number"]                                        # never guessed: the shell skips
    assert "Which team are you applying to?" in r["open"]                                  # the brain's own best guess
    # Assisted and Hybrid: nothing is guessed, the legal question stays open and is asked
    r2 = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "fill_through": True, "questions": QS})
    assert LEGAL_Q not in r2["answers"] and LEGAL_Q in r2["open"] and r2["never"] == [] and r2["reasons"] == {}


def test_the_guessed_counts_are_kept_and_hold_the_application_for_the_persons_send(A):
    mod = A
    rid = _add(mod, "Analyst", COMPANY, "https://jobs.example.com/a/1")
    assert _post(mod, "/api/autoapply/agent/ready", {"record_id": rid, "sig": "s", "run": {}, "guessed": 3, "guessed_legal": 1})["ok"]
    data = mod._record_data(mod._get_record(rid))
    assert data["agent_guessed"]["guessed"] == 3 and data["agent_guessed"]["guessed_legal"] == 1
    mod._set_apply_mode("auto")
    assert mod._may_send_alone("auto", 90, {}) is True
    assert mod._may_send_alone("auto", 90, data) is False
    assert mod._may_send_alone("auto", 90, {"agent_guessed": {"guessed": 2, "guessed_legal": 0}}) is True   # a guess, none legal
    row = mod._aa_row(mod._get_record(rid), data, {})
    assert row["guessed"] == 3 and row["guessed_legal"] == 1 and row["auto_send"] is False and row["action"] == "send"
    # the daily summary counts them, in words
    c = mod._auto_counts()
    assert c["ready"] == 1 and c["check"] == 3
    from notify.daily import summary_text
    t = summary_text(0, 1, 0, check=3, could_not=1)
    assert "3 answers need your check before you send." in t and "1 form I could not do." in t
    assert summary_text(0, 1, 0) == "Today: 0 sent for you, 1 ready for your Send, 0 need an answer."


def test_a_required_never_guessed_field_skips_the_application_and_it_is_not_a_question(A):
    mod = A
    rid = _add(mod, "Analyst", COMPANY, "https://jobs.example.com/a/2")
    r = _post(mod, "/api/autoapply/agent/skip", {"record_id": rid, "field": "Social Security Number", "url": "https://jobs.example.com/a/2"})
    assert r == {"ok": True, "state": "skipped", "field": "Social Security Number"}
    data = mod._record_data(mod._get_record(rid))
    from submit.stages import app_stage
    st = app_stage(data)
    assert st["stage"] == "failed" and st["reason"] == "cannot_fill" and "never fill in for you" in st["detail"]
    assert "Social Security Number" in data["submission"]["message"] and data["agent_skip"]["field"] == "Social Security Number"
    c = mod._auto_counts()
    assert c["could_not"] == 1 and c["need"] == 0                                   # a job the agent could not do, not a question
    mod._set_apply_mode("auto")
    assert rid not in [j["recordId"] for j in _post(mod, "/api/autoapply/auto/plan", {"peek": True})["jobs"]]   # no batch picks it again


# --------------------------------------------------------------------------- the setup checklist
def test_the_checklist_round_trip(A):
    mod = A
    c = _get(mod, "/api/autoapply/auto/checklist")
    assert c["total"] == 14 and c["open"] == 14 and [x["id"] for x in c["items"]] == [i for i, *_ in S.AUTO_ITEMS]
    assert c["sentence"].startswith("Auto never stops to ask. Answer these once so it never has to guess.")
    assert "best judgement" in c["terms"] and "responsible for checking" in c["terms"]
    # it never blocks turning Auto on
    assert _post(mod, "/api/submit/settings", {"apply_mode": "auto"})["apply_mode"] == "auto"
    save = lambda step, fields: _post(mod, "/api/apply-setup/save", {"step": step, "fields": fields})   # noqa: E731
    d = save("forms", {"criminal": "No", "dismissed": "No", "covenants": "No", "relatives": "No", "other_names": "None",
                       "clearance": "None", "current_salary": "90000", "terms": "always"})
    assert d["forms"]["criminal"] == "No" and d["forms"]["dismissed"] == "No"
    save("wants", {"notice": "Two weeks"})
    c2 = _get(mod, "/api/autoapply/auto/checklist")
    done = {x["id"] for x in c2["items"] if x["answered"]}
    assert {"criminal", "dismissed", "covenants", "relatives", "other_names", "clearance", "current_salary", "background", "start"} <= done
    assert c2["open"] == 14 - len(done) and c2["open"] < 14
    # saved under their own keys, so a form's own wording finds them and nothing is guessed
    assert S.KEYS["criminal"] == "@criminal_record" and S.KEYS["dismissed"] == "@dismissed"
    r = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "auto_guess": True, "questions": [
        {"question": "Have you ever been convicted of a felony?", "options": ["Yes", "No"]},
        {"question": "Have you ever been terminated or asked to resign from any position?", "options": ["Yes", "No"]}]})
    assert set(r["answers"].values()) == {"No"} and set(r["origins"].values()) == {"library"} and r["reasons"] == {}
    # removing one opens it again
    save("forms", {"criminal": ""})
    assert "criminal" not in {x["id"] for x in _get(mod, "/api/autoapply/auto/checklist")["items"] if x["answered"]}


def test_the_new_keys_do_not_swallow_other_questions():
    from autoapply.answers import normalize
    assert normalize("Have you ever been convicted of a felony?") == "@criminal_record"
    assert normalize("Do you have any pending criminal charges?") == "@criminal_record"
    assert normalize("Have you ever been dismissed or asked to resign from a job?") == "@dismissed"
    for q in ("Do you consent to a criminal background check?", "If yes, please explain the conviction", "Reason for termination",
              "Why did you leave your last job?"):
        assert not normalize(q).startswith("@"), q


# --------------------------------------------------------------------------- the plan's ordering
def _feed(questions):
    return lambda url: {"id": 1, "questions": questions}


def _q(label, required=True, values=()):
    return {"label": label, "required": required, "fields": [{"type": "multi_value_single_select" if values else "input_text",
                                                               "values": [{"label": v} for v in values]}]}


def test_the_feeds_questions_are_read_and_counted():
    from sourcing import formquestions as FQ
    assert FQ.greenhouse_ref(GH + "4506632", "") == ("brightwater", "4506632")
    assert FQ.greenhouse_ref("https://careers.example.com/job?gh_jid=77", "greenhouse:acme:77") == ("acme", "77")
    assert FQ.greenhouse_ref("https://jobs.lever.co/acme/1", "") is None
    seen = []
    qs = FQ.greenhouse_questions("brightwater", "1", fetch=lambda u: (seen.append(u), {"questions": [
        _q("First Name"), _q("Resume/CV"), _q("Gender", values=["Male", "Female", "Decline To Self Identify"]),
        _q("How did you hear about us?"), _q("Why do you want to work here?"), _q(LEGAL_Q, values=["Yes", "No"]),
        _q("What is your GPA?"), _q("Favourite tool", required=False)]})[1])
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/brightwater/jobs/1?questions=true"]
    assert len(qs) == 8 and qs[5] == {"label": LEGAL_Q, "required": True, "type": "multi_value_single_select", "options": ["Yes", "No"]}
    # unknown: the non-compete and the GPA. Not the name, the file, the self-identification, how-did-you-hear, the essay, the optional one
    assert FQ.unknown_count(qs) == 2
    assert FQ.unknown_count(qs, answered=[LEGAL_Q]) == 1
    assert FQ.greenhouse_questions("x", "1", fetch=lambda u: (_ for _ in ()).throw(OSError("down"))) is None
    assert FQ.greenhouse_questions("x", "1", fetch=lambda u: {"id": 1}) is None          # no questions in the answer: nothing known


def test_auto_takes_the_forms_it_can_finish_without_a_guess_first(A, monkeypatch):
    mod = A
    mod._set_apply_mode("auto")
    forms = {"11": [_q("First Name"), _q(LEGAL_Q, values=["Yes", "No"]), _q("What is your GPA?")],      # 2 unknown
             "12": [_q("First Name"), _q("How did you hear about us?")]}                                 # 0 unknown
    calls = []

    def fetch(url):
        calls.append(url)
        return {"id": 1, "questions": forms[re.search(r"/jobs/(\d+)", url).group(1)]}
    monkeypatch.setattr(mod, "_FORM_FETCH", fetch)
    hard = _add(mod, "Analyst", "Acme", GH + "11")                 # approved first
    clean = _add(mod, "Analyst", "Bolt", GH + "12")
    nofeed = _add(mod, "Analyst", "Cove", "https://jobs.lever.co/cove/1")
    jobs = _post(mod, "/api/autoapply/auto/plan", {"peek": True})["jobs"]
    assert [j["recordId"] for j in jobs] == [clean, nofeed, hard], jobs
    assert [j["form_score"] for j in jobs] == [0.0, 0.5, 2.0]
    # kept on the record: the next plan asks the feed nothing
    n = len(calls)
    assert mod._record_data(mod._get_record(hard))["form_questions"]["unknown"] == 2
    _post(mod, "/api/autoapply/auto/plan", {"peek": True})
    assert len(calls) == n == 2
    # the fallback signal: what earlier runs met, per employer, then per form system
    done = _add(mod, "Engineer", "Cove", "https://jobs.lever.co/cove/0")
    _post(mod, "/api/autoapply/agent/ready", {"record_id": done, "sig": "s", "run": {}, "guessed": 4, "guessed_legal": 0})
    hist = mod._prefs()["form_history"]
    assert hist["co:cove"]["runs"] == 1 and hist["co:cove"]["guessed"] == 4 and hist["sys:lever.co"]["guessed"] == 4
    other = _add(mod, "Analyst", "Dune", "https://jobs.lever.co/dune/1")
    by = {j["recordId"]: j["form_score"] for j in _post(mod, "/api/autoapply/auto/plan", {"peek": True})["jobs"]}
    assert by[nofeed] == 4.0 and by[other] == 4.0 and by[clean] == 0.0       # the employer's own runs; the system's for a new employer
    # a saved answer changes the feed's count: the non-compete is no longer unknown
    _post(mod, "/api/apply-setup/save", {"step": "forms", "fields": {"covenants": "No"}})
    fresh = _add(mod, "Analyst", "Echo", GH + "11")
    by = {j["recordId"]: j["form_score"] for j in _post(mod, "/api/autoapply/auto/plan", {"peek": True, "limit": 10})["jobs"]}
    assert by[fresh] == 1.0


def test_under_test_the_feed_is_never_called_without_a_stand_in(A):
    mod = A
    mod._set_apply_mode("auto")
    rid = _add(mod, "Analyst", "Acme", GH + "11")
    jobs = _post(mod, "/api/autoapply/auto/plan", {"peek": True})["jobs"]
    assert jobs[0]["recordId"] == rid and jobs[0]["form_score"] == 0.5


# --------------------------------------------------------------------------- words
def test_the_words_are_in_five_locales_and_say_what_auto_does():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for k in ("aa.ans.flag.bestGuess", "aa.ans.from.bestGuess", "mode.autoAsk", "mode.autoTerms", "mode.checkN", "mode.checkLegalN",
              "setup.criminal", "setup.dismissed"):
        assert f'"{k}"' in js, k
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert en["aa.ans.flag.bestGuess"] == "Best guess. Check it." and en["mode.autoAsk"] == S.AUTO_SENTENCE and en["mode.autoTerms"] == S.AUTO_TERMS
    assert en["mode.checkN"] == "{n} answers need your check"
    for code in ("de", "en", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in ["aa.ans.flag.bestGuess", "aa.ans.flag.bestGuessWhy", "mode.autoAsk", "mode.autoTerms", "mode.check1", "mode.checkN",
                  "mode.checkLegal1", "mode.checkLegalN", "mode.autoOpenN", "setup.criminal", "setup.dismissed"] + \
                 ["mode.ck." + i for i, *_ in S.AUTO_ITEMS]:
            assert loc.get(k) and not re.search(r"[—–]", loc[k]), (code, k)


def test_the_docs_say_what_auto_does_when_it_does_not_know():
    doc = (ROOT / "docs" / "apply-agent.md").read_text(encoding="utf-8")
    assert "## 31. What Auto does when it does not know" in doc and "never guessed" in doc.lower()
    assert "Set aside: no saved answer" not in (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the shell
def test_the_shell_never_sets_an_auto_application_aside_for_a_question():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_auto_never_stops.js")], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


# --------------------------------------------------------------------------- a real Workday form (2026-11-19)
# A hidden run on a real Workday tenant reached Application Questions and was set aside for three
# questions. Two were bugs (work authorization IS saved; the employer is not in the history), one
# a legal declaration with no saved answer. The labels below are the form's own (the employer is
# the real one in the run's log; nothing about the person is).
OCH = "Ochsner Health"
WA_Q = ("Are you legally authorized to work in the United States without requiring visa sponsorship (e.g., H-1B visa status, "
        "etc.) from an employer now or in the future?")
WA_OPTS = ["Yes, I am legally authorized to work in the United States. (I do not and will not need visa sponsorship from an employer)",
           "I currently need a visa sponsorship from an employer to work in the United States",
           "In the future, I will need visa sponsorship from an employer to work in the United States"]
PREV_Q = "Have you previously been employed by Ochsner or any of companies, partners, or affiliates?"
CUR_Q = ("Are you currently employed by any of the following Ochsner companies, partners, or affiliates?Companies Include: "
         "Ochsner Health, Ochsner LSU Health Shreveport (OLHS), Ochsner Health Plan, St. Charles")
EXCL_Q = ("Have you ever been suspended, sanctioned, excluded, or otherwise precluded from participating in Medicare, Medicaid, "
          "or any other federal, state or private health insurance program?")


def _wa(now, future, authorized="Yes", status="f1_opt"):
    from autoapply import workauth as W
    return W.clean({"countries": {"US": {"status": status, "authorized": authorized, "sponsor_now": now, "sponsor_future": future}}})


def test_the_combined_work_authorization_question_is_answered_from_the_saved_profile():
    from autoapply import workauth as W
    assert W.classify(WA_Q) == "authorized_no_sponsor"
    # authorized, no sponsorship now, sponsorship in the future: "No" to "without sponsorship now or in the future"
    assert W.answer(WA_Q, _wa("No", "Yes"), options=["Yes", "No"], field_type="radio") == "No"
    # the form's own choices state a situation instead of Yes or No: the one that is the person's
    assert W.answer(WA_Q, _wa("No", "Yes"), options=WA_OPTS, field_type="select") == WA_OPTS[2]
    assert W.answer(WA_Q, _wa("Yes", "Yes"), options=WA_OPTS, field_type="select") == WA_OPTS[1]
    assert W.answer(WA_Q, _wa("No", "No", status="us_citizen"), options=WA_OPTS, field_type="select") == WA_OPTS[0]
    assert W.answer(WA_Q, _wa("No", "No", status="us_citizen"), options=["Yes", "No"], field_type="radio") == "Yes"
    # nothing saved: never a guess here (Auto's best guess is a separate, flagged step)
    assert W.answer(WA_Q, {}, options=WA_OPTS, field_type="select") is None


@pytest.mark.parametrize("q", [
    "Are you authorized to work in the US without the need for sponsorship?",
    "Are you legally authorized to work in the U.S. without sponsorship now or in the future?",
    "Can you work in the United States without requiring sponsorship currently or in the future?",
    "Are you legally eligible to work in the United States without the need for visa sponsorship, now or in the future?",
])
def test_variants_of_the_combined_question(q):
    from autoapply import workauth as W
    assert W.classify(q) == "authorized_no_sponsor", q
    assert W.answer(q, _wa("No", "Yes"), options=["Yes", "No"], field_type="radio") == "No"
    assert W.answer(q, _wa("No", "No", status="green_card"), options=["Yes", "No"], field_type="radio") == "Yes"


def test_worked_here_reads_the_employers_group_and_the_currently_employed_form():
    from autoapply.planner import currently_here_question, worked_here_question
    # the form drops "our" and says "companies, partners, or affiliates": the employer's own group
    assert worked_here_question(PREV_Q, OCH) and not currently_here_question(PREV_Q, OCH)
    assert worked_here_question(CUR_Q, OCH) and currently_here_question(CUR_Q, OCH)
    assert qclass.worked_here(PREV_Q, OCH) and qclass.worked_here(CUR_Q, OCH)
    # a lone partner is someone else, never the employer
    assert not worked_here_question("Is your domestic partner employed by Ochsner?", OCH)
    assert not worked_here_question("Do you have a partner who works at Ochsner?", OCH)


def test_both_worked_here_questions_get_no_from_a_history_without_the_employer():
    from autoapply.asks import known_answer
    for q in (PREV_Q, CUR_Q):
        got = known_answer(q, ["Yes", "No"], per_company=True, worked_here=("No", "history"), company=OCH)
        assert got == ("No", "history"), (q, got)
    # the employer IS in the history: "previously" is the person's one-tap confirm, as before, and
    # "currently employed" is never answered Yes from the history (it may be a past job)
    assert known_answer(PREV_Q, ["Yes", "No"], per_company=True, worked_here=("Yes", "confirm"), company=OCH) is None
    assert known_answer(CUR_Q, ["Yes", "No"], per_company=True, worked_here=("Yes", "library"), company=OCH) is None
    v = protocol.check({"op": "select", "index": 4, "option": "No"}, CUR_Q, ["Yes", "No"],
                       type("M", (), {"company": OCH, "employers": ["Northwind Analytics"], "block": "x", "facts": []})())
    assert v.ok


def test_the_exclusion_declaration_is_legal_a_setup_question_and_a_flagged_guess_in_auto():
    from autoapply.answers import normalize
    assert qclass.question_class(EXCL_Q) == qclass.LEGAL and normalize(EXCL_Q) == "@excluded_programs"
    assert S.KEYS["excluded"] == "@excluded_programs" and "excluded" in [i for i, *_ in S.AUTO_ITEMS]
    assert guess.choose(EXCL_Q, ["Yes", "No"])[0] == "No"
    v = protocol.check({"op": "select", "index": 4, "option": "No"}, EXCL_Q, ["Yes", "No"], None, auto=True)
    assert v.ok and v.source == "best_guess" and v.klass == qclass.LEGAL
    assert not protocol.check({"op": "select", "index": 4, "option": "No"}, EXCL_Q, ["Yes", "No"], None).ok   # Assisted asks
    assert not normalize("Do you consent to an exclusion list check?").startswith("@excluded")


def test_the_ochsner_page_in_auto_three_from_the_material_one_flagged_guess_none_set_aside(A, monkeypatch):
    mod = A
    monkeypatch.setattr(mod, "_work_auth", lambda: _wa("No", "Yes"))
    monkeypatch.setattr(mod, "_recall_ctx", lambda company="", data=None: {"per_company": True, "worked_here": ("No", "history"),
                                                                           "hold": frozenset()})
    rid = _add(mod, "Patient Access Representative", OCH, "https://ochsner.wd1.myworkdayjobs.com/en-US/External/job/1")
    qs = [{"question": CUR_Q, "options": ["Yes", "No"]}, {"question": PREV_Q, "options": ["Yes", "No"]},
          {"question": WA_Q, "options": WA_OPTS}, {"question": EXCL_Q, "options": ["Yes", "No"]}]
    r = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "fill_through": True, "auto_guess": True, "record_id": rid,
                                                "questions": qs})
    assert r["answers"] == {CUR_Q: "No", PREV_Q: "No", WA_Q: WA_OPTS[2], EXCL_Q: "No"}, r["answers"]
    assert r["origins"][WA_Q] == "workauth" and r["origins"][CUR_Q] == "history" and r["origins"][PREV_Q] == "history"
    assert [q for q, o in r["origins"].items() if o == "best_guess"] == [EXCL_Q] and r["classes"][EXCL_Q] == "legal"
    assert r["open"] == [] and r["never"] == [] and r["ok"] is True
    # Assisted: the same three from the material, and the declaration is asked
    r2 = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "fill_through": True, "record_id": rid, "questions": qs})
    assert r2["open"] == [EXCL_Q] and EXCL_Q not in r2["answers"]


# --------------------------------------------------------------------------- Send all, and a saved work authorization the form words differently
def test_send_all_asks_once_before_sending_a_guessed_legal_answer():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_send_all.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="saConfirm"' in html and "send_all.js" in html
    for code in ("de", "en", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in ("sendall.titleN", "sendall.lead", "sendall.review", "sendall.othersN", "sendall.all", "sendall.countN"):
            assert loc.get(k) and not re.search(r"[\u2014\u2013]", loc[k]), (code, k)


ODD_OPTS = ["Option A: authorized, status known", "Option B: different status"]


def test_a_saved_work_authorization_the_form_words_differently_says_so_on_the_flag():
    wa = _wa("No", "Yes")
    assert guess.reason_for(WA_Q, wa) == guess.WA_SAVED_REASON == "your work authorization is saved but this form words it differently"
    assert guess.reason_for(WA_Q, {}) == "" and guess.reason_for(LEGAL_Q, wa) == ""
    # the brain's guess on such a question: legal, best_guess, and that reason, whatever it wrote
    v = protocol.check({"op": "select", "index": 4, "option": ODD_OPTS[0], "evidence": "picked the closest"}, WA_Q, ODD_OPTS, None,
                       auto=True, reason=guess.reason_for(WA_Q, wa))
    assert v.ok and v.source == "best_guess" and v.klass == qclass.LEGAL and v.evidence == guess.WA_SAVED_REASON
    # and through the brain's step, from the reasons the step route hands in
    view = f'URL: https://jobs.example/apply\n[3]<select label="{WA_Q[:80]}" value="" options=2 first="{"|".join(ODD_OPTS)}" required>\n[9]<button>Next</button>'
    reply = json.dumps({"memory": "", "evaluation": "", "summary": "", "done": None,
                        "actions": [{"op": "select", "index": 3, "option": ODD_OPTS[0], "source": "best_guess", "evidence": "closest"}]})
    out = brain.step({"view": view, "step": 2, "left": [WA_Q], "history": [], "auto": True,
                      "guess_reasons": {WA_Q: guess.WA_SAVED_REASON}}, Scripted(reply), {}, None, placeholders=[])
    assert out["actions"][0]["proof"] == {"source": "best_guess", "origin": "best_guess", "evidence": guess.WA_SAVED_REASON, "class": "legal"}


def test_the_ask_route_gives_that_reason_to_its_own_guess(A, monkeypatch):
    mod = A
    monkeypatch.setattr(mod, "_work_auth", lambda: _wa("No", "Yes"))
    opts = ["Yes, I am authorized", "Prefer not to say"]           # a form whose wording the profile cannot be matched to
    r = _post(mod, "/api/autoapply/agent/ask", {"known_only": True, "auto_guess": True,
                                                "questions": [{"question": WA_Q, "options": opts}]})
    assert r["origins"][WA_Q] == "best_guess" and r["reasons"][WA_Q] == guess.WA_SAVED_REASON and r["classes"][WA_Q] == "legal"
