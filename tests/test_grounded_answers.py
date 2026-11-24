"""Grounded answers before an ask (autoapply/grounded.py, 2026-11-17): a question the person's saved
facts answer is answered from that fact, never put to them. Real wordings that were asked although
the profile says them: "Where in the US do you currently reside?" (Upgrade, Greenhouse), "Current
company" (Lever), "Do you currently reside in one of the following locations?" (Gusto).

The model is a stub: these tests check what code accepts, what it sends, and when it calls.
"""
from __future__ import annotations

import importlib
import json
import re

import pytest

from autoapply import grounded as G

# The apply agent is off by default (assisted apply only); these tests use its routes, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

H = {"X-Tailor-Extension": "1"}
ME = {"city": "Chicago", "state": "IL", "postal_code": "60601", "country": "United States"}
PROFILE = {
    "experience": [
        {"org": "Acme Analytics Inc.", "roles": [{"title": "Data Analyst", "dates": "March 2022 - Present"}]},
        {"org": "Stanbic Bank", "roles": [{"title": "Analyst", "dates": "2020 to 2021"}]},
    ],
    "education": [{"school": "University of Ghana", "degree": "BSc Economics", "date": "2015 - 2019"}],
    "skills": {"Languages": "SQL, Python", "Tools": "Tableau"},
}
GUSTO_Q = "Do you currently reside in one of the following locations?"
GUSTO_OPTS = ["Yes - Denver, CO", "Yes - Chicago, IL", "Yes - New York, NY", "No - None of the above"]
EEO_SECRET = "Zyxwv Selfid"


class Stub:
    """A model that answers question n with replies[n] ({"answer", "fact"} or None), and records
    every message it was sent."""

    def __init__(self, replies=None, by_question=None):
        self.replies, self.by_question, self.calls, self.users = replies or {}, by_question or {}, 0, []

    def ground_answers(self, system, user):
        self.calls += 1
        self.users.append(user)
        qs = json.loads(re.search(r"<questions>\n(.*?)\n</questions>", user, re.S).group(1))
        out = []
        for q in qs:
            r = self.replies.get(q["n"]) or self.by_question.get(q["question"])
            out.append({"n": q["n"], **r} if r else {"n": q["n"], "none": True})
        return json.dumps({"answers": out})


class Lib:
    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return list(self.rows)


def facts(applicant=ME, profile=PROFILE, rows=()):
    return G.build_facts(applicant, profile, Lib(rows) if rows else None, questions=[])


def page(stub, items, fs=None, **kw):
    return G.answer_page(stub, [{"question": q, "options": o} for q, o in items], fs if fs is not None else facts(),
                         **kw)


# --------------------------------------------------------------------------- the three real examples
def test_the_three_real_wordings_are_answered_from_the_profile_in_one_call():
    stub = Stub(by_question={
        "Where in the US do you currently reside?": {"answer": "Chicago, IL", "fact": "loc"},
        "Current company": {"answer": "Acme Analytics Inc.", "fact": "job1"},
        GUSTO_Q: {"answer": "Yes - Chicago, IL", "fact": "loc"},
    })
    got = page(stub, [("Where in the US do you currently reside?", []), ("Current company", []), (GUSTO_Q, GUSTO_OPTS)])
    assert stub.calls == 1
    assert got["Where in the US do you currently reside?"] == {"answer": "Chicago, IL", "fact": "loc", "kind": "location"}
    assert got["Current company"] == {"answer": "Acme Analytics Inc.", "fact": "job1", "kind": "job"}
    assert got[GUSTO_Q]["answer"] == "Yes - Chicago, IL"


def test_none_of_the_above_for_someone_who_lives_elsewhere():
    austin = {"city": "Austin", "state": "TX", "country": "United States"}
    stub = Stub({1: {"answer": "No - None of the above", "fact": "loc"}})
    assert page(stub, [(GUSTO_Q, GUSTO_OPTS)], facts(austin))[GUSTO_Q]["answer"] == "No - None of the above"
    # ...but never for someone who lives in one of the listed places
    stub = Stub({1: {"answer": "No - None of the above", "fact": "loc"}})
    assert page(stub, [(GUSTO_Q, GUSTO_OPTS)]) == {}


def test_a_list_of_states_gets_the_persons_state():
    stub = Stub({1: {"answer": "Illinois", "fact": "loc"}})
    assert page(stub, [("Which state do you live in?", ["California", "Illinois", "Texas"])]) == {
        "Which state do you live in?": {"answer": "Illinois", "fact": "loc", "kind": "location"}}


# --------------------------------------------------------------------------- what stays an ask
def test_a_question_no_fact_answers_still_asks():
    stub = Stub()                                          # the model says none
    assert page(stub, [("What is your notice period?", [])]) == {}
    # a made-up answer citing a fact that does not say it is refused
    stub = Stub({1: {"answer": "2 weeks", "fact": "loc"}})
    assert page(stub, [("What is your notice period?", [])]) == {}


def test_an_answer_its_fact_does_not_support_is_rejected():
    for q, opts, reply in [
        ("Where do you live?", [], {"answer": "New York, NY", "fact": "loc"}),          # a Chicago address
        ("Where do you live?", [], {"answer": "Chicago, IN", "fact": "loc"}),            # another state
        ("Where do you live?", [], {"answer": "Chicago, IL and open to moving", "fact": "loc"}),
        ("Current company", [], {"answer": "Globex", "fact": "job1"}),                  # not a saved org
        ("Current company", [], {"answer": "Stanbic Bank", "fact": "job2"}),            # a past job
        ("Have you ever worked at Initech?", ["Yes", "No"], {"answer": "No", "fact": "job1"}),   # a No is never proven
        ("Are you based in Chicago?", ["Yes", "No"], {"answer": "Yes", "fact": "job1"}),          # the wrong fact
        ("Do you live nearby?", ["Yes", "No"], {"answer": "No", "fact": "loc"}),                  # names no place
        ("What is your highest degree?", ["Bachelor's Degree", "Master's Degree"],
         {"answer": "Master's Degree", "fact": "edu1"}),
        ("Current company", [], {"answer": "Acme Analytics Inc.", "fact": "job9"}),     # no such fact
    ]:
        assert page(Stub({1: reply}), [(q, opts)]) == {}, (q, reply)


def test_an_option_answer_must_be_one_of_the_options():
    stub = Stub({1: {"answer": "Chicago, IL", "fact": "loc"}})
    assert page(stub, [("Where do you currently reside?", ["Illinois", "Texas", "Other"])]) == {}
    stub = Stub({1: {"answer": "Yes - Chicago", "fact": "loc"}})
    assert page(stub, [(GUSTO_Q, GUSTO_OPTS)]) == {}


def test_supported_answers_of_each_kind():
    rows = [{"key": "@salary", "question": "What is your desired salary?", "answer": "$120,000", "source": "you"},
            {"key": "@notice_period", "question": "What is your notice period?", "answer": "Two weeks",
             "source": "you"}]
    fs = facts(rows=rows)
    ids = {f.data.get("key"): f.id for f in fs if f.kind == "answer"}
    cases = [
        ("Are you based in Chicago?", ["Yes", "No"], {"answer": "Yes", "fact": "loc"}, "Yes"),
        ("Are you located in Canada?", ["Yes", "No"], {"answer": "No", "fact": "loc"}, "No"),
        ("Current job title", [], {"answer": "Data Analyst", "fact": "job1"}, "Data Analyst"),
        ("Have you worked at Stanbic Bank?", ["Yes", "No"], {"answer": "Yes", "fact": "job2"}, "Yes"),
        ("Highest level of education", ["High School", "Bachelor's Degree", "Master's Degree"],
         {"answer": "Bachelor's Degree", "fact": "edu1"}, "Bachelor's Degree"),
        ("School", [], {"answer": "University of Ghana", "fact": "edu1"}, "University of Ghana"),
        ("Graduation year", [], {"answer": "2019", "fact": "edu1"}, "2019"),
        ("Do you have experience with Tableau?", ["Yes", "No"], {"answer": "Yes", "fact": "skills"}, "Yes"),
        ("Expected base compensation (USD)", [], {"answer": "120000", "fact": ids["@salary"]}, "120000"),
        ("Notice period at your current employer", [], {"answer": "Two weeks", "fact": ids["@notice_period"]},
         "Two weeks"),
    ]
    for q, opts, reply, want in cases:
        got = page(Stub({1: reply}), [(q, opts)], fs)
        assert got.get(q, {}).get("answer") == want, (q, reply, got)


def test_years_with_a_skill_is_not_total_years():
    rows = [{"key": "@years_experience", "question": "How many years of experience do you have?", "answer": "5",
             "source": "you"}]
    fs = facts(rows=rows)
    fid = next(f.id for f in fs if f.kind == "answer")
    assert page(Stub({1: {"answer": "5", "fact": fid}}), [("Years of experience with Kubernetes?", [])], fs) == {}


# --------------------------------------------------------------------------- what reaches the model
def test_self_identification_legal_and_consent_questions_never_reach_the_call():
    stub = Stub()
    items = [("Gender", ["Male", "Female", "I don't wish to answer"]),
             ("What is your veteran status?", ["I am a protected veteran", "I am not a protected veteran"]),
             ("Please check one of the boxes below:", ["Yes, I have a disability (or previously had a disability)",
                                                       "No, I do not have a disability", "I do not want to answer"]),
             ("Will you now or in the future require visa sponsorship?", ["Yes", "No"]),
             ("Are you legally authorized to work in the United States?", ["Yes", "No"]),
             ("I certify that the information I have provided is true and complete.", ["Yes"]),
             ("I agree to the privacy policy", []),
             ("What is your desired salary?", []),                     # no salary saved
             ("Why do you want to work here?", [])]
    assert page(stub, items) == {} and stub.calls == 0
    page(stub, items + [("Current company", [])])
    assert stub.calls == 1
    sent = json.loads(re.search(r"<questions>\n(.*?)\n</questions>", stub.users[0], re.S).group(1))
    assert [q["question"] for q in sent] == ["Current company"]


def test_no_call_when_nothing_is_open_or_nothing_is_saved():
    stub = Stub()
    assert page(stub, []) == {} and stub.calls == 0
    assert G.answer_page(stub, [{"question": "Current company", "options": []}], []) == {} and stub.calls == 0


def test_the_facts_carry_no_self_identification_or_legal_value():
    rows = [{"key": "@gender", "question": "Gender", "answer": EEO_SECRET, "source": "you"},
            {"key": "@race", "question": "What is your race?", "answer": EEO_SECRET + " Race", "source": "you"},
            {"key": "@citizenship", "question": "Country of citizenship", "answer": "Zyxcountry", "source": "you"},
            {"key": "need sponsorship visa", "question": "Will you need visa sponsorship?", "answer": "Zyxvisa",
             "source": "you"},
            {"key": "experience have sql", "question": "Do you have experience with SQL?",
             "answer": "Zyxdrafted", "source": "drafted"},
            {"key": "@notice_period", "question": "What is your notice period?", "answer": "Two weeks",
             "source": "you"}]
    stub = Stub()
    G.answer_page(stub, [{"question": "Current company", "options": []}],
                  G.build_facts(ME, PROFILE, Lib(rows), questions=["Current company"]),
                  eeo_values=[EEO_SECRET, EEO_SECRET + " Race"])
    sent = stub.users[0]
    for bad in (EEO_SECRET, "Zyxcountry", "Zyxvisa", "Zyxdrafted"):
        assert bad not in sent, bad
    assert "Two weeks" in sent and "Chicago" in sent and "Acme Analytics" in sent


def test_the_eeo_guard_withholds_a_value_that_slipped_into_a_fact():
    rows = [{"key": "favourite thing", "question": "Favourite thing", "answer": EEO_SECRET, "source": "you"}]
    stub = Stub()
    G.answer_page(stub, [{"question": "Current company", "options": []}],
                  G.build_facts(ME, PROFILE, Lib(rows), questions=[]), eeo_values=[EEO_SECRET])
    assert EEO_SECRET not in stub.users[0] and "[withheld]" in stub.users[0]


def test_a_tried_question_is_not_sent_twice():
    cache: dict = {}
    stub = Stub({1: {"answer": "Chicago, IL", "fact": "loc"}})
    fs = facts()
    first = page(stub, [("Where are you located?", [])], fs, cache=cache)
    again = page(stub, [("Where are you located?", [])], fs, cache=cache)
    assert first == again and stub.calls == 1
    page(stub, [("What is your notice period?", [])], fs, cache=cache)
    page(stub, [("What is your notice period?", [])], fs, cache=cache)
    assert stub.calls == 2                                   # a none is remembered too


# --------------------------------------------------------------------------- the ask route
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
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: {**PROFILE, "identity": {
        "name": "Ama Test", "city": "Chicago", "state": "IL", "country": "United States"}})
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    return mod, sent


def test_the_route_answers_from_saved_facts_and_names_where_it_came_from(A, monkeypatch):
    mod, sent = A
    lib = mod._answer_lib()
    try:
        lib.save("Gender", EEO_SECRET, "you")
    finally:
        lib.close()
    stub = Stub(by_question={
        "Where in the US do you currently reside?": {"answer": "Chicago, IL", "fact": "loc"},
        "Current company": {"answer": "Acme Analytics Inc.", "fact": "job1"},
        GUSTO_Q: {"answer": "Yes - Chicago, IL", "fact": "loc"},
    })
    monkeypatch.setattr(mod, "_make_llm", lambda: stub)
    qs = [{"question": "Where in the US do you currently reside?", "options": []},
          {"question": "Current company", "options": []},
          {"question": GUSTO_Q, "options": GUSTO_OPTS},
          {"question": "What is your notice period?", "options": []},
          {"question": "Gender", "options": ["Male", "Female", "I don't wish to answer"]}]
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": qs},
                                   headers=H).get_json()
    assert stub.calls == 1
    assert r["answers"]["Where in the US do you currently reside?"] == "Chicago, IL"
    assert r["answers"]["Current company"] == "Acme Analytics Inc."
    assert r["answers"][GUSTO_Q] == "Yes - Chicago, IL"
    assert r["origins"]["Current company"] == "grounded" and r["facts"]["Current company"] == "job"
    assert r["facts"][GUSTO_Q] == "location"
    assert r["open"] == ["What is your notice period?"] and sent == []
    assert EEO_SECRET not in stub.users[0] and "Gender" not in stub.users[0]
    # a grounded answer is the fact's, never saved as a new answer of the person's
    lib = mod._answer_lib()
    try:
        assert lib.lookup("Current company") is None
    finally:
        lib.close()
    # the same page again (the ask after the check): no second call
    mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": qs}, headers=H)
    assert stub.calls == 1


def test_the_route_makes_no_call_when_the_rules_answered_everything(A, monkeypatch):
    mod, _sent = A
    stub = Stub()
    monkeypatch.setattr(mod, "_make_llm", lambda: stub)
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [
        {"question": "Gender", "options": ["Male", "Female", "I don't wish to answer"]}]}, headers=H).get_json()
    assert r["open"] == [] and stub.calls == 0
