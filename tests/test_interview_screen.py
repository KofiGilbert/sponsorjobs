"""Round 1: the HireVue-style ONE-WAY recorded video screen (decision of 2026-10-02).

Offline and deterministic: FakeLLM, no key, no network, no camera, no Whisper model. Local
transcription is OFF by default (RESUME_AGENT_DISABLE_STT=1); a test that needs answers with words
uses the ``hear`` fixture (tests/conftest.py), which uploads a small recording and runs the real
transcribe endpoint with the engine swapped for one that hears the given text. There is no typed
transcript anywhere: an answer that cannot be heard is scored as not answered.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")   # never load a real STT engine in tests
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def _prep(c):
    return c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme",
                                      "jd": "SQL, Tableau dashboards, stakeholder reporting."}).get_json()["prep"]


def _screen(c, prep, **body):
    r = c.post("/api/screens", json={"prep_id": prep["id"], **body})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _blob(data=b"\x1aE\xdf\xa3 fake webm bytes", name="answer.webm", ctype="video/webm"):
    return {"file": (io.BytesIO(data), name, ctype)}


_GOOD = "At Acme I built a Tableau dashboard that cut reporting time in half, working with the ops team over six weeks. " * 2


# ---- the contract shape: config + 5 questions with kinds + one practice prompt -------------------
def test_screen_defaults_to_five_questions_with_the_hirevue_mix_and_clocks(client):
    A, c = client
    s = _screen(c, _prep(c))
    assert s["config"] == {"prep_s": 30, "answer_s": 120, "retakes": 1, "questions": 5, "extra_time": False}
    kinds = [q["kind"] for q in s["questions"]]
    assert kinds == ["motivation", "behavioural", "behavioural", "behavioural", "situational"]
    assert [q["i"] for q in s["questions"]] == [0, 1, 2, 3, 4]
    assert all(q["text"] and q["competency"] for q in s["questions"])
    assert "Acme" in s["questions"][0]["text"]                       # motivation names the company
    assert s["practice"]["kind"] == "practice" and s["practice"]["text"]
    assert "warm_up" not in s["questions"][0] and len(s["answers"]) == 5   # no warm-up anymore
    assert s["screen"]["id"] == s["id"]                               # also under `screen`
    assert "HireVue" in s["disclaimer"] and "Not affiliated" in s["disclaimer"]


@pytest.mark.parametrize("n,kinds", [
    (4, ["motivation", "behavioural", "behavioural", "situational"]),
    (3, ["motivation", "behavioural", "situational"]),
])
def test_fewer_questions_drop_behavioural_first(client, n, kinds):
    A, c = client
    s = _screen(c, _prep(c), questions=n)
    assert s["config"]["questions"] == n and [q["kind"] for q in s["questions"]] == kinds


def test_question_count_is_bounded_three_to_five(client):
    A, c = client
    prep = _prep(c)
    assert c.post("/api/screens", json={"prep_id": prep["id"], "questions": 2}).status_code == 400
    assert c.post("/api/screens", json={"prep_id": prep["id"], "questions": 6}).status_code == 400
    assert c.post("/api/screens", json={"prep_id": "nope"}).status_code == 404


def test_extra_time_doubles_the_clocks_only(client):
    A, c = client
    s = _screen(c, _prep(c), extra_time=True)
    assert s["config"] == {"prep_s": 60, "answer_s": 240, "retakes": 1, "questions": 5, "extra_time": True}


def test_mix_is_enforced_whatever_the_model_returns():
    """The fit is pure: too few, wrong kinds, or extras from the model still yield exactly one
    question per slot, in slot order, falling back to the prep's questions, then defaults."""
    import ui.app as A
    kinds = A._screen_mix(5)
    prep = {"role": "Analyst", "company": "Acme",
            "questions": [{"q": "Prep behavioural one", "type": "behavioral", "competency": "Ownership"},
                          {"q": "Prep why us", "type": "motivational", "competency": "Motivation"}]}
    generated = [{"q": "Model technical", "kind": "technical", "competency": "SQL"},
                 {"q": "Model behavioural", "kind": "behavioral", "competency": "Teamwork"},
                 {"q": "Model junk", "kind": "riddle"}]
    out = A._fit_screen_questions(generated, kinds, prep)
    assert [q["kind"] for q in out] == kinds[:-1] + ["technical"]    # last slot accepts technical
    assert out[0]["q"] == "Prep why us"                                # motivation from the prep
    assert out[1]["q"] == "Model behavioural" and out[2]["q"] == "Prep behavioural one"
    assert "proud of" in out[3]["q"]                                   # generic default fills the gap
    assert out[4]["q"] == "Model technical"
    assert len(A._fit_screen_questions([], A._screen_mix(3), {"role": "X"})) == 3
    assert A._screen_mix(9) == A._screen_mix(5) and A._screen_mix(1) == A._screen_mix(3)


# ---- practice prompts: unlimited, never stored --------------------------------------------------
def test_practice_prompts_are_unlimited_and_never_stored(client):
    A, c = client
    s = _screen(c, _prep(c))
    texts = {c.get(f"/api/screens/{s['id']}/practice?seen={i}").get_json()["text"] for i in range(10)}
    assert len(texts) >= 8                                             # a real pool, not one line
    r = c.get(f"/api/screens/{s['id']}/practice").get_json()
    assert r["kind"] == "practice" and r["text"]
    stored = c.get(f"/api/screens/{s['id']}").get_json()
    assert "practice" not in stored and len(stored["answers"]) == 5    # nothing persisted
    scored_texts = {q["text"] for q in stored["questions"]}
    assert not (texts & scored_texts)                                  # never leaks a scored question
    assert c.get("/api/screens/nope/practice").status_code == 404


# ---- lifecycle: heard answers -> score -> report with the disclaimer -----------------------------
def test_lifecycle_heard_answers_then_score_with_disclaimer(client, hear):
    A, c = client
    s = _screen(c, _prep(c))
    sid = s["id"]
    for q in s["questions"]:
        r = hear(c, sid, q['i'], _GOOD)
        assert r["status"] == "ok" and r["delivery_metrics"]["words"] > 0
    out = c.post(f"/api/screens/{sid}/score").get_json()
    res = out["result"]
    assert out["screen"]["result"]["score"] == res["score"]
    assert isinstance(res["score"], int) and res["passed"] is True and res["threshold"] == 70
    assert res["why"] and res["improvements"] and res["competencies"]
    assert len(res["answers"]) == 5 and all(a["score"] >= 0 and a["kind"] for a in res["answers"])
    assert res["disclaimer"] == ("Practice for a HireVue-style one-way video interview. Not affiliated "
                                 "with HireVue, Inc. This score is ours, not theirs.")
    assert res["is_practice"] is True and out["screen"]["passed"] is True


def test_score_needs_at_least_one_answer_and_thin_answers_fail(client, hear):
    A, c = client
    s = _screen(c, _prep(c))
    assert c.post(f"/api/screens/{s['id']}/score").status_code == 400
    hear(c, s['id'], 0, "I did some stuff.")
    res = c.post(f"/api/screens/{s['id']}/score").get_json()["result"]
    assert res["passed"] is False and res["score"] < 70


# ---- uploads: stored locally, bad input rejected, ONE retake enforced server-side -----------------
def test_upload_stores_blob_and_rejects_bad_input(client, monkeypatch):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    ok = c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    assert ok.status_code == 200 and ok.get_json()["recorded"] is True
    assert (A._RECORDINGS_DIR / sid / "answer_0.webm").exists()   # stored to the local dir
    bad = c.post(f"/api/screens/{sid}/answers/0",
                 data={"file": (io.BytesIO(b"hello"), "a.txt", "text/plain")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400
    monkeypatch.setattr(A, "_RECORDING_MAX", 10)
    big = c.post(f"/api/screens/{sid}/answers/0", data=_blob(b"x" * 500),
                 content_type="multipart/form-data")
    assert big.status_code == 413
    assert c.post(f"/api/screens/{sid}/answers/9", data=_blob(), content_type="multipart/form-data").status_code == 404


def test_one_retake_per_question_third_upload_is_409(client, hear):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    hear(c, sid, 1, _GOOD)                                              # the first take, heard
    assert c.get(f"/api/screens/{sid}").get_json()["answers"][1]["takes_left"] == 1
    r2 = c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    assert r2.status_code == 200 and r2.get_json()["takes_left"] == 0
    # a new take replaces the earlier one: its transcript no longer applies
    assert c.get(f"/api/screens/{sid}").get_json()["answers"][1]["transcript"] == ""
    r3 = c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    assert r3.status_code == 409 and r3.get_json()["no_takes_left"] is True and r3.get_json()["retakes"] == 1
    # the limit is per question
    assert c.post(f"/api/screens/{sid}/answers/2", data=_blob(), content_type="multipart/form-data").status_code == 200


# ---- no typed answers, ever: unheard answers are scored as not answered --------------------------
def test_there_is_no_typed_transcript_endpoint(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    r = c.post(f"/api/screens/{sid}/answers/0/transcript", json={"transcript": "copied from somewhere"})
    assert r.status_code in (404, 405)
    assert c.get(f"/api/screens/{sid}").get_json()["answers"][0]["transcript"] == ""
    assert not any(str(rule) .endswith("/transcript") for rule in A.app.url_map.iter_rules()
                   if "/api/screens/" in str(rule))


def test_transcribe_without_an_engine_says_so_and_never_asks_to_type(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    t = c.post(f"/api/screens/{sid}/answers/0/transcribe").get_json()
    assert t["available"] is False and t["status"] == "unavailable"
    assert "type" not in str(t).lower() and "paste" not in str(t).lower()


def test_finish_scores_unheard_answers_as_not_answered(client, hear):
    A, c = client
    s = _screen(c, _prep(c))
    sid = s["id"]
    for q in s["questions"]:
        hear(c, sid, q["i"], _GOOD if q["i"] < 3 else "")              # the last two are not heard
    out = c.post(f"/api/screens/{sid}/finish", json={}).get_json()
    res = out["result"]
    assert len(res["answers"]) == 5
    heard = [a for a in res["answers"] if not a["unheard"]]
    unheard = [a for a in res["answers"] if a["unheard"]]
    assert len(heard) == 3 and [a["i"] for a in unheard] == [3, 4]
    assert all(a["score"] == 0 and a["feedback"] == "We couldn't hear this answer."
               and "microphone" in a["tip"] and a["transcript"] == "" for a in unheard)
    # scored as not answered: the zeros count, so the result is below the all-heard average
    assert res["score"] < 82 and res["passed"] is False and out["screen"]["result"]["score"] == res["score"]


def test_finish_with_nothing_heard_gives_a_plain_result_without_a_model_call(client, hear, monkeypatch):
    A, c = client
    s = _screen(c, _prep(c), questions=3)
    for q in s["questions"]:
        hear(c, s["id"], q["i"], "")
    from llm.base import FakeLLM
    monkeypatch.setattr(FakeLLM, "score_interview_screen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))
    res = c.post(f"/api/screens/{s['id']}/finish", json={}).get_json()["result"]
    assert res["score"] == 0 and res["passed"] is False and "couldn't hear" in res["why"]
    assert all(a["unheard"] for a in res["answers"])


def test_finish_transcribes_answers_that_were_never_transcribed_and_retries_a_failure(client, monkeypatch):
    """The rescue path: an interview whose recordings never got words (the old silent failure)
    is transcribed at the end, a failed attempt is tried once more, then it is scored."""
    A, c = client
    s = _screen(c, _prep(c), questions=3)
    sid = s["id"]
    for q in s["questions"]:
        c.post(f"/api/screens/{sid}/answers/{q['i']}", data=_blob(), content_type="multipart/form-data")
    import interview.transcribe as T
    calls = []

    def detail(path):
        calls.append(Path(path).name)
        if Path(path).name == "answer_1.webm" and calls.count("answer_1.webm") == 1:
            return {"text": "", "status": "failed", "error": "TypeError: boom", "seconds": None, "language": ""}
        return {"text": _GOOD, "status": "ok", "error": "", "seconds": 42.0, "language": "en"}

    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "transcribe_detail", detail)
    out = c.post(f"/api/screens/{sid}/finish", json={}).get_json()
    assert calls == ["answer_0.webm", "answer_1.webm", "answer_1.webm", "answer_2.webm"]
    res = out["result"]
    assert len(res["answers"]) == 3 and not any(a["unheard"] for a in res["answers"])
    assert res["passed"] is True
    stored = A._load_screens()[0]["answers"]
    assert all(a["stt_status"] == "ok" and a["seconds"] == 42.0 for a in stored)   # real length kept
    # finishing again does not transcribe again
    c.post(f"/api/screens/{sid}/finish", json={})
    assert len(calls) == 4


def test_finish_without_speech_to_text_says_so_and_scores_nothing(client, monkeypatch):
    A, c = client
    s = _screen(c, _prep(c), questions=3)
    for q in s["questions"]:
        c.post(f"/api/screens/{s['id']}/answers/{q['i']}", data=_blob(), content_type="multipart/form-data")
    out = c.post(f"/api/screens/{s['id']}/finish", json={}).get_json()     # DISABLE_STT=1 here
    assert out["stt_unavailable"] is True and "isn't ready" in out["message"]
    assert "type" not in out["message"].lower()
    assert out["screen"]["result"] is None and A._load_screens()[0]["result"] is None
    # once speech to text is there, Try again (retry) transcribes and scores
    import interview.transcribe as T
    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "transcribe_detail", lambda p: {"text": _GOOD, "status": "ok", "error": "",
                                                           "seconds": None, "language": "en"})
    again = c.post(f"/api/screens/{s['id']}/finish", json={"retry": True}).get_json()
    assert again["result"]["score"] > 0 and len(again["result"]["answers"]) == 3


def test_a_slow_transcription_never_writes_back_over_an_upload_that_landed_meanwhile(client, monkeypatch):
    A, c = client
    s = _screen(c, _prep(c), questions=3)
    sid = s["id"]
    c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    import interview.transcribe as T

    def detail(path):          # while answer 0 is being transcribed, answer 1 uploads
        c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
        return {"text": _GOOD, "status": "ok", "error": "", "seconds": None, "language": "en"}

    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "transcribe_detail", detail)
    assert c.post(f"/api/screens/{sid}/answers/0/transcribe").get_json()["status"] == "ok"
    got = c.get(f"/api/screens/{sid}").get_json()["answers"]
    assert got[0]["transcript"] == _GOOD
    assert got[1]["recorded"] is True and got[1]["takes"] == 1          # the upload was not lost


def test_words_from_an_older_take_are_dropped_after_a_retake(client, monkeypatch):
    A, c = client
    sid = _screen(c, _prep(c), questions=3)["id"]
    c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    import interview.transcribe as T

    def detail(path):          # the person retakes while the first take is still transcribing
        c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
        return {"text": "the first take", "status": "ok", "error": "", "seconds": None, "language": "en"}

    monkeypatch.setattr(T, "available", lambda: True)
    monkeypatch.setattr(T, "transcribe_detail", detail)
    assert c.post(f"/api/screens/{sid}/answers/0/transcribe").get_json()["status"] == "stale"
    a = c.get(f"/api/screens/{sid}").get_json()["answers"][0]
    assert a["transcript"] == "" and a["takes"] == 2


def test_speech_model_status_and_prepare_endpoints(client, monkeypatch):
    A, c = client
    st = c.get("/api/interview/stt").get_json()
    assert st["engine"] is False and st["model"] == "unavailable"        # DISABLE_STT=1 here
    import interview.transcribe as T
    monkeypatch.setattr(T, "prepare", lambda: {"engine": True, "model": "downloading", "done_mb": 12,
                                               "total_mb": 140, "error": ""})
    assert c.post("/api/interview/stt/prepare", json={}).get_json()["model"] == "downloading"


def test_transcribe_module_degrades_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    import interview.transcribe as T
    importlib.reload(T)
    assert T.available() is False
    (tmp_path / "x.webm").write_bytes(b"not really audio")
    assert T.transcribe(tmp_path / "x.webm") == ""     # skip, not raise
    m = T.delivery_metrics("um so I like basically built a dashboard you know")
    assert m["words"] > 0 and m["fillers"] >= 3 and "hint" in m


# ---- privacy + user control: no raw video in the report; delete removes record + recordings ------
def test_report_hides_raw_video_and_delete_removes_everything(client, hear):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    hear(c, sid, 1, _GOOD, upload=False)
    report = c.post(f"/api/screens/{sid}/score").get_json()["screen"]
    assert "video_path" not in report["answers"][1] and report["answers"][1]["recorded"] is True
    rec_dir = A._RECORDINGS_DIR / sid
    assert rec_dir.exists()
    assert c.post(f"/api/screens/{sid}/delete").get_json()["ok"] is True
    assert not rec_dir.exists()
    assert c.get(f"/api/screens/{sid}").status_code == 404


# ---- competency breakdown: what a one-way screen grades but never shows the candidate -------------
def test_score_report_breaks_results_down_by_competency(client, hear):
    A, c = client
    s = _screen(c, _prep(c))
    for q in s["questions"]:
        hear(c, s['id'], q['i'], _GOOD)
    comps = c.post(f"/api/screens/{s['id']}/score").get_json()["result"]["competencies"]
    assert comps and all({"name", "score", "count"} <= set(cb) for cb in comps)
    assert {cb["name"] for cb in comps} <= {q["competency"] for q in s["questions"]}
    assert [cb["score"] for cb in comps] == sorted(cb["score"] for cb in comps)   # weakest first


def test_competency_breakdown_helper_averages_and_skips_unscored():
    import ui.app as A
    answers = [
        {"competency": "Communication", "per_answer_feedback": {"score": 80}},
        {"competency": "Communication", "per_answer_feedback": {"score": 60}},
        {"competency": "Ownership", "per_answer_feedback": {"score": 40}},
        {"competency": "Ownership", "per_answer_feedback": None},          # unscored, ignored
        {"competency": "", "per_answer_feedback": {"score": 10}},          # untagged, ignored
    ]
    assert A._competency_breakdown(answers) == [{"name": "Ownership", "score": 40, "count": 1},
                                                {"name": "Communication", "score": 70, "count": 2}]


# ---- 'See a stronger version': STAR coaching for one answer ---------------------------------------
def test_coach_one_answer_into_a_stronger_star_version(client, hear):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    assert c.post(f"/api/screens/{sid}/answers/1/coach").status_code == 400   # no transcript yet
    hear(c, sid, 1, "At Acme I built a dashboard and it helped the team.")
    r = c.post(f"/api/screens/{sid}/answers/1/coach")
    assert r.status_code == 200
    coaching = r.get_json()["coaching"]
    assert "star" in coaching and "tighter" in coaching and "improve" in coaching


# ---- delivery readout: pace / length / fillers -----------------------------------------------------
def test_scored_answer_carries_delivery_metrics_for_the_readout(client, hear):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    hear(c, sid, 1, "um so I basically built a dashboard you know and it helped a lot " * 3)
    report = c.post(f"/api/screens/{sid}/score").get_json()["screen"]
    dm = report["answers"][1]["delivery_metrics"]
    assert {"wpm", "words", "speak_sec", "fillers"} <= set(dm)
    assert dm["words"] > 0 and dm["fillers"] >= 3


# ---- export: the practice report as a .docx -------------------------------------------------------
def test_download_report_docx_after_scoring(client, hear):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    assert c.get(f"/api/screens/{sid}/report.docx").status_code == 400
    hear(c, sid, 1, _GOOD)
    c.post(f"/api/screens/{sid}/score")
    r = c.get(f"/api/screens/{sid}/report.docx")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.data[:2] == b"PK" and len(r.data) > 1000


def test_report_docx_builder_is_offline_and_grounded(tmp_path):
    from tailoring.docx_export import build_screen_report_docx
    from docx import Document
    screen = {"role": "Data Analyst", "company": "Acme",
              "result": {"score": 82, "passed": True, "threshold": 70, "why": "Strong, specific answers.",
                         "competencies": [{"name": "Communication", "score": 70, "count": 2}],
                         "improvements": ["Lead with the result."]},
              "answers": [{"q": "Tell me about a project.", "competency": "Ownership",
                           "per_answer_feedback": {"score": 84, "feedback": "Clear ownership."},
                           "delivery_metrics": {"wpm": 150, "words": 120, "speak_sec": 48, "fillers": 2}}]}
    out = build_screen_report_docx(screen, tmp_path / "r.docx")
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "Interview practice report" in text and "82 out of 100" in text
    assert "Communication" in text and "Tell me about a project." in text
    assert "not a real hiring decision" in text.lower()


# ---- history + readiness across attempts ----------------------------------------------------------
def test_screen_carries_a_creation_timestamp_for_history(client):
    A, c = client
    s = _screen(c, _prep(c))
    assert isinstance(s["created"], int) and s["created"] > 0
    listed = c.get("/api/screens").get_json()["screens"]
    assert any(x["id"] == s["id"] and x["created"] == s["created"] for x in listed)


def test_readiness_overview_synthesizes_all_attempts(client, hear):
    A, c = client
    prep = _prep(c)

    def _attempt(text):
        s = _screen(c, prep)
        for q in s["questions"]:
            hear(c, s['id'], q['i'], text)
        return c.post(f"/api/screens/{s['id']}/score").get_json()["result"]["score"]

    assert c.get("/api/interview/readiness").get_json()["verdict"] is None
    s1 = _attempt("thin.")
    s2 = _attempt(_GOOD)
    d = c.get("/api/interview/readiness").get_json()
    assert d["attempts"] == 2 and d["screens"] == 2 and d["live"] == 0
    assert d["verdict"] in ("ready", "getting there", "not yet")
    assert d["best"] == max(s1, s2) and d["latest"] == s2
    assert isinstance(d["competencies"], list) and len(d["weakest"]) <= 3


def test_readiness_helper_is_pure_and_folds_in_live_reports():
    import ui.app as A
    screens = [
        {"created": 1, "role": "SWE", "result": {"score": 40, "passed": False,
         "competencies": [{"name": "Communication", "score": 40}]}},
        {"created": 2, "role": "SWE", "result": {"score": 80, "passed": True,
         "competencies": [{"name": "Communication", "score": 60}, {"name": "Ownership", "score": 90}]}},
    ]
    d = A._interview_readiness_overview(screens)
    assert d["attempts"] == 2 and d["best"] == 80 and d["latest"] == 80 and d["improved"] is True
    assert d["passed_any"] is True and d["verdict"] == "ready"
    assert d["weakest"][0]["name"] == "Communication"
    live = [{"started": 3, "ended": 4, "role": "SWE", "report": {"score": 50, "passed": False,
             "competencies": [{"name": "Ownership", "score": 50}]}}]
    d2 = A._interview_readiness_overview(screens, live)
    assert d2["attempts"] == 3 and d2["live"] == 1 and d2["latest"] == 50 and d2["verdict"] == "getting there"


def test_delivery_metrics_flag_hedging_and_ownership():
    from interview.transcribe import delivery_metrics
    hedgy = delivery_metrics("I think we maybe sort of did a good job and I guess it worked out.")
    assert hedgy["hedges"] >= 3 and hedgy["ownership"] == "i"
    teamy = delivery_metrics("We built it, we shipped it, and our team owned the whole rollout.")
    assert teamy["we_count"] >= 3 and teamy["ownership"] == "we"
    strong = delivery_metrics("I led the migration. I cut load time in half. I owned the rollout.")
    assert strong["hedges"] == 0 and strong["ownership"] == "i"
