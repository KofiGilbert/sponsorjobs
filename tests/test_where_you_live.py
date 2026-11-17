"""Where the person lives is on the profile: never asked again, in any wording (2026-11-17,
Upgrade on Greenhouse asked "Where in the US do you currently reside?")."""
from autoapply.planner import where_you_live, plan_known, KnownContext
from autoapply.fields import FormField

ME = {"city": "Chicago", "state": "IL"}


def test_text_questions_get_city_and_state():
    for q in ("Where in the US do you currently reside?", "Where are you located?",
              "Current location", "Which state do you currently live in?",
              "City and state of residence", "Where are you based?"):
        assert where_you_live(q, [], ME, "text") == "Chicago, IL", q


def test_a_list_picks_the_city_else_the_state():
    assert where_you_live("Where in the US do you currently reside?",
                          ["New York, NY", "Chicago, IL", "Other"], ME, "select") == "Chicago, IL"
    assert where_you_live("Which state do you live in?", ["California", "Illinois", "Texas"],
                          {"city": "Chicago", "state": "Illinois"}, "select") == "Illinois"
    assert where_you_live("Which state do you live in?", ["CA", "IL", "TX"], ME, "select") == "IL"


def test_unclear_lists_and_other_questions_are_left_alone():
    assert where_you_live("Where do you currently reside?", ["East", "West"], ME, "select") is None
    for q in ("Are you willing to relocate?", "Which office location are you applying for?",
              "Do you currently reside in one of the following locations?",
              "Are you a lawful permanent resident?", "Where would you prefer to work from?"):
        assert where_you_live(q, [], ME, "text") is None, q
    assert where_you_live("Where are you located?", [], {}, "text") is None


def test_the_planner_fills_it_and_never_asks():
    f = [FormField("r", "Where in the US do you currently reside?", "text")]
    res = plan_known(f, KnownContext(applicant=ME))
    assert [(a.ref, a.value, a.origin) for a in res.actions] == [("r", "Chicago, IL", "profile")]
    assert res.left == []


def test_lgbtq_is_self_identification_and_declines_without_a_saved_answer():
    f = [FormField("q", "Do you identify as LGBTQ+?", "select", ["Yes", "No", "I prefer not to say"])]
    res = plan_known(f, KnownContext())
    assert [(a.ref, a.value) for a in res.actions] == [("q", "I prefer not to say")]
