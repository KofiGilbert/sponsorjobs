"""Past interviews: every scored report is listed, newest first, so none is lost after leaving it."""
from ui.app import _all_reports


def test_lists_both_rounds_newest_first_and_skips_unscored():
    preps = [{"id": "p1", "role": "Analyst", "company": "Acme"}]
    screens = [{"id": "s1", "prep_id": "p1", "created": 100, "result": {"score": 61, "passed": False}},
               {"id": "s2", "prep_id": "p1", "created": 150}]                       # never scored
    interviews = [{"id": "i1", "prep_id": "p1", "started": 200, "ended": 300, "role": "Lead Analyst",
                   "report": {"score": 74, "passed": True}},
                  {"id": "i2", "prep_id": "p1", "started": 400, "report": None}]   # call never got going
    cards = _all_reports(screens, interviews, preps)
    assert [c["id"] for c in cards] == ["i1", "s1"]
    assert cards[0]["round"] == 2 and cards[0]["role"] == "Lead Analyst" and cards[0]["passed"]
    assert cards[1]["round"] == 1 and cards[1]["role"] == "Analyst" and cards[1]["company"] == "Acme"


def test_no_prep_checklist_before_an_interview():
    """The likely questions show in the report afterwards, not on the screen before the call."""
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[1] / "ui/static/app.js").read_text()
    assert 'id="prepGoalsR2"' not in js and 'id="prepGoals"' not in js
    assert js.count('id="repGoals"') == 2


def test_landing_keeps_one_line_and_the_list_has_its_own_page():
    """The landing shows only the newest report; the full list is a separate page (choice B)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "ui/templates/index.html").read_text()
    assert 'id="prepLast"' in html and 'id="prepPastPage"' in html and 'id="prepPastLink"' in html
    assert 'id="prepPast"' not in html
