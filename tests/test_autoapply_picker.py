"""Auto-apply picks from the jobs the person sees on Jobs, never queues a role twice, skips
poor fits, and states its real reach (autoapply/picker.py + /api/autoapply/run|preview)."""
from __future__ import annotations

import ui.app as app
from autoapply import picker
from submit.policy import submission_policy
from ui.records import CVRecords


def _job(sid, title, company, url=None, **kw):
    return {"source_id": sid, "source": "freehire", "title": title, "company": company,
            "location": kw.pop("location", "New York, NY"),
            "url": url or f"https://boards.greenhouse.io/{company.lower()}/jobs/{sid}", **kw}


# --------------------------------------------------------------------------- picker units
def test_saved_jobs_come_first_then_board_by_profile_fit():
    board = [_job("b1", "Nurse Practitioner", "Health"), _job("b2", "Backend Engineer", "Acme"),
             _job("s1", "Data Analyst", "Beta")]
    saved = [_job("s1", "Data Analyst", "Beta")]
    targets = {"title_words": {"backend", "engineer"}, "skill_words": set()}
    rows = picker.candidates(board, saved, targets=targets)
    assert [r["source_id"] for r in rows] == ["s1", "b2", "b1"]   # saved once, then best title fit
    assert rows[0]["saved"] is True


def test_candidates_apply_the_saved_search_and_focus():
    board = [_job("1", "Backend Engineer", "Acme", location="Austin, TX"),
             _job("2", "Backend Engineer", "Beta", location="New York, NY", visa=[{"code": "H-1B"}]),
             _job("3", "Designer", "Gamma", location="New York, NY")]
    rows = picker.candidates(board, [], facets={"q": "backend"}, locations=["new york"])
    assert [r["source_id"] for r in rows] == ["2"]
    rows = picker.candidates(board, [], sponsor_only=True)
    assert [r["source_id"] for r in rows] == ["2"]


def test_pick_skips_queued_and_applied_by_id_and_by_cross_feed_keys():
    rows = [_job("a", "Backend Engineer", "Acme"),
            # same opening through a second feed: different id, same apply URL
            _job("other-feed:9", "Backend Engineer (Remote)", "Acme",
                 url="https://boards.greenhouse.io/acme/jobs/a?utm_source=x"),
            _job("c", "Data Engineer", "Beta"),
            _job("d", "ML Engineer", "Gamma")]
    records = [
        {"source_job": {"source_id": "a", "url": rows[0]["url"]}, "company": "Acme",
         "role": "Backend Engineer", "status": "ready"},
        # no source_id, matched on company + role only; already applied
        {"source_job": {}, "company": "Beta", "role": "Data Engineer · B: skills-first",
         "status": "applied"},
    ]
    res = picker.pick(rows, records=records, score_fn=lambda j: 90, count=5, min_fit=60)
    assert [j["source_id"] for j in res["jobs"]] == ["d"]
    assert res["queued"] == 2 and res["applied"] == 1


def test_pick_applies_the_fit_floor_and_counts_why():
    rows = [_job(str(i), f"Role {i}", f"Co{i}") for i in range(6)]
    scores = {"0": 40, "1": 75, "2": None, "3": 59, "4": 60, "5": 95}
    res = picker.pick(rows, records=[], score_fn=lambda j: scores[j["source_id"]],
                      count=2, min_fit=60)
    assert [j["source_id"] for j in res["jobs"]] == ["1", "4"]
    assert res["jobs"][0]["fit"] == 75
    assert res["below"] == 2 and res["unscored"] == 1
    assert picker.skip_summary(res, 60) == ("2 below your fit floor of 60%, "
                                            "1 with no description to score")


def test_pick_takes_one_opening_once_even_if_the_board_lists_it_twice():
    rows = [_job("x1", "Backend Engineer", "Acme", url="https://acme.recruitee.com/o/backend"),
            _job("x2", "Backend Engineer", "Acme", url="https://acme.recruitee.com/o/backend")]
    res = picker.pick(rows, records=[], score_fn=lambda j: 80, count=5, min_fit=60)
    assert len(res["jobs"]) == 1


def test_pick_is_bounded():
    rows = [_job(str(i), f"Role {i}", f"Co{i}") for i in range(100)]
    res = picker.pick(rows, records=[], score_fn=lambda j: 10, count=5, min_fit=60, max_scored=7)
    assert res["scored"] == 7 and res["jobs"] == []


def test_reach_uses_the_apply_url_host_not_the_feed_name():
    rows = [_job("1", "A", "Acme", url="https://acme.recruitee.com/o/eng"),   # source "freehire"
            _job("2", "B", "Beta", url="https://jobs.lever.co/beta/1"),
            {"source_id": "3", "source": "recruitee", "url": "https://www.linkedin.com/jobs/view/3"}]
    assert picker.reach(rows, submission_policy) == {"auto": 1, "total": 3}


def test_clamp_min_fit():
    assert picker.clamp_min_fit("70") == 70
    assert picker.clamp_min_fit(400) == 95
    assert picker.clamp_min_fit(-3) == 0
    assert picker.clamp_min_fit("x") == picker.DEFAULT_MIN_FIT


# --------------------------------------------------------------------------- endpoints
PROFILE = {"identity": {"name": "Sam Rivera"},
           "experience": [{"org": "Acme", "roles": [{"title": "Backend Engineer",
                                                     "bullets": ["Built Python APIs on AWS with SQL"]}]}],
           "skills": {"Languages": ["Python", "SQL"], "Cloud": ["AWS"]}}
GOOD_JD = "Backend Engineer. Requirements: Python, SQL and AWS. You build REST APIs."
POOR_JD = "Requirements: Java, Kubernetes, Go, Rust, Scala, Terraform and C++."


def _setup(tmp_path, monkeypatch, board):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_LEARN_FILE", tmp_path / "aa_learn.json")   # skip reasons, lists
    monkeypatch.setattr(app, "_PREFS_PATH", tmp_path / "answers.json")    # the visa answer
    monkeypatch.setattr(app, "_board_rows", lambda: [dict(j) for j in board])
    tailored = []

    def fake_tailor(job, llm, tex, tname, scratch):
        recs = CVRecords(db)
        try:
            rid = recs.add(job["title"], job["company"], 80, "", data={
                "source_job": {"url": job.get("url"), "source": job.get("source"),
                               "source_id": job.get("source_id")}, "status": "ready"})
        finally:
            recs.close()
        tailored.append(job["source_id"])
        return {"ok": True, "id": rid, "role": job["title"], "company": job["company"], "coverage": 80}

    monkeypatch.setattr(app, "_tailor_job_unattended", fake_tailor)
    monkeypatch.setattr(app, "_load_template", lambda name: ("", "classic"))
    return app.app.test_client(), tailored


def _board():
    return [_job("g1", "Backend Engineer", "Acme", jd_text=GOOD_JD,
                 url="https://acme.recruitee.com/o/backend-engineer"),
            _job("g2", "Senior Backend Engineer", "Beta", jd_text=GOOD_JD),
            _job("p1", "Platform Engineer", "Gamma", jd_text=POOR_JD),
            _job("g3", "Backend Developer", "Delta", jd_text=GOOD_JD)]


def test_run_without_a_profile_offers_the_resume_action(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch, _board())
    r = client.post("/api/autoapply/run", json={"count": 3})
    assert r.status_code == 400 and r.get_json()["action"] == "add_resume"


def test_run_picks_from_the_feed_skips_poor_fits_and_never_duplicates(tmp_path, monkeypatch):
    client, tailored = _setup(tmp_path, monkeypatch, _board())
    app._memory().save("default", PROFILE, {}, [])

    first = client.post("/api/autoapply/run", json={"count": 2, "min_fit": 60}).get_json()
    assert first["ok"] and first["queued"] == 2
    assert all(t["fit"] >= 60 for t in first["tailored"])
    assert tailored == ["g1", "g2"]

    second = client.post("/api/autoapply/run", json={"count": 2, "min_fit": 60}).get_json()
    assert second["queued"] == 1 and tailored == ["g1", "g2", "g3"]   # only the one left
    assert second["passed"]["queued"] == 2 and second["passed"]["below"] == 1
    assert "1 below your fit floor of 60%" in second["summary"]
    assert "2 already in your queue" in second["summary"]

    third = client.post("/api/autoapply/run", json={"count": 2, "min_fit": 60})
    assert third.status_code == 400 and "fit floor" in third.get_json()["error"]
    assert len(tailored) == 3                                            # still no duplicates


def test_run_remembers_the_fit_floor(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch, _board())
    app._memory().save("default", PROFILE, {}, [])
    client.post("/api/autoapply/run", json={"count": 1, "min_fit": 80})
    assert client.get("/api/autoapply/preview").get_json()["min_fit"] == 80


def test_saved_job_goes_first(tmp_path, monkeypatch):
    client, tailored = _setup(tmp_path, monkeypatch, _board())
    app._memory().save("default", PROFILE, {}, [])
    w = app._watchlist()
    try:
        w.set_saved("s9", True, _job("s9", "Backend Engineer", "Saved Co", jd_text=GOOD_JD))
    finally:
        w.close()
    monkeypatch.setattr(app, "_aa_fit", lambda j: 90)
    client.post("/api/autoapply/run", json={"count": 1})
    assert tailored == ["s9"]


def test_preview_reports_matches_and_real_reach(tmp_path, monkeypatch):
    client, _ = _setup(tmp_path, monkeypatch, _board())
    app._memory().save("default", PROFILE, {}, [])
    d = client.get("/api/autoapply/preview").get_json()
    assert d["has_profile"] is True and d["matches"] == 4
    assert d["reach"] == {"auto": 1, "total": 4}          # g1's recruitee.com apply URL
    d = client.get("/api/autoapply/preview?titles=developer").get_json()
    assert d["matches"] == 1 and d["reach"]["auto"] == 0


def test_same_title_in_another_city_is_a_different_opening():
    rows = [_job("ny", "Software Engineer", "NetApp", location="New York, NY"),
            _job("sj", "Software Engineer", "NetApp", location="San Jose, CA")]
    records = [{"source_job": {"source_id": "ny", "url": rows[0]["url"], "location": "New York, NY"},
                "company": "NetApp", "role": "Software Engineer", "status": "ready"}]
    res = picker.pick(rows, records=records, score_fn=lambda j: 80, count=5, min_fit=60)
    assert [j["source_id"] for j in res["jobs"]] == ["sj"] and res["queued"] == 1


def test_career_site_and_workday_copies_of_one_opening_are_one_job():
    # Seen on the real feed: Cisco's own site and its Workday board list requisition 2019448
    # under different company and location spellings; the URL and role keys both miss it.
    rec = {"source_job": {"source_id": "careersite:careers.cisco.com:2019448",
                          "url": "https://careers.cisco.com/global/en/job/X2019448/Software-Engineer",
                          "location": "Glendale, California, United States of America"},
           "company": "Cisco", "role": "Software Engineer · Classic", "status": "ready"}
    rows = [_job("workday:cisco.wd5/Cisco_Careers:2019448", "Software Engineer", "Cisco Systems Inc",
                 url="https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/x/Software-Engineer_2019448",
                 location="Glendale, California, US"),
            _job("careersite:careers.cisco.com:2024295", "Software Engineer", "Cisco",
                 location="Milpitas, California, United States of America")]
    res = picker.pick(rows, records=[rec], score_fn=lambda j: 80, count=5, min_fit=60)
    assert [j["source_id"] for j in res["jobs"]] == ["careersite:careers.cisco.com:2024295"]
    assert res["queued"] == 1
