"""Every resume chat is kept, and an unfinished one survives the engine (Kofi, 2026-11-10: an app
restart lost his D365 Software Developer resume, because the live builder session lived only in
memory). The session is written to local SQLite after each request that touched it; the most
recent unfinished one comes back on its own after a restart; any one opens by id from a list
(newest first, searchable); a saved one is kept but never reopens by itself; a chat whose job has
closed opens as a record and says so. FakeLLM and real pdflatex, no model calls."""

from __future__ import annotations

import pytest

import ui.app as app
from conftest import requires_latex

JD = "Data Analyst at Northwind. Required skills: SQL, Python, dashboards, statistics, Tableau, Excel."
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    monkeypatch.setattr(app, "_feed_base_url", lambda: "")
    app._SESSION.pop("s", None)
    app._SESSION_SIG.clear()
    monkeypatch.setitem(app._SESSION_RESTORE, "tried", True)
    yield app.app.test_client()
    app._SESSION.pop("s", None)


def _restart():
    """What an engine restart leaves: nothing in memory, the store on disk."""
    app._SESSION.pop("s", None)
    app._SESSION_SIG.clear()
    app._SESSION_RESTORE["tried"] = False


def _build(c, jd=JD):
    assert c.post("/api/session/start", json={"jd": jd, "template": "shetty"}).status_code == 200
    st = c.post("/api/session/answer", json={"text": SKELETON}).get_json()
    if st.get("phase") != "review":
        st = c.post("/api/session/answer", json={"text": EXTRAS}).get_json()
    assert st["phase"] == "review" and st.get("pdf"), st.get("messages")
    return st


@requires_latex
def test_an_unfinished_resume_survives_an_engine_restart_with_its_chat(client):
    built = _build(client)
    before = client.get("/api/session/state").get_json()
    _restart()

    after = client.get("/api/session/state").get_json()
    assert after["active"] is True and after["phase"] == "review"
    assert after["session_id"] == before["session_id"]
    assert after["role"] == before["role"] and after["template"] == "shetty"
    assert [(t["role"], t["content"]) for t in after["history"]] == \
           [(t["role"], t["content"]) for t in before["history"]]          # the whole chat
    assert after["pdf"] == built["pdf"] or after["pdf"].split("&v=")[0] == built["pdf"].split("&v=")[0]
    assert after["preview"] == before["preview"]                          # the same page
    assert after["coverage"] == before["coverage"]
    pdf = client.get(after["pdf"])
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"            # still downloadable
    # and the chat carries on, on the restored session
    more = client.post("/api/session/answer", json={"text": "Thanks, that looks good."})
    assert more.status_code == 200


@requires_latex
def test_a_saved_resume_is_kept_but_not_reopened_on_restart(client):
    _build(client)
    acc = client.post("/api/session/accept").get_json()
    assert acc["ok"] is True
    _restart()
    assert client.get("/api/session/state").get_json() == {"active": False}
    rows = client.get("/api/sessions").get_json()["sessions"]
    assert len(rows) == 1 and rows[0]["status"] == "saved" and rows[0]["record_id"] == acc["record_id"]
    # still opens by id, as the record it is
    st = client.post(f"/api/sessions/{rows[0]['id']}/open").get_json()
    assert st["active"] is True and st["phase"] == "done" and st["expired"] is False


def test_the_list_is_newest_first_searchable_and_deletable(client):
    for jd in ("Data Analyst at Northwind. SQL, Python.",
               "Business Analyst at Contoso. Requirements, SQL.",
               "Site Reliability Engineer at MongoDB. Kubernetes, Go."):
        assert client.post("/api/session/start", json={"jd": jd}).status_code == 200
    rows = client.get("/api/sessions").get_json()["sessions"]
    assert [r["company"] for r in rows] == ["MongoDB", "Contoso", "Northwind"]
    assert rows[0]["live"] is True and not any(r["live"] for r in rows[1:])
    assert {r["status"] for r in rows} == {"in_progress"}
    for k in ("role", "company", "template", "template_label", "fill", "created_at", "updated_at"):
        assert k in rows[0]
    assert [r["company"] for r in client.get("/api/sessions?q=contoso").get_json()["sessions"]] == ["Contoso"]
    assert [r["company"] for r in client.get("/api/sessions?q=reliability").get_json()["sessions"]] == ["MongoDB"]

    # opening an older one makes it the open chat, with its own job
    st = client.post(f"/api/sessions/{rows[2]['id']}/open").get_json()
    assert st["company"] == "Northwind" and st["session_id"] == rows[2]["id"]
    assert client.get("/api/session/state").get_json()["company"] == "Northwind"

    # deleting the open one closes the builder and leaves the others
    assert client.delete(f"/api/sessions/{rows[2]['id']}").get_json()["deleted"] is True
    assert client.get("/api/session/state").get_json() == {"active": False}
    assert [r["company"] for r in client.get("/api/sessions").get_json()["sessions"]] == ["MongoDB", "Contoso"]
    assert client.post(f"/api/sessions/{rows[2]['id']}/open").status_code == 404


def test_the_most_recent_unfinished_one_is_the_one_restored(client):
    client.post("/api/session/start", json={"jd": "Data Analyst at Northwind. SQL."})
    client.post("/api/session/start", json={"jd": "Business Analyst at Contoso. SQL."})
    _restart()
    assert client.get("/api/session/state").get_json()["company"] == "Contoso"


def _feed_job(sid="greenhouse::nw-1", title="Data Analyst", company="Northwind"):
    """A job as the Jobs page holds it (no posting URL, so no network check runs)."""
    w = app._watchlist()
    try:
        w.upsert_jobs([{"source_id": sid, "source": "greenhouse", "company": company, "title": title,
                        "location": "Chicago, IL", "remote": "", "url": "", "posted_at": "2026-11-09", "jd_text": f"{title} at {company}. SQL, Python, Tableau."}])
    finally:
        w.close()
    return sid


def test_a_closed_job_opens_as_a_record_and_says_so_once(client):
    jid = _feed_job()
    assert client.post("/api/session/start_job", json={"source_id": jid}).status_code == 200
    s = app._SESSION["s"]
    assert s.job_id == jid
    client.post("/api/session/answer", json={"text": "I'm Kwame Asante."})      # kept with its job
    sid = str(s.jobname)
    w = app._watchlist()
    try:
        w.mark_closed(jid, "", "no longer accepting")
    finally:
        w.close()
    _restart()
    app._SESSION_RESTORE["tried"] = True
    st = client.post(f"/api/sessions/{sid}/open").get_json()
    assert st["expired"] is True and st["expired_reason"] == "posting closed"
    first = st["history"][-1]
    assert first["role"] == "agent"
    # the AI says it plainly, with the role and company from the job, not invented
    assert first["content"].startswith("The Data Analyst role at Northwind has closed")
    assert "record" in first["content"] and st["messages"] == [first["content"]]
    assert "\u2014" not in first["content"] and "\u2013" not in first["content"]
    # the note's facts: the company and the day it was found closed
    assert st["expired_note"]["company"] == "Northwind" and st["expired_note"]["closed_at"]
    again = client.post(f"/api/sessions/{sid}/open").get_json()
    assert again["expired"] is True
    assert sum(1 for t in again["history"] if "has closed" in (t["content"] or "")) == 1
    # the job is stamped closed with its reason, and NOT moved to Rejected
    card = client.get(f"/api/tracker/job?source_id={jid}").get_json()
    assert card["closed_at"] and card["closed_reason"] == "posting closed"
    assert card["stage"] == "archived"          # Closed, not Rejected


def test_a_job_still_open_is_not_expired(client):
    client.post("/api/session/start", json={"jd": "Data Analyst at Northwind. SQL, Python."})
    sid = str(app._SESSION["s"].jobname)
    st = client.post(f"/api/sessions/{sid}/open").get_json()
    assert st["expired"] is False and not any("has closed" in (t["content"] or "") for t in st["history"])


def test_erasing_memory_erases_every_kept_chat(client):
    client.post("/api/session/start", json={"jd": "Data Analyst at Northwind. SQL."})
    assert client.get("/api/sessions").get_json()["sessions"]
    assert client.post("/api/memory/erase", json={}).status_code == 200
    assert client.get("/api/sessions").get_json()["sessions"] == []
    _restart()
    assert client.get("/api/session/state").get_json() == {"active": False}


def test_a_pasted_description_gets_its_own_tracker_job(client):
    client.post("/api/session/start", json={"jd": "Business Analyst at Contoso\nLocation: Austin, TX\n"
                                                    "Requirements gathering, SQL, stakeholder workshops."})
    s = app._SESSION["s"]
    assert s.job_id == f"pasted:{s.jobname}"
    w = app._watchlist()
    try:
        row = w.board_job(s.job_id)
        assert w.get_job(s.job_id)["jd_text"].startswith("Business Analyst at Contoso")
        assert s.job_id not in {j["source_id"] for j in w.list_jobs(limit=100)}   # never on the Jobs feed
    finally:
        w.close()
    assert row["source"] == "pasted" and row["company"] == "Contoso" and row["stage"] == "in_progress"
    assert row["location"] == "Austin, TX" and "Business Analyst" in row["title"]
    rows = client.get(f"/api/sessions?job_id={s.job_id}").get_json()["sessions"]
    assert [r["id"] for r in rows] == [str(s.jobname)] and rows[0]["current"] is True


@requires_latex
def test_the_job_card_carries_its_resume_session(client):
    _build(client)
    s = app._SESSION["s"]
    board = client.get("/api/tracker/board").get_json()
    cards = [c for col in board["columns"].values() for c in col]
    card = next(c for c in cards if c["source_id"] == s.job_id)
    assert card["has_session"] is True
    sess = card["session"]
    assert sess["status"] == "in_progress" and sess["template"] == "shetty"
    assert isinstance(sess["fill"], int) and isinstance(sess["keywords"], int) and sess["updated_at"]
    assert sess["pdf"] == f"/api/cv.pdf?job={s.jobname}"
    acc = client.post("/api/session/accept").get_json()
    board = client.get("/api/tracker/board").get_json()
    cards = [c for col in board["columns"].values() for c in col]
    mine = [c for c in cards if c["source_id"] == s.job_id]
    assert len(mine) == 1 and len(cards) == 1          # the saved resume is the same job, one card
    assert mine[0]["session"]["status"] == "saved"
    assert mine[0]["session"]["pdf"] == f"/api/cv.pdf?job=cv-{acc['record_id']}"


def test_one_job_keeps_every_session_newest_is_current(client):
    jid = _feed_job()
    client.post("/api/session/start_job", json={"source_id": jid})
    first = str(app._SESSION["s"].jobname)
    client.post("/api/session/answer", json={"text": "I'm Kwame Asante."})
    client.post("/api/session/start_job", json={"source_id": jid, "template": "data"})
    second = str(app._SESSION["s"].jobname)
    panel = client.get(f"/api/tracker/job?source_id={jid}").get_json()
    assert [x["id"] for x in panel["sessions"]] == [second, first]
    assert [x["current"] for x in panel["sessions"]] == [True, False]
    assert panel["has_session"] is True and panel["session"]["id"] == second
    # "open this job's resume chat" is one lookup, and lands on the current one
    client.post(f"/api/sessions/{first}/open")
    st = client.post("/api/tracker/session/open", json={"source_id": jid}).get_json()
    assert st["session_id"] == second and st["expired"] is False
    assert client.post("/api/tracker/session/open", json={"source_id": "nope"}).status_code == 404


@requires_latex
def test_home_continue_lists_jobs_in_progress_then_recent(client):
    _build(client)                                         # one resume in progress
    live = app._SESSION["s"]
    jid = _feed_job("greenhouse::cl-1", "Senior Data Analyst", "ConData")
    client.post("/api/session/start_job", json={"source_id": jid})
    w = app._watchlist()
    try:
        w.mark_closed(jid, "", "closed")
    finally:
        w.close()
    client.post(f"/api/sessions/{app._SESSION['s'].jobname}/open")      # detects the closed posting
    d = client.get("/api/tracker/continue").get_json()
    assert d["count"] == 1
    assert [x["source_id"] for x in d["in_progress"]] == [live.job_id]
    row = d["in_progress"][0]
    assert row["title"] and row["company"] == "Northwind" and isinstance(row["keywords"], int)
    assert [x["source_id"] for x in d["recent"]] == [jid] and d["recent"][0]["closed_at"]
    # the card keeps In progress after Accept & Save, until it is applied
    client.post(f"/api/sessions/{live.jobname}/open")
    client.post("/api/session/accept")
    card = client.get(f"/api/tracker/job?source_id={live.job_id}").get_json()
    assert card["stage"] == "in_progress" and card["session"]["status"] == "saved"
    recs = client.get("/api/records").get_json()["records"]
    assert recs[0]["session_id"] == str(live.jobname) and recs[0]["job_id"] == live.job_id


def test_a_jobs_page_role_carries_its_session(client):
    jid = _feed_job()
    client.post("/api/session/start_job", json={"source_id": jid})
    rows = app._jobs_with_sessions([{"source_id": jid}, {"source_id": "other::1"}])
    assert rows[0]["my_session"]["status"] == "in_progress" and rows[0]["my_session"]["updated_at"]
    assert "my_session" not in rows[1]


def test_an_applied_job_whose_posting_closes_stays_applied_and_says_so(client):
    # The application stands when a posting closes after it: the card stays in Applied with
    # "Posting closed ...", and the chat reopens with the closed note, worded for an application.
    jid = _feed_job()
    assert client.post("/api/session/start_job", json={"source_id": jid}).status_code == 200
    client.post("/api/session/answer", json={"text": "I'm Kwame Asante."})
    sid = str(app._SESSION["s"].jobname)
    w = app._watchlist()
    try:
        w.move_stage(jid, "applied")
        w.mark_closed(jid, "", "no longer accepting")
    finally:
        w.close()
    _restart()
    app._SESSION_RESTORE["tried"] = True
    st = client.post(f"/api/sessions/{sid}/open").get_json()
    assert st["expired"] is True and st["expired_note"]["applied"] is True
    assert st["expired_note"]["closed_at"]
    first = st["history"][-1]["content"]
    assert first.startswith("The posting for the Data Analyst role at Northwind has closed")
    assert "application still stands" in first
    assert "—" not in first and "–" not in first and "CV" not in first
    card = client.get(f"/api/tracker/job?source_id={jid}").get_json()
    assert card["stage"] == "applied" and card["closed_at"]
    board = client.get("/api/tracker/board").get_json()
    cards = [c for c in board["columns"]["applied"] if c["source_id"] == jid]
    assert cards and cards[0]["closed_at"] and not board["columns"]["archived"]
    for stage in ("interviewing", "offer"):
        w = app._watchlist()
        try:
            w.move_stage(jid, stage)
            app._close_posting_on_board(w, jid)
            assert w.stage_of(jid) == stage
        finally:
            w.close()
