"""The lazy job-detail endpoint returns the full JD text and a pre-tailor MATCH —
how many of the JD's key terms the saved PROFILE already supports (deterministic,
no model). This is what powers the master-detail feed's right pane."""
from __future__ import annotations

import ui.app as app
from intake.memory import ConversationMemory
from sourcing.watchlist import Watchlist

JD = ("Senior Data Engineer. We need strong Python and SQL to build ETL pipelines "
      "on AWS. Experience with Airflow, Spark, and Kubernetes required. Machine "
      "learning exposure a plus.")

JOB = {
    "source_id": "greenhouse:acme:1", "source": "greenhouse", "company": "Acme",
    "title": "Senior Data Engineer", "location": "Chicago, IL", "remote": "remote",
    "url": "https://boards.greenhouse.io/acme/jobs/1", "jd_text": JD,
    "posted_at": "2026-07-13",
}

# Profile supports Python/SQL/AWS/Airflow; not Kubernetes/Spark → real gaps.
PROFILE = {
    "identity": {"name": "Sam Rivera"},
    "skills": {"Computing": "Python, SQL, AWS, Airflow"},
    "experience": [{"org": "DataCo", "roles": [{"title": "Engineer",
                    "bullets": ["Built ETL pipelines in Python and SQL on AWS with Airflow"]}]}],
}


def _seed(tmp_path, monkeypatch, with_profile=True):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    w = Watchlist(db); w.upsert_jobs([JOB]); w.close()
    if with_profile:
        ConversationMemory(db).save("default", PROFILE, {}, [])
    return db


def test_detail_returns_full_jd(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()
    assert "Python" in d["jd"] and "Airflow" in d["jd"] and "Kubernetes" in d["jd"]
    assert d["job"]["title"] == "Senior Data Engineer"


def test_detail_match_counts_supported_terms(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    m = app.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()["match"]
    assert m is not None
    assert m["total"] >= m["covered"] >= 1
    assert 0 < m["ratio"] <= 1
    # Kubernetes is wanted but unsupported → shows up as a gap the person can weigh.
    assert any("kubernetes" in t.lower() for t in m["missing"])


def test_detail_no_profile_invites_setup(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, with_profile=False)
    d = app.app.test_client().get("/api/jobs/detail?source_id=greenhouse:acme:1").get_json()
    assert d["jd"]            # JD still shows
    assert d["match"] is None  # no profile → the UI invites setup instead of a fake score


def test_detail_missing_role_404(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = app.app.test_client().get("/api/jobs/detail?source_id=nope:x:9")
    assert r.status_code == 404


# --- Honest multi-dimensional fit (feature #1): must-have vs nice-to-have, verdict, seniority ---

def test_match_separates_must_haves_from_nice_to_haves():
    # Heading-style JD: everything before "Nice to have" is required, the rest is optional.
    jd = ("Data Engineer. Required: strong Python and SQL, plus Kubernetes and Spark. "
          "Nice to have: Tableau, Scala, and Airflow.")
    prof = {"skills": {"c": "Python, SQL"}, "experience": [{"org": "X",
            "roles": [{"title": "Eng", "bullets": ["ETL in Python and SQL"]}]}]}
    m = _match_with_profile(jd, "Data Engineer", prof)
    # An unsupported REQUIRED skill lands in required_missing...
    assert any("kubernetes" in t.lower() for t in m["required_missing"])
    # ...while skills under "Nice to have" never show up as a missing must-have.
    assert not any(x in " ".join(m["required_missing"]).lower() for x in ("tableau", "scala"))
    assert any("tableau" in t.lower() for t in m["optional_missing"])
    assert m["required_total"] >= m["required_covered"] >= 1
    assert m["verdict"] in {"strong", "good", "moderate", "weak"}


def test_match_verdict_weighted_to_must_haves_not_raw_percent():
    # All must-haves covered but plenty of nice-to-haves missing → honest verdict is NOT "weak"
    # even though the raw keyword % is low (a screener gates on the must-haves).
    jd = ("Data Analyst. Required: Python and SQL. "
          "Nice to have: Tableau, Spark, AWS, Kubernetes, Airflow, Scala.")
    prof = {"skills": {"c": "Python, SQL"}, "experience": [{"org": "X",
            "roles": [{"title": "Analyst", "bullets": ["Analysis in Python and SQL"]}]}]}
    m = _match_with_profile(jd, "Data Analyst", prof)
    assert m["required_covered"] == m["required_total"] >= 1
    assert m["ratio"] < 0.6                        # raw % is low...
    assert m["verdict"] in {"strong", "good"}      # ...but the honest verdict is favourable


def test_match_flags_seniority_for_students():
    jd = "We need 8+ years of experience leading teams. Python required."
    prof = {"skills": {"c": "Python"}, "experience": [{"org": "X",
            "roles": [{"title": "Dev", "bullets": ["Python work"]}]}]}
    m = _match_with_profile(jd, "Staff Engineer", prof)
    assert m["seniority"] and m["seniority"]["level"] == "senior"
    assert "8+" in m["seniority"]["note"] or "years" in m["seniority"]["note"]

    entry = _match_with_profile("Great first role. Python required.", "Marketing Intern", prof)
    assert entry["seniority"] and entry["seniority"]["level"] == "entry"


def _match_with_profile(jd, title, profile):
    """Compute a match against an injected profile without a DB round-trip."""
    orig = app._memory
    class _Mem:
        def load(self, _name): return {"profile": profile}
    app._memory = lambda: _Mem()
    try:
        return app._job_match(jd, title)
    finally:
        app._memory = orig
