"""Round 1: the HireVue-style ONE-WAY recorded video screen (decision of 2026-10-02).

Offline and deterministic: FakeLLM, no key, no network, no camera. Local transcription is forced
OFF (RESUME_AGENT_DISABLE_STT=1) so the type-transcript fallback is exercised and no Whisper model
is ever loaded. Recordings are attached as plain blobs / transcripts, bypassing the browser camera.
"""

from __future__ import annotations

import importlib
import io

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
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


# ---- lifecycle: transcripts -> score -> report with the disclaimer -------------------------------
def test_lifecycle_type_transcripts_then_score_with_disclaimer(client):
    A, c = client
    s = _screen(c, _prep(c))
    sid = s["id"]
    for q in s["questions"]:
        r = c.post(f"/api/screens/{sid}/answers/{q['i']}/transcript", json={"transcript": _GOOD})
        assert r.status_code == 200 and r.get_json()["delivery_metrics"]["words"] > 0
    out = c.post(f"/api/screens/{sid}/score").get_json()
    res = out["result"]
    assert out["screen"]["result"]["score"] == res["score"]
    assert isinstance(res["score"], int) and res["passed"] is True and res["threshold"] == 70
    assert res["why"] and res["improvements"] and res["competencies"]
    assert len(res["answers"]) == 5 and all(a["score"] >= 0 and a["kind"] for a in res["answers"])
    assert res["disclaimer"] == ("Practice for a HireVue-style one-way video interview. Not affiliated "
                                 "with HireVue, Inc. This score is ours, not theirs.")
    assert res["is_practice"] is True and out["screen"]["passed"] is True


def test_score_needs_at_least_one_answer_and_thin_answers_fail(client):
    A, c = client
    s = _screen(c, _prep(c))
    assert c.post(f"/api/screens/{s['id']}/score").status_code == 400
    c.post(f"/api/screens/{s['id']}/answers/0/transcript", json={"transcript": "I did some stuff."})
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


def test_one_retake_per_question_third_upload_is_409(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/1/transcript", json={"transcript": _GOOD})
    r1 = c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    assert r1.status_code == 200 and r1.get_json()["takes_left"] == 1
    # a new take replaces the earlier one: its transcript no longer applies
    assert c.get(f"/api/screens/{sid}").get_json()["answers"][1]["transcript"] == ""
    r2 = c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    assert r2.status_code == 200 and r2.get_json()["takes_left"] == 0
    r3 = c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    assert r3.status_code == 409 and r3.get_json()["no_takes_left"] is True and r3.get_json()["retakes"] == 1
    # the limit is per question
    assert c.post(f"/api/screens/{sid}/answers/2", data=_blob(), content_type="multipart/form-data").status_code == 200


# ---- local transcription self-skips; typed fallback works without it ------------------------------
def test_transcription_self_skips_and_typed_fallback_works(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/0", data=_blob(), content_type="multipart/form-data")
    t = c.post(f"/api/screens/{sid}/answers/0/transcribe").get_json()
    assert t["available"] is False and t.get("message")
    r = c.post(f"/api/screens/{sid}/answers/0/transcript", json={"transcript": "typed answer here"})
    assert r.status_code == 200
    assert c.get(f"/api/screens/{sid}").get_json()["answers"][0]["transcript"] == "typed answer here"


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
def test_report_hides_raw_video_and_delete_removes_everything(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/1", data=_blob(), content_type="multipart/form-data")
    c.post(f"/api/screens/{sid}/answers/1/transcript", json={"transcript": _GOOD})
    report = c.post(f"/api/screens/{sid}/score").get_json()["screen"]
    assert "video_path" not in report["answers"][1] and report["answers"][1]["recorded"] is True
    rec_dir = A._RECORDINGS_DIR / sid
    assert rec_dir.exists()
    assert c.post(f"/api/screens/{sid}/delete").get_json()["ok"] is True
    assert not rec_dir.exists()
    assert c.get(f"/api/screens/{sid}").status_code == 404


# ---- competency breakdown: what a one-way screen grades but never shows the candidate -------------
def test_score_report_breaks_results_down_by_competency(client):
    A, c = client
    s = _screen(c, _prep(c))
    for q in s["questions"]:
        c.post(f"/api/screens/{s['id']}/answers/{q['i']}/transcript", json={"transcript": _GOOD})
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
def test_coach_one_answer_into_a_stronger_star_version(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    assert c.post(f"/api/screens/{sid}/answers/1/coach").status_code == 400   # no transcript yet
    c.post(f"/api/screens/{sid}/answers/1/transcript",
           json={"transcript": "At Acme I built a dashboard and it helped the team."})
    r = c.post(f"/api/screens/{sid}/answers/1/coach")
    assert r.status_code == 200
    coaching = r.get_json()["coaching"]
    assert "star" in coaching and "tighter" in coaching and "improve" in coaching


# ---- delivery readout: pace / length / fillers -----------------------------------------------------
def test_scored_answer_carries_delivery_metrics_for_the_readout(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    c.post(f"/api/screens/{sid}/answers/1/transcript",
           json={"transcript": "um so I basically built a dashboard you know and it helped a lot " * 3})
    report = c.post(f"/api/screens/{sid}/score").get_json()["screen"]
    dm = report["answers"][1]["delivery_metrics"]
    assert {"wpm", "words", "speak_sec", "fillers"} <= set(dm)
    assert dm["words"] > 0 and dm["fillers"] >= 3


# ---- export: the practice report as a .docx -------------------------------------------------------
def test_download_report_docx_after_scoring(client):
    A, c = client
    sid = _screen(c, _prep(c))["id"]
    assert c.get(f"/api/screens/{sid}/report.docx").status_code == 400
    c.post(f"/api/screens/{sid}/answers/1/transcript", json={"transcript": _GOOD})
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


def test_readiness_overview_synthesizes_all_attempts(client):
    A, c = client
    prep = _prep(c)

    def _attempt(text):
        s = _screen(c, prep)
        for q in s["questions"]:
            c.post(f"/api/screens/{s['id']}/answers/{q['i']}/transcript", json={"transcript": text})
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
