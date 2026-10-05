"""Cover-letter craft check (feature #6): a deterministic honest review of a drafted letter --
a real hook (not a template opener), names the company, sane length, and the fabrication guard
(no JD skill claimed that the profile cannot back). No model call, so it is exact and testable."""
from __future__ import annotations

from drafting.cover_review import review_cover_letter

PROFILE = {
    "identity": {"name": "Sam Rivera"},
    "skills": {"Computing": "Python, SQL, AWS"},
    "experience": [{"org": "DataCo", "roles": [{"title": "Engineer",
                    "bullets": ["Built ETL pipelines in Python and SQL on AWS"]}]}],
}
JD = "We need Python, SQL, and Kubernetes to build data pipelines on AWS."

# A crafted letter: real hook, names the company, good length, only backed skills.
GOOD = ("Dear Hiring Team,\n\n"
        "Building data pipelines that survive real traffic is the work I care about most, and it "
        "is exactly what this role at Acme asks for. " + ("Over three years I have shipped ETL in "
        "Python and SQL on AWS, cutting nightly batch times and keeping data teams unblocked. ") * 6
        + "\n\nI would love to bring that to Acme.\n\nSincerely,\nSam Rivera")


def _check(review, cid):
    return next((c for c in review["checks"] if c["id"] == cid), None)


def test_crafted_letter_is_ready():
    r = review_cover_letter(GOOD, company="Acme", profile=PROFILE, jd_text=JD)
    assert r["verdict"] == "ready"
    assert _check(r, "hook")["level"] == "pass"
    assert _check(r, "personalized")["level"] == "pass"
    assert _check(r, "honesty")["level"] == "pass"


def test_template_opener_is_flagged():
    letter = ("Dear Hiring Team,\n\nI am writing to apply for the Engineer position at Acme. " +
              "I have used Python and SQL on AWS to deliver results. " * 8 + "\n\nSincerely,\nSam")
    r = review_cover_letter(letter, company="Acme", profile=PROFILE, jd_text=JD)
    assert _check(r, "hook")["level"] == "flag"
    assert r["verdict"] in {"review", "check"}


def test_not_naming_the_company_is_flagged():
    letter = ("Dear Hiring Team,\n\nData pipelines are my craft. " +
              "I have shipped Python and SQL on AWS for years. " * 8 + "\n\nSincerely,\nSam")
    r = review_cover_letter(letter, company="Acme", profile=PROFILE, jd_text=JD)
    assert _check(r, "personalized")["level"] == "flag"


def test_unbacked_skill_claim_is_the_load_bearing_flag():
    # The letter claims Kubernetes (a JD skill) the profile never shows -> honesty flag + "check".
    letter = ("Dear Hiring Team,\n\nScaling systems is my craft, and it is what Acme needs. " +
              "I have deep hands-on Kubernetes experience running production clusters. " * 6 +
              "\n\nSincerely,\nSam")
    r = review_cover_letter(letter, company="Acme", profile=PROFILE, jd_text=JD)
    h = _check(r, "honesty")
    assert h["level"] == "flag" and any("kubernetes" in t.lower() for t in h["items"])
    assert r["verdict"] == "check"


def test_length_flags_too_short():
    r = review_cover_letter("Dear Acme,\n\nI build data pipelines in Python.\n\nSam",
                            company="Acme", profile=PROFILE, jd_text=JD)
    assert _check(r, "length")["level"] == "warn"


def test_honesty_check_skipped_without_jd():
    # Without a JD we cannot judge which nouns are claimed job skills, so the guard is omitted.
    r = review_cover_letter(GOOD, company="Acme", profile=PROFILE)
    assert _check(r, "honesty") is None
