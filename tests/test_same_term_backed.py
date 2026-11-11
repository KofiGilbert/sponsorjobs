"""An abbreviation and its long form are one supported term (Kofi's Google rebuild, 2026-11-10).

The pre-send review flagged "Artificial Intelligence" as "A claim on your resume that your
profile does not back", though his profile is full of AI: an AI Senior Business Intelligence
Manager role, an "AI-assisted data pipeline", Claude and OpenAI in his skills. The review matched
the exact phrase only; now any spelling of the term (tailoring/keywords.spellings) backs it."""

from tailoring.keywords import term_backed
from tailoring.reviewer import review_resume

PROFILE = {
    "experience": [{"org": "BlackOrigin Inc.", "roles": [{
        "title": "AI Senior Business Intelligence Manager",
        "bullets": ["Built an AI-assisted data pipeline that processed 280,000+ job postings a day."]}]}],
    "skills": {"AI & Data": "Claude, OpenAI, Python, SQL, machine learning"},
}


def test_the_long_form_on_the_page_is_backed_by_the_abbreviation_in_the_profile():
    cov = {"present": ["Artificial Intelligence", "Python", "ML"], "missing_supported": [], "missing": []}
    r = review_resume(PROFILE, cov, "clean", 0.96)
    honesty = next(c for c in r["checks"] if c["id"] == "honesty")
    assert honesty["level"] == "pass" and honesty["items"] == []
    assert r["verdict"] == "ready"


def test_each_spelling_backs_the_other_and_nothing_else():
    assert term_backed("Artificial Intelligence", "AI-assisted analytics")
    assert term_backed("AI", "research in artificial intelligence")
    assert term_backed("NLP", "natural language processing")
    assert term_backed("Large Language Models", "Built tools on LLMs")
    # "Business Intelligence" is not "Artificial Intelligence", and an ad's own pair counts.
    assert not term_backed("Artificial Intelligence", "Business Intelligence dashboards")
    assert term_backed("Total Cost of Ownership", "cut TCO by a third",
                       jd_text="Own the Total Cost of Ownership (TCO) model.")


def test_a_term_the_profile_lacks_is_still_flagged():
    cov = {"present": ["Kubernetes"], "missing_supported": [], "missing": []}
    honesty = next(c for c in review_resume(PROFILE, cov)["checks"] if c["id"] == "honesty")
    assert honesty["level"] == "flag" and honesty["items"] == ["Kubernetes"]
