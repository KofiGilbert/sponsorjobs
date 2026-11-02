"""Is the posting still open? (sourcing/liveness.py, 2026-11-02)

3 of the owner's 12 "Waiting on you" jobs had been closed by the employer (Oracle Cloud HCM,
US Bank on Workday, Amazon), and the app spent his AI credits tailoring and applying to them.

* each adapter, replayed from the real answers recorded on 2026-11-02 (trimmed, no network):
  the three real closed postings read "closed", live postings on the same ATSs read "open";
* "unknown" is never closed: no answer, a 429, a bot wall, a board that is gone, a page that
  says nothing clear;
* the tailor guard: a closed job gets no model call and no package, and leaves the board;
* the queue re-check: closed records move to the closed-posting state ("Job closed"), each
  record is asked at most once in 12 hours;
* the apply agent: the engine's precheck and the first step say closed before any brain call
  (the batch skip and its sum-up run under node in tests/js/test_agent_real_pages.js).
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest

from sourcing import liveness as L

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / "tests" / "fixtures" / "liveness" / "real_responses.json").read_text(encoding="utf-8"))
CASES = {c["name"]: c for c in FIX["cases"]}
_REAL_HTTP_GET = L.http_get        # conftest keeps every other test off the network


@pytest.fixture(autouse=True)
def _fresh_cache():
    L.clear_cache()
    yield
    L.clear_cache()


def _replay(case):
    calls = []

    def fetch(url, accept, follow):
        calls.append(url)
        r = case["responses"][url]
        return L.Resp(r["status"], r["body"], r["url"], r["location"])
    return fetch, calls


def _fake(table):
    """fetch from {url: (status, body[, final_url[, location]])}; anything else is no answer."""
    calls = []

    def fetch(url, accept, follow):
        calls.append(url)
        if url not in table:
            return L.Resp(0, "", url)
        t = table[url]
        return L.Resp(t[0], t[1], t[2] if len(t) > 2 else url, t[3] if len(t) > 3 else "")
    return fetch, calls


# --- the real answers of 2026-11-02 ------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CASES))
def test_each_adapter_on_the_real_answers(name):
    case = CASES[name]
    fetch, calls = _replay(case)
    got = L.check(case["url"], case.get("source_id", ""), fetch=fetch, use_cache=False)
    assert got["status"] == case["expect"], (name, got)
    assert calls, "every verdict comes from an answer"


def test_the_three_closed_jobs_of_the_owners_queue_read_closed():
    for name in ("workday_usbank_closed", "oracle_closed", "amazon_closed"):
        assert CASES[name]["expect"] == L.CLOSED
    # and the same ATSs' live postings read open
    for name in ("workday_usbank_open", "workday_gartner_open", "oracle_open", "amazon_open",
                 "greenhouse_upgrade_open", "lever_open", "ashby_open", "workable_open", "recruitee_open"):
        assert CASES[name]["expect"] == L.OPEN


def test_each_ats_uses_its_own_public_json():
    def asked(name):
        return list(CASES[name]["responses"])
    assert asked("workday_usbank_closed") == [
        "https://usbank.wd1.myworkdayjobs.com/wday/cxs/usbank/US_Bank_Careers/job/Minneapolis-MN/Business-Intelligence-Analyst_2026-0019717"]
    assert asked("oracle_closed")[0].startswith(
        "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?")
    assert "finder=ById;Id=%22347651%22,siteNumber=jobsearch" in asked("oracle_closed")[0]
    assert asked("greenhouse_company_domain_open") == [
        "https://boards-api.greenhouse.io/v1/boards/octanelending/jobs/8015779003"]   # the board from the source_id
    assert asked("lever_open")[0].startswith("https://api.lever.co/v0/postings/15five/")
    assert asked("workable_open")[1] == "https://apply.workable.com/api/v2/accounts/huggingface/jobs/81B46579FE"
    assert asked("recruitee_open") == ["https://channable.recruitee.com/api/offers/senior-engineering-manager-core"]
    assert all("/api/" not in u for u in asked("ashby_open"))       # jobs.ashbyhq.com robots.txt disallows /api/


# --- unknown is never closed --------------------------------------------------------------------

WD = "https://acme.wd5.myworkdayjobs.com/External/job/Austin-TX/Analyst_R1"
WD_API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Austin-TX/Analyst_R1"


@pytest.mark.parametrize("answer", [
    (0, ""),                                                     # no answer at all
    (429, '{"errorCode":"S99","httpStatus":429}'),               # rate limited
    (403, "<html>Access denied</html>"),                         # a bot wall is not Workday's JSON
    (500, ""),
    (200, '{"unexpected": true}'),
])
def test_workday_unclear_answers_are_unknown(answer):
    fetch, _ = _fake({WD_API: answer})
    assert L.check(WD, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN


def test_workday_locale_prefix_and_404():
    fetch, calls = _fake({WD_API: (404, '{"errorCode":"S21","httpStatus":404}')})
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Analyst_R1"
    assert L.check(url, fetch=fetch, use_cache=False)["status"] == L.CLOSED
    assert calls == [WD_API]


def test_a_board_that_is_gone_is_not_a_closed_job():
    gh = "https://job-boards.greenhouse.io/acme/jobs/123456"
    fetch, _ = _fake({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/123456": (404, "{}"),
                      "https://boards-api.greenhouse.io/v1/boards/acme": (404, "{}")})
    assert L.check(gh, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN
    lv = "https://jobs.lever.co/acme/abc-123"
    fetch, _ = _fake({"https://api.lever.co/v0/postings/acme/abc-123?mode=json": (404, "{}"),
                      "https://api.lever.co/v0/postings/acme?mode=json&limit=1": (404, "{}")})
    assert L.check(lv, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN


def test_oracle_no_item_on_a_wrong_site_is_unknown():
    url = "https://x.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/nosuchsite/job/12345"
    rest = "https://x.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
    fetch, _ = _fake({
        rest + 'recruitingCEJobRequisitionDetails?onlyData=true&finder=ById;Id=%2212345%22,siteNumber=nosuchsite':
            (200, '{"items": [], "count": 0}'),
        rest + "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=nosuchsite,limit=1":
            (200, '{"items": [{"TotalJobsCount": 0}]}'),
    })
    assert L.check(url, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN


def test_smartrecruiters_api_is_never_asked():
    url = "https://jobs.smartrecruiters.com/Acme/744000012345678"
    fetch, calls = _fake({url: (200, "<html><body><h1>Analyst</h1><p>Apply now</p></body></html>")})
    assert L.check(url, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN
    assert all("api.smartrecruiters.com" not in u for u in calls)


# --- any other careers page: closed only on a clear signal -------------------------------------

PAGE = "https://careers.example.com/job/12345678/analyst"


def test_a_short_closed_line_is_closed():
    body = "<html><body><header>Careers</header><div>This position is no longer available.</div></body></html>"
    fetch, _ = _fake({PAGE: (200, body)})
    out = L.check(PAGE, fetch=fetch, use_cache=False)
    assert out["status"] == L.CLOSED and "no longer available" in out["why"]


def test_a_job_description_that_mentions_closing_is_not_closed():
    jd = ("<html><body><h1>Analyst</h1><p>" + "We build data products for our clients across many markets. " * 6
          + "Applications are closed for this role once we have enough candidates, so apply soon.</p>"
          + "<button>Apply now</button></body></html>")
    fetch, _ = _fake({PAGE: (200, jd)})
    assert L.check(PAGE, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN


def test_404_410_and_a_redirect_to_search_are_closed_but_a_canonical_redirect_is_not():
    for code in (404, 410):
        fetch, _ = _fake({PAGE: (code, "")})
        assert L.check(PAGE, fetch=fetch, use_cache=False)["status"] == L.CLOSED
    fetch, _ = _fake({PAGE: (200, "<html><body>Search jobs</body></html>", "https://careers.example.com/search?q=")})
    assert L.check(PAGE, fetch=fetch, use_cache=False)["status"] == L.CLOSED
    fetch, _ = _fake({PAGE: (200, "<html><body>Analyst</body></html>", "https://careers.example.com/en/job/12345678/analyst")})
    assert L.check(PAGE, fetch=fetch, use_cache=False)["status"] == L.UNKNOWN
    posting = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Analyst"}</script>'
    fetch, _ = _fake({PAGE: (200, "<html><head>" + posting + "</head><body>Analyst</body></html>")})
    assert L.check(PAGE, fetch=fetch, use_cache=False)["status"] == L.OPEN


# --- polite: cached per URL, tracking params dropped, a few at a time ---------------------------

def test_cached_per_url_without_tracking_params():
    fetch, calls = _fake({PAGE: (404, "")})
    assert L.check(PAGE + "?utm_source=freehire.me", fetch=fetch)["status"] == L.CLOSED
    again = L.check(PAGE + "?utm_source=other", fetch=fetch)
    assert again["status"] == L.CLOSED and again.get("cached") and len(calls) == 1
    later = L.check(PAGE, fetch=fetch, now=time.time() + L.TTL[L.CLOSED] + 1)
    assert not later.get("cached") and len(calls) == 2


def test_check_many_and_posting_status():
    fetch, _ = _fake({PAGE: (404, ""), "https://careers.example.com/job/99999999": (200, "<html>x</html>")})
    out = L.check_many([(1, PAGE, ""), (2, "https://careers.example.com/job/99999999", ""), (3, "not a link", "")],
                       fetch=fetch)
    assert {k: v["status"] for k, v in out.items()} == {1: L.CLOSED, 2: L.UNKNOWN, 3: L.UNKNOWN}
    assert L.posting_status(PAGE, fetch=fetch) == L.CLOSED


def test_requests_carry_the_sourcing_user_agent(monkeypatch):
    seen = {}

    class _Opener:
        def open(self, req, timeout):
            seen["ua"], seen["timeout"] = req.get_header("User-agent"), timeout
            raise OSError("offline")
    monkeypatch.setattr(L.urllib.request, "build_opener", lambda *a: _Opener())
    r = _REAL_HTTP_GET("https://example.com/x")
    assert r.status == 0
    from sourcing.ats import _UA
    assert seen == {"ua": _UA, "timeout": L.TIMEOUT}


# --- the app: tailor guard, queue re-check, the apply agent ------------------------------------

H = {"X-Tailor-Extension": "1"}
CLOSED_URL = "https://usbank.wd1.myworkdayjobs.com/US_Bank_Careers/job/Minneapolis-MN/Business-Intelligence-Analyst_2026-0019717"
OPEN_URL = "https://gartner.wd5.myworkdayjobs.com/EXT/job/Irving-TX/Business-Analyst_113018"


def _verdicts(monkeypatch, closed=(CLOSED_URL,)):
    asked = []

    def fake(url, source_id="", **kw):
        asked.append(url)
        st = L.CLOSED if url in closed else L.OPEN
        return {"status": st, "why": "Workday: the posting was taken down" if st == L.CLOSED else "ok", "at": time.time()}
    monkeypatch.setattr(L, "check", fake)
    return asked


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    ids = {}
    for name, url, sid, status in (("closed", CLOSED_URL, "workday:usbank.wd1/US_Bank_Careers:2026-0019717", "approved"),
                                   ("open", OPEN_URL, "workday:gartner.wd5/EXT:113018", "approved"),
                                   ("review", CLOSED_URL + "?x=1", "", "ready")):
        ids[name] = recs.add("Analyst", "Acme", 70, "", data={
            "jd_text": "SQL.", "status": status,
            "source_job": {"url": url, "source": "workday", "source_id": sid}})
    recs.close()
    return mod, mod.app.test_client(), ids


def _no_model(mod, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the model was called for a closed job")
    monkeypatch.setattr(mod, "_make_llm", boom)
    monkeypatch.setattr(mod, "_count_package", boom)


def _row(c, rid):
    return next(it for t in c.get("/api/autoapply/board", headers=H).get_json()["tiles"].values()
                for it in t if it["id"] == rid)


def test_tailor_from_a_closed_job_spends_nothing_and_takes_it_off_the_board(A, monkeypatch):
    mod, c, ids = A
    _verdicts(monkeypatch)
    _no_model(mod, monkeypatch)
    sid = "workday:usbank.wd1/US_Bank_Careers:2026-0019717"
    w = mod._watchlist()
    w.upsert_jobs([{"source_id": sid, "source": "workday", "company": "U.S. Bank", "title": "BI Analyst",
                    "url": CLOSED_URL, "location": "Minneapolis, MN", "remote": "", "jd_text": "SQL and Tableau.",
                    "posted_at": "", "salary": ""}])
    w.close()
    r = c.post("/api/session/start_job", headers=H, json={"source_id": sid})
    body = r.get_json()
    assert r.status_code == 409 and body["closed"] is True
    assert body["error"] == "This job is closed. Nothing was spent."
    w = mod._watchlist()
    try:
        assert sid in w.closed_ids()
        assert sid not in {j["source_id"] for j in w.list_jobs()}            # off the Jobs board
    finally:
        w.close()
    assert sid in mod._closed_job_ids()
    # the queued record of the same job moved to the closed-posting state too
    assert mod._record_data(mod._get_record(ids["closed"]))["submission"]["reason"] == "posting_closed"


def test_unattended_tailor_skips_a_closed_job_before_the_package(A, monkeypatch):
    mod, c, ids = A
    _verdicts(monkeypatch)
    _no_model(mod, monkeypatch)
    out = mod._tailor_job_unattended({"title": "BI Analyst", "company": "U.S. Bank", "url": CLOSED_URL,
                                      "source_id": "workday:usbank.wd1/US_Bank_Careers:2026-0019717"},
                                     None, "", "classic", "_scratch")
    assert out == {"ok": False, "role": "BI Analyst", "reason": "This job is closed. Nothing was spent.", "closed": True}

    class Reached(Exception):
        pass

    def reached(llm):
        raise Reached
    monkeypatch.setattr(mod, "_count_package", reached)
    with pytest.raises(Reached):                       # an open job goes on to the package as before
        mod._tailor_job_unattended({"title": "Analyst", "url": OPEN_URL, "source_id": ""}, None, "", "classic", "_s")


def test_the_queue_recheck_moves_closed_jobs_and_asks_once_in_12_hours(A, monkeypatch):
    mod, c, ids = A
    monkeypatch.setattr(mod, "_liveness_kick", lambda rows: False)       # this test runs the sweep itself
    asked = _verdicts(monkeypatch, closed=(CLOSED_URL, CLOSED_URL + "?x=1"))
    out = mod._liveness_recheck()
    assert out == {"checked": 3, "closed": 2}
    row = _row(c, ids["closed"])
    assert row["tile"] == "needs" and row["reason"] == "posting_closed" and row["action"] == "view"
    assert _row(c, ids["review"])["reason"] == "posting_closed"
    assert _row(c, ids["open"])["tile"] == "waiting"
    data = mod._record_data(mod._get_record(ids["open"]))
    assert data["liveness"]["status"] == "open"
    n = len(asked)
    assert mod._liveness_recheck() == {"checked": 0, "closed": 0} and len(asked) == n     # not again within 12 h
    later = time.time() + mod.LIVENESS_RECHECK_S + 5
    assert mod._liveness_recheck(now=later)["checked"] == 1                               # only the open one is still queued


def test_the_recheck_also_closes_a_paused_run(A, monkeypatch):
    """A run paused on "needs you" whose posting then closed moves to Job closed, pause cleared."""
    mod, c, ids = A
    monkeypatch.setattr(mod, "_liveness_kick", lambda rows: False)
    _verdicts(monkeypatch, closed=(CLOSED_URL, CLOSED_URL + "?x=1"))
    recs = mod._records()
    try:
        recs.merge_data(ids["closed"], {"agent_park": {"kind": "person", "message": "This one needs you."}})
    finally:
        recs.close()
    assert mod._liveness_recheck()["closed"] == 2
    data = mod._record_data(mod._get_record(ids["closed"]))
    assert data["submission"]["reason"] == "posting_closed" and not data.get("agent_park")
    assert _row(c, ids["closed"])["action"] == "view"


def test_the_board_starts_the_recheck_in_the_background(A, monkeypatch):
    mod, c, ids = A
    _verdicts(monkeypatch)
    b = c.get("/api/autoapply/board", headers=H).get_json()
    assert b["rechecking"] is True
    for _ in range(100):
        st = c.get("/api/autoapply/liveness", headers=H).get_json()
        if not st["running"]:
            break
        time.sleep(0.05)
    assert st["running"] is False and st["closed"] == 1 and st["checked"] == 3
    assert _row(c, ids["closed"])["reason"] == "posting_closed"
    assert c.get("/api/autoapply/board", headers=H).get_json()["rechecking"] is False


def test_the_apply_agent_precheck_and_first_step_say_closed_before_any_brain_call(A, monkeypatch):
    mod, c, ids = A
    monkeypatch.setattr(mod, "_liveness_kick", lambda rows: False)
    _verdicts(monkeypatch, closed=(CLOSED_URL, CLOSED_URL + "?x=1"))
    _no_model(mod, monkeypatch)
    r = c.post("/api/autoapply/agent/precheck", headers=H, json={"record_id": ids["open"], "url": OPEN_URL}).get_json()
    assert r["closed"] is False and _row(c, ids["open"])["tile"] == "waiting"
    r = c.post("/api/autoapply/agent/precheck", headers=H, json={"record_id": ids["closed"], "url": CLOSED_URL}).get_json()
    assert r["closed"] is True and r["phrase"]
    assert _row(c, ids["closed"])["reason"] == "posting_closed"
    # the step guard: a run that skipped the precheck still costs no brain call
    r = c.post("/api/autoapply/agent/step", headers=H, json={
        "record_id": ids["review"], "step": 1, "url": CLOSED_URL, "view": "[1]<button>Apply</button>", "history": []}).get_json()
    assert r["ok"] is False and r["closed"] is True and r["reason"] == "job-closed" and r["cost"] == 0.0
    assert _row(c, ids["review"])["reason"] == "posting_closed"
    # a later step (history carried) is never intercepted: the check is before the FIRST call
    r = c.post("/api/autoapply/agent/step", headers=H, json={
        "record_id": ids["open"], "step": 2, "url": OPEN_URL, "view": "[1]<button>Apply</button>",
        "history": [{"step": 1}]}).get_json()
    assert not r.get("closed")
