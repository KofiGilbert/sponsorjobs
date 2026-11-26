"""Immigration and background questions by meaning (2026-11-26, autoapply/legalai.py).

On Axon the app left "Are you an alien who has been admitted to the United States under a
nonimmigrant visa?" for the person although their saved work authorization answers it. Now:

  * a work-authorization or immigration question the rules left open is answered from the saved
    work authorization, the model mapping question and options to it;
  * a background question (convictions, court orders, fugitive...) is NEVER filled without the
    person's own saved answer to a question with the same meaning, whatever the model says;
  * answers to background questions are saved when the person submits a form (the engine keeps
    only those the model reads as background, and only a value that is one of the options),
    shown in Memory with Edit and Forget, and not used while "Always ask me" is on.
Offline: FakeLLM, the model is a stub that records what it was sent. Made-up values only.
"""
from __future__ import annotations

import importlib
import json

import pytest

from autoapply import legalai

ALIEN = "Are you an alien who has been admitted to the United States under a nonimmigrant visa? (i.e. H-1B, TN, F1)"
FELONY = "Have you ever been convicted in any court of a felony?"


class Model:
    def __init__(self, topic, pick):
        self.topic, self.pick, self.calls = topic, pick, []

    def __call__(self, system, user):
        self.calls.append(user)
        t = self.topic(user) if callable(self.topic) else self.topic
        p = self.pick(user) if callable(self.pick) else self.pick
        return json.dumps({"topic": t, "pick": p})


@pytest.fixture
def model(tmp_path):
    def make(topic, pick):
        m = Model(topic, pick)
        legalai.configure(tmp_path / "legal.json", model=m)
        return m
    yield make
    legalai.configure(None, None)


def test_work_authorization_from_the_saved_profile(model):
    m = model("work_authorization", 1)
    v, topic = legalai.pick(ALIEN, ["Yes", "No"], "In the US: F-1 student on OPT, authorized to work now.", [])
    assert (v, topic) == ("Yes", "work_authorization")
    assert "F-1 student on OPT" in m.calls[0] and ALIEN in m.calls[0]
    # cached: the same question, options and facts cost nothing again
    legalai.pick(ALIEN, ["No", "Yes"], "In the US: F-1 student on OPT, authorized to work now.", [])
    assert len(m.calls) == 1


def test_a_background_question_is_never_filled_from_nothing(model):
    m = model("background", 2)                              # the model "picks" No with nothing saved
    v, topic = legalai.pick(FELONY, ["Yes", "No"], "In the US: F-1 student on OPT.", [])
    assert v is None and topic == "background"
    assert "Saved background answers: (none)" in m.calls[0]


def test_a_background_question_takes_the_saved_answer_with_the_same_meaning(model):
    model("background", 2)
    saved = [("Have you been convicted of a felony in any court?", "No")]
    v, topic = legalai.pick(FELONY, ["Yes", "No"], "", saved)
    assert (v, topic) == ("No", "background")


def test_parse_is_strict():
    assert legalai.parse("nothing", ["Yes", "No"]) == (None, "other")
    assert legalai.parse('{"topic": "weird", "pick": 1}', ["Yes", "No"]) == (None, "other")
    assert legalai.parse('{"topic": "background", "pick": 3}', ["Yes", "No"]) == (None, "background")
    assert legalai.parse('{"topic": "work_authorization", "pick": 2}', ["Yes", "No"]) == ("No", "work_authorization")


# ---------------------------------------------------------------------------------- the engine
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    yield A, A.app.test_client()
    legalai.configure(None, None)


def _fields(*qs):
    return [{"ref": f"q{i}", "label": q, "type": "select", "required": True, "options": ["Yes", "No"]} for i, q in enumerate(qs)]


def test_the_plan_answers_the_immigration_question_and_never_a_background_one_from_nothing(client, monkeypatch):
    A, c = client
    m = Model(lambda u: "background" if "felony" in u else "work_authorization",
              lambda u: 2 if "felony" in u else 1)
    legalai.configure(A._DATA / "legal.json", model=m)
    monkeypatch.setattr(A, "_legal_facts", lambda lib=None: ("In the US: F-1 student on OPT, authorized to work now.", []))
    r = c.post("/api/autoapply/plan", json={"url": "https://job-boards.greenhouse.io/acme/jobs/1", "fields": _fields(ALIEN, FELONY)},
               headers={"X-Tailor-Extension": "1"}).get_json()
    ops = {o["ref"]: o for o in r.get("ops") or []}
    left = [x["ref"] for x in r.get("left") or []]
    assert "q1" not in ops and "q1" in left                 # the felony question: the person's
    if "q0" in ops:                                         # the immigration question: from the profile
        assert ops["q0"]["text"] == "Yes" and ops["q0"]["from"] == "workauth"
    else:
        pytest.fail(f"the immigration question was left: {r}")


def test_background_answers_are_saved_on_submit_shown_and_edited(client):
    A, c = client
    m = Model(lambda u: "background" if "felony" in u.lower() else "other", 0)
    legalai.configure(A._DATA / "legal.json", model=m)
    r = c.post("/api/autoapply/background/capture", json={"record_id": 1, "url": "https://x.test/apply", "items": [
        {"question": FELONY, "value": "No", "options": ["Yes", "No"]},
        {"question": "How did you hear about us?", "value": "LinkedIn", "options": ["LinkedIn", "Other"]},
        {"question": "Have you ever been convicted of a felony offence?", "value": "Maybe", "options": ["Yes", "No"]},
    ]}).get_json()
    assert r == {"ok": True, "saved": 1}
    page = c.get("/api/memory/page").get_json()
    rows = page["background"]
    assert [x["label"] for x in rows] == [FELONY] and rows[0]["value"] == "No"
    # never under Other answers as well
    assert page["others"] == 0
    # edit and forget
    page = c.post("/api/memory/answer", json={"key": rows[0]["key"], "value": "Yes"}).get_json()
    assert page["background"][0]["value"] == "Yes"
    page = c.post("/api/memory/forget", json={"id": rows[0]["id"]}).get_json()
    assert page["background"] == []


def test_always_ask_me_keeps_the_saved_background_answers_out(client):
    A, c = client
    lib = A._answer_lib()
    try:
        lib.save(legalai.BG_PREFIX + FELONY, "No", "you")
    finally:
        lib.close()
    _wa, bg = A._legal_facts()
    assert bg == [(FELONY, "No")]
    page = c.post("/api/memory/background-ask", json={"on": True}).get_json()
    assert page["bg_always_ask"] is True
    assert A._legal_facts()[1] == []
