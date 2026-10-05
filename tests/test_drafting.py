"""Drafting: cover letters + screening answers from PROFILE + JD (CLAUDE.md §5).

Grounded in the person's real material, nothing fabricated, one answer per question in
order. Driven with the deterministic FakeLLM — no network.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Machine Learning Engineer - Synechron. Python, machine learning, NLP, SQL, Azure."

PROFILE = {
    "identity": {"name": "Maya Rodriguez", "email": "maya@example.com"},
    "skills": {"Computing": "Python, SQL, PyTorch", "Knowledge": "Machine Learning, NLP"},
    "experience": [{"org": "Continental Trust Bank", "title": "Principal Engineer",
                    "dates": "2021 - Present", "location": "Chicago, IL",
                    "bullets": ["Built ML pipelines."]}],
}


# -- LLM backend (FakeLLM) --------------------------------------------- #
def test_fake_cover_letter_is_grounded_and_invents_nothing():
    from llm.base import FakeLLM
    letter = FakeLLM().draft_cover_letter(JD, PROFILE, role="ML Engineer", company="Synechron")
    assert "Maya Rodriguez" in letter                       # signs with the real name
    assert "ML Engineer" in letter and "Synechron" in letter
    assert any(s in letter for s in ("Python", "SQL", "Machine Learning"))  # a real skill
    assert "%" not in letter                                # no fabricated metric


def test_fake_screening_one_answer_per_question_in_order():
    from llm.base import FakeLLM
    qs = ["Why do you want to work here?", "What is your greatest strength?",
          "Describe a hard problem you solved."]
    ans = FakeLLM().answer_screening_questions(JD, PROFILE, qs)
    assert len(ans) == len(qs) and all(a.strip() for a in ans)
    assert any(s in " ".join(ans) for s in ("Python", "SQL", "Machine Learning"))


# -- orchestrator ------------------------------------------------------ #
def test_drafter_cover_letter_reports_grounding():
    from drafting import cover_letter
    from llm.base import FakeLLM
    out = cover_letter(JD, PROFILE, FakeLLM(), role="ML Engineer", company="Synechron")
    assert out["cover_letter"].strip() and out["word_count"] > 20
    # Skills present in BOTH the JD and the profile are reported (the grounding view).
    assert set(out["skills_used"]) & {"Python", "SQL", "NLP", "Machine Learning"}


def test_drafter_screening_pairs_and_drops_blanks():
    from drafting import screening_answers
    from llm.base import FakeLLM
    out = screening_answers(JD, PROFILE, FakeLLM(), ["Why us?", "  ", ""])
    assert [p["question"] for p in out["answers"]] == ["Why us?"]   # blanks dropped
    assert out["answers"][0]["answer"].strip()


# -- session integration ----------------------------------------------- #
def _session(template_source, fake_llm, workdir):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir)
    s.profile = PROFILE
    return s


def test_session_cover_letter_uses_profile_and_remembers_it(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    out = s.cover_letter()
    assert "Maya Rodriguez" in out["cover_letter"]
    # Remembered so accept() can bundle it into the application package.
    assert s.last_cover_letter == out["cover_letter"]


def test_drafting_endpoints(template_source, fake_llm, workdir):
    import ui.app as app
    s = _session(template_source, fake_llm, workdir)
    app._SESSION["s"] = s
    try:
        client = app.app.test_client()
        r = client.post("/api/session/cover_letter", json={"tone": "warm"})
        assert r.status_code == 200 and "Maya Rodriguez" in r.get_json()["cover_letter"]
        r2 = client.post("/api/session/screening",
                         json={"questions": ["Why do you want this role?"]})
        body = r2.get_json()
        assert len(body["answers"]) == 1 and body["answers"][0]["answer"].strip()
    finally:
        app._SESSION.pop("s", None)


# -- extension draft endpoint (assisted apply fills essay questions) ---- #
# The extension sends this header via its background worker; the endpoint is locked to it.
EXT = {"X-Tailor-Extension": "1"}


def test_profile_draft_endpoint_answers_and_cover_letter(monkeypatch):
    """The extension posts detected essay questions + JD; the app drafts grounded
    answers (and optionally a cover letter) from the SAVED profile. Extension-only."""
    import ui.app as app
    from llm.base import FakeLLM
    monkeypatch.setattr(app, "_saved_full_profile", lambda: PROFILE)
    monkeypatch.setattr(app, "_make_llm", lambda: FakeLLM())
    r = app.app.test_client().post("/api/profile/draft", headers=EXT, json={
        "jd": JD, "questions": ["Why do you want this role?", "Describe a challenge."],
        "cover_letter": True})
    assert r.status_code == 200
    # Locked down: no CORS, so a web page can't invoke the model on the user's key.
    assert r.headers.get("Access-Control-Allow-Origin") is None
    body = r.get_json()
    assert body["loaded"] is True
    assert len(body["answers"]) == 2 and all(a["answer"].strip() for a in body["answers"])
    assert body["cover_letter"] and "Maya Rodriguez" in body["cover_letter"]


def test_profile_draft_endpoint_without_profile_is_safe(monkeypatch):
    import ui.app as app
    from llm.base import FakeLLM
    monkeypatch.setattr(app, "_saved_full_profile", lambda: {})
    monkeypatch.setattr(app, "_make_llm", lambda: FakeLLM())
    body = app.app.test_client().post(
        "/api/profile/draft", headers=EXT,
        json={"jd": JD, "questions": ["Why?"]}).get_json()
    assert body["loaded"] is False and body["answers"] == [] and body["cover_letter"] is None


# -- referral message (privacy-preserving: draft locally, the person finds + sends) ------
def test_fake_referral_message_is_grounded_and_personal():
    from llm.base import FakeLLM
    msg = FakeLLM().draft_referral_message(
        "ML Engineer", "Synechron", PROFILE, jd_text=JD,
        recipient_name="Sam", relationship="we both studied at State")
    assert "Maya Rodriguez" in msg                          # signs with the real name
    assert "ML Engineer" in msg and "Synechron" in msg      # names the specific role + company
    assert "Sam" in msg                                     # greets the recipient
    assert "we both studied at State" in msg                # uses a genuine tie when given
    assert any(s in msg for s in ("Python", "SQL", "Machine Learning"))   # a real skill
    assert "%" not in msg                                   # no fabricated metric


def test_drafter_referral_reports_grounding():
    from drafting import referral_message
    from llm.base import FakeLLM
    out = referral_message("ML Engineer", "Synechron", PROFILE, FakeLLM(), jd_text=JD)
    assert out["message"].strip() and out["word_count"] > 15
    assert set(out["skills_used"]) & {"Python", "SQL", "NLP", "Machine Learning"}


def test_profile_referral_endpoint_drafts_from_saved_profile(monkeypatch):
    """The extension posts the role + company (+ JD); the app drafts a grounded outreach
    message from the SAVED profile. Extension-only, locked to the model like every other
    tailoring call, and never auto-sent: the person picks who to contact and sends it."""
    import ui.app as app
    from llm.base import FakeLLM
    monkeypatch.setattr(app, "_saved_full_profile", lambda: PROFILE)
    monkeypatch.setattr(app, "_make_llm", lambda: FakeLLM())
    r = app.app.test_client().post("/api/profile/referral", headers=EXT, json={
        "role": "ML Engineer", "company": "Synechron", "jd": JD})
    assert r.status_code == 200
    # Locked down: no CORS, so a web page can't invoke the model on the user's key.
    assert r.headers.get("Access-Control-Allow-Origin") is None
    body = r.get_json()
    assert body["loaded"] is True
    assert body["message"] and "Maya Rodriguez" in body["message"] and "Synechron" in body["message"]


def test_profile_referral_endpoint_without_profile_is_safe(monkeypatch):
    import ui.app as app
    from llm.base import FakeLLM
    monkeypatch.setattr(app, "_saved_full_profile", lambda: {})
    monkeypatch.setattr(app, "_make_llm", lambda: FakeLLM())
    body = app.app.test_client().post(
        "/api/profile/referral", headers=EXT,
        json={"role": "ML Engineer", "company": "Synechron"}).get_json()
    assert body["loaded"] is False and body["message"] is None
