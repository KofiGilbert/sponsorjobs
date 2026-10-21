"""Design pass 15 (2026-10-20): stated skills count toward every job match, thin ads can't score
100% on a mismatched title, and dismissing a saved job takes it off the Tracker too."""
from __future__ import annotations

import tempfile

import ui.app as app
from notify.alerts import match_percent, profile_targets, title_fit
from sourcing.watchlist import Watchlist
from tailoring.keywords import with_stated_skills

# A product/AI person whose skills were typed under "Stated skills" (essentials.skills_input),
# not into profile.skills: the case that showed "Weak fit, 0 of 15 skills".
PROFILE = {
    "identity": {"name": "Ada Example"},
    "skills": {},
    "experience": [{"org": "Acme AI", "roles": [{"title": "Product Manager",
                    "bullets": ["Led the roadmap for an AI assistant"]}]},
                   {"org": "Beta", "roles": [{"title": "Associate Product Manager",
                    "bullets": ["Shipped onboarding"]}]}],
}
ESS = {"skills_input": ["Python", "SQL", "A/B testing", "Figma", "Jira", "Tableau",
                        "machine learning", "product strategy", "roadmapping", "Experimentation"]}

PM_JD = ("Product Manager, AI. Requirements: experience with Python, SQL, A/B testing, Figma, "
         "Jira, Tableau and machine learning. You will own product strategy and roadmapping. "
         "Nice to have: Experimentation platforms, Looker, dbt.")
THIN_JD = "Cybersecurity Architect. You will design secure systems. Python and SQL required."


def _match(jd, title, snap):
    orig = app._memory

    class _Mem:
        def load(self, _name):
            return snap
    app._memory = lambda: _Mem()
    try:
        return app._job_match(jd, title)
    finally:
        app._memory = orig


# --- 7. stated skills count ----------------------------------------------------------------------

def test_with_stated_skills_merges_without_mutating():
    out = with_stated_skills(PROFILE, ESS)
    assert "Python" in out["skills"]["Stated"]
    assert PROFILE["skills"] == {}                      # input untouched
    assert with_stated_skills({"skills": ["Go"]}, {"skills_input": ["Rust"]})["skills"] == ["Go", "Rust"]
    assert with_stated_skills(PROFILE, {}) == PROFILE


def test_job_match_counts_stated_skills():
    without = _match(PM_JD, "Product Manager, AI", {"profile": PROFILE})
    with_ = _match(PM_JD, "Product Manager, AI", {"profile": PROFILE, "essentials": ESS})
    assert without["covered"] <= 1
    assert with_["covered"] >= 7
    assert with_["verdict"] in {"good", "strong"}
    assert with_["score"] >= 70


def test_stated_skills_reach_the_prescore_targets():
    t = profile_targets(app._match_profile({"profile": PROFILE, "essentials": ESS}))
    assert "python" in t["skill_words"] and "figma" in t["skill_words"]


def test_extension_style_essentials_only_profile_matches():
    m = _match(PM_JD, "Product Manager", {"essentials": {**ESS, "experience": PROFILE["experience"]}})
    assert m and m["covered"] >= 7


# --- 15. thin ads and mismatched titles ----------------------------------------------------------

def test_thin_jd_with_mismatched_title_drops_well_below_60():
    m = _match(THIN_JD, "Cybersecurity Architect", {"profile": PROFILE, "essentials": ESS})
    assert m["total"] < 8 and m["covered"] == m["total"]      # every named skill "matches"
    assert m["title_fit"] is not None and m["title_fit"] < 0.2
    assert match_percent(m) < 45
    assert m["verdict"] == "weak"


def test_thin_jd_with_matching_title_stays_high():
    jd = "Product Manager. Python and SQL required."
    m = _match(jd, "Product Manager", {"profile": PROFILE, "essentials": ESS})
    assert match_percent(m) >= 85


def test_rich_jd_good_fit_stays_high():
    m = _match(PM_JD, "Product Manager, AI", {"profile": PROFILE, "essentials": ESS})
    assert match_percent(m) >= 75


def test_senior_title_lowers_title_fit_for_a_non_senior_profile():
    t = profile_targets(PROFILE)
    plain = title_fit("Product Manager", t, ["Product Manager"])
    senior = title_fit("Senior Product Manager", t, ["Product Manager"])
    assert senior < plain
    assert title_fit("Senior Product Manager", t, ["Senior Product Manager"]) == plain


def test_match_percent_thin_without_titles_is_capped():
    # No titles to compare: a 2-skill ad that is fully covered still can't read as 100%.
    assert match_percent({"ratio": 1.0, "required_total": 2, "required_covered": 2, "total": 2}) == 70
    # Unchanged behaviour for a dict without a total (older callers).
    assert match_percent({"ratio": 0.5, "required_total": 4, "required_covered": 4}) == 80


def test_autoapply_fit_floor_still_filters_the_mismatch():
    from autoapply.picker import DEFAULT_MIN_FIT
    m = _match(THIN_JD, "Cybersecurity Architect", {"profile": PROFILE, "essentials": ESS})
    good = _match(PM_JD, "Product Manager, AI", {"profile": PROFILE, "essentials": ESS})
    assert match_percent(m) < DEFAULT_MIN_FIT <= match_percent(good)


# --- 14. dismiss leaves the Tracker too ----------------------------------------------------------

JOB = {"source_id": "freehire::abc", "source": "freehire", "company": "Acme", "title": "PM",
       "location": "NYC", "remote": "", "url": "http://x", "posted_at": "2026-10-01",
       "salary": "", "jd_text": ""}


def test_dismissing_a_saved_job_removes_it_from_saved_and_the_tracker():
    w = Watchlist(tempfile.mktemp(suffix=".db"))
    w.set_saved(JOB["source_id"], True, JOB)
    assert w.stage_of(JOB["source_id"]) == "saved"
    w.dismiss(JOB["source_id"])
    assert JOB["source_id"] not in w.saved_ids()
    assert w.stage_of(JOB["source_id"]) in (None, "")
    assert all(j["source_id"] != JOB["source_id"] for j in w.board_jobs(None))


def test_dismissing_an_applied_job_keeps_its_tracker_history():
    w = Watchlist(tempfile.mktemp(suffix=".db"))
    w.set_saved(JOB["source_id"], True, JOB)
    w.move_stage(JOB["source_id"], "applied")
    w.dismiss(JOB["source_id"])
    assert w.stage_of(JOB["source_id"]) == "applied"
