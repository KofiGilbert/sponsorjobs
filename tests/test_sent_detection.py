"""Did the person actually send the application? The layered answer (2026-11-26).

* The shell's one detector (shell-electron/sent_detect.js) combines the page, the network, the
  form going blank, a popup and the person's own yes; its node tests run here.
* The confirmation pages of the day's runs count through the fill engine's own markers.
* Every Applied move the app makes by itself records WHICH evidence decided it, and the Tracker's
  timeline says it ("Application submitted: sent to Greenhouse, form cleared after Submit").
* Starting Autofill on an application already sent is asked about first (/api/autofill/sent-state).
* "Did you submit?" is asked after the fill, and ONE more time a day later (the daily sweep).
* A confirmation email moves a card only when it surely names that application: a guess among
  several, or a nameless mail from the hiring system only, asks instead.

Every company and person here is made up. No test reaches the network (tests/conftest.py)."""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

from inbox import detect, matching
from inbox.suggestions import build_suggestions
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}


# --------------------------------------------------------------------------- the shell, under node

@pytest.mark.parametrize("script,ok", [("test_sent_detect.js", "sent_detect ok"), ("test_sent_pages.js", "sent pages ok")])
def test_the_detector_under_node(script, ok):
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert ok in out.stdout


def test_the_detector_is_the_one_place_that_announces():
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    # every source feeds the detector; only sentFired announces
    assert main.count("appliedWatch.announceSubmitted(") == 1
    fired = main[main.index("async function sentFired("):]
    assert "appliedWatch.announceSubmitted(" in fired[:fired.index("\n}\n")]
    arm = main[main.index("function armSuccessWatch("):main.index("function armNetWatch(")]
    for sig in ('kind: "probe"', 'kind: "page", how: "marker"', 'kind: "page", how: "model"'):
        assert sig in arm, sig
    assert 'sentDetect.signal({ kind: "popup"' in main                    # (d) windows the site opens
    net = main[main.index("function armNetWatch("):main.index("async function sentFired(")]
    assert "maxPostDataSize: 0" in main and "postData" not in net          # never a request body
    assert "console." not in net                                          # and nothing logged
    assert 'disarmSuccessWatch("closed")' in main and 'disarmSuccessWatch("left-site")' in main
    pre = (ROOT / "shell-electron" / "preload.js").read_text(encoding="utf-8")
    assert '"assist:ask-sent"' in pre and '"assist:ask-sent"' in main


# --------------------------------------------------------------------------- the engine

@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.delenv("SJ_APPLY_AGENT", raising=False)
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_todo_liveness_kick", lambda jobs=None: False)
    return mod


def _add(A, role, sid, url="https://boards.greenhouse.io/lark/jobs/1", **data):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, "Larkspur Labs", 70, "", jd_label=role,
                        data={"status": "approved", "source_job": {"url": url, "source_id": sid}, **data})
    finally:
        recs.close()


def _data(A, rid):
    recs = CVRecords(A.DB_PATH)
    try:
        return A._record_data(recs.get(rid))
    finally:
        recs.close()


def _card(c, sid):
    cols = c.get("/api/tracker/board").get_json()["columns"]
    return next((x for col in cols.values() for x in col if x["source_id"] == sid), None)


def _notes(c, sid):
    c.get("/api/tracker/board")                  # the board takes in the records first
    return [a.get("note") or "" for a in c.get(f"/api/tracker/activity?source_id={sid}").get_json()["activity"]]


def test_evidence_words_come_from_a_fixed_list(A):
    ev = A._sent_evidence([{"kind": "network-ats", "ats": "Greenhouse"}, {"kind": "form-reset"}])
    assert ev["kinds"] == ["network-ats", "form-reset"] and ev["label"] == "sent to Greenhouse, form cleared after Submit"
    # a name the engine does not know is never stored as said; unknown kinds are dropped
    assert A._sent_evidence([{"kind": "network-ats", "ats": "<b>Evil</b>"}])["label"] == "sent to the hiring system"
    assert A._sent_evidence([{"kind": "made-up"}, "x"]) is None
    assert A._sent_evidence("page-marker") is None
    assert A._sent_evidence([{"kind": "form-reset", "gone": True}])["label"] == "form closed after Submit"


def test_an_applied_move_records_and_shows_which_evidence(A):
    rid = _add(A, "Data Analyst", "gh:lark1")
    c = A.app.test_client()
    body = {"url": "https://boards.greenhouse.io/lark/jobs/1", "page_url": "https://boards.greenhouse.io/lark/jobs/1",
            "record_id": rid, "evidence": [{"kind": "network-ats", "ats": "Greenhouse"}, {"kind": "form-reset"}]}
    out = c.post("/api/autofill/applied", headers=H, json=body).get_json()
    assert out["matched"] is True
    assert _data(A, rid)["sent_evidence"]["label"] == "sent to Greenhouse, form cleared after Submit"
    card = _card(c, "gh:lark1")
    assert card["stage"] == "applied"
    assert "Application submitted: sent to Greenhouse, form cleared after Submit" in _notes(c, "gh:lark1")


def test_the_models_yes_is_recorded_as_the_thank_you_page(A, monkeypatch):
    monkeypatch.setattr(A, "_confirm_model", lambda s, u: '{"submitted": "yes"}')
    rid = _add(A, "Engineer", "gh:lark2", url="https://boards.greenhouse.io/lark/jobs/2")
    c = A.app.test_client()
    done = {"url": "https://boards.greenhouse.io/lark/jobs/2", "page_url": "https://boards.greenhouse.io/lark/jobs/2/confirmation",
            "record_id": rid, "text": "Thank you for applying. Your application has been received.", "fields": 0}
    assert c.post("/api/autofill/confirm-check", headers=H, json=done).get_json()["verdict"] == "yes"
    assert _data(A, rid)["sent_evidence"]["label"] == "thank-you page"
    assert "Application submitted: thank-you page" in _notes(c, "gh:lark2")


def test_you_said_yes_shows_on_the_timeline(A):
    rid = _add(A, "Analyst", "gh:lark3", url="https://boards.greenhouse.io/lark/jobs/3")
    c = A.app.test_client()
    _card(c, "gh:lark3")
    assert c.post(f"/api/record/{rid}/status", json={"status": "applied", "evidence": "said-yes"}).status_code == 200
    assert _data(A, rid)["sent_evidence"]["label"] == "you said yes"
    assert "Application submitted: you said yes" in _notes(c, "gh:lark3")
    # a plain Mark as applied keeps its old line and claims no evidence
    rid2 = _add(A, "Planner", "gh:lark4", url="https://boards.greenhouse.io/lark/jobs/4")
    _card(c, "gh:lark4")
    c.post(f"/api/record/{rid2}/status", json={"status": "applied"})
    assert not _data(A, rid2).get("sent_evidence")


def test_the_duplicate_guard_knows_an_application_was_sent(A):
    rid = _add(A, "Data Analyst", "gh:lark5", url="https://boards.greenhouse.io/lark/jobs/5")
    c = A.app.test_client()
    st = c.get(f"/api/autofill/sent-state?record_id={rid}").get_json()
    assert st["applied"] is False and st["company"] == "Larkspur Labs"
    c.post("/api/autofill/applied", headers=H, json={"record_id": rid, "url": "https://boards.greenhouse.io/lark/jobs/5",
                                                     "evidence": [{"kind": "page-marker"}]})
    st = c.get(f"/api/autofill/sent-state?record_id={rid}").get_json()
    assert st["applied"] is True and st["applied_at"] and st["label"] == "thank-you page"
    # a confirmation email on a record nothing moved yet counts too
    rid2 = _add(A, "Engineer", "gh:lark6", url="https://boards.greenhouse.io/lark/jobs/6",
                confirmation={"subject": "Thank you for applying", "sender": "Larkspur"})
    st = c.get(f"/api/autofill/sent-state?record_id={rid2}").get_json()
    assert st["applied"] is True and st["label"] == "confirmation email"
    # the card further on than its record (dragged to Interviewing)
    rid3 = _add(A, "Planner", "gh:lark7", url="https://boards.greenhouse.io/lark/jobs/7")
    _card(c, "gh:lark7")
    c.post("/api/tracker/move", json={"source_id": "gh:lark7", "stage": "interviewing"})
    assert c.get(f"/api/autofill/sent-state?record_id={rid3}").get_json()["applied"] is True
    assert c.get("/api/autofill/sent-state?record_id=99999").status_code == 404
    # the same role at the same company reposted under a new card: asked too
    rid4 = _add(A, "Planner", "gh:lark8", url="https://boards.greenhouse.io/lark/jobs/8")
    st = c.get(f"/api/autofill/sent-state?record_id={rid4}").get_json()
    assert st["applied"] is True and st["repost"] is True
    rid5 = _add(A, "Welder", "gh:lark9", url="https://boards.greenhouse.io/lark/jobs/9")
    assert c.get(f"/api/autofill/sent-state?record_id={rid5}").get_json()["applied"] is False
    js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    guard = js[js.index("async function applyInPane("):js.index("function sentDay(")]
    assert "/api/autofill/sent-state" in guard and "already-applied" in guard and "again: true" in guard


def test_evidence_stops_the_ask_and_the_daily_sweep_asks_once_more(A, monkeypatch):
    monkeypatch.setattr(A.time, "time", lambda: 1_893_456_000.0 + 3600)       # an hour after the fill
    base = {"filled_at": "2030-01-01T00:00:00+00:00"}
    assert A._ask_submitted(base, "approved") is True
    assert A._ask_submitted({**base, "sent_evidence": {"label": "x"}}, "approved") is False
    assert A._ask_submitted({**base, "confirmation": {"subject": "x"}}, "approved") is False
    no = {**base, "submit_q_hidden": base["filled_at"]}
    assert A._ask_submitted(no, "approved") is False                          # No: not again that day
    later = 1_893_456_000.0 + A.ASK_AGAIN_AFTER_S + 60
    assert A._ask_submitted(no, "approved", now=later) is True                # once more after a day
    assert A._ask_submitted({**no, "submit_q_late": "1"}, "approved", now=later) is False


def test_every_new_bar_string_has_every_locale():
    import json
    import re
    keys = ("fill.againApplied", "fill.againAppliedNoDate", "fill.sentHow", "fill.fillAgain", "fill.notNow",
            "fill.skippedApplied", "fill.askSentJob", "fill.askSent", "fill.yesSent", "fill.notYet", "fill.markedSent")
    js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k) and not re.search(r"[—–]", d[k]), (code, k)
            if code != "en":
                assert d[k] != en[k], (code, k)
            for ph in re.findall(r"\{\w+\}", en[k]):
                assert ph in d[k], (code, k, ph)
    for k in keys:
        assert f'"{k}"' in js, k


# --------------------------------------------------------------------------- the confirmation email

GH_STRIPE = {"source_id": "greenhouse::stripe-da", "company": "Stripe", "title": "Data Analyst",
             "url": "https://job-boards.greenhouse.io/stripe/jobs/7000001", "stage": "approved"}
CORDOVA = {"source_id": "careers::cordova-pe", "company": "Cordova Freight", "title": "Platform Engineer",
           "url": "https://careers.cordovafreight.com/jobs/1", "stage": "approved"}
CORDOVA_2 = {**CORDOVA, "source_id": "careers::cordova-sre", "title": "Site Reliability Engineer",
             "url": "https://careers.cordovafreight.com/jobs/2"}


def _mail(frm, subject, body, mid="m1"):
    return {"id": mid, "from": frm, "subject": subject, "body": body, "date": "Thu, 26 Nov 2026 10:00:00 +0000"}


def test_ats_no_reply_confirmations_are_recognised():
    for subject, body in (("Thank you for your application", "Hi Alex, we have received your application."),
                          ("Your application", "Thanks for your application to Cordova Freight."),
                          ("Application received", "We've got your application and will review it soon."),
                          ("Confirmation of your application", "Hello Alex,")):
        t = detect.triage(_mail("no-reply@us.greenhouse-mail.io", subject, body))
        assert t["kind"] == "application_received", (subject, t)
    # a rejection that thanks you for applying is still a rejection
    t = detect.triage(_mail("no-reply@hire.lever.co", "Thank you for your application",
                            "Unfortunately we have decided to move forward with other candidates."))
    assert t["kind"] == "rejection"


def test_a_nameless_hiring_system_mail_never_moves_the_lone_card():
    """Applied to another company on Greenhouse from his own Chrome: its nameless "thank you"
    must not move the one Greenhouse card the app knows about."""
    msg = _mail("no-reply@us.greenhouse-mail.io", "Thank you for applying", "We have received your application.")
    job, lane, ambiguous = matching.match_detail(msg, [GH_STRIPE])
    assert job["source_id"] == GH_STRIPE["source_id"] and ambiguous is True
    s = build_suggestions([msg], [GH_STRIPE])
    assert [(x["stage"], x["auto"], x["ambiguous"]) for x in s] == [("applied", False, True)]   # it asks


def test_another_companys_confirmation_never_moves_this_card():
    msg = _mail("Acme Robotics <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Acme Robotics",
                "Hi Alex, we have received your application for Data Analyst.")
    s = build_suggestions([msg], [GH_STRIPE])
    assert all(not x["auto"] for x in s)
    # and the same mail for Stripe, naming the role, is sure and moves it by itself
    ok = _mail("Stripe <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Stripe",
               "Hi Alex, we have received your application for Data Analyst.", mid="m2")
    s = build_suggestions([ok], [GH_STRIPE])
    assert [(x["source_id"], x["auto"], x["ambiguous"]) for x in s] == [(GH_STRIPE["source_id"], True, False)]


def test_two_jobs_at_one_company_ask_unless_the_role_decides():
    plain = _mail("Cordova Talent <talent@cordovafreight.com>", "Thank you for applying",
                  "We have received your application.")
    job, lane, ambiguous = matching.match_detail(plain, [CORDOVA, CORDOVA_2])
    assert lane == "domain" and ambiguous is True
    assert [x["auto"] for x in build_suggestions([plain], [CORDOVA, CORDOVA_2])] == [False]
    named = _mail("Cordova Talent <talent@cordovafreight.com>", "Thank you for applying",
                  "We have received your application for Site Reliability Engineer.", mid="m3")
    job, lane, ambiguous = matching.match_detail(named, [CORDOVA, CORDOVA_2])
    assert job["source_id"] == CORDOVA_2["source_id"] and ambiguous is False
    assert [x["auto"] for x in build_suggestions([named], [CORDOVA, CORDOVA_2])] == [True]
    # the same role reposted (two cards, same title): a guess, so it asks
    repost = {**CORDOVA_2, "source_id": "careers::cordova-sre-2", "url": "https://careers.cordovafreight.com/jobs/3"}
    assert matching.match_detail(named, [CORDOVA_2, repost])[2] is True


def test_an_inbox_move_records_the_confirmation_email(A):
    c = A.app.test_client()
    snap = {"source_id": "careers::northwind-da", "company": "Northwind Traders", "title": "Data Analyst",
            "url": "https://careers.northwind.com/jobs/1"}
    recs = CVRecords(A.DB_PATH)
    try:
        rid = recs.add("Data Analyst", "Northwind Traders", 70, "", jd_label="Data Analyst",
                       data={"status": "approved", "source_job": snap})
    finally:
        recs.close()
    _card(c, snap["source_id"])
    msg = _mail("Northwind Careers <careers@northwind.com>", "Thank you for applying",
                "We have received your application for Data Analyst.", mid="nw-1")
    r = c.post("/api/inbox/scan", json={"messages": [msg], "draft": False})
    assert r.status_code == 200, r.get_json()
    assert _card(c, snap["source_id"])["stage"] == "applied"
    assert _data(A, rid)["sent_evidence"]["label"] == "confirmation email"


def test_the_orb_carries_the_daily_sweep(A):
    from datetime import datetime, timedelta, timezone
    two_days = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    rid = _add(A, "Data Analyst", "gh:lark10", url="https://boards.greenhouse.io/lark/jobs/10", filled_at=two_days)
    got = [c for c in A._orb_candidates(datetime.now()) if c["type"] == "sent"]
    assert [c["text"] for c in got] == ["Did you send your Larkspur Labs application?"]
    assert got[0]["key"].startswith(f"sent:{rid}:")
    # evidence it was sent: no nudge
    recs = CVRecords(A.DB_PATH)
    try:
        recs.merge_data(rid, {"sent_evidence": A._sent_evidence([{"kind": "email"}])})
    finally:
        recs.close()
    assert not [c for c in A._orb_candidates(datetime.now()) if c["type"] == "sent"]
