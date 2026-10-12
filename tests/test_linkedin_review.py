"""LinkedIn profile review: the pure scorer (linkedin_review.py), the one-call drafting with the
numeric fact gate, and the app endpoints the extension and the Profile page use.

Read-only by rule (CLAUDE.md §7): the app only ever receives what the person's own open page
shows; it never fetches LinkedIn, and the person applies every fix by hand.
"""

from __future__ import annotations

import json

import pytest

import ui.app as app
from intake.memory import ConversationMemory
from linkedin_review import (ABOUT_MAX, HEADLINE_MAX, SECTIONS, draft_headline_about,
                             role_terms, score_profile, target_role_from)
from llm.base import FakeLLM

STRONG = {
    "slug": "ada-lovelace", "custom_url": True, "name": "Ada Lovelace",
    "headline": "Data Engineer | Python, SQL, Airflow on AWS | Pipelines analytics teams can trust",
    "about": ("I am a data engineer who builds pipelines in Python and SQL on AWS. " * 8
              + "Most recently I owned the warehouse model for 30 analysts. "
              + "Open to data engineer roles. Reach out here and I will reply quickly."),
    "location": "Chicago, Illinois, United States", "photo": True, "banner": True,
    "open_to_work": True,
    "experience": [{"title": "Data Engineer", "company": "Acme", "dates": "Mar 2022 - Present",
                    "description": "- Built ETL pipelines in Python that cut load time by 40%.",
                    "has_description": True}],
    "education": [{"school": "UIUC", "degree": "M.S. Computer Science", "dates": "2017 - 2019"}],
    "skills": ["Python", "SQL", "Apache Airflow", "AWS", "ETL", "dbt", "Spark", "Kafka", "Docker",
               "Git", "Linux", "Terraform", "Snowflake", "Pandas", "Data Modeling"],
    "licenses": [{"name": "AWS Certified Data Engineer", "issuer": "AWS"}],
    "featured_count": 1, "projects_count": 0, "recommendations_count": 2,
    "activity": True, "activity_recent": True, "followers": 1204,
}
EMPTY = {"slug": "sam-student-1a2b3c4d5", "custom_url": False}


def test_fourteen_sections_sum_to_one_hundred_points():
    assert len(SECTIONS) == 14
    assert sum(w for _, _, w in SECTIONS) == 100
    r = score_profile(STRONG, "Data Engineer", ["python", "sql", "airflow", "aws"])
    assert [s["key"] for s in r["sections"]] == [k for k, _, _ in SECTIONS]
    assert r["score"] == round(sum(s["points"] for s in r["sections"]))


def test_strong_profile_scores_high_and_empty_profile_low():
    strong = score_profile(STRONG, "Data Engineer", ["python", "sql", "airflow", "aws"])
    empty = score_profile(EMPTY, "Data Engineer")
    assert strong["score"] >= 90
    assert empty["score"] <= 5
    for s in strong["sections"] + empty["sections"]:
        assert 0 <= s["score"] <= 100 and s["reason"] and s["fix"]
    # An empty section gives a concrete fix, not a shrug.
    by = {s["key"]: s for s in empty["sections"]}
    assert "photo" in by["photo"]["fix"].lower()
    assert "public profile url" in by["custom_url"]["fix"].lower()


def test_headline_rewards_keywords_and_punishes_job_title_only():
    terms = role_terms("Data Engineer", ["python", "sql"])
    bare = score_profile({"headline": "Analyst at Acme"}, "Data Engineer", ["python", "sql"])
    rich = score_profile({"headline": STRONG["headline"]}, "Data Engineer", ["python", "sql"])
    weak = score_profile({"headline": "Seeking opportunities | Data Engineer | Python, SQL"},
                         "Data Engineer", ["python", "sql"])
    h = lambda r: next(s for s in r["sections"] if s["key"] == "headline")   # noqa: E731
    assert h(rich)["score"] == 100
    assert h(bare)["score"] < h(rich)["score"]
    assert "job title" in h(bare)["reason"]
    assert h(weak)["score"] < h(rich)["score"] and "looking" in h(weak)["reason"]
    assert "data" in terms and "engineer" in terms and "python" in terms


def test_about_wants_first_person_keywords_and_a_call_to_action():
    a = lambda x: next(s for s in score_profile(x, "Data Engineer", ["python", "sql", "aws"])["sections"]   # noqa: E731
                       if s["key"] == "about")
    assert a({})["score"] == 0
    third = a({"about": "Ada is a data engineer. " * 30})
    assert "first person" in third["reason"] and "call to action" in third["reason"]
    assert a({"about": STRONG["about"]})["score"] == 100


def test_experience_wants_descriptions_numbers_bullets_and_recency():
    e = lambda rows: next(s for s in score_profile({"experience": rows}, "x")["sections"]   # noqa: E731
                          if s["key"] == "experience")
    old = e([{"title": "Clerk", "company": "A", "dates": "2010 - 2012", "description": "", "has_description": False}])
    assert old["score"] < 50 and "no role has a description" in old["reason"] and "nothing recent" in old["reason"]
    assert e(STRONG["experience"])["score"] == 100
    assert e([])["score"] == 0


def test_skills_count_and_relevance_to_the_target_role():
    s = lambda sk, terms: next(x for x in score_profile({"skills": sk}, "Data Engineer", terms)["sections"]   # noqa: E731
                               if x["key"] == "skills")
    few_off = s(["Excel", "Word"], ["python", "sql", "aws"])
    assert few_off["score"] < 50 and "none match" in few_off["reason"]
    assert "python" in few_off["fix"]                     # the fix names the missing terms
    assert s(STRONG["skills"], ["python", "sql", "aws"])["score"] == 100


def test_custom_url_and_open_to_work_and_location():
    r = {s["key"]: s for s in score_profile(EMPTY, "x")["sections"]}
    assert r["custom_url"]["score"] == 0 and r["open_to_work"]["score"] == 40 and r["location"]["score"] == 0
    r2 = {s["key"]: s for s in score_profile({"slug": "ada-lovelace", "open_to_work": True,
                                              "location": "Chicago, IL"}, "x")["sections"]}
    assert r2["custom_url"]["score"] == 100 and r2["open_to_work"]["score"] == 100
    assert r2["location"]["score"] == 100


def test_target_role_comes_from_the_last_tailored_job_then_the_profile():
    prof = {"experience": [{"org": "Acme", "roles": [{"title": "Data Engineer"}]}]}
    assert target_role_from(prof, [{"role": "Analytics Engineer, A: experience-first", "jd_label": ""}]) == "Analytics Engineer"
    assert target_role_from(prof, [{"role": "", "jd_label": "ML Engineer"}]) == "ML Engineer"
    assert target_role_from(prof, []) == "Data Engineer"
    assert target_role_from({}, []) == ""


# ------------------------------------------------------------------ drafting

PROFILE = {
    "identity": {"name": "Ada Lovelace"},
    "skills": {"Computing": "Python, SQL, AWS"},
    "experience": [{"org": "Acme", "roles": [{"title": "Data Engineer", "dates": "2022 - Present",
                                               "bullets": ["Built ETL pipelines for 30 analysts."]}]}],
}


def test_drafts_fit_the_limits_and_are_grounded():
    d = draft_headline_about(FakeLLM(), PROFILE, "Data Engineer", {"headline": "x", "about": ""})
    assert 0 < len(d["headline"]) <= HEADLINE_MAX
    assert 0 < len(d["about"]) <= ABOUT_MAX
    assert "Data Engineer" in d["headline"] and "Python" in d["headline"]
    assert d["about"].startswith("I am Ada Lovelace")
    assert d["flags"] == []            # the fake writes no figures the profile lacks


def test_drafts_are_trimmed_and_invented_figures_are_flagged():
    class Loud:
        def draft_linkedin_profile(self, profile, role, h="", a=""):
            return {"headline": "Data Engineer | " + "Python, SQL, AWS, " * 30,
                    "about": "I cut costs by 45% for 12 teams. " * 120}
    d = draft_headline_about(Loud(), PROFILE, "Data Engineer", {})
    assert len(d["headline"]) <= HEADLINE_MAX and len(d["about"]) <= ABOUT_MAX
    assert d["headline"].startswith("Data Engineer | Python")
    flagged = {f["field"]: f["figures"] for f in d["flags"]}
    assert flagged["about"] == ["12", "45"]           # neither figure is in the saved profile


def test_drafting_treats_the_persons_own_page_text_as_source_material():
    class Loud:
        def draft_linkedin_profile(self, profile, role, h="", a=""):
            return {"headline": "Data Engineer | 40% faster pipelines", "about": "I am Ada."}
    d = draft_headline_about(Loud(), PROFILE, "Data Engineer",
                             {"headline": "", "about": "",
                              "experience": [{"description": "cut load time by 40%"}]})
    assert d["flags"] == []


# ------------------------------------------------------------------ endpoints

def _seed(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "_LI_REVIEW_PATH", tmp_path / "linkedin_review.json")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    app._SESSION.pop("s", None)
    m = ConversationMemory(db)
    m.save("default", PROFILE, {"identity": {"name": "Ada Lovelace"}}, [])
    m.close()
    return db


def test_get_before_any_review_is_honest(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/profile/linkedin-review").get_json()
    assert d == {"ok": True, "has_review": False}


def test_extension_event_scores_without_the_model_and_stores_locally(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    c = app.app.test_client()
    # Through the extension's event channel (the background worker only knows the prefs route).
    r = c.post("/api/profile/prefs", json={"event": {"type": "linkedin_review", "profile": STRONG}},
               headers={"X-Tailor-Extension": "1"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["has_review"] is True
    assert d["score"] >= 85 and d["drafts"] is None        # no model spend from the page
    assert d["review"]["target_role"] == "Data Engineer"     # from the saved profile
    stored = json.loads((tmp_path / "linkedin_review.json").read_text())
    assert stored["extracted"]["name"] == "Ada Lovelace"
    assert "Cache-Control" in r.headers and r.headers["Cache-Control"] == "no-store"
    # The Profile page now sees it.
    g = c.get("/api/profile/linkedin-review").get_json()
    assert g["has_review"] is True and g["score"] == d["score"] and g["name"] == "Ada Lovelace"


def test_extension_event_needs_the_extension_header(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = app.app.test_client().post("/api/profile/prefs",
                                   json={"event": {"type": "linkedin_review", "profile": STRONG}})
    assert r.status_code == 403


def test_rescore_from_the_app_drafts_headline_and_about(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    c = app.app.test_client()
    c.post("/api/profile/linkedin-review", json={"profile": STRONG, "draft": False})
    d = c.post("/api/profile/linkedin-review", json={}).get_json()    # re-score the stored page
    assert d["has_review"] is True and d["draft_error"] == ""
    assert d["drafts"]["headline"].startswith("Data Engineer | Python")
    assert len(d["drafts"]["about"]) <= ABOUT_MAX and d["drafts"]["flags"] == []
    assert len(d["review"]["sections"]) == 14


def test_rescore_with_nothing_stored_says_so(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().post("/api/profile/linkedin-review", json={}).get_json()
    assert d["ok"] is True and d["has_review"] is False


def test_extraction_is_cleaned_and_capped(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    odd = {"headline": "x" * 5000, "skills": ["a"] * 500 + [None, 7], "experience": ["junk", {"title": "T"}],
           "followers": "lots", "photo": "yes", "surprise": {"deep": 1}}
    d = app.app.test_client().post("/api/profile/linkedin-review", json={"profile": odd, "draft": False}).get_json()
    assert d["has_review"] is True
    x = json.loads((tmp_path / "linkedin_review.json").read_text())["extracted"]
    assert len(x["headline"]) == 3000 and len(x["skills"]) == 100
    assert x["experience"] == [{"title": "T", "company": "", "dates": "", "description": "",
                                "has_description": False}]
    assert x["followers"] == 0 and x["photo"] is True and "surprise" not in x


def test_draft_failure_keeps_the_score(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    def boom():
        raise RuntimeError("no key saved")
    monkeypatch.setattr(app, "_make_llm", boom)
    d = app.app.test_client().post("/api/profile/linkedin-review", json={"profile": STRONG}).get_json()
    assert d["has_review"] is True and d["score"] >= 85
    assert d["drafts"] is None and "no key" in d["draft_error"]
