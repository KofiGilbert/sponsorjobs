"""P6: the extension's server side, the profile-vs-JD keyword MATCH estimate and the read-only
TRACKING mirror. Honest empty-profile behavior, honest labeling (not a real ATS score), and the
extension-only guard. Offline, no key, no network.
"""

from __future__ import annotations

import importlib

import pytest

HDR = {"X-Tailor-Extension": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")
    import ui.app as A
    importlib.reload(A)
    A.DB_PATH = str(tmp_path / "m.db")
    A.PALACE_DIR = tmp_path / "palace"
    return A, A.app.test_client()


JD = ("Data Analyst. Required: SQL, Python, Tableau. "
      "Nice to have: Spark, Airflow.")


def _seed_profile(A):
    A._memory().save("default", {
        "identity": {"name": "Sam Rivera"},
        "experience": [{"org": "Acme", "bullets": ["Built SQL and Python data pipelines."]}],
        "skills": {"Data": "SQL, Python, ETL"},
    }, {}, [])


# ---- Task 2: match score, honest and grounded --------------------------------------------------
def test_match_score_scores_coverage_and_marks_skills(client):
    A, c = client
    _seed_profile(A)
    r = c.post("/api/match/score", json={"jd": JD}, headers=HDR).get_json()
    assert r["has_profile"] is True
    assert isinstance(r["score"], int) and 0 <= r["score"] <= 100
    covered = {s["term"].lower() for s in r["skills"] if s["covered"]}
    missing_skills = {s["term"].lower() for s in r["skills"] if not s["covered"]}
    required = {s["term"].lower() for s in r["skills"] if s["required"]}
    optional = {s["term"].lower() for s in r["skills"] if not s["required"]}
    assert "sql" in covered and "python" in covered          # profile supports these
    assert "spark" in missing_skills                         # JD wants it, profile lacks it
    assert "sql" in required and "python" in required        # before "Nice to have" -> required
    assert "spark" in optional                               # under "Nice to have" -> optional
    # The overall (broader) missing list still surfaces JD tools outside the skill vocab.
    assert any(t.lower() == "tableau" for t in r["missing"])
    assert "not a real ats" in r["note"].lower()             # honesty label


def test_match_score_is_honest_when_there_is_no_profile(client):
    A, c = client
    r = c.post("/api/match/score", json={"jd": JD}, headers=HDR).get_json()
    assert r["has_profile"] is False and r["score"] is None
    assert "add your profile" in r["note"].lower()           # never fabricate a number


def test_match_endpoint_is_extension_guarded(client):
    _, c = client
    assert c.post("/api/match/score", json={"jd": JD}).status_code == 403   # no header -> refused


# ---- Task 6: tracking mirror -------------------------------------------------------------------
def test_tracking_summary_mirrors_the_records(client):
    A, c = client
    recs = A.CVRecords(A.DB_PATH)
    recs.add("SWE", "Acme", 70.0, "", data={"status": "applied"})
    recs.add("Analyst", "Beta", 60.0, "", data={"status": "ready"})
    recs.add("PM", "Gamma", 50.0, "", data={"status": "applied"})
    recs.close()
    d = c.get("/api/tracking/summary", headers=HDR).get_json()
    assert d["total"] == 3 and d["applied"] == 2 and d["in_review"] == 1
    assert d["by_status"]["applied"] == 2 and d["app_url"].startswith("http://127.0.0.1")
    assert c.get("/api/tracking/summary").status_code == 403   # extension-guarded


# ---- the Required/Optional split helper --------------------------------------------------------
def test_split_required_optional_heuristic():
    from tailoring.keywords import split_required_optional
    req, opt = split_required_optional("Requirements: SQL, Python. Nice to have: Spark, Kafka.")
    assert "sql" in req.lower() and "python" in req.lower() and "spark" not in req.lower()
    assert "spark" in opt.lower() and "kafka" in opt.lower()
    # no optional marker -> everything is required context
    req2, opt2 = split_required_optional("Requirements: SQL and Python.")
    assert opt2 == "" and "sql" in req2.lower()
