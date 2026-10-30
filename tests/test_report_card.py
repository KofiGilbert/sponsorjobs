"""The interview report card (interview/report.py) and its wiring into both rounds.

Offline and deterministic: FakeLLM answers the STAR / coverage rubric and the prep goals, local
transcription is off, no camera, no network. The analysis is pinned with fixed transcripts; the
endpoints go through the Flask test client like the Round-1 and Round-2 suites do.
"""

from __future__ import annotations

import importlib
import io

import pytest

from interview import report as R

_GOOD = ("At Acme I built a Tableau dashboard that cut reporting time in half, working with the "
         "ops team over six weeks. " * 2)
_UM = "Um, so, like, I basically, you know, did the thing and, uh, it kind of worked out actually."


# ---- the arithmetic: fillers, pace, seconds ------------------------------------------------------
def test_fillers_are_counted_whole_word_and_phrases_once():
    f = R.count_fillers(_UM)
    assert f["total"] == 7
    assert f["by_word"] == {"um": 1, "like": 1, "basically": 1, "you know": 1, "uh": 1,
                            "kind of": 1, "actually": 1}
    assert R.count_fillers("Likely unlike alike.")["total"] == 0          # "like" is whole-word only
    assert R.count_fillers("")["total"] == 0


def test_answer_metrics_use_measured_seconds_else_estimate():
    m = R.answer_metrics("one two three four five six seven eight nine ten", 4.0)
    assert m["words"] == 10 and m["seconds"] == 4.0 and m["seconds_estimated"] is False
    assert m["wpm"] == 150.0
    e = R.answer_metrics("one two three four five six seven eight nine ten", None)
    assert e["seconds_estimated"] is True and e["seconds"] > 0 and abs(e["wpm"] - 130) < 1
    assert R.answer_metrics(_UM, 30)["fillers_per_minute"] == 14.0


@pytest.mark.parametrize("wpm,label", [(0, "unknown"), (120, "slow"), (140, "normal"),
                                       (160, "normal"), (161, "fast")])
def test_pace_label_against_the_140_to_160_guideline(wpm, label):
    assert R.pace_label(wpm) == label


# ---- the rubric reply, whatever the model sends ---------------------------------------------------
def test_star_rubric_is_normalised_and_tolerant():
    r = R.parse_star_rubric({"star": {"situation": "yes", "task": 1, "action": True, "result": "no"},
                             "coverage": 2.6, "evidence": "Says what happened. Then rambles."})
    assert r["star"] == {"situation": True, "task": True, "action": True, "result": False}
    assert r["missing"] == ["result"] and r["coverage"] == 3 and r["evidence"] == "Says what happened."
    s = R.parse_star_rubric('noise {"situation": true, "coverage": 9} trailing')
    assert s["star"]["situation"] is True and s["star"]["task"] is None and s["coverage"] == 3
    assert s["star_known"] is False
    bad = R.parse_star_rubric("not json at all")
    assert bad["coverage"] is None and bad["missing"] == [] and bad["star_known"] is False


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    def check_star_coverage(self, q, t):
        self.calls += 1
        return {"star": {"situation": True, "task": True, "action": "did" in t, "result": "%" in t},
                "coverage": 3 if "%" in t else 1, "evidence": f"Rubric for {q[:10]}."}


# ---- the whole card from fixed transcripts ----------------------------------------------------------
def test_build_report_card_rows_delivery_strengths_and_three_weakest():
    llm = _CountingLLM()
    answers = [
        {"q": "Why us?", "transcript": "I did it and we hit 40% faster delivery. " * 10, "seconds": 60,
         "competency": "Motivation", "per_answer_feedback": {"score": 85}},
        {"q": "Tell me about a time.", "transcript": _UM, "seconds": 20, "competency": "Ownership",
         "per_answer_feedback": {"score": 40}},
        {"q": "Skipped one.", "transcript": "", "seconds": None},
        {"q": "Hard problem?", "transcript": "I did the work carefully for weeks. " * 8, "seconds": None,
         "competency": "Problem solving", "per_answer_feedback": {"score": 70}},
        {"q": "Conflict?", "transcript": "Short.", "seconds": 3, "competency": "Communication",
         "per_answer_feedback": {"score": 20}},
    ]
    card = R.build_report_card(answers, llm, overall_score=66, round_no=2)
    assert card["version"] == R.REPORT_CARD_VERSION and card["round"] == 2 and card["overall"] == 66
    assert llm.calls == 4                                   # one rubric call per answered question
    rows = card["answers"]
    assert [r["i"] for r in rows] == [0, 1, 3, 4]           # the unanswered one is skipped, indices kept
    first = rows[0]
    assert first["wpm"] == 90.0 and first["coverage"] == 3 and first["missing"] == []
    assert first["star"]["result"] is True and first["evidence"].startswith("Rubric for")
    assert rows[1]["fillers"] == 7 and rows[1]["coverage"] == 1 and rows[1]["missing"] == ["result"]
    assert rows[2]["seconds_estimated"] is True and rows[3]["seconds_estimated"] is False
    d = card["delivery"]
    assert d["answers"] == 4 and d["total_fillers"] == 7 and d["any_estimated"] is True
    assert d["pace"] in ("slow", "normal", "fast") and d["pace_note"]
    assert d["avg_wpm"] == R.words_per_minute(d["total_words"], d["total_seconds"])
    assert card["strengths"] and all(isinstance(s, str) for s in card["strengths"])
    weakest = card["weakest"]
    assert len(weakest) == 3 and all(w["drill"] for w in weakest)
    assert weakest[0]["i"] == 4                             # "Short." is the weakest moment
    assert {w["i"] for w in weakest} == {4, 1, 3}           # the strong first answer is not drilled
    assert weakest[1]["i"] == 1 and weakest[1]["drill"].startswith("Give a full answer")   # 17 words
    assert weakest[2]["i"] == 3 and weakest[2]["drill"] == "Answer the question asked in your first sentence, then add the story."


def test_card_without_a_rubric_still_has_the_arithmetic():
    card = R.build_report_card([{"q": "Q", "transcript": _GOOD, "seconds": 30}], llm=None)
    row = card["answers"][0]
    assert row["coverage"] is None and row["star_known"] is False and row["wpm"] > 0
    assert len(card["weakest"]) == 1 and card["weakest"][0]["drill"]
    assert R.build_report_card([], llm=None)["answers"] == [] and R.build_report_card([], None)["weakest"] == []


def test_drill_suggestions_follow_the_biggest_weakness():
    assert "full answer" in R.drill_for({"words": 10})
    assert R.drill_for({"words": 80, "coverage": 1, "missing": []}).startswith("Answer the question")
    assert "result" in R.drill_for({"words": 80, "coverage": 3, "missing": ["result"]})
    assert "YOU did" in R.drill_for({"words": 80, "coverage": 3, "missing": ["action"]})
    assert "context" in R.drill_for({"words": 80, "coverage": 3, "missing": ["situation"]})
    assert "filler" in R.drill_for({"words": 80, "coverage": 3, "missing": [], "fillers_per_minute": 6})
    assert "Slow down" in R.drill_for({"words": 80, "coverage": 3, "missing": [], "wpm": 190})
    assert "pace" in R.drill_for({"words": 80, "coverage": 3, "missing": [], "wpm": 100})
    assert "every part" in R.drill_for({"words": 80, "coverage": 2, "missing": [], "wpm": 150})


def test_score_trend_oldest_first():
    assert R.score_trend([]) == {"direction": "none", "delta": 0, "first": None, "latest": None,
                                 "best": None, "count": 0}
    assert R.score_trend([55])["direction"] == "none"
    up = R.score_trend([55, 60, 72])
    assert up["direction"] == "up" and up["delta"] == 17 and up["best"] == 72 and up["count"] == 3
    assert R.score_trend([70, 69])["direction"] == "flat" and R.score_trend([70, 50])["direction"] == "down"


def test_fake_llm_rubric_is_deterministic_and_strict_shape():
    from llm.base import FakeLLM
    r = FakeLLM().check_star_coverage("Tell me about a result.", _GOOD)
    assert set(r) == {"star", "coverage", "evidence"} and set(r["star"]) == set(R.STAR_PARTS)
    assert r["coverage"] == 3 and r["star"]["result"] is True
    assert FakeLLM().check_star_coverage("Q", "Yes.")["coverage"] == 0


# ---- the endpoints: both rounds carry the card, additively --------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    monkeypatch.delenv("AVATAR_API_KEY", raising=False)
    import ui.app as A
    importlib.reload(A)
    monkeypatch.setattr(A, "_TRANSCRIPT_WAIT_S", 0)
    monkeypatch.setattr(A, "_broker_reachable", lambda: True)
    monkeypatch.setattr(A, "_broker_get", lambda path: (0, None))
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (0, None))
    return A, A.app.test_client()


def _prep(c, **extra):
    body = {"role": "Data Analyst", "company": "Acme",
            "jd": "SQL and Tableau dashboards for our ops team. We ship weekly."}
    body.update(extra)
    return c.post("/api/preps", json=body).get_json()["prep"]


def _screen(c, prep, **body):
    r = c.post("/api/screens", json={"prep_id": prep["id"], **body})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_round1_score_carries_the_card_with_measured_seconds(client):
    A, c = client
    s = _screen(c, _prep(c))
    sid = s["id"]
    # the first take is uploaded with its measured length; the rest are typed with a length
    up = c.post(f"/api/screens/{sid}/answers/0",
                data={"file": (io.BytesIO(b"\x1aE\xdf\xa3 bytes"), "a.webm", "video/webm"), "seconds": "45.4"},
                content_type="multipart/form-data")
    assert up.status_code == 200
    c.post(f"/api/screens/{sid}/answers/0/transcript", json={"transcript": _GOOD})
    for q in s["questions"][1:]:
        c.post(f"/api/screens/{sid}/answers/{q['i']}/transcript",
               json={"transcript": _GOOD if q["i"] < 4 else _UM, "seconds": 30 if q["i"] < 4 else None})
    out = c.post(f"/api/screens/{sid}/score").get_json()
    res = out["result"]
    assert isinstance(res["score"], int) and res["passed"] in (True, False)   # the old score, untouched
    card = res["report_card"]
    assert card["round"] == 1 and card["overall"] == res["score"]
    assert len(card["answers"]) == 5
    assert card["answers"][0]["seconds"] == 45.4 and card["answers"][0]["seconds_estimated"] is False
    assert card["answers"][1]["seconds"] == 30 and card["answers"][4]["seconds_estimated"] is True
    assert card["answers"][4]["fillers"] == 7 and card["answers"][4]["coverage"] == 1
    assert all(set(r["star"]) == {"situation", "task", "action", "result"} for r in card["answers"])
    assert card["delivery"]["avg_wpm"] > 0 and card["delivery"]["pace"] in ("slow", "normal", "fast")
    assert len(card["weakest"]) == 3 and card["weakest"][0]["i"] == 4 and card["weakest"][0]["drill"]
    assert card["strengths"]
    # the stored screen hands the same card back, and the public answers show the measured length
    got = c.get(f"/api/screens/{sid}").get_json()
    assert got["result"]["report_card"]["answers"][0]["wpm"] == card["answers"][0]["wpm"]
    assert got["drill"] is False


def test_bad_or_absent_seconds_never_break_the_upload(client):
    A, c = client
    s = _screen(c, _prep(c))
    r = c.post(f"/api/screens/{s['id']}/answers/0",
               data={"file": (io.BytesIO(b"\x1aE\xdf\xa3"), "a.webm", "video/webm"), "seconds": "nope"},
               content_type="multipart/form-data")
    assert r.status_code == 200
    assert A._measured_seconds("nope") is None and A._measured_seconds(-3) is None
    assert A._measured_seconds("12.26") == 12.3 and A._measured_seconds(0) is None


def test_round2_card_takes_seconds_from_the_transcript_timestamps(client, monkeypatch):
    A, c = client
    from tests.test_round2_cvi import _on_a_pass
    prep = _prep(c)
    _on_a_pass(A, monkeypatch)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    rep = c.post("/api/interviews/cvi/end", json={"interview_id": iid}).get_json()["report"]
    card = rep["report_card"]
    assert card["round"] == 2 and card["overall"] == rep["score"]
    assert [r["seconds"] for r in card["answers"]] == [40.0, 45.0]           # the candidate turns' durations
    assert all(r["seconds_estimated"] is False for r in card["answers"])
    assert card["delivery"]["total_seconds"] == 85.0 and card["delivery"]["any_estimated"] is False
    assert rep["transcript"] and len(card["weakest"]) == 2                   # transcript still viewable
    stored = c.get(f"/api/interviews/{iid}").get_json()["report"]
    assert stored["report_card"]["delivery"]["avg_wpm"] == card["delivery"]["avg_wpm"]


def test_dialogue_seconds_fall_back_to_gaps_then_unknown():
    import ui.app as A
    turns = [{"role": "interviewer", "content": "Tell me about a hard problem.", "seconds_from_start": 0},
             {"role": "candidate", "content": "I did " + "lots of careful work with numbers " * 5, "seconds_from_start": 5},
             {"role": "interviewer", "content": "Thanks.", "seconds_from_start": 65}]
    items = A._pair_dialogue(turns, [])
    assert len(items) == 1 and items[0]["seconds"] == 60.0
    pasted = A._pair_dialogue([{"role": "interviewer", "content": "Q?"},
                               {"role": "candidate", "content": "I did " + "the work with numbers " * 5}], [])
    assert pasted[0]["seconds"] is None
    assert A._turn_seconds({"duration": 0}, None) is None and A._turn_seconds({"duration": "x"}, None) is None


# ---- goals per job: generated once, cached, shown on the start screen --------------------------------
def test_goals_are_built_once_per_job_and_cached(client, monkeypatch):
    A, c = client
    from llm.base import FakeLLM
    calls = {"n": 0}
    real = FakeLLM.interview_goals

    def counted(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)
    monkeypatch.setattr(FakeLLM, "interview_goals", counted)
    prep = _prep(c)
    g1 = c.get(f"/api/preps/{prep['id']}/goals").get_json()["goals"]
    assert len(g1["questions"]) == 5 and len(g1["mentions"]) == 3 and g1["company_facts"]
    assert g1["cached"] is False and g1["generated"] > 0 and "Acme" in g1["questions"][0]
    assert any("ops team" in f for f in g1["company_facts"])              # facts the JD itself states
    g2 = c.get(f"/api/preps/{prep['id']}/goals").get_json()["goals"]
    assert g2["cached"] is True and g2["generated"] == g1["generated"] and calls["n"] == 1
    # a second prep for the SAME job (same role + company typed again) shares the cache
    prep2 = _prep(c)
    assert prep2["id"] != prep["id"]
    g3 = c.get(f"/api/preps/{prep2['id']}/goals").get_json()["goals"]
    assert g3["cached"] is True and calls["n"] == 1
    # a different job builds its own; refresh rebuilds on demand
    other = _prep(c, role="Product Manager", company="Beta Co", jd="Roadmaps. Our customers are banks.")
    assert c.get(f"/api/preps/{other['id']}/goals").get_json()["goals"]["cached"] is False and calls["n"] == 2
    assert c.get(f"/api/preps/{prep['id']}/goals?refresh=1").get_json()["goals"]["cached"] is False
    assert calls["n"] == 3
    assert c.get("/api/preps/nope/goals").status_code == 404


def test_goals_for_a_saved_application_use_the_tailored_cv(client, monkeypatch):
    A, c = client
    recs = A._records()
    try:
        rid = recs.add("Data Analyst", "Coupa", 40.0, "", jd_label="Data Analyst",
                       data={"jd": "SQL dashboards. We are a spend platform.",
                             "page_profile": {"experience": [{"org": "Acme", "title": "Analyst",
                                                              "bullets": ["Built Tableau dashboards"]}],
                                              "skills": ["SQL", "Tableau"]}})
    finally:
        recs.close()
    seen = {}
    from llm.base import FakeLLM
    real = FakeLLM.interview_goals

    def spy(self, profile, role, company="", jd="", cv=""):
        seen["cv"] = cv
        return real(self, profile, role, company, jd, cv=cv)
    monkeypatch.setattr(FakeLLM, "interview_goals", spy)
    prep = c.post("/api/preps", json={"record_id": rid}).get_json()["prep"]
    g = c.get(f"/api/preps/{prep['id']}/goals").get_json()["goals"]
    assert g["job_key"] == f"record:{rid}" and "Tableau" in seen["cv"]
    assert A._job_key({"record_id": rid}) == f"record:{rid}"
    assert A._job_key({"role": "Data Analyst", "company": "Acme"}) == "job:data analyst|acme"
    assert A._job_key({"id": "x"}) == "prep:x"


# ---- re-drill: a Round-1 screen with just the three questions, through the normal start flow --------
def test_redrill_starts_a_round1_screen_with_just_those_questions(client):
    A, c = client
    prep = _prep(c)
    drill = [{"q": "Tell me about a time you had to work with someone who disagreed with you.",
              "competency": "Communication"},
             {"q": "Walk me through a hard problem you solved.", "competency": "Problem solving", "kind": "behavioural"},
             "Why do you want to work at Acme?"]
    s = _screen(c, prep, drill=drill)
    assert s["drill"] is True and s["config"]["questions"] == 3
    assert [q["text"] for q in s["questions"]] == [drill[0]["q"], drill[1]["q"], drill[2]]
    assert [q["kind"] for q in s["questions"]] == ["drill", "behavioural", "drill"]
    assert [q["competency"] for q in s["questions"]] == ["Communication", "Problem solving", "General"]
    assert s["config"]["prep_s"] == 30 and s["config"]["answer_s"] == 120 and s["practice"]["text"]
    # the same clocks, uploads and scoring as any Round-1 screen; the result carries its own card
    for q in s["questions"]:
        c.post(f"/api/screens/{s['id']}/answers/{q['i']}/transcript", json={"transcript": _GOOD, "seconds": 50})
    res = c.post(f"/api/screens/{s['id']}/score").get_json()["result"]
    assert len(res["report_card"]["answers"]) == 3 and len(res["report_card"]["weakest"]) == 3
    # a drill with extra time still doubles the clocks; one question is enough; six is capped at five
    assert _screen(c, prep, drill=[drill[2]], extra_time=True)["config"]["prep_s"] == 60
    assert _screen(c, prep, drill=[drill[2]] * 6)["config"]["questions"] == 5
    # an empty or junk drill is a clean 400, never a generated five
    assert c.post("/api/screens", json={"prep_id": prep["id"], "drill": []}).status_code == 400
    assert c.post("/api/screens", json={"prep_id": prep["id"], "drill": [{"nope": 1}]}).status_code == 400
    assert A._drill_questions("not a list") == []


# ---- history per job with the overall-score trend ------------------------------------------------------
def test_history_lists_both_rounds_for_the_same_job_oldest_first(client, monkeypatch):
    A, c = client
    prep = _prep(c)
    empty = c.get(f"/api/interview/history?prep_id={prep['id']}").get_json()
    assert empty["cards"] == [] and empty["trend"]["direction"] == "none" and empty["role"] == "Data Analyst"
    # attempt 1: a weak screen; attempt 2: a drill that goes better; attempt 3: a live interview
    s1 = _screen(c, prep)
    for q in s1["questions"]:
        c.post(f"/api/screens/{s1['id']}/answers/{q['i']}/transcript", json={"transcript": "Too short."})
    c.post(f"/api/screens/{s1['id']}/score")
    s2 = _screen(c, prep, drill=["Why us?", "A hard problem?"])
    for q in s2["questions"]:
        c.post(f"/api/screens/{s2['id']}/answers/{q['i']}/transcript", json={"transcript": _GOOD})
    c.post(f"/api/screens/{s2['id']}/score")
    from tests.test_round2_cvi import _on_a_pass
    _on_a_pass(A, monkeypatch)
    iid = c.post("/api/interviews/cvi/start", json={"prep_id": prep["id"], "skip_screen": True}).get_json()["interview_id"]
    c.post("/api/interviews/cvi/end", json={"interview_id": iid})
    # an unrelated job's screen never shows up here
    other = _prep(c, role="Product Manager", company="Beta Co", jd="Roadmaps.")
    s3 = _screen(c, other)
    c.post(f"/api/screens/{s3['id']}/answers/0/transcript", json={"transcript": _GOOD})
    c.post(f"/api/screens/{s3['id']}/score")
    h = c.get(f"/api/interview/history?prep_id={prep['id']}").get_json()
    assert [x["kind"] for x in h["cards"]] == ["screen", "screen", "interview"]
    assert [x["round"] for x in h["cards"]] == [1, 1, 2]
    assert [x["drill"] for x in h["cards"]] == [False, True, False]
    assert h["cards"][0]["id"] == s1["id"] and h["cards"][2]["id"] == iid
    assert all(x["has_card"] for x in h["cards"]) and h["cards"][1]["questions"] == 2
    assert h["trend"]["count"] == 3 and h["trend"]["direction"] == "up" and h["trend"]["first"] < h["trend"]["latest"]
    # the same job typed again on a new prep shares the history
    again = _prep(c)
    assert len(c.get(f"/api/interview/history?prep_id={again['id']}").get_json()["cards"]) == 3
    assert len(c.get(f"/api/interview/history?prep_id={other['id']}").get_json()["cards"]) == 1
    assert c.get("/api/interview/history?prep_id=nope").status_code == 404


def test_history_helper_is_pure_and_skips_unscored():
    import ui.app as A
    preps = [{"id": "p1", "role": "X", "company": "Y"}, {"id": "p2", "role": "x", "company": "y "},
             {"id": "p3", "role": "Other", "company": ""}]
    screens = [{"id": "s1", "prep_id": "p1", "created": 2, "result": {"score": 50}, "answers": [{"transcript": "a"}]},
               {"id": "s0", "prep_id": "p2", "created": 1, "result": {"score": 40, "report_card": {}}, "answers": []},
               {"id": "sx", "prep_id": "p1", "created": 3, "result": None, "answers": []},
               {"id": "so", "prep_id": "p3", "created": 4, "result": {"score": 90}, "answers": []}]
    live = [{"id": "i1", "prep_id": "p2", "ended": 5, "report": {"score": 70, "answers": [1, 2]}},
            {"id": "i0", "prep_id": "p2", "started": 6, "report": None}]
    h = A._history_for(preps[0], screens, live, preps)
    assert [x["id"] for x in h["cards"]] == ["s0", "s1", "i1"]
    assert h["cards"][0]["has_card"] is True and h["cards"][1]["has_card"] is False
    assert h["trend"] == {"direction": "up", "delta": 30, "first": 40, "latest": 70, "best": 70, "count": 3}
