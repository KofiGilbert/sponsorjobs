"""A template may rename its section headings and add a Licenses & Certifications section
(2026-10-09). The body is one single column for every field; the headings are what make it
read as a research, nursing, or consulting resume."""
from intake.template_manifest import headings
from tailoring.assembler import extract_preamble, render_cv

MIN = "\\documentclass{article}\n\\begin{document}"
PROFILE = {
    "identity": {"name": "Sam Rivera"},
    "education": [{"school": "State University", "degree": "Ph.D. in Chemistry", "date": "2024"}],
    "experience": [{"org": "State University", "roles": [{"title": "Graduate Researcher",
                                                            "dates": "2019 - 2024",
                                                            "bullets": ["Ran the lab."]}]}],
    "skills": {"Methods": "NMR, HPLC"},
    "extracurricular": [{"title": "Teaching Assistant", "bullets": ["Taught 60 students."]}],
    "interests": "Chess",
    "certifications": ["Registered Nurse (RN), State Board, 2024", "BLS, American Heart Association"],
}


def test_headings_rename_sections_without_changing_the_body():
    pre = extract_preamble(MIN)
    secs = ["education", "experience", "skills", "extracurricular", "interests"]
    plain = render_cv(pre, PROFILE, sections=secs)
    renamed = render_cv(pre, PROFILE, sections=secs, headings={
        "experience": "Research Experience", "skills": "Technical Skills",
        "extracurricular": "Teaching \\& Service", "additional": "Awards \\& Interests"})
    assert "\\large Experience}" in plain and "\\large Research Experience}" in renamed
    assert "\\large Technical Skills}" in renamed and "\\large Skills}" not in renamed
    assert "\\large Teaching \\& Service}" in renamed
    assert "\\large Awards \\& Interests}" in renamed
    # Same content either way: only the headings moved.
    assert "Ran the lab." in renamed and "Taught 60 students." in renamed


def test_headings_do_not_leak_between_renders():
    pre = extract_preamble(MIN)
    render_cv(pre, PROFILE, sections=["experience"], headings={"experience": "Clinical Experience"})
    again = render_cv(pre, PROFILE, sections=["experience"])
    assert "\\large Experience}" in again and "Clinical" not in again


def test_certifications_section_renders_one_bullet_per_credential():
    pre = extract_preamble(MIN)
    tex = render_cv(pre, PROFILE, sections=["education", "certifications", "experience"])
    assert "\\large Licenses \\& Certifications}" in tex
    assert "Registered Nurse (RN), State Board, 2024" in tex
    assert "BLS, American Heart Association" in tex
    # Absent from a template that does not list it, and silent when the person has none.
    assert "Certifications" not in render_cv(pre, PROFILE, sections=["education", "experience"])
    none = dict(PROFILE, certifications=[])
    assert "Certifications" not in render_cv(pre, none, sections=["education", "certifications"])


def test_manifest_headings_helper_is_total():
    assert headings({}) == {}
    assert headings({"headings": "nope"}) == {}
    assert headings({"headings": {"skills": "Technical Skills", "x": "  "}}) == {"skills": "Technical Skills"}
