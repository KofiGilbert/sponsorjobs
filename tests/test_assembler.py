"""Acceptance tests for the CV assembler (CLAUDE.md §2 — pour PROFILE into shape).

Covers:
  * assemble_cv produces a one-page PDF,
  * it reuses the template's preamble verbatim (shape untouched),
  * the CV is filled from the profile (no template placeholder survives),
  * no bullet is ever truncated mid-sentence — including after one-page fitting.
"""

from __future__ import annotations

import pytest

from tailoring.assembler import assemble_cv, extract_preamble, render_cv
from tailoring.latex_template import LatexTemplate
from conftest import requires_latex

PROSE_SECTIONS = {"experience", "projects", "extracurricular"}


@pytest.fixture
def cv_profile() -> dict:
    """A compact real-person PROFILE whose bullets are complete sentences."""
    return {
        "identity": {
            "name": "Jordan Alvarez",
            "address": "12 Maple Ave, Chicago, IL 60601",
            "phone": "(312) 555-0101",
            "email": "jordan.alvarez@example.com",
            "linkedin": "https://linkedin.com/in/jordan-alvarez",
            "github": "https://github.com/jalvarez",
            "blog": "https://jordanalvarez.dev",
        },
        "education": [
            {
                "school": "Northwestern University",
                "location": "Evanston, IL",
                "degree": "Master of Science in Computer Science",
                "date": "June 2019",
                "courses": "Machine Learning, Distributed Systems, Cloud Architecture",
            },
        ],
        "skills": {
            "Computing": "Python, SQL, AWS, Azure, Docker, Kubernetes",
            "AI/ML": "LLMs, RAG, prompt engineering, MLOps",
        },
        "projects": [
            {
                "org": "Open RAG Toolkit",
                "location": "Chicago, IL",
                "title": "Creator",
                "dates": "2023 - Present",
                "bullets": [
                    "Built an open-source retrieval-augmented generation toolkit adopted by two teams.",
                ],
            },
        ],
        "experience": [
            {
                "org": "Acme Data",
                "location": "Chicago, IL",
                "roles": [
                    {
                        "title": "Senior Engineer",
                        "dates": "2019 - Present",
                        "bullets": [
                            "Delivered production machine learning services on AWS with monitoring.",
                            "Led a platform migration that cut deployment time by half.",
                        ],
                    },
                ],
            },
        ],
        "extracurricular": [
            {
                "title": "Meetup Organizer",
                "date": "2021 - Present",
                "bullets": [
                    "Run a local AI community hosting monthly technical talks.",
                ],
            },
        ],
        "interests": "Cycling, chess, photography",
    }


def _prose_bullets(tex: str) -> list[str]:
    """Every rendered prose bullet (Experience/Projects/Extracurricular)."""
    tmpl = LatexTemplate(tex)
    return [
        b.text for b in tmpl.bullets if b.section.lower() in PROSE_SECTIONS
    ]


def _assemble(template_source, profile, workdir, fake_llm, **kw):
    return assemble_cv(
        template_source, profile, "Senior ML Engineer role using AWS and RAG.",
        fake_llm, workdir, tailor=False, **kw,
    )


# --------------------------------------------------------------------------- #

@requires_latex
def test_assembled_cv_is_one_page(template_source, cv_profile, workdir, fake_llm):
    res = _assemble(template_source, cv_profile, workdir, fake_llm)
    assert res.ok, res.summary()
    assert res.compile.pages == 1
    assert res.pdf_path is not None and res.pdf_path.exists()


def test_preamble_is_reused_verbatim(template_source, cv_profile, workdir, fake_llm):
    res = _assemble(template_source, cv_profile, workdir, fake_llm)
    preamble = extract_preamble(template_source)
    # The shape (packages, geometry, colours, macros) is byte-identical.
    assert res.tex_source.startswith(preamble)
    assert preamble in template_source


def test_cv_is_filled_from_profile_not_template(
    template_source, cv_profile, workdir, fake_llm
):
    res = _assemble(template_source, cv_profile, workdir, fake_llm)
    tex = res.tex_source
    # The person's own content is present...
    assert "Jordan Alvarez" in tex
    assert "retrieval-augmented generation toolkit" in tex
    # ...and NONE of the template's placeholder clay survives.
    for placeholder in ("Jonathan", "Arc Group", "Crestline", "Zenith", "Kirkwood"):
        assert placeholder not in tex


def test_no_bullet_is_truncated_mid_sentence(
    template_source, cv_profile, workdir, fake_llm
):
    res = _assemble(template_source, cv_profile, workdir, fake_llm)
    bullets = _prose_bullets(res.tex_source)
    assert bullets  # sanity: we found prose bullets
    for b in bullets:
        assert b.rstrip().endswith((".", "!", "?")), f"truncated bullet: {b!r}"


@requires_latex
def test_overflow_is_fit_to_one_page_without_midsentence_cuts(
    template_source, cv_profile, workdir, fake_llm
):
    """A profile far too long for one page is fit down by dropping whole bullets,
    never by cutting a sentence — so every surviving bullet stays complete."""
    import copy

    big = copy.deepcopy(cv_profile)
    # Pile on many long, complete-sentence bullets across several roles.
    long_bullet = (
        "Designed and shipped a scalable machine learning platform on AWS and "
        "Azure serving hundreds of teams with governed data pipelines."
    )
    big["experience"] = [
        {
            "org": f"Company {n}",
            "location": "Chicago, IL",
            "roles": [
                {
                    "title": "Principal Engineer",
                    "dates": "2015 - 2020",
                    "bullets": [long_bullet, long_bullet, long_bullet, long_bullet],
                }
            ],
        }
        for n in range(6)
    ]

    res = _assemble(template_source, big, workdir, fake_llm, jobname="overflow")
    assert res.compile.pages == 1, res.summary()
    # 6 roles x 4 bullets cannot fit; the JD-driven selection keeps at most 4 roles and 3
    # bullets each, so the page is fit by dropping WHOLE bullets/roles, never by cutting.
    kept = sum(len(r.get("bullets") or []) for e in res.profile_used["experience"]
               for r in (e.get("roles") or [e]))
    assert 0 < kept < 24, kept
    for b in _prose_bullets(res.tex_source):
        assert b.rstrip().endswith((".", "!", "?")), f"truncated bullet: {b!r}"


def test_experienced_template_manifest_and_order(cv_profile, workdir, fake_llm):
    """The experienced-hire template: summary leads, experience precedes education,
    projects stay in (the Amazon case-study layout), extracurricular survives."""
    import json
    from pathlib import Path

    m = json.loads(Path("config/templates/experienced.json").read_text(encoding="utf-8"))
    assert Path(m["tex"]).exists()
    s = m["sections"]
    assert s.index("summary") < s.index("experience") < s.index("education")
    assert "projects" in s and "skills" in s
    # The insider layout deliberately ends at skills: no extracurricular/interests.
    assert "extracurricular" not in s and "interests" not in s

    tex_src = Path(m["tex"]).read_text(encoding="utf-8")
    prof = dict(cv_profile)
    prof["summary"] = "Operations leader with real scale numbers."
    prof["extracurricular"] = [{"title": "Team Captain, Intramural Soccer",
                                "date": "", "bullets": []}]
    res = _assemble(tex_src, prof, workdir, fake_llm,
                    jobname="experienced", sections=m["sections"])
    body = res.tex_source
    order = [body.find("Summary"), body.find("Experience"),
             body.find("Education"), body.find("Projects")]
    assert all(i >= 0 for i in order), "a section is missing from the rendered CV"
    assert order == sorted(order), f"sections out of order: {order}"
    # Data present on the profile must NOT render when the template omits the section.
    assert "Intramural Soccer" not in body


def test_project_renders_name_when_no_org():
    """A project carries its heading in `name` (experience uses `org`). Regression: the heading
    was blank whenever only `name` was set, so the project showed as a bare link with no title."""
    from tailoring.assembler import _projects
    tex = _projects({"projects": [
        {"name": "SuperCash Games", "link": "supercashgames.com",
         "bullets": ["Built a live real-money gaming platform end to end."]}]})
    assert "SuperCash Games" in tex                       # the title renders, not just the link
    assert "Built a live real-money gaming platform" in tex


def test_experience_still_renders_org():
    """The name fallback must not regress experience, which keys its heading on `org`."""
    from tailoring.assembler import _experience
    tex = _experience({"experience": [
        {"org": "Stanbic Bank Ghana", "title": "Senior Product Manager", "dates": "2018 - 2024",
         "bullets": ["Led enterprise product delivery."]}]})
    assert "Stanbic Bank Ghana" in tex


@requires_latex
def test_a_thin_profile_is_not_trimmed_into_a_half_page(template_source, fake_llm, workdir):
    """Kofi's first real run (2026-10-07): 5 roles and 9 bullets came out at about half a page
    because role selection had cut a role and bullets BEFORE anyone looked at the page. When
    the page is short and selection dropped material, the builder must bring the person's own
    material back rather than ship the hole."""
    from tailoring.assembler import _count_material, assemble_cv
    roles = []
    for i in range(5):
        roles.append({"org": f"Company {i}", "title": f"Analyst {i}", "location": "Accra, GH",
                      "start": f"Jan 20{10 + i}", "end": f"Dec 20{10 + i}",
                      "bullets": [f"Built reporting pipelines in SQL for team {i}, cutting the monthly "
                                  f"close from ten days to four and saving {i + 1}00 analyst hours a year."]
                                 + ([f"Led a cross-functional project with {i + 2} teams that shipped a "
                                     "customer dashboard adopted by 40 managers within a quarter.",
                                     "Mentored two junior analysts, both promoted within the year."]
                                    if i in (1, 3) else [])})
    profile = {"identity": {"name": "Test Person", "email": "t@example.com", "phone": "555-0100",
                            "location": "Chicago, IL"},
               "education": [{"school": "DePaul University", "degree": "MBA, Business Analytics",
                              "start": "Sep 2024", "end": "Dec 2025", "location": "Chicago, IL"}],
               "experience": roles, "skills": {"Data": ["SQL", "Tableau", "Python"]}, "projects": []}
    assert _count_material(profile) == (5, 9)
    res = assemble_cv(template_source, profile, "Business Analyst. SQL, Tableau, dashboards.",
                      fake_llm, workdir, jobname="thin", tailor=False)
    assert res.ok
    # Everything the person gave is on the page: no role and no bullet silently dropped.
    assert _count_material(res.profile_used) == (5, 9)
    assert res.fill_ratio is not None
