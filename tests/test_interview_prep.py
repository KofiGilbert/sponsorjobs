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


def test_star_story_bank_crud(client):
    """Build reusable STAR stories once, tagged with the competencies they show, and reuse them.
    Grounded in the person's own material; the app stores, never invents."""
    _, c = client
    assert c.get("/api/stories").get_json()["stories"] == []
    r = c.post("/api/stories", json={"title": "Payments migration",
               "situation": "Legacy system was slow.", "action": "I led the rewrite.",
               "result": "Cut latency in half.", "competencies": ["Ownership", "Problem solving"]})
    assert r.status_code == 200
    st = r.get_json()["story"]
    assert st["title"] == "Payments migration" and st["competencies"] == ["Ownership", "Problem solving"]
    assert st["action"] == "I led the rewrite."
    assert c.post("/api/stories", json={"title": ""}).status_code == 400   # title required
    listed = c.get("/api/stories").get_json()["stories"]
    assert len(listed) == 1 and listed[0]["id"] == st["id"]
    assert c.delete(f"/api/stories/{st['id']}").get_json()["ok"] is True
    assert c.get("/api/stories").get_json()["stories"] == []


def test_story_match_ranks_by_competency_then_keywords(client):
    _, c = client
    # A migration story tagged Problem solving, and an unrelated leadership story.
    c.post("/api/stories", json={"title": "DB migration", "action": "I migrated the database and cut latency.",
                                 "competencies": ["Problem solving"]})
    c.post("/api/stories", json={"title": "Ran the bake sale", "action": "I organized volunteers.",
                                 "competencies": ["Leadership"]})
    # A problem-solving question about a database -> the migration story wins (competency + keywords).
    m = c.post("/api/stories/match", json={"question": "Tell me about a hard database problem you solved.",
                                           "competency": "Problem solving"}).get_json()["matches"]
    assert m and m[0]["title"] == "DB migration"
    # No stories match a totally unrelated competency+question with no overlap.
    empty = c.post("/api/stories/match", json={"question": "xyzzy?", "competency": "Nothing"}).get_json()["matches"]
    assert empty == []


def test_story_match_helper_is_pure():
    import ui.app as A
    stories = [{"id": "a", "title": "Led rewrite", "action": "cut latency", "competencies": ["Ownership"]}]
    hit = A._match_stories_to_question("Tell me about ownership of a rewrite.", "Ownership", stories)
    assert hit and hit[0]["id"] == "a" and hit[0]["score"] >= 5   # competency match boosts it
    assert A._match_stories_to_question("unrelated", "None", stories) == []


def test_structure_rough_notes_into_star(client):
    """Low-friction story building: paste rough notes, get a STAR structure back to seed a bank
    entry. Grounded in what they wrote; the shape (S/T/A/R) always comes back."""
    _, c = client
    r = c.post("/api/stories/structure",
               json={"text": "At Acme the reporting was slow so I rebuilt the pipeline and it got much faster."})
    assert r.status_code == 200
    star = r.get_json()["star"]
    assert set(star) == {"situation", "task", "action", "result"}
    assert c.post("/api/stories/structure", json={"text": ""}).status_code == 400   # needs notes


# --- STAR story bank: suggest from the profile + wire into coaching (feature #7) ----------------

def _seed_profile(A):
    """Give the app a saved profile with real experience/projects to draft stories from."""
    A._memory().save("default", {
        "experience": [{"org": "DataCo", "roles": [{"title": "Engineer",
            "bullets": ["Built ETL pipelines in Python cutting nightly runtime 40 percent",
                        "Led the migration of the reporting stack to a new warehouse"]}]}],
        "projects": [{"title": "Portfolio site", "bullets": ["Shipped a React app used by 200 people"]}],
    }, {}, [])


def test_suggest_stories_are_drawn_from_the_real_profile(client):
    A, c = client
    _seed_profile(A)
    sug = c.post("/api/stories/suggest", json={}).get_json()["suggestions"]
    assert sug and all(s["title"] and "competencies" in s for s in sug)
    # Every draft is grounded in the person's own bullets (nothing invented).
    joined = " ".join(s["action"] for s in sug).lower()
    assert "etl" in joined or "migration" in joined or "react" in joined


def test_suggest_skips_titles_already_in_the_bank(client):
    A, c = client
    _seed_profile(A)
    first = c.post("/api/stories/suggest", json={}).get_json()["suggestions"]
    c.post("/api/stories", json=first[0])                    # keep the first draft
    again = c.post("/api/stories/suggest", json={}).get_json()["suggestions"]
    assert first[0]["title"].lower() not in [s["title"].lower() for s in again]


def test_suggest_without_experience_is_empty_with_a_note(client):
    A, c = client
    A._memory().save("default", {"skills": {"c": "Python"}}, {}, [])
    d = c.post("/api/stories/suggest", json={}).get_json()
    assert d["suggestions"] == [] and d.get("note")


def test_coaching_surfaces_a_matching_saved_story(client):
    A, c = client
    _seed_profile(A)
    c.post("/api/stories", json={"title": "Reporting migration", "action": "I migrated the stack.",
                                 "competencies": ["Ownership"]})
    prep = c.post("/api/preps", json={"role": "Data Engineer", "company": "Acme"}).get_json()["prep"]
    # The model's 'delivered a result' question carries competency "Ownership".
    idx = next(i for i, q in enumerate(prep["questions"]) if q.get("competency") == "Ownership")
    d = c.post(f"/api/preps/{prep['id']}/coach",
               json={"index": idx, "answer": "I handled a hard migration once and it worked out."}).get_json()
    assert any(s["title"] == "Reporting migration" for s in (d.get("stories") or []))


def test_fake_llm_suggest_is_grounded_and_bounded():
    from llm.base import FakeLLM
    out = FakeLLM().suggest_star_stories({"experience": [{"org": "X", "roles": [
        {"title": "Dev", "bullets": ["Did a thing", "Did another thing"]}]}]})
    assert out["stories"] and len(out["stories"]) <= 6
    assert all(s["action"] and "competencies" in s for s in out["stories"])
    assert FakeLLM().suggest_star_stories({})["stories"] == []   # nothing to draw from
