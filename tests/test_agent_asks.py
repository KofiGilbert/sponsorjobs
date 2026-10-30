"""The apply agent's "ask the person" flow (docs/apply-agent.md): the reply is READ, not saved raw;
a question back gets the options; EEO questions offer the decline choice first (or take it from
the standing preference); several open questions go out as one numbered message; and what the
person's material already answers is never asked. Live failure this pins (Lam Research,
2026-10-30): the whole reply "Yes, I will need H1B / Male / Don't you have this?" was saved and
typed as the Gender answer, and "What are the options under ethnicity ?" was saved as Ethnicity.

The FakeLLM reply reader is a test double: the tests that use it check the plumbing. The rules
that protect the person (what is accepted, what is saved) are checked with stub models.
"""
from __future__ import annotations

import importlib
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply.answers import AnswerLibrary, normalize
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known
from llm.base import FakeLLM

H = {"X-Tailor-Extension": "1"}
LAM_REPLY = "Yes, I will need H1B\n\nMale\n\nDon't you have this information on my profile already ?"
LAM_CITIZENSHIP = ("For the sole purpose of determining export licensing requirements, please provide your most "
                   "recent country/region of citizenship or legal permanent residence (if you are a citizen of "
                   "more than one, list each)")
GENDER_OPTS = ["Male", "Female", "I don't wish to answer"]
ETH_OPTS = ["Asian", "Black or African American", "White", "Hispanic or Latino", "I don't wish to answer"]
# Workday's US self-identification choices (long, over Telegram's 30-character button labels).
WD_ETHNICITY = ["American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)",
                "Asian (Not Hispanic or Latino) (United States of America)",
                "Black or African American (Not Hispanic or Latino) (United States of America)",
                "Hispanic or Latino (United States of America)",
                "Native Hawaiian or Other Pacific Islander (Not Hispanic or Latino) (United States of America)",
                "Two or More Races (Not Hispanic or Latino) (United States of America)",
                "White (Not Hispanic or Latino) (United States of America)",
                "I do not wish to answer"]
GH_VETERAN = ["I am not a protected veteran",
              "I identify as one or more of the classifications of protected veteran",
              "I don't wish to answer"]
CC305 = ["Yes, I have a disability, or have had one in the past",
         "No, I do not have a disability and have not had one in the past",
         "I do not want to answer"]


class Lib:
    """A stand-in answer library: {normalized key: answer}."""

    def __init__(self, d):
        self.d = d

    def lookup(self, q):
        k = normalize(q)
        return {"key": k, "answer": self.d[k]} if k in self.d else None


class Model:
    """A stub reply reader that answers with a fixed raw text."""

    def __init__(self, raw):
        self.raw = raw

    def ask_reply(self, s, u):
        return self.raw


# --------------------------------------------------------------------------- A: reading a reply
def test_a_multi_part_reply_keeps_only_the_part_that_answers():
    got = AK.read_reply(FakeLLM(), [{"question": "Gender", "options": GENDER_OPTS}], LAM_REPLY)
    assert got["answers"] == {0: "Male"} and not got["unclear"]
    got = AK.read_reply(FakeLLM(), [{"question": "Gender", "options": []}], LAM_REPLY)
    assert got["answers"] == {0: "Male"}
    # the order of the parts does not decide it (data-10)
    rev = "Male\n\nYes, I will need H1B"
    assert AK.read_reply(FakeLLM(), [{"question": "Gender", "options": GENDER_OPTS}], rev)["answers"] == {0: "Male"}
    assert AK.read_reply(FakeLLM(), [{"question": "Gender", "options": []}], rev)["answers"] == {0: "Male"}


def test_a_a_question_back_is_not_an_answer():
    got = AK.read_reply(FakeLLM(), [{"question": "Ethnicity", "options": ETH_OPTS}],
                        "What are the options under ethnicity ?")
    assert got["answers"] == {} and got["question"].startswith("What are the options") and not got["unclear"]
    msg = AK.options_reply([{"question": "Ethnicity", "options": ETH_OPTS}])
    assert "Asian" in msg and "I don't wish to answer" in msg
    msg = AK.options_reply([{"question": "Ethnicity", "options": []}])
    assert "cannot read the choices" in msg and "Prefer not to say" in msg


def test_a_a_failed_or_stalled_model_falls_back_to_the_plain_reading():
    """engine-2: a model error is not the person's unclear reply. The plain reading takes over
    (a choice's words or number, a short line for one free-text question), never a guess."""
    class Broken:
        def ask_reply(self, s, u):
            raise RuntimeError("down")

    class Stalled:
        def ask_reply(self, s, u):
            time.sleep(3)
            return '{"answers": [{"n": 1, "answer": "Male"}]}'

    one = [{"question": "Gender", "options": GENDER_OPTS}]
    assert AK.read_reply(Broken(), one, "Male")["answers"] == {0: "Male"}            # no model needed
    assert AK.read_reply(Broken(), one, "2")["answers"] == {0: "Female"}             # its number
    got = AK.read_reply(Broken(), one, "the second one I suppose")
    assert got["answers"] == {} and got["unclear"] and got["error"]
    got = AK.read_reply(Broken(), [{"question": "Years of experience?", "options": []}], "5")
    assert got["answers"] == {0: "5"} and got["error"]
    t0 = time.time()
    got = AK.read_reply(Stalled(), [{"question": "Years of experience?", "options": []}], "about 5", timeout=0.3)
    assert time.time() - t0 < 2 and got["error"] and got["answers"] == {0: "about 5"}
    assert AK.read_reply(FakeLLM(), one, "   ")["unclear"]


def test_a_the_model_answer_is_validated():
    q = [{"question": "Gender", "options": GENDER_OPTS}]
    # a question is never an answer; an unknown number is dropped; an option is snapped
    assert AK.read_reply(Model('{"answers":[{"n":1,"answer":"what?"}]}'), q, "x")["answers"] == {}
    assert AK.read_reply(Model('{"answers":[{"n":4,"answer":"Male"}]}'), q, "x")["answers"] == {}
    assert AK.read_reply(Model('```json\n{"answers":[{"n":1,"answer":"male"}]}\n```'), q, "x")["answers"] == {0: "Male"}
    # data-10: the whole Lam reply as "the answer" is refused, with and without the page's choices
    lam = Model('{"answers":[{"n":1,"answer":"Yes, I will need H1B. Male"}]}')
    assert AK.read_reply(lam, q, LAM_REPLY)["answers"] == {}
    assert AK.read_reply(lam, [{"question": "Gender", "options": []}], LAM_REPLY)["answers"] == {}
    # engine-3: a complete list takes only its own choices ("Asian" is no veteran status)
    vet = [{"question": "Veteran Status", "options": GH_VETERAN}]
    assert AK.read_reply(Model('{"answers":[{"n":1,"answer":"Asian"}]}'), vet, "x")["answers"] == {}
    # engine-4: a partly read list takes the person's own short words
    cit = [{"question": "Country of citizenship", "options": ["Afghanistan", "Albania"], "partial": True}]
    assert AK.read_reply(Model('{"answers":[{"n":1,"answer":"Ghana"}]}'), cit, "Ghana")["answers"] == {0: "Ghana"}
    assert AK.read_reply(FakeLLM(), cit, "Ghana")["answers"] == {0: "Ghana"}


def test_a_numbers_are_the_ones_the_person_saw():
    """engine-3: after "1. Yes", the two still open keep numbers 2 and 3 for the reader."""
    items = [{"n": 2, "question": "Are you willing to relocate?", "options": ["Yes", "No"]},
             {"n": 3, "question": "Are you willing to travel up to 50%?", "options": ["Yes", "No"]}]
    got = AK.read_reply(FakeLLM(), items, "2. Yes\n3. No")
    assert got["answers"] == {0: "Yes", 1: "No"}
    assert '"n": 2' in AK.build_user(items, "x") and '"n": 3' in AK.build_user(items, "x")
    got = AK.read_reply(Model('{"answers":[{"n":1,"answer":"Yes"},{"n":3,"answer":"No"}]}'), items, "x")
    assert got["answers"] == {1: "No"}                    # there is no question 1 open any more


# --------------------------------------------------------------------------- the app routes
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    sent = []

    class Chan:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Chan())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    return mod, sent


def _ask_in_thread(mod, body):
    out = {}

    def run():
        out["r"] = mod.app.test_client().post("/api/autoapply/agent/ask", json=body, headers=H).get_json()
    th = threading.Thread(target=run)
    th.start()
    for _ in range(50):                            # wait until the question is registered and sent
        with mod._ASKS_LOCK:
            if any(a.get("open", True) and a.get("sent") is not False for a in mod._ASKS.values()):
                break
        time.sleep(0.1)
    return th, out


def _lib_answer(mod, q):
    lib = mod._answer_lib()
    try:
        hit = lib.lookup(q)
        return hit and hit["answer"]
    finally:
        lib.close()


def test_a_telegram_reply_is_read_and_only_the_answer_saved(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "g1", "question": "Gender", "options": [], "timeout_s": 15})
    assert mod._agent_ask_event("text", LAM_REPLY) == "Got it: Male."
    th.join(20)
    assert out["r"]["ok"] and out["r"]["answer"] == "Male", out
    assert _lib_answer(mod, "What is your gender?") == "Male"


def test_a_a_question_back_gets_the_options_and_the_run_keeps_waiting(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "e1", "question": "Ethnicity", "options": ETH_OPTS, "timeout_s": 15})
    reply = mod._agent_ask_event("text", "What are the options under ethnicity ?")
    assert "Asian" in reply and "Got it" not in reply
    assert th.is_alive()                           # still waiting for the real answer
    assert _lib_answer(mod, "Ethnicity") is None
    assert mod._agent_ask_event("text", "Asian") == "Got it: Asian."
    th.join(20)
    assert out["r"]["answer"] == "Asian" and _lib_answer(mod, "What is your ethnicity?") == "Asian"


def test_a_bar_free_text_is_read_too_and_an_unclear_reply_asks_again(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "b1", "question": "Gender", "options": GENDER_OPTS, "timeout_s": 15})
    c = mod.app.test_client()
    d = c.post("/api/autoapply/agent/answer", json={"id": "b1", "answer": "hmm ok"}, headers=H).get_json()
    assert d["ok"] and d["done"] is False and "could not tell" in d["reply"]
    d = c.post("/api/autoapply/agent/answer", json={"id": "b1", "answer": "what are the choices?"}, headers=H).get_json()
    assert d["done"] is False and "Female" in d["reply"]
    d = c.post("/api/autoapply/agent/answer", json={"id": "b1", "answer": "I don't wish to answer", "exact": True}, headers=H).get_json()
    assert d["done"] is True and d["reply"] == "Got it: I don't wish to answer."
    th.join(20)
    assert out["r"]["answer"] == "I don't wish to answer"


def test_a_a_model_failure_is_not_counted_as_unclear(A, monkeypatch):
    """engine-2: while the reader is down a typed choice still answers, and an unreadable reply
    gets "could not read that just now" without using up the one re-ask."""
    mod, sent = A

    class Broken:
        def ask_reply(self, s, u):
            raise RuntimeError("overloaded")
    monkeypatch.setattr(mod, "_make_llm", lambda: Broken())
    th, out = _ask_in_thread(mod, {"id": "m1", "question": "Gender", "options": GENDER_OPTS, "timeout_s": 15})
    assert "could not read that just now" in mod._agent_ask_event("text", "the usual")
    assert "could not read that just now" in mod._agent_ask_event("text", "the usual please")
    with mod._ASKS_LOCK:
        assert mod._ASKS["m1"]["unclear"] == 0
    assert mod._agent_ask_event("text", "female") == "Got it: Female."
    th.join(20)
    assert out["r"]["answer"] == "Female"


def test_a_a_timed_out_question_no_longer_captures_telegram_text(A):
    mod, sent = A
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"question": "Favourite colour?", "timeout_s": 5},
                                   headers=H).get_json()
    assert r["reason"] == "timeout"
    assert mod._agent_ask_event("text", "hello there") is None


def test_a_a_command_is_never_an_answer_and_stop_ends_the_wait(A):
    """engine-5: /stop goes on to the command router (None) and ends the wait; the run stops."""
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "s1", "question": "Gender", "options": GENDER_OPTS, "timeout_s": 15,
                                   "record_id": 7})
    assert mod._agent_ask_event("text", "/skip 99") is None            # another application: still open
    assert mod._agent_ask_event("text", "/list") is None
    with mod._ASKS_LOCK:
        assert mod._ASKS["s1"]["open"] is True
    t0 = time.time()
    assert mod._agent_ask_event("text", "/stop") is None
    th.join(20)
    assert out["r"] == {"ok": False, "id": "s1", "reason": "stopped"} and time.time() - t0 < 5
    assert _lib_answer(mod, "Gender") is None
    assert mod._agent_ask_event("text", "Male") is None                # nothing open any more


def test_a_text_is_taken_only_by_a_question_telegram_received(A, monkeypatch):
    mod, sent = A

    class Down:
        def send(self, text, buttons=None):
            raise RuntimeError("telegram down")
    monkeypatch.setattr(mod, "_active_channel", lambda: Down())
    out = {}
    th = threading.Thread(target=lambda: out.update(r=mod.app.test_client().post(
        "/api/autoapply/agent/ask", json={"id": "u1", "question": "Years of experience?", "timeout_s": 15},
        headers=H).get_json()))
    th.start()
    for _ in range(50):
        with mod._ASKS_LOCK:
            if "u1" in mod._ASKS:
                break
        time.sleep(0.1)
    time.sleep(0.5)                                # the failed Telegram send is over by now
    with mod._ASKS_LOCK:
        assert mod._ASKS["u1"]["sent"] is False
    assert mod._agent_ask_event("text", "can you tailor my resume for Stripe") is None
    d = mod.app.test_client().post("/api/autoapply/agent/answer", json={"id": "u1", "answer": "5"}, headers=H).get_json()
    assert d["done"] is True                       # the bar still answers it
    th.join(20)
    assert out["r"]["answer"] == "5"


# --------------------------------------------------------------------------- B: options as buttons
def test_b_a_single_question_with_options_goes_out_with_buttons(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "o1", "question": "Gender", "options": GENDER_OPTS, "timeout_s": 15})
    text, buttons = sent[-1]
    assert "Gender" in text and "Tap a choice" in text
    labels = [row[0][0] for row in buttons]
    assert labels[0] == "I don't wish to answer" and set(labels) == set(GENDER_OPTS)   # C: decline first
    tap = next(row[0][1] for row in buttons if row[0][0] == "Male")
    assert mod._agent_ask_event("button", tap) == "Got it: Male."
    th.join(20)
    assert out["r"]["answer"] == "Male"


def _broker_check(text, buttons):
    from backend.telegram_relay import validate_buttons, validate_send
    from notify.channel import _as_rows
    rows = _as_rows(buttons)
    validate_send({"text": text, "buttons": rows})       # raises on anything the broker refuses
    return validate_buttons(rows)


@pytest.mark.parametrize("question,options", [
    ("Ethnicity", WD_ETHNICITY),                         # 8 choices, labels up to 93 characters
    ("Veteran Status", GH_VETERAN),                      # 3 choices, one of 69 characters
    ("Disability Status", CC305),
    ("Gender", ["Male", "Female", "Non-binary", "I don't wish to answer"]),
])
def test_b_buttons_fit_the_broker_contract(A, question, options):
    """shell-1 / engine-1: the broker takes at most 3 rows of 1 to 3 buttons with labels of 30
    characters. The buttons fit, the full choices are in the text, and a cut label's button
    still picks the full choice."""
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "bt", "question": question, "options": options, "timeout_s": 15})
    text, buttons = sent[-1]
    rows = _broker_check(text, buttons)
    assert sum(len(r) for r in rows) == len(options)
    if any(len(o) > 30 for o in options) or len(options) > 3:
        for o in options:
            assert o in text                       # the full choices, numbered in the message
    longest = max(options, key=len)
    pick = next(b for row in rows for b in row if b["label"].endswith("…") or longest.startswith(b["label"].split(". ", 1)[-1]))
    with mod._ASKS_LOCK:
        want = mod._ASKS["bt"]["options"][int(pick["id"].rsplit(":", 1)[1])]
    assert mod._agent_ask_event("button", pick["id"]) == f"Got it: {want}."
    th.join(20)
    assert out["r"]["answer"] == want and want in options


def test_b_a_refused_button_message_is_sent_again_without_buttons(A, monkeypatch):
    mod, sent = A
    from notify.channel import ChannelError

    class Strict:
        def send(self, text, buttons=None):
            if buttons:
                raise ChannelError("http_400", "buttons must be at most 3 rows")
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Strict())
    th, out = _ask_in_thread(mod, {"id": "nb", "question": "Ethnicity", "options": WD_ETHNICITY, "timeout_s": 15})
    text, buttons = sent[-1]
    assert buttons is None and "1. I do not wish to answer" in text and "number of your choice" in text
    assert mod._agent_ask_event("text", "2") == f"Got it: {WD_ETHNICITY[0]}."   # 2 = the second listed
    th.join(20)
    assert out["r"]["answer"] == WD_ETHNICITY[0]


def test_b_options_are_cleaned_capped_and_decline_first():
    many = ["Select...", "A", "B", "C", "D", "E", "F", "G", "H", "I", "Decline to self-identify"]
    got = AK.order_options("Race", many)
    assert got[0] == "Decline to self-identify" and len(got) == 8 and "Select..." not in got
    assert AK.order_options("Favourite colour", ["Red", "Prefer not to say"]) == ["Red", "Prefer not to say"]
    # shell-5 / page-1: a decline worded "Choose not to ..." is a choice, not a prompt
    assert AK.order_options("Gender", ["Select One", "Female", "Male", "Choose not to disclose"]) == \
        ["Choose not to disclose", "Female", "Male"]
    assert AK.clean_options(["-- Please Select --", "Choose not to self-identify", "Search Engine (Google, Bing)",
                             "Please contact me", "Choose your country"]) == \
        ["Choose not to self-identify", "Search Engine (Google, Bing)", "Please contact me"]
    # engine-6: the federal disability form's decline comes first
    assert AK.order_options("Disability Status", CC305)[0] == "I do not want to answer"


def test_b_node_shell_helpers_match():
    """The shell's orderOptions, its question list and label matching (shell-4, shell-5)."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    root = Path(__file__).resolve().parents[1]
    js = r"""
const A = require(%r);
const eq = (a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify(a) + " != " + JSON.stringify(b)); };
eq(A.orderOptions('Gender', ['Select one','Male','Female','Prefer not to say']), ['Prefer not to say','Male','Female']);
eq(A.orderOptions('Department', ['Sales','Prefer not to say']), ['Sales','Prefer not to say']);
eq(A.orderOptions('Gender', ['Select One','Female','Male','Choose not to disclose']), ['Choose not to disclose','Female','Male']);
eq(A.orderOptions('Disability Status', %s)[0], 'I do not want to answer');
const long = %r;
const view = '[3]<combobox label="' + long.slice(0, 80) + '" value="" expanded=false required>\n'
  + '[4]<button>Error-' + long.slice(0, 74) + '</button>\n'
  + '[5]<combobox label="Gender" value="Choose not to disclose" expanded=false required>\n'
  + '[7]<select label="Race" value="" options=9 first="Select ...|American Indian or Alaskan Native|Asian|Black or African American" required>';
eq(A.questionsFor(view, [long], false), [long]);           // the 74-character Error- stub folds in
eq(A.indexFor(view, long), 3);                              // the view's 80-character label is found
eq(A.questionsFor(view, [long, 'Gender'], false), [long]);  // a chosen decline is an answer
eq(A.optionsFor(view, 'Race'), { options: ['American Indian or Alaskan Native','Asian','Black or African American'], partial: true });
console.log('ok');
""" % (str(root / "shell-electron" / "agent.js"), str(CC305).replace("'", '"'), LAM_CITIZENSHIP)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_b_messages_stay_under_telegrams_limit():
    """engine-8: six long questions with eight long choices each still fit one message."""
    from backend.telegram_relay import validate_send
    opt = ["Choice number %d with a long explanation of what it means for this application form" % i
           for i in range(8)]
    items = [{"n": i, "question": ("Question %d " % i) + "x" * 280, "options": opt, "all_options": opt}
             for i in range(1, 7)]
    for text in (AK.prompt_text(items), AK.options_reply(items), AK.unclear_reply(items),
                 AK.prompt_text(items[:1], list_choices=True, buttons=False)):
        assert len(text) <= 4096
        validate_send({"text": text})


# --------------------------------------------------------------------------- C: EEO questions
def test_c_eeo_decline_pref_picks_the_decline_choice_without_asking(A, monkeypatch):
    mod, sent = A
    monkeypatch.setattr(mod, "_load_prefs", lambda: {"eeo_decline": True})
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
        "question": "Veteran Status", "options": ["I am a protected veteran", "I am not a protected veteran",
                                                  "I don't wish to answer"], "timeout_s": 5}, headers=H).get_json()
    assert r["ok"] and r["known"] and r["answer"] == "I don't wish to answer"
    assert sent == []                              # nothing went to Telegram
    assert _lib_answer(mod, "Veteran Status") is None   # a standing preference, not a saved answer
    # engine-6: the CC-305 "I do not want to answer" is the decline too
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
        "question": "Disability Status", "options": CC305, "timeout_s": 5}, headers=H).get_json()
    assert r["ok"] and r["answer"] == "I do not want to answer" and sent == []


def test_c_known_answer_order_library_then_decline_never_a_guess():
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({"@gender": "Male"})) == ("Male", "library")
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({}), eeo_decline=True) == ("I don't wish to answer", "eeo_decline")
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({}), eeo_decline=False) is None
    assert AK.known_answer("Gender", ["Male", "Female"], library=Lib({}), eeo_decline=True) is None
    # data-2: a saved "Yes" is never read as a protected-veteran self-identification
    assert AK.known_answer("Veteran Status", GH_VETERAN, library=Lib({"@veteran": "Yes"})) is None
    # data-9: a saved decline fits the next site's decline wording
    lever = ["Male", "Female", "Decline to self-identify"]
    assert AK.known_answer("Gender", lever, library=Lib({"@gender": "I don't wish to answer"})) == \
        ("Decline to self-identify", "library")
    # engine-6: CC-305 with the decline preference
    assert AK.known_answer("Disability Status", CC305, library=Lib({}), eeo_decline=True) == \
        ("I do not want to answer", "eeo_decline")


def test_c_the_planner_uses_a_saved_eeo_answer(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("Gender", "Male", "you")
        f = FormField(ref="g", label="What is your gender?", type="select", options=["Male", "Female"])
        res = plan_known([f], KnownContext(library=lib))
        assert [(a.ref, a.value, a.origin) for a in res.actions] == [("g", "Male", "library")]
    finally:
        lib.close()


def test_c_an_eeo_checkbox_group_ticks_only_the_saved_choice(tmp_path):
    """data-1: each box of a native group is labelled "Question: Option"; one saved answer ticks
    its own box (or the decline box for a saved decline), never every box by polarity."""
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("Race", "Asian", "you")
        lib.save("Disability Status", CC305[0], "you")
        lib.save("Gender", "I don't wish to answer", "you")
        boxes = [FormField(f"r{i}", "Race: " + o, "checkbox") for i, o in
                 enumerate(["Asian", "White", "Black or African American", "I do not wish to self-identify"])]
        boxes += [FormField(f"d{i}", "Disability Status: " + o, "checkbox") for i, o in enumerate(CC305)]
        boxes += [FormField(f"g{i}", "Gender: " + o, "checkbox") for i, o in enumerate(["Male", "Female", "Decline"])]
        res = plan_known(boxes, KnownContext(library=lib))
        assert sorted((a.ref, a.value) for a in res.actions) == [("d0", "Yes"), ("g2", "Yes"), ("r0", "Yes")]
        assert normalize("Race: Asian") != "@race"
    finally:
        lib.close()


def test_c_the_planner_never_maps_an_eeo_answer_by_yes_no_or_prefix(tmp_path):
    """data-2, data-3, data-9: exact choice, or decline for decline, nothing else."""
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("Are you a veteran?", "Yes", "you")              # any service: its own key
        lib.save("Disability Status", "Yes, I have a disability (or previously had a disability)", "you")
        lib.save("Gender", "I don't wish to answer", "you")
        fields = [FormField("v", "Veteran Status", "select", GH_VETERAN),
                  FormField("a", "Do you have a disability that would require an accommodation during the "
                                 "interview process?", "radio", ["Yes", "No"]),
                  FormField("g", "Gender", "select", ["Male", "Female", "Decline to self-identify"])]
        res = plan_known(fields, KnownContext(library=lib))
        assert [(a.ref, a.value) for a in res.actions] == [("g", "Decline to self-identify")]
        assert {f.ref for f in res.left} == {"v", "a"}
    finally:
        lib.close()


# --------------------------------------------------------------------------- D: one message
def test_d_several_questions_go_out_as_one_numbered_message(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "d1", "timeout_s": 15, "questions": [
        {"question": "Gender", "options": GENDER_OPTS}, {"question": "Ethnicity", "options": ETH_OPTS}]})
    assert len(sent) == 1
    text, buttons = sent[0]
    assert buttons is None and "1. Gender" in text and "2. Ethnicity" in text
    assert mod._agent_ask_event("text", "1. Male\n2. Asian") == "Got it: 1. Male; 2. Asian."
    th.join(20)
    assert out["r"]["answers"] == {"Gender": "Male", "Ethnicity": "Asian"}
    assert _lib_answer(mod, "Gender") == "Male" and _lib_answer(mod, "Ethnicity") == "Asian"


def test_d_a_partial_reply_keeps_waiting_for_the_rest(A):
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "d2", "timeout_s": 15, "questions": [
        {"question": "Gender", "options": GENDER_OPTS}, {"question": "Ethnicity", "options": ETH_OPTS}]})
    reply = mod._agent_ask_event("text", "Male")
    assert reply == "Got it: 1. Male. Still open: 2. Ethnicity." and th.is_alive()
    assert mod._agent_ask_event("text", "White") == "Got it: 1. Male; 2. White."
    th.join(20)
    assert out["r"]["ok"]


def test_d_numbered_answers_keep_their_numbers_after_a_partial_reply(A):
    """engine-3: "1. Yes", then "2. Yes / 3. No" lands on relocate and travel, as numbered."""
    mod, sent = A
    qs = ["Are you legally authorized to work in the United States?", "Are you willing to relocate?",
          "Are you willing to travel up to 50%?"]
    th, out = _ask_in_thread(mod, {"id": "d3", "timeout_s": 15, "force": True,
                                   "questions": [{"question": q, "options": ["Yes", "No"]} for q in qs]})
    reply = mod._agent_ask_event("text", "1. Yes")
    assert reply == f"Got it: 1. Yes. Still open: 2. {qs[1]}; 3. {qs[2]}."
    assert mod._agent_ask_event("text", "2. Yes\n3. No") == "Got it: 1. Yes; 2. Yes; 3. No."
    th.join(20)
    assert out["r"]["answers"] == {qs[0]: "Yes", qs[1]: "Yes", qs[2]: "No"}
    assert _lib_answer(mod, qs[2]) == "No"


def test_d_an_answer_with_a_question_back_also_gets_the_choices(A):
    """engine-7: "Male. What are the options under ethnicity?" answers 1 and lists 2's choices."""
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "d4", "timeout_s": 15, "questions": [
        {"question": "Gender", "options": GENDER_OPTS}, {"question": "Ethnicity", "options": ETH_OPTS}]})
    reply = mod._agent_ask_event("text", "Male. What are the options under ethnicity?")
    assert reply.startswith("Got it: 1. Male. Still open: 2. Ethnicity.") and "Black or African American" in reply
    assert mod._agent_ask_event("text", "2. Asian") == "Got it: 1. Male; 2. Asian."
    th.join(20)


def test_d_one_question_cut_short_is_asked_once(A):
    """shell-4 on the server: a Workday "Error-" stub of a long question is the same question."""
    mod, sent = A
    th, out = _ask_in_thread(mod, {"id": "d5", "timeout_s": 15, "force": True, "questions": [
        {"question": LAM_CITIZENSHIP[:74]}, {"question": LAM_CITIZENSHIP}, {"question": "Gender", "options": GENDER_OPTS}]})
    text = sent[-1][0]
    assert text.count("export licensing") == 1 and "2. Gender" in text and LAM_CITIZENSHIP in text
    assert mod._agent_ask_event("text", "1. Ghana\n2. Male") == "Got it: 1. Ghana; 2. Male."
    th.join(20)
    assert out["r"]["answers"] == {LAM_CITIZENSHIP: "Ghana", "Gender": "Male"}


# --------------------------------------------------------------------------- E: don't re-ask
@pytest.mark.parametrize("q,key", [
    (LAM_CITIZENSHIP, "@citizenship"), (LAM_CITIZENSHIP[:200], "@citizenship"),
    ("Country of citizenship", "@citizenship"), ("Nationality", "@citizenship"),
    ("Gender", "@gender"), ("What is your gender?", "@gender"), ("Gender identity", "@gender"),
    ("Gender (optional)", "@gender"), ("Please select your gender", "@gender"),
    ("Sex", "@sex"), ("Gender pronouns", "@pronouns"), ("What are your pronouns?", "@pronouns"),
    ("Ethnicity", "@ethnicity"), ("What is your ethnicity?", "@ethnicity"),
    ("Race", "@race"), ("Race/Ethnicity", "@race"), ("What is your race?", "@race"),
    ("Veteran Status", "@veteran"), ("Are you a protected veteran?", "@veteran"), ("Protected veteran status", "@veteran"),
    ("Disability Status", "@disability"), ("Do you have a disability?", "@disability"),
])
def test_e_canonical_keys(q, key):
    assert normalize(q) == key


@pytest.mark.parametrize("q,not_key", [
    ("Race: Asian", "@race"), ("Gender: Male", "@gender"),                                  # data-1
    ("Are you a veteran?", "@veteran"),                                                     # data-2
    ("Are you a veteran or active member of the United States Armed Forces?", "@veteran"),
    ("Do you have a disability that would require an accommodation during the interview process?", "@disability"),
    ("Disability accommodations", "@disability"),                                          # data-3
    ("What is your current citizenship status?", "@citizenship"),                          # data-4
    ("Citizenship Status (for export control purposes)", "@citizenship"),
    ("U.S. Citizenship (required for export control)", "@citizenship"), ("Citizenship", "@citizenship"),
    ("Gender pronouns", "@gender"), ("Sex", "@gender"), ("Race", "@ethnicity"),            # data-5
])
def test_e_different_questions_keep_different_keys(q, not_key):
    assert normalize(q) != not_key


def _old_db(path, rows):
    AnswerLibrary(path).close()
    con = sqlite3.connect(path)
    for key, q, a, at in rows:
        con.execute("INSERT INTO answers (key, question, answer, source, updated_at, uses) VALUES (?,?,?,'you',?,0)",
                    (key, q, a, at))
    con.execute("UPDATE meta SET v='old' WHERE k='keys_version'")
    con.commit()
    con.close()


def test_m1_raw_replies_survive_the_rekey_unused_until_the_person_replaces_them(tmp_path):
    """M1: the re-key deletes nothing. A raw reply saved before replies were read stays in the
    library (the person can see and delete it) but is never handed out, so the agent asks again;
    the person's new answer replaces it through the normal save."""
    path = tmp_path / "a.db"
    _old_db(path, [("gender", "Gender", LAM_REPLY, 1.0),
                   ("ethnicity", "Ethnicity", "What are the options under ethnicity ?", 2.0),
                   ("country citizenship", "Country of citizenship", "Ghana", 3.0)])
    lib = AnswerLibrary(path)
    try:
        rows = {r["key"]: r["answer"] for r in lib.list()}
        assert rows == {"@gender": LAM_REPLY, "@ethnicity": "What are the options under ethnicity ?",
                        "@citizenship": "Ghana"}                        # re-keyed, nothing deleted
        assert lib.lookup("Gender") is None and lib.lookup("What is your ethnicity?") is None
        assert lib.lookup(LAM_CITIZENSHIP)["answer"] == "Ghana"
        assert AK.known_answer("Gender", GENDER_OPTS, library=lib) is None
        res = plan_known([FormField("g", "Gender", "text"), FormField("e", "Ethnicity", "select", ETH_OPTS)],
                         KnownContext(library=lib))
        assert res.actions == [] and {f.ref for f in res.left} == {"g", "e"}
        assert lib.save("Ethnicity", "What are the options?", "you") is None   # never saved anew
        assert lib.save("Gender", "Male", "you")["answer"] == "Male"           # the person's answer replaces it
        assert lib.lookup("Gender")["answer"] == "Male" and lib.count() == 3
    finally:
        lib.close()
    con = sqlite3.connect(path)                     # a later change of the key rules deletes nothing either
    con.execute("UPDATE meta SET v='older' WHERE k='keys_version'")
    con.commit()
    con.close()
    lib = AnswerLibrary(path)
    try:
        assert lib.count() == 3 and lib.get_key("@ethnicity")["answer"].endswith("?")
    finally:
        lib.close()


def test_m1_the_apps_readers_skip_a_raw_reply(A):
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.conn.execute("INSERT INTO answers (key, question, answer, source, uses) VALUES ('@gender','Gender',?,'you',0)",
                         (LAM_REPLY,))
        lib.conn.commit()
    finally:
        lib.close()
    assert mod._known_answers_for(["Gender"], None, {}, {}) == {}
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [
        {"question": "Gender", "options": GENDER_OPTS}]}, headers=H).get_json()
    assert r["open"] == ["Gender"] and r["answers"] == {}


def test_e_a_usable_answer_wins_the_new_key_and_good_answers_stay_usable(tmp_path):
    """data-6: no answer is lost to the re-key. A usable answer claims the canonical key before a
    raw reply worded another way; a question written as the answer to an open question, and a
    long Workday self-identification choice, stay usable."""
    path = tmp_path / "a.db"
    wd = "American Indian or Alaska Native (Not Hispanic or Latino) (United States of America)"
    _old_db(path, [("gender", "Gender", "Male\nI will need H1B", 5.0),
                   ("gender your", "Your gender", "Male", 1.0),
                   ("any have questions us", "Do you have any questions for us?",
                    "What does success look like in the first 90 days?", 2.0),
                   ("race", "Race", wd, 3.0)])
    lib = AnswerLibrary(path)
    try:
        assert lib.count() == 4
        assert lib.lookup("Gender")["answer"] == "Male"
        assert lib.lookup("Do you have any questions for us?")["answer"].startswith("What does success")
        assert lib.lookup("Race")["answer"] == wd
    finally:
        lib.close()


def test_e_save_and_update_never_lose_the_persons_entry(tmp_path):
    """data-7: a question as the answer to an open question saves; an edit the library refuses
    keeps the entry it was editing."""
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        row = lib.save("Do you have any questions for us?", "What does success look like in 90 days?", "you")
        assert row and lib.lookup("Do you have any questions for us?")["answer"].endswith("90 days?")
        g = lib.save("Gender", "Male", "you")
        assert lib.update(g["key"], question="Gender", answer="What are the options?") is None
        assert lib.lookup("Gender")["answer"] == "Male"
        moved = lib.update(row["key"], question="Any questions for the team?", answer="How is success measured?")
        assert moved and lib.get_key(row["key"]) is None and lib.count() == 2
    finally:
        lib.close()


def test_e_known_answers_consult_workauth_and_the_library(A, monkeypatch):
    mod, sent = A
    monkeypatch.setattr(mod, "_load_prefs", lambda: {"work_auth": {"countries": {"US": {
        "status": "f1_opt", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes"}}}})
    lib = mod._answer_lib()
    try:
        lib.save("Country of citizenship", "Ghana", "you")
        lib.save("Gender", "Male", "you")
    finally:
        lib.close()
    # data-8: Lam's real export-control wording (its "a citizen of" tail included), and the
    # engine's 200-character cut of it, find the saved country, never the work-auth "No"
    labels = [LAM_CITIZENSHIP, LAM_CITIZENSHIP[:200], "What is your gender?"]
    known = mod._known_answers_for(labels, None, {}, {"first_name": "K"})
    assert known == {labels[0]: "Ghana", labels[1]: "Ghana", labels[2]: "Male"}
    q = "Will you now or in the future require visa sponsorship for employment?"
    assert mod._known_answers_for([q], None, {}, {})[q] == "Yes"
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
        "question": q, "options": ["Yes", "No"], "timeout_s": 5}, headers=H).get_json()
    assert r["ok"] and r["known"] and r["answer"] == "Yes" and sent == []
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
        "question": LAM_CITIZENSHIP, "timeout_s": 5}, headers=H).get_json()
    assert r["ok"] and r["answer"] == "Ghana"


def test_e_a_partly_read_list_still_finds_the_saved_answer(A):
    """engine-4 / shell-2: the shell sends every choice it read (all_options) and says when the
    page has more (partial): a saved answer beyond the eight offered is found, and on a cut list
    the person's own answer still counts."""
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.save("Country of citizenship", "Ghana", "you")
    finally:
        lib.close()
    countries = ["Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Argentina", "Armenia", "Australia",
                 "Austria", "Brazil", "Ghana", "India"]
    c = mod.app.test_client()
    r = c.post("/api/autoapply/agent/ask", json={"question": "Country of citizenship", "options": countries[:8],
                                                 "all_options": countries, "timeout_s": 5}, headers=H).get_json()
    assert r["ok"] and r["answer"] == "Ghana"
    r = c.post("/api/autoapply/agent/ask", json={"question": "Country of citizenship", "options": countries[:4],
                                                 "all_options": countries[:4], "partial": True, "timeout_s": 5},
               headers=H).get_json()
    assert r["ok"] and r["answer"] == "Ghana"            # the brain picks it on the page
    assert AK.known_answer("Country of citizenship", countries[:4], library=Lib({"@citizenship": "Ghana"})) is None
    th, out = _ask_in_thread(mod, {"id": "p1", "question": "Country of birth", "options": countries[:8],
                                   "all_options": countries[:8], "partial": True, "timeout_s": 15})
    assert "more choices" in sent[-1][0]
    assert mod._agent_ask_event("text", "Ghana") == "Got it: Ghana."
    th.join(20)
    assert out["r"]["answer"] == "Ghana"


def test_e_force_is_per_question(A, monkeypatch):
    """shell-6: a question whose known answer did not stick is asked for real; the others in the
    batch still get the person's material first."""
    mod, sent = A
    monkeypatch.setattr(mod, "_load_prefs", lambda: {"eeo_decline": True})
    lib = mod._answer_lib()
    try:
        lib.save("How many years of experience do you have?", "5", "you")
    finally:
        lib.close()
    th, out = _ask_in_thread(mod, {"id": "f1", "timeout_s": 15,
                                   "force_questions": ["How many years of experience do you have?"],
                                   "questions": [{"question": "How many years of experience do you have?"},
                                                 {"question": "Veteran Status", "options": GH_VETERAN}]})
    text = sent[-1][0]
    assert "years of experience" in text and "Veteran" not in text
    assert mod._agent_ask_event("text", "6") == "Got it: 6."
    th.join(20)
    assert out["r"]["answers"] == {"How many years of experience do you have?": "6",
                                   "Veteran Status": "I don't wish to answer"}


def test_e_known_only_reports_what_is_still_open(A):
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.save("Gender", "Male", "you")
    finally:
        lib.close()
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [
        {"question": "Gender", "options": GENDER_OPTS}, {"question": "Ethnicity", "options": ETH_OPTS}]}, headers=H).get_json()
    assert r["ok"] is False and r["answers"] == {"Gender": "Male"} and r["open"] == ["Ethnicity"] and sent == []


def test_e_separate_questions_never_overwrite_each_other(A):
    """data-4, data-5: race and ethnicity, sex and gender, pronouns and gender, a citizenship
    status and the country of citizenship each keep their own answer."""
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.save("Ethnicity", "Not Hispanic or Latino", "you")
        lib.save("Race", "Asian", "you")
        lib.save("Gender", "Male", "you")
        lib.save("Country of citizenship", "Ghana", "you")
        lib.save("What is your current citizenship status?", "Other", "you")
    finally:
        lib.close()
    assert _lib_answer(mod, "Ethnicity") == "Not Hispanic or Latino" and _lib_answer(mod, "Race") == "Asian"
    assert _lib_answer(mod, "Country of citizenship") == "Ghana"
    known = mod._known_answers_for(["Ethnicity", "Gender pronouns", "Sex"], None, {}, {})
    assert known == {"Ethnicity": "Not Hispanic or Latino"}


def test_e_answers_given_this_run_reach_the_brain(A):
    mod, sent = A
    view = ("URL: https://acme.test/apply\nTitle: Apply\n[Start of page]\n"
            "[3]<combobox label=\"Gender\" value=\"\" expanded=false required>\n"
            "[4]<button>Next</button>\n[End of page]")
    r = mod.app.test_client().post("/api/autoapply/agent/step", json={
        "record_id": None, "step": 2, "max_steps": 40, "url": "https://acme.test/apply", "view": view,
        "left": ["Gender"], "history": [], "answers": {"Gender": "I don't wish to answer"}}, headers=H).get_json()
    assert "answer_1" in r["placeholders"], r
