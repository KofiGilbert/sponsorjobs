"""Auto-apply run: tailor the top-N fresh roles from the saved profile and queue them,
without drifting the base profile. The heavy build path needs LaTeX."""
from __future__ import annotations

from docx import Document

import ui.app as app
from conftest import requires_latex
from sourcing.watchlist import Watchlist

JOB = {
    "source_id": "greenhouse:northwind:1", "source": "greenhouse", "company": "Northwind",
    "title": "Senior Software Engineer", "location": "Remote", "remote": "remote",
    "url": "https://boards.greenhouse.io/northwind/jobs/1",
    "jd_text": "Senior Software Engineer. Build REST APIs in Python on AWS with SQL.",
    "posted_at": "2026-07-13",
}


def _cv_docx(tmp_path):
    doc = Document()
    for line in ["Sam Rivera", "sam.rivera@example.com", "Software Engineer at Acme"]:
        doc.add_paragraph(line)
    p = tmp_path / "resume.docx"
    doc.save(str(p))
    return p


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    return db


def test_autoapply_needs_a_profile(tmp_path, monkeypatch):
    db = _isolate(tmp_path, monkeypatch)
    Watchlist(db).upsert_jobs([JOB])                      # a job, but no profile yet
    r = app.app.test_client().post("/api/autoapply/run", json={"count": 1})
    assert r.status_code == 400 and "profile" in r.get_json()["error"].lower()


# --- focus: which KINDS of roles the autopilot tailors today ------------------ #

_FOCUS_JOBS = [
    {"title": "Senior Backend Engineer", "location": "New York, NY", "company": "Acme"},
    {"title": "Data Scientist", "location": "Remote", "company": "Beta"},
    {"title": "Frontend Developer", "location": "Austin, TX", "company": "Gamma"},
]


def test_select_focus_jobs_empty_focus_returns_all():
    assert app._select_focus_jobs(_FOCUS_JOBS, [], [], False) == _FOCUS_JOBS


def test_select_focus_jobs_filters_by_title_and_preserves_order():
    jobs = _FOCUS_JOBS + [{"title": "Backend Lead", "location": "Denver"}]
    picked = app._select_focus_jobs(jobs, ["backend"], [], False)
    assert [j["title"] for j in picked] == ["Senior Backend Engineer", "Backend Lead"]


def test_select_focus_jobs_sponsor_only_keeps_only_badged(monkeypatch):
    def fake_tag(js):
        for j in js:
            j["visa"] = [{"code": "H-1B"}] if j["company"] == "Acme" else []
        return js
    picked = app._select_focus_jobs(_FOCUS_JOBS, [], [], True, tag_fn=fake_tag)
    assert [j["company"] for j in picked] == ["Acme"]


def test_select_focus_jobs_sponsor_only_is_a_noop_without_a_tagger():
    # Defensive: no tag function injected -> can't judge sponsorship, so don't drop anything.
    assert app._select_focus_jobs(_FOCUS_JOBS, [], [], True, tag_fn=None) == _FOCUS_JOBS


def test_autoapply_focus_that_matches_nothing_returns_a_clear_message(tmp_path, monkeypatch):
    db = _isolate(tmp_path, monkeypatch)
    app._memory().save("default",
                       {"experience": [{"org": "Acme", "title": "Engineer",
                                        "bullets": ["did things"]}]}, {}, [])
    Watchlist(db).upsert_jobs([JOB])                       # title: "Senior Software Engineer"
    r = app.app.test_client().post("/api/autoapply/run",
                                   json={"count": 1, "focus": {"titles": ["nursing"]}})
    assert r.status_code == 400 and "focus" in r.get_json()["error"].lower()


@requires_latex
def test_autoapply_tailors_queues_and_keeps_base_profile(tmp_path, monkeypatch):
    db = _isolate(tmp_path, monkeypatch)
    client = app.app.test_client()
    cv = _cv_docx(tmp_path)
    with open(cv, "rb") as f:
        assert client.post("/api/profile/from_cv", data={"file": (f, "resume.docx")},
                           content_type="multipart/form-data").get_json()["ok"]
    base = client.get("/api/profile").get_json()          # snapshot the base profile
    Watchlist(db).upsert_jobs([JOB])

    r = client.post("/api/autoapply/run", json={"count": 1}).get_json()
    assert r["ok"] and r["considered"] == 1
    assert r["queued"] >= 1, r                             # the role got tailored + queued
    recs = client.get("/api/records").get_json()["records"]
    assert any(x["company"] == "Northwind" for x in recs)  # landed in the review queue

    after = client.get("/api/profile").get_json()          # base profile unchanged by the batch
    assert after["identity"]["name"] == base["identity"]["name"]
    assert (after["experience"] or [])[0]["org"] == (base["experience"] or [])[0]["org"]

    # The batch tailors into a throwaway scratch row and deletes it — none should linger,
    # and "default" is the profile it loaded from, never a mutated target.
    mem = app._memory()
    try:
        rows = mem._conn.execute(
            "SELECT name FROM conversation_memory WHERE name LIKE '\\_autoapply%' ESCAPE '\\'"
        ).fetchall()
    finally:
        mem.close()
    assert rows == [], f"scratch profiles leaked: {rows}"


def test_the_seed_includes_the_only_ats_that_can_auto_submit():
    """The seed shipped greenhouse + lever + ashby, and submission on all three is gated
    behind the EMPLOYER's key (§7), so they are ASSISTED forever. Recruitee is the only ATS
    on the AUTO allowlist, so the paid autopilot had ZERO possible targets out of the box:
    not few, zero, by construction, from the seed list. Measured on the real feed: 133 jobs,
    24 reachable, 0 auto-submittable.

    Sober about the size of this: Recruitee is a mostly-European ATS and only 1 of
    Channable's 15 offers is in the US, so this does not make autopilot meaningful. It makes
    the AUTO path real and exercised rather than theoretical. The honest fix for the paid
    tier is what it CLAIMS, which is why the page now states the true reach.
    """
    from sourcing.service import SEED_COMPANIES
    from submit.allowlist import auto_hosts
    seeded_ats = {ats for _c, ats, _b in SEED_COMPANIES}
    auto_ats = {h.split(".")[0] for h in auto_hosts()}
    assert seeded_ats & auto_ats, (
        f"nothing in the seed can ever auto-submit: seeded {sorted(seeded_ats)}, "
        f"AUTO lane is {sorted(auto_ats)}")
