"""Knock-out screening questions are marked for the person (drafting/drafter.py), and a
rejection in the inbox is tagged and never gets a drafted reply (inbox/)."""

from __future__ import annotations

from drafting.drafter import knockout_kind, screening_answers
from inbox.detect import application_status
from inbox.service import scan_inbox


class _LLM:
    def answer_screening_questions(self, jd, profile, questions):
        return ["yes"] * len(questions)

    def draft_email_reply(self, *a, **k):
        return "Thanks, happy to talk."


def test_knockout_questions_are_recognised_by_wording():
    assert knockout_kind("Are you legally authorized to work in the United States?") == "work_authorization"
    assert knockout_kind("Will you now or in the future require sponsorship?") == "sponsorship"
    assert knockout_kind("Do you have 5+ years of Python experience?") == "years_experience"
    assert knockout_kind("What are your salary expectations?") == "salary"
    assert knockout_kind("Do you hold a bachelor's degree?") == "degree"
    assert knockout_kind("Why do you want to work here?") is None


def test_screening_answers_carry_the_knockout_mark():
    out = screening_answers("Python role", {"skills": {"Languages": "Python"}}, _LLM(),
                            ["Why us?", "Are you authorized to work in the US?"])
    assert [a["knockout"] for a in out["answers"]] == [None, "work_authorization"]
    assert [k["question"] for k in out["knockouts"]] == ["Are you authorized to work in the US?"]


def test_rejection_wording_is_tagged_and_gets_no_draft():
    assert application_status({"subject": "Your application", "body":
                               "Unfortunately we have decided to move forward with other candidates."}) == "rejected"
    assert application_status({"subject": "Next steps", "body": "We'd like to schedule a call."}) == "advanced"
    assert application_status({"subject": "Hi", "body": "Just checking in."}) == ""

    msgs = [{"id": "1", "from": "Ann <ann@acme.com>", "subject": "Your application to Acme",
             "body": "Thank you for your application. Unfortunately we will not be moving forward.",
             "date": "2026-09-01"},
            {"id": "2", "from": "Bob <bob@acme.com>", "subject": "Next steps at Acme",
             "body": "We'd love to schedule a call about your application.", "date": "2026-09-02"}]
    out = scan_inbox(msgs, profile={}, llm=_LLM())
    by_id = {r["id"]: r for r in out["recruiters"]}
    assert by_id["1"]["status"] == "rejected" and by_id["1"]["reply"] == ""
    assert by_id["2"]["status"] == "advanced" and by_id["2"]["reply"]
