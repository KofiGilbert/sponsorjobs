"""Report data the person can trust: no impossible pace, no interviewer fragment as a question."""
from interview.report import answer_metrics
from ui.app import _pair_dialogue


def test_impossible_pace_is_estimated_not_reported():
    words = " ".join(["word"] * 57)
    m = answer_metrics(words, 1.4)                 # 2,443 words a minute: a bad timestamp
    assert m["seconds_estimated"] and 100 <= m["wpm"] <= 200


def test_real_pace_is_kept():
    m = answer_metrics(" ".join(["word"] * 150), 60)
    assert not m["seconds_estimated"] and m["wpm"] == 150


def test_cut_off_interviewer_fragment_is_not_a_question():
    turns = [{"role": "interviewer", "content": "Tell me about a dashboard you built for leaders?"},
             {"role": "candidate", "content": "I built one for the sales team that tracked weekly pipeline numbers"},
             {"role": "interviewer", "content": "That's"},
             {"role": "candidate", "content": "and it changed how the leaders set quarterly targets for every region"}]
    items = _pair_dialogue(turns, [])
    assert len(items) == 1
    assert "quarterly targets" in items[0]["transcript"]
