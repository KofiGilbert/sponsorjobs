"""Interview prep (Feature 3): practice BEFORE a landed interview, grounded in real experience.

Acceptance: start a prep for a role (role required) and get practice questions; coach the person's
OWN written answer into structured, honest feedback (STAR + concrete gaps + a tightened version of
THEIR answer). Pre-interview only. Uses the FakeLLM, so no network/model is needed.
"""

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


def test_prep_create_requires_role(client):
    _, c = client
    assert c.post("/api/preps", json={"role": ""}).status_code == 400


def test_coach_attaches_a_deterministic_sponsorship_tip(client):
    """Coaching the visa/sponsorship question always includes concrete guidance, regardless
    of the model; a normal question does not."""
    _, c = client
    pid = c.post("/api/preps", json={"role": "SWE"}).get_json()["prep"]["id"]
    fb = c.post(f"/api/preps/{pid}/coach",
                json={"question": "Will you now or in the future need visa sponsorship?",
                      "answer": "Umm, I think so."}).get_json()
    assert "tip" in fb and "opt" in fb["tip"].lower()
    fb2 = c.post(f"/api/preps/{pid}/coach",
                 json={"question": "Tell me about a project.", "answer": "I built " + "x" * 40}).get_json()
    assert "tip" not in fb2


def test_prep_questions_come_from_the_model_only(client):
    """A prep's question set is exactly what the JD-grounded model produced; no fixed generic
    questionnaire is appended (the essentials augmenter was retired on 2026-10-02)."""
    _, c = client
    prep = c.post("/api/preps", json={"role": "Data Analyst"}).get_json()["prep"]
    assert prep["questions"] and all(set(q) == {"q", "type", "competency", "why"} for q in prep["questions"])
    assert not any("sponsor" in q["q"].lower() for q in prep["questions"])


def test_prep_questions_and_coaching(client):
    A, c = client

    # start a prep -> practice questions for the role
    r = c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme"})
    prep = r.get_json()["prep"]
    assert prep["role"] == "Data Analyst" and prep["company"] == "Acme"
    qs = prep["questions"]
    assert qs and all(set(q) >= {"q", "type", "why"} for q in qs)
    assert c.get("/api/preps").get_json()["preps"][0]["id"] == prep["id"]

    pid = prep["id"]
    # coaching needs an actual answer
    assert c.post(f"/api/preps/{pid}/coach", json={"index": 0, "answer": ""}).status_code == 400

    # a thin answer gets structured, honest feedback (assessment + improvements + a tighter version)
    fb = c.post(f"/api/preps/{pid}/coach", json={"index": 0, "answer": "I did some analysis once."}).get_json()
    assert fb["assessment"]
    assert isinstance(fb["star"], dict) and {"situation", "task", "action", "result"} <= set(fb["star"])
    assert fb["improve"] and "honesty" in fb

    # delete
    c.delete(f"/api/preps/{pid}")
    assert c.get("/api/preps").get_json()["preps"] == []


def test_mock_session_readiness(client):
    """End of a mock run: an honest overall readiness read. Needs at least one real answer."""
    A, c = client
    pid = c.post("/api/preps", json={"role": "Backend Engineer"}).get_json()["prep"]["id"]

    # no answers -> refused
    assert c.post(f"/api/preps/{pid}/readiness", json={"answers": []}).status_code == 400

    # substantive answers -> a readiness verdict with strengths and gaps
    answers = [{"q": "Tell me about a project.", "a": "I built " + "x" * 80},
               {"q": "A hard bug?", "a": "I debugged " + "y" * 80}]
    r = c.post(f"/api/preps/{pid}/readiness", json={"answers": answers}).get_json()
    assert r["readiness"] in ("not yet", "getting there", "ready")
    assert r["summary"] and isinstance(r["strengths"], list) and isinstance(r["gaps"], list)


def test_spaced_review_schedule_and_reschedule(client):
    """After a mock, schedule questions for spaced review; nailing one pushes it further out,
    a shaky rep resets it. Scheduling the same question twice does not duplicate it."""
    A, c = client
    from datetime import date, timedelta
    pid = c.post("/api/preps", json={"role": "PM"}).get_json()["prep"]["id"]

    # schedule two questions -> first review due a couple of days out (not due yet)
    r = c.post(f"/api/preps/{pid}/schedule", json={"questions": ["Q one?", "Q two?"]}).get_json()
    assert r["added"] == 2
    # re-scheduling the same ones adds nothing (no duplicates)
    assert c.post(f"/api/preps/{pid}/schedule", json={"questions": ["Q one?", "Q two?"]}).get_json()["added"] == 0

    revs = c.get("/api/reviews").get_json()["reviews"]
    assert len(revs) == 2 and all(not x["is_due"] for x in revs)  # due in 2 days, not yet
    rid = revs[0]["id"]

    # "nailed it" grows the interval (reps 0 -> 1 => 5 days out)
    d = c.post(f"/api/reviews/{rid}/done", json={"good": True}).get_json()
    assert d["reps"] == 1 and d["due"] == (date.today() + timedelta(days=5)).isoformat()
    # "still shaky" resets to the first interval (2 days)
    d = c.post(f"/api/reviews/{rid}/done", json={"good": False}).get_json()
    assert d["reps"] == 0 and d["due"] == (date.today() + timedelta(days=2)).isoformat()

    # delete
    c.delete(f"/api/reviews/{rid}")
    assert len(c.get("/api/reviews").get_json()["reviews"]) == 1


def test_review_helpers():
    import ui.app as A
    assert A._interval_days(0) == 2 and A._interval_days(1) == 5
    assert A._interval_days(99) == 60  # clamps to the last interval
    assert A._review_public({"due": "2000-01-01"})["is_due"] is True
    assert A._review_public({"due": "2999-01-01"})["is_due"] is False


def test_focused_prep_drills_one_competency(client):
    """From the competency breakdown, a person can practice their weakest area. A focused prep
    generates questions that all drill that competency."""
    _, c = client
    r = c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme", "focus": "Communication"})
    assert r.status_code == 200
    prep = r.get_json()["prep"]
    assert prep["focus"] == "Communication"
    qs = prep["questions"]
    assert qs and all(q.get("competency") == "Communication" for q in qs)   # every question on-focus


def test_questions_are_company_aware_when_the_employer_is_known(client):
    """When the company is known, practice reflects THAT employer (its values/interview style),
    not a generic screen; with no company, nothing company-specific is forced."""
    _, c = client
    with_co = c.post("/api/preps", json={"role": "SWE", "company": "Amazon"}).get_json()["prep"]["questions"]
    assert any("amazon" in q["q"].lower() or q.get("competency") == "Company fit" for q in with_co)
    no_co = c.post("/api/preps", json={"role": "SWE"}).get_json()["prep"]["questions"]
    assert not any(q.get("competency") == "Company fit" for q in no_co)


def test_prep_seeds_from_a_saved_record_and_reuses_it(client):
    """Memory-first entry: pointing a prep at a resume you already built seeds the role, company,
    and JD straight from that record (no re-typing), and tapping a round again for the same
    application reuses that prep instead of piling up duplicates. An unknown id is a clean 404."""
    A, c = client
    recs = A._records()
    try:
        rid = recs.add("Principal Agentic Architect", "Coupa", 40.0, "",
                       jd_label="Agentic AI", data={"jd": "Build AI agents with Claude and OpenAI."})
    finally:
        recs.close()
    p1 = c.post("/api/preps", json={"record_id": rid}).get_json()["prep"]
    assert p1["role"] == "Principal Agentic Architect" and p1["company"] == "Coupa"   # seeded, not asked
    assert p1["questions"]                                                            # questions generated
    p2 = c.post("/api/preps", json={"record_id": rid}).get_json()["prep"]
    assert p2["id"] == p1["id"]                                                       # reused, not duplicated
    assert len(c.get("/api/preps").get_json()["preps"]) == 1
    assert c.post("/api/preps", json={"record_id": 999999}).status_code == 404        # clean 404


def test_hard_difficulty_makes_a_more_demanding_screen(client):
    """A candidate can choose a HARD screen: senior-level, demanding questions to ramp up."""
    _, c = client
    hard = c.post("/api/preps", json={"role": "Data Analyst", "difficulty": "hard"}).get_json()["prep"]
    assert hard["difficulty"] == "hard"
    joined = " ".join(q["q"].lower() for q in hard["questions"])
    assert "senior-level" in joined or "defend" in joined          # the demanding probe is present
    std = c.post("/api/preps", json={"role": "Data Analyst"}).get_json()["prep"]
    assert std["difficulty"] == "standard"
    assert "defend the trade-offs" not in " ".join(q["q"].lower() for q in std["questions"])


def test_cheatsheet_docx_is_a_real_grounded_one_pager(client):
    """A game-day cheat sheet a candidate can review right before the interview: the questions
    plus STAR and sponsorship reminders, as a real .docx built offline."""
    _, c = client
    pid = c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme"}).get_json()["prep"]["id"]
    r = c.get(f"/api/preps/{pid}/cheatsheet.docx")
    assert r.status_code == 200
    assert r.data[:2] == b"PK" and len(r.data) > 1000        # a real .docx
    from io import BytesIO
    from docx import Document
    text = "\n".join(p.text for p in Document(BytesIO(r.data)).paragraphs)
    assert "cheat sheet" in text.lower() and "STAR" in text and "sponsorship" in text.lower()
    assert c.get("/api/preps/nope/cheatsheet.docx").status_code == 404


# --- Setup page pre-fill: the newest tailored resume or Tracker job (2026-10) -------------------

def test_prefill_is_empty_when_nothing_exists(client):
    _, c = client
    d = c.get("/api/preps/prefill").get_json()
    assert d["role"] == "" and d["jd"] == "" and d["record_id"] is None and d["cv"] == ""


def test_prefill_uses_the_newest_tailored_resume(client):
    A, c = client
    recs = A._records()
    try:
        rid = recs.add(role="Data Analyst · Classic", company="Acme", coverage=80, pdf_path="",
                       jd_label="Data Analyst", data={"jd_text": "Analyse data with SQL.",
                       "page_profile": {"experience": [{"org": "DataCo", "roles": [
                           {"title": "Analyst", "bullets": ["Built SQL dashboards"]}]}]}})
    finally:
        recs.close()
    d = c.get("/api/preps/prefill").get_json()
    assert d["role"] == "Data Analyst" and d["company"] == "Acme"     # template label stripped
    assert d["jd"] == "Analyse data with SQL." and d["record_id"] == rid
    assert "SQL dashboards" in d["cv"] and d["cv_name"] == "Data Analyst at Acme"
    # Starting from it links the saved application, and a second start reuses that prep.
    p1 = c.post("/api/preps", json={"role": d["role"], "jd": d["jd"], "record_id": rid}).get_json()["prep"]
    p2 = c.post("/api/preps", json={"role": d["role"], "jd": d["jd"], "record_id": rid}).get_json()["prep"]
    assert p1["id"] == p2["id"]


def test_prefill_prefers_a_newer_tracker_job(client):
    A, c = client
    recs = A._records()
    try:
        recs.add(role="Old Role", company="OldCo", coverage=50, pdf_path="", jd_label="Old Role",
                 data={"jd_text": "old"})
    finally:
        recs.close()
    w = A._watchlist()
    try:
        A._board_reconcile(w)               # the record lands on the board first, as in the app
        w.track_job("gh:1", "interviewing", now="2999-01-01T09:00:00+00:00",
                    job={"source_id": "gh:1", "source": "greenhouse", "company": "NewCo",
                         "title": "Platform Engineer", "jd_text": "Run Kubernetes."})
    finally:
        w.close()
    d = c.get("/api/preps/prefill").get_json()
    assert d["role"] == "Platform Engineer" and d["company"] == "NewCo"
    assert d["jd"] == "Run Kubernetes." and d["record_id"] is None and d["cv"] == ""
