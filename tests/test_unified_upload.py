"""One upload button: the app reads any document and routes it by CONTENT — a
transcript fills courses, a certificate becomes a credential, everything else is a
CV — so the person never picks the wrong button. Plus: a decline is acknowledged
once, never repeated on every later turn.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake, classify_document

JD = "Data Scientist - Acme - Chicago. Python, SQL, machine learning, statistics."

TRANSCRIPT = (
    "University Transcript\nStudent: Ana Cruz   GPA: 3.8\n"
    "Fall 2015\nCS 229 Machine Learning        A\n"
    "MATH 221 Introduction to Statistics   A-\n"
    "HIST 101 Ancient History       B\n"
    "Spring 2016\nCS 246 Data Mining             A\n"
)
CV_TEXT = ("Jane Doe\njane@example.com | (312) 555-0100\n"
           "Experience\nData Analyst at Acme, Chicago, 2020 - 2022\n"
           "Education\nB.S. Computer Science, MIT, 2019\nSkills\nPython, SQL")
CERT_TEXT = ("This is to certify that Kofi Gilbert has successfully completed the "
             "AWS Certified Cloud Practitioner exam.")


def _s(template_source, fake_llm, workdir):
    return WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


def test_classify_document_by_content_and_filename():
    assert classify_document("", "SSR_TSRPT.pdf") == "transcript"      # filename hint
    assert classify_document(TRANSCRIPT, "scan_20240101.pdf") == "transcript"  # content
    assert classify_document(CV_TEXT, "resume.docx") == "cv"
    assert classify_document(CERT_TEXT, "aws.pdf") == "certificate"


def test_upload_routes_transcript_to_courses_not_cv(template_source, fake_llm, workdir):
    """The reported bug: a transcript uploaded through the one button must be READ as
    a transcript (fills JD-relevant courses), never mis-handled as a CV."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.essentials["education"] = [{"school": "Northwestern", "degree": "M.S. in Statistics",
                                  "date": "2018", "courses": ""}]
    s.ingest_document(TRANSCRIPT, "SSR_TSRPT.pdf")
    courses = s.essentials["education"][0]["courses"]
    assert "Machine Learning" in courses          # transcript path ran (courses filled)
    assert "Ancient History" not in courses         # irrelevant course dropped


def test_upload_routes_certificate_to_credentials(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.ingest_document(CERT_TEXT, "aws.pdf")
    certs = " ".join(s.essentials.get("certifications", []))
    assert "Certified" in certs                     # recorded as a credential


def test_decline_is_acknowledged_once_not_every_turn(template_source, fake_llm, workdir):
    """Regression: 'we'll skip the blog' must not repeat on every subsequent ask —
    only what was JUST declined is handed to the phrasing model."""
    s = _s(template_source, fake_llm, workdir)
    captured = []
    s.llm.ask_missing = lambda missing, answered, role="", declined=None: (
        captured.append(list(declined or [])) or "Question?")
    s.essentials["declined"] = ["blog"]
    s._ask_missing(["your GitHub link"])
    s._ask_missing(["your GitHub link"])            # same standing decline, new turn
    assert captured[0] == ["blog"]                  # acknowledged the first time
    assert captured[1] == []                         # never re-acknowledged
