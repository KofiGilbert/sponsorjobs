"""Self-identification on any form's wording (owner decision 2026-11-26, autoapply/selfid.py).

At Lam Research the agent asked for "Veteran" although the person's veteran status was saved: the
site asked a plain Yes / No under the federal notice and the word rules did not read it. Now:

  * the word rules stay the instant first pass (Lam's notice reads as the protected veteran
    question, so its plain "No" is chosen with no model call);
  * otherwise the model gets the question, the site's options AND the person's saved answer, and
    picks the option, or says none fits;
  * the pick is cached on this computer by question, options and saved answer: a repeat costs
    nothing;
  * when none fits the person is asked once, and their pick is kept for that wording;
  * the standing "Decline to self-identify" always wins: the page's decline choice, in any words.

The fixture (tests/fixtures/selfid/wordings.json) holds real systems' public questions and options;
the saved answers below are made up.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoapply import eeo, selfid

W = json.loads((Path(__file__).parent / "fixtures" / "selfid" / "wordings.json").read_text(encoding="utf-8"))
NOT_PROTECTED = "I am not a protected veteran"


class Model:
    """A stand-in for the AI: records every request, answers with a fixed pick."""

    def __init__(self, pick):
        self.pick, self.calls = pick, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return json.dumps({"pick": self.pick(user) if callable(self.pick) else self.pick})


@pytest.fixture
def model(tmp_path):
    def make(pick):
        m = Model(pick)
        selfid.configure(tmp_path / "selfid_options.json", model=m)
        return m
    yield make
    selfid.configure(None, None)


def test_lam_veteran_under_the_federal_notice_is_answered_by_the_word_rules(model):
    m = model(0)
    q = W["lam_veteran"]
    assert eeo.choose("@veteran", q["options"], {"@veteran": NOT_PROTECTED}, question=q["question"]) == "No"
    assert eeo.choose("@veteran", q["options"], {"@veteran": "I am a protected veteran"}, question=q["question"]) == "Yes"
    assert m.calls == []                                     # instant: no model call


@pytest.mark.parametrize("name,saved,want", [
    ("workday_veteran", NOT_PROTECTED, "I am not a protected veteran"),
    ("workday_veteran", "I am a protected veteran", "I identify as one or more of the classifications of protected veteran"),
    ("greenhouse_veteran", "Prefer not to say", "I don't wish to answer"),
    ("icims_veteran", NOT_PROTECTED, "I am not a protected veteran"),
    ("icims_veteran", "Prefer not to say", "I choose not to self-identify"),
    ("greenhouse_gender", "Male", "Male"),
    ("greenhouse_hispanic", "Not Hispanic or Latino", "No"),
    ("greenhouse_disability", "No", "No, I do not have a disability and have not had one in the past"),
    ("lam_disability", "No", "No"),
])
def test_real_wordings_with_the_word_rules(model, name, saved, want):
    m = model(0)
    q = W[name]
    key = eeo.topic_key(q["question"])
    assert eeo.choose(key, q["options"], {key: saved}, question=q["question"]) == want
    assert m.calls == []


def test_the_model_gets_the_question_the_options_and_the_saved_answer(model):
    # a plain "Veteran" with Yes / No and no notice: the word rules are not sure, the model decides
    m = model(2)
    opts = ["Choose not to disclose", "No", "Yes"]
    got = eeo.choose("@veteran", opts, {"@veteran": NOT_PROTECTED}, question="Veteran")
    assert got == "No"
    assert len(m.calls) == 1
    system, user = m.calls[0]
    assert NOT_PROTECTED in user                              # the saved answer goes to the model
    assert "Veteran" in user and all(o in user for o in opts)
    assert "pick" in system


def test_any_language(model):
    m = model(lambda user: 1)
    opts = ["No soy un veterano protegido", "Me identifico como veterano protegido", "Prefiero no responder"]
    assert eeo.choose("@veteran", opts, {"@veteran": NOT_PROTECTED}, question="Estado de veterano") == opts[0]
    assert len(m.calls) == 1


def test_the_pick_is_cached(model, tmp_path):
    m = model(2)
    opts = ["Choose not to disclose", "No", "Yes"]
    for _ in range(3):
        assert selfid.choose("@veteran", "Veteran", opts, NOT_PROTECTED) == "No"
    assert len(m.calls) == 1                                  # the repeats cost nothing
    # the same options in another order, same saved answer: the same cached pick
    assert selfid.choose("@veteran", "Veteran", list(reversed(opts)), NOT_PROTECTED) == "No"
    assert len(m.calls) == 1
    # another saved answer is another question for the model
    selfid.choose("@veteran", "Veteran", opts, "I am a protected veteran")
    assert len(m.calls) == 2
    assert (tmp_path / "selfid_options.json").exists()


def test_none_fits_asks_once_then_remembers(model):
    m = model(0)                                              # the model: nothing fits
    opts = ["Active duty", "Reserve", "Choose not to disclose"]
    assert selfid.choose("@veteran", "Military status", opts, NOT_PROTECTED) is None
    assert len(m.calls) == 1
    # the person is asked and picks; that wording is answered without asking next time
    assert selfid.learn("@veteran", "Military status", opts, NOT_PROTECTED, "Choose not to disclose") is True
    assert selfid.choose("@veteran", "Military status", opts, NOT_PROTECTED) == "Choose not to disclose"
    assert len(m.calls) == 1
    assert selfid.learn("@veteran", "Military status", opts, NOT_PROTECTED, "Something else") is False


def test_no_model_means_the_person_is_asked(tmp_path):
    selfid.configure(tmp_path / "c.json", model=None)
    try:
        assert selfid.choose("@veteran", "Veteran", ["No", "Yes"], NOT_PROTECTED) is None
    finally:
        selfid.configure(None, None)


def test_a_broken_reply_picks_nothing(model):
    assert selfid.parse("no json here", ["A", "B"]) is None
    assert selfid.parse('{"pick": 3}', ["A", "B"]) is None
    assert selfid.parse('{"pick": 0}', ["A", "B"]) is None
    assert selfid.parse('{"pick": 2}', ["A", "B"]) == "B"


def test_the_standing_decline_always_wins(model):
    m = model(lambda user: 3)                                 # the model finds an unusual decline
    # the usual decline words: no call
    assert eeo.decline_pick(W["greenhouse_gender"]["options"], "@gender", "Gender") == "Decline To Self Identify"
    assert m.calls == []
    # words the rules do not know: the model finds the decline choice, never the saved answer
    opts = ["Hombre", "Mujer", "Sin respuesta"]
    assert eeo.decline_pick(opts, "@gender", "Sexo") == "Sin respuesta"
    assert selfid.DECLINE_ANSWER in m.calls[0][1]


def test_the_decline_wins_in_the_ask_path(model, monkeypatch):
    from autoapply import asks
    m = model(0)

    class Lib:
        def lookup(self, q):
            return {"answer": NOT_PROTECTED, "key": "@veteran"} if "eteran" in q else None

    got = asks.known_answer("Veteran status", W["workday_veteran"]["options"], library=Lib(), eeo_decline=True)
    assert got and got[0] == "I do not wish to self-identify" and got[1] == "eeo_decline"
    assert m.calls == []
