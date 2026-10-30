"""The apply agent's "ask the person" flow (docs/apply-agent.md): the reply is READ, not saved raw;
a question back gets the options; EEO questions offer the decline choice first (or take it from
the standing preference); several open questions go out as one numbered message; and what the
person's material already answers is never asked. Live failure this pins (Lam Research,
2026-10-30): the whole reply "Yes, I will need H1B / Male / Don't you have this?" was saved and
typed as the Gender answer, and "What are the options under ethnicity ?" was saved as Ethnicity.
"""
from __future__ import annotations

import importlib
import shutil
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
GENDER_OPTS = ["Male", "Female", "I don't wish to answer"]
ETH_OPTS = ["Asian", "Black or African American", "White", "Hispanic or Latino", "I don't wish to answer"]


# --------------------------------------------------------------------------- A: reading a reply
def test_a_multi_part_reply_keeps_only_the_part_that_answers():
    got = AK.read_reply(FakeLLM(), [{"question": "Gender", "options": GENDER_OPTS}], LAM_REPLY)
    assert got["answers"] == {0: "Male"} and not got["unclear"]
    got = AK.read_reply(FakeLLM(), [{"question": "Gender", "options": []}], LAM_REPLY)
    assert got["answers"] == {0: "Male"}


def test_a_a_question_back_is_not_an_answer():
    got = AK.read_reply(FakeLLM(), [{"question": "Ethnicity", "options": ETH_OPTS}],
                        "What are the options under ethnicity ?")
    assert got["answers"] == {} and got["question"].startswith("What are the options") and not got["unclear"]
    msg = AK.options_reply([{"question": "Ethnicity", "options": ETH_OPTS}])
    assert "Asian" in msg and "I don't wish to answer" in msg
    msg = AK.options_reply([{"question": "Ethnicity", "options": []}])
    assert "cannot read the choices" in msg and "Prefer not to say" in msg


def test_a_an_empty_or_failed_reading_is_unclear_never_a_guess():
    class Broken:
        def ask_reply(self, s, u):
            raise RuntimeError("down")
    assert AK.read_reply(Broken(), [{"question": "Gender", "options": GENDER_OPTS}], "Male")["unclear"]
    assert AK.read_reply(FakeLLM(), [{"question": "Gender", "options": GENDER_OPTS}], "   ")["unclear"]


def test_a_the_model_answer_is_validated():
    class Model:
        def __init__(self, raw):
            self.raw = raw

        def ask_reply(self, s, u):
            return self.raw
    q = [{"question": "Gender", "options": GENDER_OPTS}]
    # a question is never an answer; an out-of-range number is dropped; an option is snapped
    assert AK.read_reply(Model('{"answers":[{"n":1,"answer":"what?"}]}'), q, "x")["answers"] == {}
    assert AK.read_reply(Model('{"answers":[{"n":4,"answer":"Male"}]}'), q, "x")["answers"] == {}
    assert AK.read_reply(Model('```json\n{"answers":[{"n":1,"answer":"male"}]}\n```'), q, "x")["answers"] == {0: "Male"}


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
    for _ in range(50):                            # wait until the question is registered
        with mod._ASKS_LOCK:
            if any(a.get("open", True) for a in mod._ASKS.values()):
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
    assert out["r"]["answer"] == "Asian" and _lib_answer(mod, "Race/Ethnicity") == "Asian"


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


def test_a_a_timed_out_question_no_longer_captures_telegram_text(A):
    mod, sent = A
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"question": "Favourite colour?", "timeout_s": 5},
                                   headers=H).get_json()
    assert r["reason"] == "timeout"
    assert mod._agent_ask_event("text", "hello there") is None


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


def test_b_options_are_cleaned_capped_and_decline_first():
    many = ["Select...", "A", "B", "C", "D", "E", "F", "G", "H", "I", "Decline to self-identify"]
    got = AK.order_options("Race", many)
    assert got[0] == "Decline to self-identify" and len(got) == 8 and "Select..." not in got
    assert AK.order_options("Favourite colour", ["Red", "Prefer not to say"]) == ["Red", "Prefer not to say"]


def test_b_node_order_options_matches():
    if not shutil.which("node"):
        pytest.skip("needs node")
    root = Path(__file__).resolve().parents[1]
    js = ("const {orderOptions}=require(%r);"
          "const a=orderOptions('Gender',['Select one','Male','Female','Prefer not to say']);"
          "if(a.join('|')!=='Prefer not to say|Male|Female')throw new Error(a.join('|'));"
          "const b=orderOptions('Department',['Sales','Prefer not to say']);"
          "if(b.join('|')!=='Sales|Prefer not to say')throw new Error(b.join('|'));"
          "console.log('ok')") % str(root / "shell-electron" / "agent.js")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


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


def test_c_known_answer_order_library_then_decline_never_a_guess():
    class Lib:
        def __init__(self, d):
            self.d = d

        def lookup(self, q):
            k = normalize(q)
            return {"key": k, "answer": self.d[k]} if k in self.d else None
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({"@gender": "Male"})) == ("Male", "library")
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({}), eeo_decline=True) == ("I don't wish to answer", "eeo_decline")
    assert AK.known_answer("Gender", GENDER_OPTS, library=Lib({}), eeo_decline=False) is None
    assert AK.known_answer("Gender", ["Male", "Female"], library=Lib({}), eeo_decline=True) is None


def test_c_the_planner_uses_a_saved_eeo_answer(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("Gender", "Male", "you")
        f = FormField(ref="g", label="What is your gender?", type="select", options=["Male", "Female"])
        res = plan_known([f], KnownContext(library=lib))
        assert [(a.ref, a.value, a.origin) for a in res.actions] == [("g", "Male", "library")]
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
    assert reply.startswith("Got it: Male.") and "Ethnicity" in reply and th.is_alive()
    assert mod._agent_ask_event("text", "White") == "Got it: 1. Male; 2. White."
    th.join(20)
    assert out["r"]["ok"]


# --------------------------------------------------------------------------- E: don't re-ask
@pytest.mark.parametrize("q,key", [
    ("For the sole purpose of determining export licensing requirements, please provide your most recent "
     "country/region of citizenship or legal permanent residence (if you are a citizen of more than one, "
     "list each)", "@citizenship"),
    ("Country of citizenship", "@citizenship"),
    ("Gender", "@gender"), ("What is your gender?", "@gender"), ("Gender identity", "@gender"),
    ("Ethnicity", "@ethnicity"), ("Race/Ethnicity", "@ethnicity"), ("What is your race?", "@ethnicity"),
    ("Veteran Status", "@veteran"), ("Are you a protected veteran?", "@veteran"),
    ("Disability Status", "@disability"), ("Do you have a disability?", "@disability"),
])
def test_e_canonical_keys(q, key):
    assert normalize(q) == key


def test_e_new_keys_rekey_old_answers_and_drop_raw_replies(tmp_path):
    import sqlite3
    path = tmp_path / "a.db"
    lib = AnswerLibrary(path)
    lib.close()
    con = sqlite3.connect(path)
    # rows saved before the canonical keys existed (the old sorted-words keys) and the raw replies
    con.execute("INSERT INTO answers (key, question, answer, source, uses) VALUES ('country citizenship','Country of citizenship','Ghana','you',0)")
    con.execute("INSERT INTO answers (key, question, answer, source, uses) VALUES ('gender','Gender',?,'you',0)", (LAM_REPLY,))
    con.execute("INSERT INTO answers (key, question, answer, source, uses) VALUES ('ethnicity','Ethnicity','What are the options under ethnicity ?','you',0)")
    con.execute("UPDATE meta SET v='old' WHERE k='keys_version'")
    con.commit()
    con.close()
    lib = AnswerLibrary(path)
    try:
        assert lib.lookup("For the sole purpose of determining export licensing requirements, please provide your "
                          "most recent country/region of citizenship")["answer"] == "Ghana"
        assert lib.lookup("Gender") is None and lib.lookup("Ethnicity") is None
        assert lib.save("Ethnicity", "What are the options?", "you") is None
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
    labels = ["For the sole purpose of determining export licensing requirements, please provide your most recent "
              "country/region of citizenship", "What is your gender?"]
    known = mod._known_answers_for(labels, None, {}, {"first_name": "K"})
    assert known == {labels[0]: "Ghana", labels[1]: "Male"}
    q = "Will you now or in the future require visa sponsorship for employment?"
    assert mod._known_answers_for([q], None, {}, {})[q] == "Yes"
    if True:
        r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
            "question": q, "options": ["Yes", "No"], "timeout_s": 5}, headers=H).get_json()
        assert r["ok"] and r["known"] and r["answer"] == "Yes" and sent == []


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


def test_e_answers_given_this_run_reach_the_brain(A):
    mod, sent = A
    view = ("URL: https://acme.test/apply\nTitle: Apply\n[Start of page]\n"
            "[3]<combobox label=\"Gender\" value=\"\" expanded=false required>\n"
            "[4]<button>Next</button>\n[End of page]")
    r = mod.app.test_client().post("/api/autoapply/agent/step", json={
        "record_id": None, "step": 2, "max_steps": 40, "url": "https://acme.test/apply", "view": view,
        "left": ["Gender"], "history": [], "answers": {"Gender": "I don't wish to answer"}}, headers=H).get_json()
    assert "answer_1" in r["placeholders"], r
