"""The applicant brain, Phase 4 (docs/applicant-brain.md, section 7.1 step 2): a question asked in
new words still finds the saved answer, safely.

The label mapper makes at most one model call per page for the wordings no rule knows, caches
each wording in ``label_map`` (a cache hit makes no call), never puts a stored value in the
payload, never uses a legal or self-identification mapping before the person confirms it once
("Are you a U.S. person?" is not "Are you a US citizen?" until they say so), and keeps a per-job
question (``none``) out of memory. The page view the agent's model reads hides a control's
chosen legal or self-identification value. Offline: FakeLLM only."""
from __future__ import annotations

import importlib
import json
import threading
import time

import pytest

from applicant import mapper as MP
from applicant import migrate as M
from applicant import redact
from autoapply import brain
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known
from llm.base import FakeLLM

H = {"X-Tailor-Extension": "1"}
PAY = "What base pay are you targeting for this position?"
USP = "Are you a U.S. person?"


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    d.mkdir()
    s = M.ensure(d)
    assert s is not None
    yield s
    M.forget_process_state(d)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Test", "email": "kofi@example.com"}}, {}, [])
    assert A._brain() is not None
    yield A, A.app.test_client()
    M.forget_process_state(data)


class Spy(FakeLLM):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def map_labels(self, system, user):
        self.sent.append(user)
        return super().map_labels(system, user)


def _save(A, q, a):
    lib = A._answer_lib()
    try:
        lib.save(q, a, "you")
    finally:
        lib.close()


def _plan(c, fields, rid=None):
    return c.post("/api/autoapply/plan", json={"url": "https://boards.greenhouse.io/acme/jobs/1", "fields": fields,
                                               "record_id": rid}, headers=H).get_json()


# --------------------------------------------------------------------------- the cache
def test_a_cache_hit_makes_no_call(st):
    llm = Spy()
    items = [{"label": PAY}, {"label": "Why now?"}]
    first = MP.map_page(st, llm, items)
    assert len(llm.sent) == 1 and first[PAY]["key"] == "@salary" and first["Why now?"]["key"] == MP.NONE
    again = MP.map_page(st, llm, items + [{"label": "Error: The field " + PAY + " is required and must have a value."}])
    assert len(llm.sent) == 1                                     # cached, and the error furniture cleaned off
    assert again[PAY]["key"] == "@salary" and int(st.meta("mapper_calls")) == 1


def test_the_migrations_rule_rows_are_not_mappings(st):
    st.conn.execute("INSERT INTO label_map (label, options_hash, key, how, confirmed) VALUES (?, '', ?, 'rule', 1)",
                    (PAY.lower(), "q:base pay position targeting"))
    assert MP.cached(st, PAY) is None
    assert MP.map_page(st, FakeLLM(), [{"label": PAY}])[PAY]["key"] == "@salary"
    assert MP.cached(st, PAY.upper())["key"] == "@salary"               # one wording, whatever its case


def test_rules_and_known_wordings_never_reach_the_model(st):
    llm = Spy()
    lib = type("L", (), {"lookup": lambda self, q: {"answer": "x"} if "cloud" in q else None})()
    MP.map_page(st, llm, [{"label": "Desired salary"}, {"label": "Gender"}, {"label": "Email"},
                          {"label": "Are you legally authorized to work in the United States?"},
                          {"label": "Which cloud certifications do you hold?"}], library=lib)
    assert llm.sent == []                                         # nothing unknown: no call at all


def test_a_failed_call_caches_nothing(st):
    class Down(FakeLLM):
        def map_labels(self, system, user):
            raise RuntimeError("broker down")
    assert MP.map_page(st, Down(), [{"label": PAY}]) == {}
    assert st.q("SELECT COUNT(*) n FROM label_map")[0]["n"] == 0  # tried again on the next page
    assert MP.map_page(st, FakeLLM(), [{"label": PAY}])[PAY]["key"] == "@salary"


def test_a_key_the_model_invents_becomes_ask():
    assert MP.parse('{"map": [{"n": 1, "key": "@ssn"}, {"n": 2, "key": "@salary"}, {"n": 9, "key": "none"}]}', 2) == \
        {1: "ask", 2: "@salary"}
    assert MP.parse("not json", 2) == {} and MP.parse('[1, 2]', 2) == {}


# --------------------------------------------------------------------------- the payload
def test_a_payload_never_contains_a_stored_value(st):
    from applicant.library import BrainLibrary
    lib = BrainLibrary(st)
    lib.save("Gender", "Female", "you")
    lib.save("Country of citizenship", "Ghana", "you")
    st.add_fact("workauth", {"status": "f1_opt", "auth_end": "2027-06"}, source="you", subject="US")
    llm = Spy()
    items = [{"label": "Which passport do you hold?", "options": ["Ghana", "Kenya", "Other"]},
             {"label": "Is your Ghana passport valid for travel?"},
             {"label": "How do you identify?", "options": ["Female", "Male", "Prefer not to say"]},
             {"label": "Were you ever on f1_opt here?"}]
    MP.map_page(st, llm, items)
    assert len(llm.sent) == 1
    vals = redact.sensitive_values(st)
    assert {"Female", "Ghana", "f1_opt"} <= set(vals)
    options = [o for it in items for o in it.get("options", [])]
    payload = json.loads(llm.sent[0].split("<questions>\n")[1].split("\n</questions>")[0])
    assert redact.leaks(payload, vals, options) == []              # only the form's own choices carry them
    assert "[withheld]" in llm.sent[0] and "f1_opt" not in llm.sent[0]
    assert ["Ghana", "Kenya", "Other"] in [q.get("options") for q in payload]


# --------------------------------------------------------------------------- normal keys fill at once
def test_a_new_salary_wording_fills_the_saved_salary_after_one_call(app):
    A, c = app
    _save(A, "Desired salary", "95000")
    FakeLLM.map_calls = 0
    r = _plan(c, [{"ref": "p", "label": PAY, "type": "text", "required": True}])
    assert [(o["ref"], o["text"]) for o in r["ops"]] == [("p", "95000")] and FakeLLM.map_calls == 1
    r = _plan(c, [{"ref": "p", "label": PAY, "type": "text", "required": True}])
    assert r["ops"][0]["text"] == "95000" and FakeLLM.map_calls == 1     # the second page makes no call
    assert A._known_answers_for([PAY], None, {}, {}) == {PAY: "95000"} and FakeLLM.map_calls == 1
    s = c.get("/api/brain/status").get_json()
    assert s["mapper"] == {"calls": 1, "by_how": {"model": 1}}


# --------------------------------------------------------------------------- legal keys wait for a tap
def test_us_person_is_not_us_citizen_without_a_confirm(app):
    A, c = app
    A._merge_prefs({"work_auth": {"countries": {"US": {"status": "us_citizen", "authorized": "Yes",
                                                       "sponsor_now": "No", "sponsor_future": "No"}}}})
    fld = {"ref": "u", "label": USP, "type": "radio", "options": ["Yes", "No"], "required": True}
    r = _plan(c, [fld])
    assert r.get("ops", []) == [] and [x["label"] for x in r["left"]] == [USP]     # never filled, never the model's
    q = [{"question": USP, "options": ["Yes", "No"]}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {}
    rw = k["reword"][USP]
    assert rw["options"] == [MP.CONFIRM, "Yes", "No"]
    assert rw["say"] == ("New wording: “Are you a U.S. person?” If it means “Are you a citizen of the "
                         "job's country?”, use your saved answer. If not, pick an answer.")
    assert "—" not in rw["say"] and "–" not in rw["say"]
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"questions": [{"question": USP, "options": rw["options"],
                                                                          "all_options": rw["options"], "partial": True}],
                                                             "timeout_s": 10, "id": "c1"}, headers=H).get_json()
    th = threading.Thread(target=ask)
    th.start()
    time.sleep(1.2)
    c.post("/api/autoapply/agent/answer", json={"id": "c1", "answer": MP.CONFIRM, "exact": True, "index": 0}, headers=H)
    th.join(15)
    assert out["r"]["ok"] and out["r"]["answers"] == {USP: "Yes"}        # the saved answer, not the tap's words
    lib = A._answer_lib()
    try:
        assert all(row["answer"] != MP.CONFIRM for row in lib.list())     # the tap is never saved as an answer
    finally:
        lib.close()
    row = MP.cached(A._brain(), USP)
    assert row["how"] == "person" and row["confirmed"] == 1
    r = _plan(c, [fld])
    assert [(o["ref"], o["text"], o["from"]) for o in r["ops"]] == [("u", "Yes", "workauth")]   # used from now on


def test_a_self_identification_wording_waits_and_the_confirm_never_names_the_value(app):
    A, c = app
    _save(A, "Gender", "Female")
    q = [{"question": "How do you identify?", "options": ["Woman", "Man", "Prefer not to say"]}]
    r = _plan(c, [{"ref": "g", "label": q[0]["question"], "type": "select", "options": q[0]["options"]}])
    assert r.get("ops", []) == []                                        # the model never picks it either
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {} and "Female" not in json.dumps(k) and "Woman" not in k["reword"][q[0]["question"]]["say"]
    MP.confirm(A._brain(), q[0]["question"], "@gender")
    r = _plan(c, [{"ref": "g", "label": q[0]["question"], "type": "select", "options": q[0]["options"]}])
    assert [(o["text"], o["sensitive"]) for o in r["ops"]] == [("Woman", True)]


def test_a_legal_wording_with_nothing_saved_is_simply_asked(app):
    A, c = app
    q = [{"question": "Which passport do you hold?", "options": ["Ghana", "Kenya"]}]
    k = c.post("/api/autoapply/agent/ask", json={"known_only": True, "questions": q}, headers=H).get_json()
    assert k["answers"] == {} and k["reword"] == {}                      # no tap with nothing behind it


def test_the_planner_holds_a_waiting_mapping_open():
    f = FormField("p", USP, "radio", ["Yes", "No"], required=True)
    res = plan_known([f], KnownContext(confirm={USP: "workauth:citizen"},
                                       work_auth={"countries": {"US": {"status": "us_citizen"}}}))
    assert res.actions == [] and res.left == [f]


# --------------------------------------------------------------------------- one job's own question
def test_none_keeps_a_per_job_question_out_of_memory(app):
    A, c = app
    out = {}

    def ask():
        out["r"] = c.post("/api/autoapply/agent/ask", json={"question": "Why now?", "timeout_s": 10, "id": "n1"},
                          headers=H).get_json()
    th = threading.Thread(target=ask)
    th.start()
    time.sleep(1.2)
    c.post("/api/autoapply/agent/answer", json={"id": "n1", "answer": "The timing fits my visa.", "exact": True,
                                                "index": 0}, headers=H)
    th.join(15)
    assert out["r"]["ok"] and out["r"]["answers"] == {"Why now?": "The timing fits my visa."}
    assert MP.cached(A._brain(), "Why now?")["key"] == MP.NONE
    lib = A._answer_lib()
    try:
        assert lib.lookup("Why now?") is None                            # used on this job, kept for none
    finally:
        lib.close()


# --------------------------------------------------------------------------- the page view
def test_the_page_view_hides_a_chosen_legal_or_self_identification_value():
    view = "\n".join([
        '[1]<select label="Country of residence" value="Ghana" options=190 first="Afghanistan">',
        '[2]<select label="Preferred office" value="Accra" options=3 first="Accra">',
        '[3]<radio label="Which passport?" option="Ghana" checked=true>',
        '[4]<radio label="Which passport?" option="Kenya" checked=false>',
        '[5]<checkbox label="Pick one: Female" checked=true>',
        '[6]<radio label="Sponsorship?" option="No" checked=true>',
        "We have offices in Ghana and Kenya.",
    ])
    out = brain.mask_values(view, ["Ghana", "Female"])
    assert '[1]<select label="Country of residence" value="(filled)" options=190 first="Afghanistan">' in out
    assert 'value="Accra"' in out                                         # not a stored value
    assert '[3]<radio label="Which passport?" option="Ghana" checked=hidden answered=true>' in out
    assert '[4]<radio label="Which passport?" option="Kenya" checked=hidden answered=true>' in out
    assert '[5]<checkbox label="Pick one: Female" checked=hidden>' in out
    assert '[6]<radio label="Sponsorship?" option="No" checked=true>' in out
    assert "We have offices in Ghana and Kenya." in out                   # the page's own text stays
    assert brain.mask_values(view, []) == view
    msg = brain.build_user_message({"view": view}, [], [], withheld=["Ghana"])
    assert 'value="Ghana"' not in msg and 'option="Ghana"' in msg


def test_the_step_route_withholds_stored_values_from_the_view(app, monkeypatch):
    A, c = app
    _save(A, "Country of citizenship", "Ghana")
    seen = {}

    def fake_step(self, system, user):
        seen["user"] = user
        return json.dumps({"memory": "", "evaluation": "", "actions": [], "done": {"outcome": "blocked", "reason": "x"}})
    monkeypatch.setattr(FakeLLM, "agent_step", fake_step)
    c.post("/api/autoapply/agent/step", json={"url": "https://boards.greenhouse.io/acme/jobs/1", "step": 1,
                                             "view": '[1]<select label="Citizenship" value="Ghana" options=190 first="Afghanistan">'},
           headers=H)
    assert 'value="(filled)"' in seen["user"] and 'value="Ghana"' not in seen["user"]
