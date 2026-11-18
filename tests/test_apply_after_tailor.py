"""Apply, step two after tailoring (Kofi, 2026-11-16: "that is only step one, what about the
applying for the job part?"). The builder's review footer offers Apply for a built resume; Apply
saves it exactly as Accept & Save does, makes it this job's Auto-apply package (the same record,
approved: Waiting on you, the Tracker's Approved column) and hands the page that package's
Auto-apply row, so the page starts Assisted apply on it the way the Auto-apply queue does. A pasted
description with no link asks once for the application link. No test reaches a real site."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_latex
from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
GH = "https://boards.greenhouse.io/acme/jobs/4242"
JD = "Data Analyst at Acme. Required skills: SQL, Python, dashboards, statistics, Tableau, Excel."
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."


def _target(source_job=None, jd=""):
    me = SimpleNamespace(source_job=source_job or {}, jd=jd, _APPLY_URL_RE=WebIntake._APPLY_URL_RE)
    return WebIntake.apply_target(me)


# --- where Apply goes ----------------------------------------------------------------------------

def test_a_job_session_applies_to_its_posting():
    t = _target({"url": GH, "source": "greenhouse", "source_id": "gh:4242"}, JD)
    assert t == {"url": GH, "from": "job", "need_url": False}


def test_a_pasted_description_without_a_link_needs_one():
    assert _target({}, JD) == {"url": "", "from": "", "need_url": True}
    assert _target({"url": "", "source_id": "pasted:cv1"}, JD)["need_url"] is True


def test_a_link_in_the_paste_counts():
    t = _target({}, JD + "\nApply here: https://jobs.lever.co/acme/123/apply.\nThanks")
    assert t == {"url": "https://jobs.lever.co/acme/123/apply", "from": "pasted", "need_url": False}


# --- the route -----------------------------------------------------------------------------------

@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    yield mod
    mod._SESSION.pop("s", None)


def _built(A, template_source, fake_llm, source_job=None, jd=JD):
    s = WebIntake(jd, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(A.DB_PATH),
                  A.WORKDIR, jobname="cvapply", palace_dir=A.WORKDIR / "p")
    s.source_job = dict(source_job or {})
    s.start()
    s.submit(SKELETON)
    if s.assembled is None:
        s.submit(EXTRAS)
    assert s.assembled is not None
    A._SESSION["s"] = s
    return s


def _count(A):
    recs = CVRecords(A.DB_PATH)
    try:
        return len(recs.list(limit=1000))
    finally:
        recs.close()


@requires_latex
def test_apply_is_in_the_state_only_once_built(A, template_source, fake_llm):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), CVRecords(A.DB_PATH),
                  A.WORKDIR, jobname="cvapply0", palace_dir=A.WORKDIR / "p")
    s.source_job = {"url": GH, "source_id": "gh:4242"}
    assert "apply" not in s.start()            # nothing to apply with before a build
    s.submit(SKELETON)
    if s.assembled is None:
        s.submit(EXTRAS)
    A._SESSION["s"] = s
    st = A.app.test_client().get("/api/session/state").get_json()
    assert st["apply"] == {"url": GH, "from": "job", "need_url": False}


@requires_latex
def test_apply_saves_the_record_and_makes_the_package(A, template_source, fake_llm):
    _built(A, template_source, fake_llm, {"url": GH, "source": "greenhouse", "source_id": "gh:4242"})
    c = A.app.test_client()
    r = c.post("/api/session/apply", json={}).get_json()
    assert r["ok"] is True and isinstance(r["record_id"], int) and r["submitted"] is False
    rid = r["record_id"]
    # (a) saved exactly as Accept & Save saves: the record and its PDF
    pdf = A.WORKDIR / f"cv-{rid}.pdf"
    assert pdf.exists() and pdf.read_bytes()[:4] == b"%PDF"
    assert c.get("/api/session/state").get_json()["phase"] == "done"
    # (b) the Auto-apply package is that record: approved (Waiting on you), with the job's link
    recs = CVRecords(A.DB_PATH)
    try:
        data = A._record_data(recs.get(rid))
    finally:
        recs.close()
    assert data["status"] == "approved" and data["approved_at"]
    assert data["source_job"]["url"] == GH
    it = r["item"]
    assert it["id"] == rid and it["tile"] == "waiting" and it["action"] == "fill"
    assert it["posting_url"] == GH and it["apply_url"].startswith("https://")
    assert it["cv_path"] and Path(it["cv_path"]).exists()        # the tailored PDF the agent uploads
    assert it["lane"] == "assisted"                              # Greenhouse: you press Send
    board = c.get("/api/autoapply/board").get_json()
    assert [x["id"] for x in board["tiles"]["waiting"]] == [rid]
    # the Tracker holds it in Approved (waiting on the person) until Send
    w = A._watchlist()
    try:
        assert w.stage_of("gh:4242") == "approved"
    finally:
        w.close()
    # a second Apply (a retry, or Apply again after a park) is the same record, never a duplicate
    again = c.post("/api/session/apply", json={}).get_json()
    assert again["record_id"] == rid and _count(A) == 1
    # Send then marks it applied the way the Auto-apply bar does, and the Tracker follows
    assert c.post(f"/api/record/{rid}/status", json={"status": "applied"}).get_json()["ok"]
    w = A._watchlist()
    try:
        assert w.stage_of("gh:4242") == "applied"
    finally:
        w.close()
    assert c.post("/api/session/apply", json={}).get_json()["submitted"] is True


@requires_latex
def test_a_pasted_description_asks_for_the_link_once(A, template_source, fake_llm):
    s = _built(A, template_source, fake_llm, {})
    c = A.app.test_client()
    first = c.post("/api/session/apply", json={}).get_json()
    assert first == {"ok": False, "need_url": True}
    assert _count(A) == 0                      # nothing saved before there is somewhere to apply
    bad = c.post("/api/session/apply", json={"url": "acme careers page"})
    assert bad.status_code == 400 and bad.get_json()["need_url"] is True
    link = "https://jobs.lever.co/acme/123/apply"
    r = c.post("/api/session/apply", json={"url": link}).get_json()
    assert r["ok"] is True and r["item"]["posting_url"] == link
    assert s.apply_target() == {"url": link, "from": "job", "need_url": False}   # never asked again
    recs = CVRecords(A.DB_PATH)
    try:
        assert A._record_data(recs.get(r["record_id"]))["source_job"]["url"] == link
    finally:
        recs.close()


def test_apply_without_a_session_is_a_plain_error(A):
    A._SESSION.pop("s", None)
    A._restore_latest_once = lambda: None
    r = A.app.test_client().post("/api/session/apply", json={})
    assert r.status_code >= 400


# --- the page ------------------------------------------------------------------------------------

def _fn(name):
    return APP_JS.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def test_the_footer_has_apply_beside_accept_and_download():
    bar = HTML.split('<div class="accept-actions">', 1)[1].split("</div>", 1)[0]
    assert bar.index('id="downloadBtn"') < bar.index('id="acceptBtn"') < bar.index('id="applyBtn"')
    assert 'id="applyBtn" type="button" hidden' in bar


def test_apply_is_the_one_primary_only_for_a_built_page_in_the_desktop_app():
    p = _fn("paintApply")
    assert "st.apply" in p and "st.one_page !== false" in p and "!st.expired" in p
    assert "window.tailorShell.agentRun" in p
    # Accept & Save steps back to ghost while Apply is shown: one primary per screen
    assert 'acc.classList.toggle("btn-primary", !on)' in p and 'acc.classList.toggle("btn-ghost", on)' in p
    assert "paintApply(st);" in APP_JS.split("function applyState", 1)[1]


def test_apply_runs_the_same_fill_as_the_auto_apply_queue():
    b = _fn("builderApply")
    assert 'api("/api/session/apply"' in b
    assert "aaFill(r.record_id, { item: r.item, autonomous: !!r.autonomous })" in b
    assert "APPLY.ask = true" in b                     # the pasted-description ask
    f = _fn("aaFill")
    assert "opts.item" in f and "aaAgentRun(id, it, auto)" in f
    # the run's progress lines reach the chat as well as the bar
    assert "applyChatProgress(info);" in APP_JS


def test_the_link_is_asked_for_once():
    r = _fn("applyLinkReply")
    assert r.index("APPLY.ask = false") < r.index("https?:")   # cleared on any reply
    sub = APP_JS.split('$("#composer").addEventListener("submit"', 1)[1].split("\n  });\n", 1)[0]
    assert "if (applyLinkReply(val)) return;" in sub


def test_a_decline_that_also_says_build_it_is_a_decline_and_never_a_project():
    from ui.session import _bare_decline
    assert _bare_decline("None, leave it off. Build it.")
    assert _bare_decline("None, leave it off")
    assert _bare_decline("no thanks, just build it")
    assert not _bare_decline("Built a churn model in Python")
