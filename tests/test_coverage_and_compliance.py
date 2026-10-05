"""§10: coverage report, no keyword stuffing, and no RED-lane code.

Covers:
  * "Each tailored CV comes with a coverage report showing which JD key terms
     are present and which are missing; supported JD terms appear in the CV
     using the JD's own phrasing, with no hidden text or unsupported keyword
     stuffing."
  * "No RED-lane code exists anywhere." (§6/§7 — enforced now as a guardrail so
     it can never creep in during later phases.)
"""

from __future__ import annotations

import re
from pathlib import Path

from tailoring.keywords import build_coverage_report, extract_jd_terms, term_present
from tailoring.tailor import tailor_resume
from conftest import requires_latex, ROOT


# -- coverage report ---------------------------------------------------- #

def test_coverage_ignores_job_posting_boilerplate():
    """The coverage term extractor must not treat scraped job-board boilerplate
    ('Company', 'Remote', 'Apply', 'Full-time', section verbs) as ATS key terms —
    that noise was dragging the coverage percentage down and cluttering 'Missing'."""
    jd = ("Company logo for, Paradigm.\nParadigm\nPrincipal AI Architect\n"
          "Remote - Full-time - Engineering\nAbout us. Apply now.\n"
          "You will Own the roadmap, Drive delivery, and Partner with stakeholders.\n"
          "Requirements: Python, SQL, machine learning, knowledge graph.")
    terms = {t.lower() for t in extract_jd_terms(jd)}
    for junk in ("company", "remote", "apply", "about", "full-time", "engineering",
                 "own", "drive", "partner"):
        assert junk not in terms, f"boilerplate leaked into coverage: {junk}"
    # real skills still captured
    assert "python" in terms and "sql" in terms

@requires_latex
def test_coverage_report_lists_present_and_missing(
    template_source, sample_profile, jd_swe, fake_llm, workdir
):
    res = tailor_resume(
        template_source, sample_profile, jd_swe, fake_llm, workdir,
        jobname="cov",
    )
    cov = res.coverage
    d = cov.to_dict()
    assert set(d) == {"present", "missing_supported", "missing_unsupported",
                      "coverage_ratio"}

    # Supported JD terms the profile has (Python, SQL, AWS...) should be present
    # in the tailored CV, in the JD's own phrasing.
    present_blob = "\n".join(cov.present)
    assert term_present("Python", present_blob)
    assert term_present("AWS", present_blob)

    # Terms the JD wants but the profile lacks are reported as gaps, not faked.
    gaps = cov.missing_unsupported
    assert term_present("Kubernetes", "\n".join(gaps))
    assert term_present("Kafka", "\n".join(gaps))

    # The report is always attached and renders human-readably.
    assert "Coverage:" in cov.render()


def test_coverage_report_is_pure_and_deterministic(sample_profile):
    jd = "We need Python, SQL, and Kubernetes with machine learning."
    cv_text = "Built systems in Python and SQL using machine learning."
    r1 = build_coverage_report(jd, cv_text, sample_profile)
    r2 = build_coverage_report(jd, cv_text, sample_profile)
    assert r1.to_dict() == r2.to_dict()
    assert term_present("Python", "\n".join(r1.present))
    # Kubernetes is neither in the CV text nor the profile -> unsupported gap.
    assert "Kubernetes" in r1.missing_unsupported


@requires_latex
def test_no_hidden_text_or_keyword_stuffing(
    template_source, sample_profile, jd_swe, fake_llm, workdir
):
    """The tailored .tex must not smuggle in hidden/white text or a keyword wall."""
    res = tailor_resume(
        template_source, sample_profile, jd_swe, fake_llm, workdir,
        jobname="stuff",
    )
    src = res.tex_source.lower()
    forbidden = [
        r"\textcolor{white}", r"\color{white}",
        r"fontsize{0", r"fontsize{1pt", r"\phantom", r"\hspace{-",
        r"\hidden",
    ]
    for token in forbidden:
        assert token not in src, f"hidden-text pattern found: {token}"

    # Unsupported terms must NOT be injected into the document body.
    body = res.tex_source
    assert not term_present("Kubernetes", body)
    assert not term_present("Kafka", body)


# -- compliance guardrail: no RED-lane code ----------------------------- #

def test_no_red_lane_code_exists():
    """Static scan: forbidden automation must not exist in the product packages.

    CLAUDE.md §7 forbids logged-in LinkedIn automation, stealth/anti-detection,
    captcha interception, and rotating proxies to dodge blocks. This test fails
    loudly if any such code is introduced in a later phase.
    """
    # Patterns chosen to catch actual automation, not incidental mentions.
    red_patterns = [
        r"undetected[_-]?chromedriver",
        r"selenium[_-]?stealth",
        r"playwright[_-]?stealth",
        r"puppeteer[_-]?extra[_-]?plugin[_-]?stealth",
        r"2captcha",
        r"anticaptcha",
        r"captcha[_ ]?solv",
        r"rotating[_ ]?prox",
        r"proxy[_ ]?rotat",
        r"linkedin.*(auto[_ ]?apply|autoapply|auto[_ ]?connect)",
        r"easy[_ ]?apply[_ ]?bot",
    ]
    packages = ["tailoring", "intake", "llm"]
    offenders: list[str] = []
    for pkg in packages:
        for py in (ROOT / pkg).rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="replace").lower()
            for pat in red_patterns:
                if re.search(pat, text):
                    offenders.append(f"{py.name}: /{pat}/")
    assert not offenders, "RED-lane code detected:\n" + "\n".join(offenders)
