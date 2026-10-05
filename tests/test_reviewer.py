"""The honest pre-send review (feature #2): a deterministic last check on the tailored resume.
Its headline job is the FABRICATION GUARD -- flag any skill shown on the resume that the person's
own profile cannot back, because that is the claim that unravels in an interview. Around it: easy
wins, honest gaps, and one-page fit. No model call, so it is exact and testable."""
from __future__ import annotations

from tailoring.reviewer import review_resume

PROFILE = {
    "skills": {"Computing": "Python, SQL, AWS"},
    "experience": [{"org": "DataCo", "roles": [{"title": "Engineer",
                    "bullets": ["Built ETL pipelines in Python and SQL on AWS"]}]}],
}


def _check(review, cid):
    return next(c for c in review["checks"] if c["id"] == cid)


def test_clean_honest_one_page_is_ready():
    cov = {"present": ["Python", "SQL", "AWS"], "missing_supported": [], "missing": []}
    r = review_resume(PROFILE, cov, "clean")
    assert r["verdict"] == "ready"
    assert r["unverified_count"] == 0
    assert _check(r, "honesty")["level"] == "pass"
    assert _check(r, "length")["level"] == "pass"


def test_unbacked_claim_is_flagged_and_blocks():
    # "Kubernetes" is shown on the resume (present) but nowhere in the profile -> the tailor drifted.
    cov = {"present": ["Python", "SQL", "Kubernetes"], "missing_supported": [], "missing": []}
    r = review_resume(PROFILE, cov, "clean")
    assert r["verdict"] == "check"
    assert r["unverified_count"] == 1
    honesty = _check(r, "honesty")
    assert honesty["level"] == "flag"
    assert honesty["items"] == ["Kubernetes"]


def test_honesty_flag_wins_over_layout():
    # An unbacked claim AND an overflow page -> honesty is the verdict (it is what costs an interview).
    cov = {"present": ["Kubernetes"], "missing_supported": [], "missing": []}
    r = review_resume(PROFILE, cov, "overflow")
    assert r["verdict"] == "check"


def test_overflow_without_fabrication_is_review():
    cov = {"present": ["Python", "SQL"], "missing_supported": [], "missing": []}
    r = review_resume(PROFILE, cov, "overflow")
    assert r["verdict"] == "review"
    assert _check(r, "length")["level"] == "warn"


def test_opportunities_and_gaps_surface_as_guidance():
    cov = {"present": ["Python"], "missing_supported": ["SQL"], "missing": ["Kubernetes", "Go"]}
    r = review_resume(PROFILE, cov, "clean")
    assert _check(r, "opportunities")["items"] == ["SQL"]
    assert _check(r, "gaps")["items"] == ["Kubernetes", "Go"]
    # Missing-from-profile skills are NOT a fabrication -- they are not on the resume.
    assert r["verdict"] == "ready"


def test_accepts_missing_unsupported_key_alias():
    # Coverage from CoverageReport.to_dict() names it "missing_unsupported"; the session dict uses
    # "missing". Both must work.
    cov = {"present": ["Python"], "missing_supported": [], "missing_unsupported": ["Rust"]}
    r = review_resume(PROFILE, cov, "clean")
    assert _check(r, "gaps")["items"] == ["Rust"]


def test_empty_coverage_is_ready():
    r = review_resume(PROFILE, {}, "clean")
    assert r["verdict"] == "ready"
    assert r["unverified_count"] == 0
