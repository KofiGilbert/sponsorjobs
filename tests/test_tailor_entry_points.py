"""Every way into a resume chat hands it the job's real description (2026-11-26).

The bug: a resume chat started from a job LINK pasted on Home kept the bare link as its job
description (82 characters, no role, no company), so the resume was tailored to nothing. Each
entry point is pinned here at the request it makes:

  * Home, a pasted link          POST /api/session/start {jd: <link>}: the posting is read first
  * Home, pasted text            POST /api/session/start {jd: <text>}: the text as given
  * Home, a best-fit role card   opens the job on the board, whose Tailor is start_job (a feed row)
  * Jobs, a card's Tailor        POST /api/session/start_job (a stored row)
  * Tracker, the panel's Tailor  POST /api/session/start_job (a tracked job)

A link that cannot be read (or a job with no description and no readable link) is never tailored:
the answer asks for the text and nothing is spent. FakeLLM, saved fixtures, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ui.app as app
from sourcing import posting_read as P
from sourcing.liveness import Resp

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures" / "posting_read"
APP_JS = ROOT / "ui" / "static" / "app.js"
H = {"X-Tailor-Extension": "1"}
LEVER = "https://jobs.lever.co/example-advisory/0a1b2c3d-1111-2222-3333-444455556666"
LEVER_API = "https://api.lever.co/v0/postings/example-advisory/0a1b2c3d-1111-2222-3333-444455556666?mode=json"
JD = ("Data Analyst at Northwind Example.\nResponsibilities: build dashboards in Tableau, write SQL "
      "and Python for reporting, and present findings.\nRequirements: two years of analytics, "
      "statistics, Excel.\n") * 2


class Net:
    def __init__(self, table):
        self.table, self.asked = table, []

    def __call__(self, url, accept="", follow=True):
        self.asked.append(url)
        hit = self.table.get(url)
        return Resp(200, hit, url) if hit is not None else Resp(404, "", url)


def _fx(name):
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    monkeypatch.setattr(app, "_feed_base_url", lambda: "")
    monkeypatch.setattr(app, "_posting_check", lambda url, sid="": {"status": "open", "why": "", "at": 0})
    net = Net({LEVER_API: _fx("lever_posting.json"), LEVER: _fx("lever_page.html")})
    monkeypatch.setattr(P, "http_get", net)
    app._SESSION.pop("s", None)
    app._SESSION_SIG.clear()
    monkeypatch.setitem(app._SESSION_RESTORE, "tried", True)
    c = app.app.test_client()
    c.net = net
    yield c
    app._SESSION.pop("s", None)


def _no_spend(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a model call or a package count before the description was known")
    monkeypatch.setattr(app, "_count_package", boom)


def _session():
    return app._SESSION["s"]


def _assert_real_description(words):
    s = _session()
    assert len(s.jd) > 200, s.jd
    assert not P.link_in(s.jd), "the session's job description is a link, not a description"
    for w in words:
        assert w in s.jd


# ------------------------------------------------------------------------- Home, a pasted link

def test_home_pasted_link_tailors_from_the_posting_text(client):
    r = client.post("/api/session/start", headers=H, json={"jd": LEVER, "template": "shetty"})
    st = r.get_json()
    assert r.status_code == 200, st
    _assert_real_description(["Power BI dashboards", "Write SQL against the data warehouse"])
    s = _session()
    assert (s.role, s.company) == ("Business Intelligence Analyst", "Example Advisory")
    assert st["role"] == "Business Intelligence Analyst" and st["company"] == "Example Advisory"
    # one line names the job that was read, before the opening
    assert st["messages"][0].startswith(
        "Read the posting: Business Intelligence Analyst at Example Advisory.")
    # the application link stays with the job, under the feed's own id
    assert s.source_job["url"] == LEVER
    assert s.job_id == "lever:example-advisory:0a1b2c3d-1111-2222-3333-444455556666"
    w = app._watchlist()
    try:
        job = w.get_job(s.job_id)
    finally:
        w.close()
    assert job and job["title"] == "Business Intelligence Analyst" and job["url"] == LEVER


def test_home_link_with_a_short_line_around_it_is_read_too(client):
    r = client.post("/api/session/start", headers=H,
                    json={"jd": f"Please tailor my resume for this role {LEVER}", "template": "shetty"})
    assert r.status_code == 200
    _assert_real_description(["Power BI dashboards"])


def test_an_unreadable_link_asks_for_the_text_and_spends_nothing(client, monkeypatch):
    _no_spend(monkeypatch)
    link = "https://jobs.lever.co/example-advisory/ffffffff-0000-0000-0000-000000000000"
    r = client.post("/api/session/start", headers=H, json={"jd": link})
    body = r.get_json()
    assert r.status_code == 422 and body["need_text"] is True and body["url"] == link
    assert "Paste the job description text here" in body["error"]
    assert "s" not in app._SESSION


def test_a_linkedin_link_is_never_fetched(client, monkeypatch):
    _no_spend(monkeypatch)
    r = client.post("/api/session/start", headers=H,
                    json={"jd": "https://www.linkedin.com/jobs/view/4012345678/"})
    body = r.get_json()
    assert r.status_code == 422 and body["need_text"] is True
    assert body["error"].startswith("I do not read LinkedIn pages.")
    assert not any("linkedin" in u for u in client.net.asked)


def test_the_text_pasted_after_a_failed_link_keeps_the_link(client):
    link = "https://careers.example.org/job/123"
    r = client.post("/api/session/start", headers=H, json={"jd": JD, "url": link, "template": "shetty"})
    assert r.status_code == 200
    _assert_real_description(["Tableau"])
    assert _session().source_job["url"] == link


# ------------------------------------------------------------------------- Home, pasted text

def test_home_pasted_text_tailors_from_that_text(client):
    r = client.post("/api/session/start", headers=H, json={"jd": JD, "template": "shetty"})
    st = r.get_json()
    assert r.status_code == 200
    assert _session().jd == JD.strip()
    assert not st["messages"][0].startswith("Read the posting")
    assert client.net.asked == []                                   # no link, nothing fetched


# ------------------------------------------------------------------------- the job cards

FEED_ROW = {"source_id": "greenhouse:examplefreight:4012345", "source": "greenhouse",
            "company": "Example Freight", "title": "Data Engineer", "location": "Boston, MA",
            "url": "https://job-boards.greenhouse.io/examplefreight/jobs/4012345",
            "jd_text": "About the role. Example Freight needs a Data Engineer. " * 6 +
                       "Requirements: Airflow, dbt, Python, SQL.", "posted_at": "", "salary": "", "remote": ""}


def test_home_best_fit_card_tailors_from_the_feed_rows_description(client, monkeypatch):
    """A Home card opens the job on the board; its Tailor is start_job on a FEED row (never
    stored locally)."""
    monkeypatch.setattr(app, "_job_from_feed", lambda sid: dict(FEED_ROW) if sid == FEED_ROW["source_id"] else None)
    r = client.post("/api/session/start_job", headers=H, json={"source_id": FEED_ROW["source_id"]})
    assert r.status_code == 200, r.get_json()
    _assert_real_description(["Airflow, dbt"])
    assert (_session().role, _session().company) == ("Data Engineer", "Example Freight")


def _store(row):
    w = app._watchlist()
    try:
        w.upsert_jobs([row])
    finally:
        w.close()


def test_jobs_card_tailor_tailors_from_the_stored_description(client):
    row = {**FEED_ROW, "source_id": "greenhouse:examplefreight:777", "title": "Analytics Engineer"}
    _store(row)
    r = client.post("/api/session/start_job", headers=H, json={"source_id": row["source_id"]})
    assert r.status_code == 200, r.get_json()
    _assert_real_description(["Airflow, dbt"])
    assert _session().role == "Analytics Engineer"


def test_jobs_card_with_no_stored_description_reads_its_link(client):
    row = {**FEED_ROW, "source_id": "lever:example-advisory:0a1b2c3d-1111-2222-3333-444455556666",
           "source": "lever", "company": "Example Advisory", "title": "Business Intelligence Analyst",
           "url": LEVER, "jd_text": ""}
    _store(row)
    r = client.post("/api/session/start_job", headers=H, json={"source_id": row["source_id"]})
    assert r.status_code == 200, r.get_json()
    _assert_real_description(["Power BI dashboards"])


def test_a_job_with_no_description_and_no_readable_link_asks_for_the_text(client, monkeypatch):
    _no_spend(monkeypatch)
    row = {**FEED_ROW, "source_id": "pasted:nothing", "source": "pasted", "url": "", "jd_text": ""}
    _store(row)
    r = client.post("/api/session/start_job", headers=H, json={"source_id": row["source_id"]})
    body = r.get_json()
    assert r.status_code == 422 and body["need_text"] is True and body["source_id"] == "pasted:nothing"


def test_tracker_panel_tailor_tailors_from_the_tracked_jobs_description(client):
    sid = "greenhouse:examplefreight:888"
    w = app._watchlist()
    try:
        w.track_job(sid, "saved", note="Saved", job={**FEED_ROW, "source_id": sid, "title": "Data Platform Engineer"})
    finally:
        w.close()
    r = client.post("/api/session/start_job", headers=H, json={"source_id": sid})
    assert r.status_code == 200, r.get_json()
    _assert_real_description(["Airflow, dbt"])
    assert _session().role == "Data Platform Engineer"


# ------------------------------------------------------------------------- the client wiring

def test_each_entry_point_reaches_one_of_the_two_reading_starts():
    """Home box -> the builder gate -> /api/session/start (the server reads a link); a Home card,
    a Jobs card and the Tracker panel -> tailorJob -> /api/session/start_job."""
    src = APP_JS.read_text(encoding="utf-8")
    home = src.split("const tailor = (text) => {", 1)[1].split("};", 1)[0]
    assert '$("#jdStart").click()' in home
    assert "!!jobLinkIn(t)" in src.split("const looksLikeJd", 1)[1].split("\n", 1)[0]
    gate = src.split('$("#jdStart").addEventListener("click"', 1)[1].split("\n  });\n", 1)[0]
    assert '"/api/session/start"' in gate and "askForJdText" in gate
    assert "openJobOnBoard(jobs.find(" in src                     # the Home card opens the job
    assert 'tailorJob(e.currentTarget.dataset.fdTailor)' in src     # whose Tailor is tailorJob
    trk = src.split("async function trkTailor", 1)[1].split("\n  }\n", 1)[0]
    assert "tailorJob(sid)" in trk
    tj = src.split("async function tailorJob", 1)[1].split("\n  }\n", 1)[0]
    assert '"/api/session/start_job"' in tj and "askForJdText" in tj
