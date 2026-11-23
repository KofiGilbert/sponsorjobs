"""A work-authorization question takes only the saved work authorization's answer, in every mode
(docs/apply-agent.md, 2026-11-23, measured at Uline): the brain once replaced the person's saved
"Yes" to "Will you now or in the future require employer visa sponsorship?" with a best-guess "No"."""
from autoapply import protocol
from autoapply.material import Material

Q = ("Will you now or in the future require employer visa sponsorship to work in the U.S. "
     "(This includes entering the H-1B lottery)?")
OPTS = ["Yes", "No"]
# On a student visa with practical training: authorized now, no sponsorship now, sponsorship later.
WA = {"countries": {"US": {"status": "F1_OPT", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes"}}}


def _mat(wa=WA):
    m = Material(block="<person>\nwork history: none saved\n</person>", company="Example Co")
    m.work_auth = dict(wa or {})
    return m


def _sel(option, source="best_guess", klass="legal"):
    return {"op": "select", "index": 7, "option": option, "proof": {"source": source, "class": klass}}


def test_best_guess_against_the_profile_is_refused_in_auto():
    v = protocol.check(_sel("No"), Q, OPTS, _mat(), auto=True)
    assert not v.ok and v.hard == "5"
    assert '"Yes"' in v.reason


def test_the_profile_answer_passes_and_is_not_a_best_guess():
    v = protocol.check(_sel("Yes"), Q, OPTS, _mat(), auto=True)
    assert v.ok and v.source == "saved_answer" and v.source != "best_guess"


def test_no_saved_work_authorization_means_the_question_is_left():
    v = protocol.check(_sel("Yes"), Q, OPTS, _mat(wa={}), auto=True)
    assert not v.ok and v.hard == "5"


def test_assisted_mode_refuses_the_wrong_answer_too():
    v = protocol.check(_sel("No", source="resume", klass="factual"), Q, OPTS, _mat(), auto=False)
    assert not v.ok and v.hard == "5"


def test_a_typed_answer_is_checked_the_same_way():
    v = protocol.check({"op": "type", "index": 3, "text": "No"}, "Do you require visa sponsorship now or in the future?",
                       (), _mat(), auto=True)
    assert not v.ok
    v = protocol.check({"op": "type", "index": 3, "text": "Yes"}, "Do you require visa sponsorship now or in the future?",
                       (), _mat(), auto=True)
    assert v.ok


def test_sponsorship_now_follows_the_profile():
    q = "Do you currently require visa sponsorship?"
    assert protocol.check(_sel("No"), q, OPTS, _mat(), auto=True).ok
    assert not protocol.check(_sel("Yes"), q, OPTS, _mat(), auto=True).ok


def test_a_saved_placeholder_still_passes():
    v = protocol.check({"op": "type", "index": 7, "text": "{{answer_1}}"}, Q, OPTS, _mat(), saved=True, auto=True)
    assert v.ok


def test_other_questions_are_untouched():
    assert protocol.work_auth_verdict(_sel("No"), "Have you ever worked for Example Co?", OPTS, _mat()) is None
    assert protocol.work_auth_verdict(_sel("Yes"), "Are you willing to relocate?", OPTS, _mat()) is None
