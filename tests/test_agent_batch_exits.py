"""The end-of-form batch is never a dead end (live, Phoenix Contact on Greenhouse, 2026-11-17).

The batch showed two questions. The first was a REQUIRED select whose ONLY option is "Yes" (an
SMS consent). The owner typed "No": the bar answered "I still could not tell. Type just the
answer, or answer on the page yourself." for ever, and the second question, already answered,
was never filled in because the batch never completed. Now (docs/apply-agent.md section 27):

* a question whose every choice the page lists is closed: a typed reply that is none of them is
  told what the form offers, with the exits (a choice, "Leave it empty", "Skip this job"); no
  model is asked and nothing loops;
* "No" to a required consent whose only choice is yes is a decision: the question is left empty
  and flagged, and the standing No is saved (``@forced_consent``), so it is never asked again;
* "Leave it empty" settles a question without an answer: the batch completes, the answer is
  never saved, and the run flags it at Ready to send;
* an answer already given is handed to the shell at once (``partial``), not held up by a sibling.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply import consents as CS

# The apply agent is off by default (assisted apply only); these tests cover it, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
URL = "https://job-boards.greenhouse.io/acme/jobs/1"
SMS = ("By selecting Yes, you agree to receive mobile messages from us in relation to this job application. "
       "Message and data rates may apply.")
COVENANT = "Are you subject to any restrictive covenants, such as a non-compete agreement?"
COLOR = "Which office color scheme do you prefer?"
PARK = {"page_url": URL, "run": {"url": URL}, "batch": False}


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_active_channel", lambda: None)

    class Reader:                                  # the reply reader finds no answer in the text
        def ask_reply(self, system, user):
            return '{"answers": [], "question": "", "unclear": true}'
    monkeypatch.setattr(mod, "_ask_reader", lambda: Reader())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    rid = recs.add("Analyst", "Acme", 70, "", data={"status": "approved", "source_job": {"url": URL}})
    recs.close()
    return mod, rid


def _post(mod, path, body):
    return mod.app.test_client().post(path, json=body, headers=H).get_json()


def _ask(mod, rid, aid, questions):
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": questions, "record_id": rid, "id": aid, "batch": True,
                                                 "park": PARK, "chunk_s": 0, "telegram": False})
    assert r["reason"] == "waiting", r
    return r


def _answer(mod, aid, index, question, **body):
    return _post(mod, "/api/autoapply/agent/answer", {"id": aid, "index": index, "question": question, **body})


def _saved(mod, key):
    lib = mod._answer_lib()
    try:
        row = lib.best(key)
        return (row or {}).get("answer")
    finally:
        lib.close()


# --------------------------------------------------------------------------- the rules, pure
def test_a_closed_choice_names_its_options_and_reads_only_exact_words():
    it = {"question": COLOR, "options": ["Red", "Blue"], "all_options": ["Red", "Blue"], "partial": False}
    assert AK.closed_choice(it) == ["Red", "Blue"]
    assert AK.closed_match(it, " blue. ") == "Blue" and AK.closed_match(it, "2") is None
    assert AK.closed_match(it, "Green") is None and AK.closed_match(it, "No") is None
    assert AK.closed_choice(dict(it, partial=True)) == []            # more choices than were read: not closed
    said = AK.offers_reply({"question": SMS, "all_options": ["Yes"]})
    assert said.startswith("This form only offers: Yes.") and "leave it empty" in said and "skip this job" in said
    assert "could not tell" not in said and "—" not in said


def test_a_required_consent_with_only_yes_is_forced_and_a_saved_no_declines_it():
    assert CS.is_forced(SMS, ["Yes"]) is True
    assert CS.is_forced(SMS, ["Yes", "No"]) is False                 # it can be declined: not forced
    assert CS.is_forced(COVENANT, ["Yes"]) is False                  # a fact, never a consent
    assert CS.is_forced("I certify the information is true", ["Yes"]) is False

    class Lib:
        def __init__(self, v):
            self.v = v

        def best(self, key):
            return {"answer": self.v} if self.v and key == CS.FORCED_KEY else None
    assert CS.saved_forced_decline(Lib("No")) is True and CS.saved_forced(Lib("No")) is False
    assert CS.saved_forced_decline(Lib("Yes")) is False and CS.saved_forced(Lib("Yes")) is True
    assert CS.saved_forced_decline(Lib("")) is False and CS.saved_forced_decline(None) is False


# --------------------------------------------------------------------------- the owner's case
def test_no_to_an_only_yes_consent_is_left_empty_and_the_answered_sibling_is_not_held_up(A):
    mod, rid = A
    _ask(mod, rid, "b001", [{"question": SMS, "options": ["Yes"], "all_options": ["Yes"]},
                            {"question": COVENANT, "options": ["Yes", "No"], "all_options": ["Yes", "No"]}])
    # the second question is answered first: it reaches the shell at once, while the first is open
    r = _answer(mod, "b001", 1, COVENANT, answer="No", exact=True)
    assert r["ok"] and r["done"] is False
    w = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "b001", "chunk_s": 5})
    assert w["reason"] == "waiting" and w["partial"] == {COVENANT: "No"}
    # "No" typed on the consent whose only choice is Yes: a decision, never "I still could not tell"
    r = _answer(mod, "b001", 0, SMS, answer="No")
    assert r["ok"] and r["done"] is True
    assert r["reply"].startswith("Left empty.") and "flag it at Ready to send" in r["reply"]
    assert "will not ask about a required consent like this again" in r["reply"]
    assert "could not tell" not in r["reply"]
    assert [(x["n"], x["answer"], x["left"]) for x in r["items"]] == [(1, "Left empty", True), (2, "No", False)]
    done = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "b001", "chunk_s": 1})
    assert done["ok"] is True and done["answers"] == {COVENANT: "No"} and done["left"] == [SMS]
    # the standing No is saved; "Left empty" is never saved as an answer to anything
    assert _saved(mod, CS.FORCED_KEY) == "No"
    lib = mod._answer_lib()
    try:
        hit = lib.lookup(SMS)
    finally:
        lib.close()
    assert not hit or hit.get("answer") != "Left empty"
    # ... so the same kind of question is never asked again: the run is told to leave it empty
    other = "I agree to receive text messages about my application. Message and data rates may apply."
    k = _post(mod, "/api/autoapply/agent/ask", {"questions": [{"question": other, "options": ["Yes"], "all_options": ["Yes"]}],
                                                 "record_id": rid, "known_only": True, "fill_through": True})
    assert k["leave"] == [other] and k["open"] == [] and k["answers"] == {} and k["ok"] is True


def test_without_a_saved_no_the_consent_is_still_asked(A):
    mod, rid = A
    k = _post(mod, "/api/autoapply/agent/ask", {"questions": [{"question": SMS, "options": ["Yes"], "all_options": ["Yes"]}],
                                                 "record_id": rid, "known_only": True, "fill_through": True})
    assert k["leave"] == [] and k["open"] == [SMS]


def test_a_typed_reply_that_is_none_of_the_choices_gets_the_offer_and_the_exits_never_a_loop(A):
    mod, rid = A
    _ask(mod, rid, "b002", [{"question": COLOR, "options": ["Red", "Blue"], "all_options": ["Red", "Blue"]},
                            {"question": "What is your badge number?"}])
    for _ in range(4):                                               # typing it again never changes the answer
        r = _answer(mod, "b002", 0, COLOR, answer="Green")
        assert r["ok"] and r["done"] is False
        assert r["reply"].startswith("This form only offers: Red, Blue.")
        assert "could not tell" not in r["reply"]
        assert r["exits"] == {"index": 0, "n": 1, "question": COLOR, "options": ["Red", "Blue"], "leave": True, "skip": True}
        assert r["items"][0]["answer"] is None
    assert _saved(mod, CS.FORCED_KEY) is None                        # not a consent: no standing answer
    # the exits work: a choice by its words ...
    r = _answer(mod, "b002", 0, COLOR, answer="blue")
    assert r["done"] is False and r["reply"].startswith("Got it: 1. Blue.") and r["items"][0]["answer"] == "Blue"
    # ... or "Leave it empty", which also settles a free-text question; then the batch completes
    r = _answer(mod, "b002", 1, "What is your badge number?", action="leave")
    assert r["done"] is True and r["reply"].startswith("Left empty.")
    assert "required consent" not in r["reply"]
    done = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "b002", "chunk_s": 1})
    assert done["ok"] is True and done["answers"] == {COLOR: "Blue"} and done["left"] == ["What is your badge number?"]


def test_leaving_the_only_question_empty_completes_the_ask_with_no_answer(A):
    mod, rid = A
    _ask(mod, rid, "b003", [{"question": COLOR, "options": ["Red", "Blue"], "all_options": ["Red", "Blue"]}])
    r = _answer(mod, "b003", 0, COLOR, action="leave")
    assert r["ok"] and r["done"] is True
    done = _post(mod, "/api/autoapply/agent/ask/wait", {"id": "b003", "chunk_s": 1})
    assert done["ok"] is True and done["answers"] == {} and done["answer"] == "" and done["left"] == [COLOR]
    # a changed mind before the batch closes: an answer replaces the "left empty"
    _ask(mod, rid, "b004", [{"question": COLOR, "options": ["Red", "Blue"], "all_options": ["Red", "Blue"]},
                            {"question": COVENANT, "options": ["Yes", "No"], "all_options": ["Yes", "No"]}])
    _answer(mod, "b004", 0, COLOR, action="leave")
    r = _answer(mod, "b004", 0, COLOR, answer="Red", exact=True)
    assert [(x["answer"], x["left"]) for x in r["items"]] == [("Red", False), (None, False)]


def test_a_free_text_reply_that_settles_nothing_still_comes_with_the_ways_out(A, monkeypatch):
    mod, rid = A
    monkeypatch.setattr(mod, "_ask_reader", lambda: None)            # the plain reading, no model
    _ask(mod, rid, "b005", [{"question": "What is your badge number?"}, {"question": "What is your locker number?"}])
    r = _answer(mod, "b005", 0, "What is your badge number?", answer="what do you mean?")
    assert r["done"] is False and r["exits"]["leave"] is True and r["exits"]["skip"] is True
    for _ in range(3):
        r = _answer(mod, "b005", 0, "What is your badge number?", answer="what do you mean?")
        assert "I still could not tell" not in (r["reply"] or "")
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert "I still could not tell. Type just the answer" not in src
    assert "I still could not tell" not in (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the shell and the bar
def test_the_guided_ask_outlines_each_field_takes_the_pages_value_and_never_asks_again():
    """shell-electron/agent.js ``guide`` with a fake pane: scroll and outline per open field in
    order; a value on the page advances and is handed over; Next passes; a Telegram answer is
    filled into the page in Hybrid; nothing is asked in Auto; Stop clears the outline."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_guide.js")], capture_output=True,
                         text=True, timeout=240)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-3000:] + out.stderr[-3000:]


def test_a_yes_to_a_forced_consent_is_the_standing_answer_and_next_decides_nothing(A):
    mod, rid = A
    one = [{"question": SMS, "options": ["Yes"], "all_options": ["Yes"]}]
    # Next on the bar (the shell's "leave", standing false): left empty, nothing saved for other forms
    _ask(mod, rid, "b006", one)
    r = _answer(mod, "b006", 0, SMS, action="leave", standing=False)
    assert r["done"] is True and "will not ask" not in r["reply"]
    assert _saved(mod, CS.FORCED_KEY) is None
    # Yes picked on the page (the shell hands it over as exact): the standing Yes, so the next
    # form's forced consent is accepted without asking
    _ask(mod, rid, "b007", one)
    r = _answer(mod, "b007", 0, SMS, answer="Yes", exact=True, on_page=True)
    assert r["done"] is True and _saved(mod, CS.FORCED_KEY) == "Yes"
    k = _post(mod, "/api/autoapply/agent/ask", {"questions": one, "record_id": rid, "known_only": True, "fill_through": True})
    assert k["answers"] == {SMS: "Yes"} and k["leave"] == [] and k["open"] == []


def test_a_telegram_reply_to_a_closed_choice_never_loops(A):
    mod, rid = A
    _ask(mod, rid, "b008", [{"question": SMS, "options": ["Yes"], "all_options": ["Yes"]}])
    for _ in range(3):
        reply, done = mod._ask_take_reply("b008", "maybe later")
        assert done is False and reply.startswith("This form only offers: Yes.")
    reply, done = mod._ask_take_reply("b008", "No")
    assert done is True and reply.startswith("Left empty.") and _saved(mod, CS.FORCED_KEY) == "No"
    _ask(mod, rid, "b009", [{"question": COVENANT, "options": ["Yes", "No"], "all_options": ["Yes", "No"]}])
    reply, done = mod._ask_take_reply("b009", "no")
    assert done is True and reply.startswith("Got it: No")


def test_the_bar_is_one_slim_line_while_the_person_answers_on_the_page():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    g = js[js.index('if (info.state === "guide" && info.guide) {'):js.index('} else if ((info.state === "ask" || info.state === "parked") && info.ask) {')]
    assert 'T("aa.guide.many", "{n} fields need you.")' in g and 'T("aa.guide.of", "{i} of {n}")' in g
    assert 'T("aa.guide.next", "Next")' in g and 'T("aa.agent.skipJob", "Skip this job")' in g
    assert "agentGuideNext()" in g and "agentStop()" in g
    assert "createElement(\"input\")" not in g and "bb-batch" not in g      # no answer box, no question list
    pre = (ROOT / "shell-electron" / "preload.js").read_text(encoding="utf-8")
    assert 'agentGuideNext: () => ipcRenderer.invoke("agent:guide-next")' in pre
    assert 'ipcMain.handle("agent:guide-next", async () => agent.guideNext());' in (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    sh = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    body = sh[sh.index("const guide = async (wc, step, open, body) => {"):sh.index("    // What a re-run brings in:")]
    assert 'progress(recordId, "guide"' in body and 'progress(recordId, "ask"' not in sh   # the list is gone from live runs
    import json
    for code in ("en", "de", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in ("aa.guide.one", "aa.guide.many", "aa.guide.of", "aa.guide.next"):
            assert loc.get(k), f"{code}: {k}"


def test_the_bar_shows_no_text_box_for_a_closed_choice_and_always_offers_the_exits():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const closedQ = (x) => (x.options || []).length > 0 && !x.partial" in js
    assert js.count("if (!closedQ(x)) {") == 1 and "if (!closedQ(q) || termsAsk) {" in js
    assert js.count('T("aa.agent.leave", "Leave it empty")') == 2      # the list and the single question
    assert 'T("aa.agent.skipJob", "Skip this job")' in js and '{ action: "leave" }' in js
    assert "/api/autoapply/agent/ask/close" in js[js.index("const skipJob = async"):js.index("const canSkip")]
    import json
    for code in ("en", "de", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in ("aa.agent.leave", "aa.agent.skipJob", "aa.agent.left", "aa.ans.flag.needsYou"):
            assert loc.get(k) and "—" not in loc[k] and "–" not in loc[k], f"{code}: {k}"
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert en["aa.ans.flag.needsYou"].startswith("Needs you: the form will not submit without it.")
