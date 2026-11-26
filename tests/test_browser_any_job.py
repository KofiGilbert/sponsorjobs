"""Autofill any job opened in the Browser (2026-11-26).

A job link the person got elsewhere and opened in the Browser tab had no application behind it
(no record, no resume), so "Autofill this page" ran with nothing to fill with. Now the click reads
the job from the page already open, puts it on the Tracker (To apply; the same card when it is
already there), and offers one choice: tailor a resume for it, or use the latest resume. Either
way the fill runs with a real record, so the site's confirmation moves the card to Applied.
LinkedIn is never read or filled. FakeLLM, saved fixtures, no network.
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
MAIN_JS = ROOT / "shell-electron" / "main.js"
H = {"X-Tailor-Extension": "1"}
LEVER = "https://jobs.lever.co/example-advisory/0a1b2c3d-1111-2222-3333-444455556666"
LEVER_API = "https://api.lever.co/v0/postings/example-advisory/0a1b2c3d-1111-2222-3333-444455556666?mode=json"
BANK = "https://careers.example-bank.com/jobs/98765-financial-analyst"


def _fx(name):
    return (FX / name).read_text(encoding="utf-8")


class Net:
    def __init__(self, table):
        self.table, self.asked = table, []

    def __call__(self, url, accept="", follow=True):
        self.asked.append(url)
        hit = self.table.get(url)
        return Resp(200, hit, url) if hit is not None else Resp(404, "", url)


# ------------------------------------------------------------------------- reading the open page

def test_the_pages_own_html_is_read_with_no_request():
    net = Net({})
    res = P.read_open_page(BANK, html=_fx("careers_jsonld.html"), fetch=net)
    assert res["ok"] and res["title"] == "Financial Analyst" and res["company"] == "Example Bank"
    assert net.asked == []


def test_an_application_form_reads_its_job_from_the_ats():
    net = Net({LEVER_API: _fx("lever_posting.json"), LEVER: _fx("lever_page.html")})
    res = P.read_open_page(LEVER + "/apply", html="<html><form><input name=name></form></html>", fetch=net)
    assert res["ok"] and res["title"] == "Business Intelligence Analyst"
    assert res["source_id"].startswith("lever:example-advisory:")


def test_a_form_page_with_no_description_reads_the_posting_it_sits_under():
    form = "https://careers.example-bank.com/jobs/98765-financial-analyst/apply"
    net = Net({"https://careers.example-bank.com/robots.txt": _fx("robots_allow.txt"),
               BANK: _fx("careers_jsonld.html")})
    res = P.read_open_page(form, html=_fx("js_shell.html"), fetch=net)
    assert res["ok"] and res["title"] == "Financial Analyst"


def test_linkedin_is_never_read():
    net = Net({})
    res = P.read_open_page("https://www.linkedin.com/jobs/view/4012345678/", html="<html>x</html>", fetch=net)
    assert res["ok"] is False and res["reason"] == "paste_site" and net.asked == []


# ------------------------------------------------------------------------- the engine routes

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    monkeypatch.setattr(app, "_feed_base_url", lambda: "")
    monkeypatch.setattr(app, "_posting_check", lambda url, sid="": {"status": "open", "why": "", "at": 0})
    (tmp_path / "cv").mkdir()
    net = Net({LEVER_API: _fx("lever_posting.json"), LEVER: _fx("lever_page.html")})
    monkeypatch.setattr(P, "http_get", net)
    app._SESSION.pop("s", None)
    c = app.app.test_client()
    c.net, c.tmp = net, tmp_path
    yield c
    app._SESSION.pop("s", None)


def _card(c, sid):
    board = c.get("/api/tracker/board", headers=H).get_json()
    for stage, cards in board["columns"].items():
        for card in cards:
            if card["source_id"] == sid:
                return stage, card
    return None, None


def _old_resume(c):
    """A resume tailored earlier for another job, its PDF on disk."""
    recs = app._records()
    try:
        rid = recs.add("Data Analyst", "Northwind Example", 80, "", data={
            "jd_text": "SQL.", "status": "ready", "render_profile": {"identity": {"name": "Test Person"}}})
    finally:
        recs.close()
    (c.tmp / "cv" / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 test")
    return rid


def test_a_job_page_the_app_does_not_know_lands_on_to_apply_once(client):
    body = {"url": BANK, "html": _fx("careers_jsonld.html"), "text": "", "canonical": ""}
    r = client.post("/api/browser/job", headers=H, json=body).get_json()
    assert r["ok"] and r["created"] is True and r["title"] == "Financial Analyst"
    assert r["line"] == "Read the posting: Financial Analyst at Example Bank. Added to To apply."
    assert r["latest"] is None                                 # no resume yet: only Tailor is offered
    stage, card = _card(client, r["source_id"])
    assert stage == "saved"                                    # the To apply column
    w = app._watchlist()
    try:
        assert "PnL reporting" in w.get_job(r["source_id"])["jd_text"]
    finally:
        w.close()
    again = client.post("/api/browser/job", headers=H, json=body).get_json()
    assert again["source_id"] == r["source_id"] and again["created"] is False
    assert again["line"].endswith("It is on your Tracker.")
    assert client.net.asked == []                              # read from the page itself


def test_a_job_already_on_the_board_is_that_card(client):
    sid = "lever:example-advisory:0a1b2c3d-1111-2222-3333-444455556666"
    w = app._watchlist()
    try:
        w.track_job(sid, "saved", note="Saved", job={"source_id": sid, "source": "lever", "company": "Example Advisory",
                                                     "title": "Business Intelligence Analyst", "location": "", "remote": "",
                                                     "url": LEVER, "jd_text": "", "posted_at": "", "salary": ""})
    finally:
        w.close()
    r = client.post("/api/browser/job", headers=H, json={"url": LEVER + "/apply", "html": "<form></form>"}).get_json()
    assert r["ok"] and r["source_id"] == sid and r["created"] is False


def test_linkedin_says_it_is_never_automated_and_offers_the_text(client):
    r = client.post("/api/browser/job", headers=H, json={"url": "https://www.linkedin.com/jobs/view/4012345678/"}).get_json()
    assert r["ok"] is False and r["need_text"] is True
    assert r["error"].startswith("I never automate LinkedIn.") and "tailor a resume" in r["error"]
    assert client.net.asked == []


def test_a_page_with_an_application_already_fills_with_it(client):
    rid = _old_resume(client)
    recs = app._records()
    try:
        recs.merge_data(rid, {"source_job": {"url": BANK, "source": "page", "source_id": ""}})
    finally:
        recs.close()
    r = client.post("/api/browser/job", headers=H, json={"url": BANK + "?utm_source=x"}).get_json()
    assert r["known"] is True and r["record_id"] == rid and r["cv_path"].endswith(".pdf")


def test_use_my_latest_resume_then_the_sites_confirmation_marks_the_card_applied(client):
    old = _old_resume(client)
    r = client.post("/api/browser/job", headers=H,
                    json={"url": BANK, "html": _fx("careers_jsonld.html")}).get_json()
    assert r["latest"]["record_id"] == old and r["latest"]["label"] == "Data Analyst at Northwind Example"
    sid = r["source_id"]
    use = client.post("/api/browser/job/latest", headers=H, json={"source_id": sid}).get_json()
    rid = use["record_id"]
    assert rid != old and use["cv_path"].endswith(".pdf") and Path(use["cv_path"]).exists()
    rec = app._get_record(rid)
    data = app._record_data(rec)
    assert data["job_id"] == sid and data["source_job"]["url"] == BANK and data["reused_from"] == old
    assert "PnL reporting" in data["jd_text"] and rec["role"] == "Financial Analyst"
    assert data["render_profile"] == {"identity": {"name": "Test Person"}}
    # the shell's success watch, after the person pressed Submit on the site
    out = client.post("/api/autofill/applied", headers=H, json={"url": BANK, "record_id": rid}).get_json()
    assert out["matched"] is True and out["record_id"] == rid
    stage, _ = _card(client, sid)
    assert stage == "applied"


def test_tailor_for_a_browser_job_starts_from_its_real_description(client):
    r = client.post("/api/browser/job", headers=H,
                    json={"url": BANK, "html": _fx("careers_jsonld.html")}).get_json()
    st = client.post("/api/session/start_job", headers=H, json={"source_id": r["source_id"]})
    assert st.status_code == 200
    s = app._SESSION["s"]
    assert "PnL reporting" in s.jd and (s.role, s.company) == ("Financial Analyst", "Example Bank")
    assert s.source_job["url"] == BANK


# ------------------------------------------------------------------------- the client wiring

def test_the_button_reads_the_job_when_no_application_is_behind_the_page():
    src = APP_JS.read_text(encoding="utf-8")
    fill = src.split("async function bbFillHere(o)", 1)[1].split("\n  }\n", 1)[0]
    assert "FILL_CTX.id == null && !(o && o.profileOnly) && window.tailorShell.readPage" in fill and "bbFillUnknown(b)" in fill
    unknown = src.split("async function bbFillUnknown(b)", 1)[1].split("\n  }\n", 1)[0]
    assert '"/api/browser/job"' in unknown
    assert '"Tailor a resume for this job (about 1 min)"' in unknown and '"Use my latest resume"' in unknown
    assert 'id: "tailor", primary: true' in unknown                  # Tailor is the default
    # the choice is data the Orb can speak
    assert '"sj:browser-choice"' in src and "window.__sjBrowserChoice" in src
    # built for a Browser job -> saved as its application -> the open page is filled with it
    state = src.split("async function browserJobState(st)", 1)[1].split("\n  }\n", 1)[0]
    assert '"/api/session/apply"' in state and "fillCtx(r.record_id" in state and "bbFillHere()" in state
    assert "if (BROWSER_JOB) browserJobState(st);" in src
    # a new address typed in the bar lets the previous application go
    assert '"sj:pane-typed"' in src.split('urlBox.addEventListener("keydown"', 1)[1].split("});", 1)[0]


def test_the_shell_never_reads_a_linkedin_page():
    src = MAIN_JS.read_text(encoding="utf-8")
    h = src.split('ipcMain.handle("pane:read-job"', 1)[1].split("\n});", 1)[0]
    assert h.index("linkedin") < h.index("executeJavaScript(READ_JOB_JS")
