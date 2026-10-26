"""Autopilot: the scheduled, capped, hands-off Auto-apply run (CLAUDE.md §4c) and its one
Telegram message per run.

Fully offline: the tailoring, the LLM, the broker usage and the channel are fakes. Nothing is
submitted anywhere except through the fake submit path, and only for the verified auto lane
with autonomous submission on."""

from __future__ import annotations

import inspect
import json
import re
import threading
from pathlib import Path

import pytest

import ui.app as app
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
DAY1, DAY2 = "2026-10-26", "2026-10-27"

GH = "https://boards.greenhouse.io/acme/jobs/1"
LV = "https://jobs.lever.co/beta/2"
RT = "https://gamma.recruitee.com/o/data-analyst"      # the one verified AUTO site


def _job(sid, title, company, url):
    return {"source_id": sid, "title": title, "company": company, "url": url,
            "jd_text": "We need Python and SQL.", "source": "ats", "location": "Austin, TX"}


JOBS = [_job("gh:1", "Data Analyst", "Acme", GH),
        _job("lv:2", "Business Analyst", "Beta", LV),
        _job("rt:3", "Analyst", "Gamma", RT),
        _job("gh:4", "Reporting Analyst", "Delta", "https://boards.greenhouse.io/delta/jobs/4")]


class FakeChannel:
    name = "official"

    def __init__(self, events=None):
        self.sent, self.acks = [], []
        self.events = list(events or [])

    def send(self, text, buttons=None):
        from notify.channel import _as_rows
        self.sent.append({"text": text, "buttons": _as_rows(buttons) or None})
        return {"ok": True, "message_id": len(self.sent)}

    def send_photo(self, path, caption="", buttons=None):
        return self.send(caption, buttons)

    def send_document(self, path, caption="", buttons=None):
        return self.send(caption, buttons)

    def poll(self):
        out, self.events = self.events, []
        return out

    def ack(self, event, text=""):
        self.acks.append(text)


@pytest.fixture()
def ap(tmp_path, monkeypatch):
    """ui.app with every store under tmp, the tailoring faked (it writes a real record, so a
    second run sees the package), two packages left on the pass, and today pinned."""
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_RATE_FILE", tmp_path / "rate.json")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    monkeypatch.setattr(app, "_TG_OFFSET_FILE", tmp_path / "off")
    monkeypatch.setattr(app, "_NOTIFY_STATE_FILE", tmp_path / "notify_state.json")
    state = {"day": DAY1, "left": 2, "channel": None, "tailored": [], "fail": set()}
    monkeypatch.setattr(app, "_now_ts_today", lambda: (0.0, state["day"]))
    monkeypatch.setattr(app, "_autopilot_packages_left", lambda: state["left"])
    monkeypatch.setattr(app, "_active_channel", lambda: state["channel"])
    monkeypatch.setattr(app, "_aa_has_profile", lambda: True)
    monkeypatch.setattr(app, "_cred", lambda k: "")
    monkeypatch.setattr(app, "_make_llm", lambda: object())
    monkeypatch.setattr(app, "_load_template", lambda name: ("", "t"))
    meta = {"titles": [], "locations": [], "sponsor_only": False, "search": None,
            "board": len(JOBS), "saved": 0}
    monkeypatch.setattr(app, "_aa_candidates", lambda focus: ([dict(j) for j in JOBS], meta))
    monkeypatch.setattr(app, "_aa_scorer",
                        lambda rows, deadline=None, network=False: (lambda job: 80))

    def fake_tailor(job, llm, tex, tname, scratch):
        if job["source_id"] in state["fail"]:
            raise RuntimeError("model hiccup")
        state["tailored"].append(job["source_id"])
        recs = CVRecords(app.DB_PATH)
        try:
            rid = recs.add(job["title"], job["company"], 70.0, "",
                           data={"jd_text": job["jd_text"],
                                 "source_job": {"url": job["url"], "source_id": job["source_id"],
                                                "location": job["location"]}})
        finally:
            recs.close()
        return {"ok": True, "id": rid, "role": job["title"], "company": job["company"],
                "coverage": 70.0}
    monkeypatch.setattr(app, "_tailor_job_unattended", fake_tailor)
    app._set_prefs({"autopilot": True, "autopilot_per_day": 2})
    return state


# --- settings ---------------------------------------------------------------------------------

def test_settings_round_trip_and_clamp(ap):
    c = app.app.test_client()
    app._set_prefs({"autopilot": False})
    d = c.get("/api/submit/settings").get_json()
    assert d["autopilot"] is False and d["autopilot_per_day"] == 2
    d = c.post("/api/submit/settings", json={"autopilot": True, "autopilot_per_day": 15}).get_json()
    assert d["autopilot"] is True and d["autopilot_per_day"] == 10
    d = c.post("/api/submit/settings", json={"autopilot_per_day": 0}).get_json()
    assert d["autopilot_per_day"] == 1
    d = c.post("/api/submit/settings", json={"autopilot_per_day": "x"}).get_json()
    assert d["autopilot_per_day"] == 1                      # unreadable: unchanged
    assert app._prefs()["autopilot"] is True


def test_focus_is_kept_for_the_scheduler(ap):
    c = app.app.test_client()
    c.post("/api/submit/settings", json={"aa_focus": {"titles": "analyst, data",
                                                      "locations": ["NYC"], "sponsor_only": 1}})
    assert app._prefs()["aa_focus"] == {"titles": ["analyst", "data"], "locations": ["NYC"],
                                        "sponsor_only": True}


# --- the scheduler ----------------------------------------------------------------------------

def test_off_no_profile_and_no_packages_never_run(ap, monkeypatch):
    app._set_prefs({"autopilot": False})
    assert app._autopilot_tick() == {"ran": False, "why": "off"}
    app._set_prefs({"autopilot": True})
    monkeypatch.setattr(app, "_aa_has_profile", lambda: False)
    assert app._autopilot_tick()["why"] == "no_profile"
    monkeypatch.setattr(app, "_aa_has_profile", lambda: True)
    ap["left"] = 0
    out = app._autopilot_tick()
    assert out["why"] == "paused" and out["paused"]["code"] == "no_packages"
    assert ap["tailored"] == []
    # Paused for the day: more packages do not wake it until tomorrow.
    ap["left"] = 5
    assert app._autopilot_tick()["why"] == "paused"
    st = app._autopilot_status()
    assert st["on"] and st["paused"]["text"] == "Paused: no packages left on your pass."
    ap["day"] = DAY2
    assert app._autopilot_tick()["tailored"] == 2


def test_no_packages_but_own_key_still_runs(ap, monkeypatch):
    ap["left"] = 0
    monkeypatch.setattr(app, "_cred", lambda k: "sk-own" if k == "ANTHROPIC_API_KEY" else "")
    assert app._autopilot_tick()["tailored"] == 2


def test_per_day_cap_then_rollover_and_no_double_tailor(ap):
    out = app._autopilot_tick()
    assert out["ran"] and out["tailored"] == 2 and out["done"] == 2
    assert ap["tailored"] == ["gh:1", "lv:2"]
    assert app._autopilot_tick()["why"] == "done"           # the cap for today
    assert ap["tailored"] == ["gh:1", "lv:2"]
    ap["day"] = DAY2
    out = app._autopilot_tick()
    assert out["tailored"] == 2 and out["done"] == 2
    assert ap["tailored"] == ["gh:1", "lv:2", "rt:3", "gh:4"]   # never a packaged role again
    ap["day"] = "2026-10-28"
    assert app._autopilot_tick()["why"] == "nothing"        # every match has a package
    assert app._autopilot_status()["done"] == 0


def test_remaining_allowance_is_what_a_run_may_tailor(ap):
    app._set_prefs({"autopilot_per_day": 3})
    # A manual run took one today; the scheduler only tailors up to its own count.
    assert app._autopilot_tick()["tailored"] == 3
    app._set_prefs({"autopilot_per_day": 4})
    assert app._autopilot_tick()["tailored"] == 1


def test_three_failed_runs_back_off_until_tomorrow(ap):
    ap["fail"] = {"gh:1", "lv:2", "rt:3", "gh:4"}
    for n in (1, 2):
        out = app._autopilot_tick()
        assert out["tailored"] == 0 and out["paused"] is None
    out = app._autopilot_tick()
    assert out["paused"]["code"] == "failures"
    assert app._autopilot_tick()["why"] == "paused"
    ap["fail"] = set()
    assert app._autopilot_tick()["why"] == "paused"          # until tomorrow
    ap["day"] = DAY2
    assert app._autopilot_tick()["tailored"] == 2


def test_no_ai_backs_off_with_the_reason(ap, monkeypatch):
    def boom():
        raise app.Unavailable("no_key", "Add your key.")
    monkeypatch.setattr(app, "_make_llm", boom)
    out = app._autopilot_tick()
    assert out["why"] == "error" and out["paused"]["code"] == "no_ai"
    assert "not reachable" in app._autopilot_status()["paused"]["text"]


def test_pass_used_up_mid_run_pauses(ap, monkeypatch):
    def used_up(job, llm, tex, tname, scratch):
        return {"ok": False, "role": job["title"], "reason": "used up", "upgrade": True}
    monkeypatch.setattr(app, "_tailor_job_unattended", used_up)
    out = app._autopilot_tick()
    assert out["tailored"] == 0 and out["paused"]["code"] == "no_packages"


def test_lock_keeps_manual_and_autopilot_apart(ap):
    assert app._AA_RUN_LOCK.acquire(blocking=False)
    try:
        assert app._autopilot_tick() == {"ran": False, "why": "busy"}
        r = app.app.test_client().post("/api/autoapply/run", json={"count": 2})
        assert r.status_code == 409 and r.get_json()["busy"] is True
    finally:
        app._AA_RUN_LOCK.release()
    assert ap["tailored"] == []
    assert app.app.test_client().post("/api/autoapply/run", json={"count": 1}).get_json()["queued"] == 1
    assert not app._AA_RUN_LOCK.locked()


def test_kick_runs_on_a_thread_only_when_on(ap, monkeypatch):
    started = []

    class T:
        def __init__(self, target=None, name="", daemon=False):
            started.append(name)

        def start(self):
            pass

        def is_alive(self):
            return False
    monkeypatch.setattr(threading, "Thread", T)
    app._AUTOPILOT_THREAD["thread"] = None
    app._set_prefs({"autopilot": False})
    app._autopilot_kick()
    assert started == []
    app._set_prefs({"autopilot": True})
    app._autopilot_kick()
    assert started == ["autopilot"]
    app._AUTOPILOT_THREAD["thread"] = None


def test_loop_kicks_on_the_scan_tick_without_telegram():
    src = inspect.getsource(app.start_notify_loop)
    assert "_autopilot_kick()" in src and "n % NOTIFY_SCAN_EVERY == 0" in src
    assert app.NOTIFY_INTERVAL * app.NOTIFY_SCAN_EVERY == 300


def test_never_submits_by_itself(ap):
    app._set_autonomous(True)
    app._autopilot_tick()
    recs = CVRecords(app.DB_PATH)
    try:
        rows = recs.list()
    finally:
        recs.close()
    assert len(rows) == 2
    assert all((app._record_data(r).get("status") or "ready") == "ready" for r in rows)


# --- alerts dedupe ----------------------------------------------------------------------------

def test_tailored_roles_are_kept_out_of_job_alerts(ap):
    from datetime import datetime
    from notify.alerts import AlertEngine, profile_targets
    st = app._notify_state()
    st.set("queue", [{"sid": "gh:1", "short": "x1", "title": "Data Analyst", "company": "Acme",
                      "score": 90, "queued_at": "2026-10-26T10:00:00+00:00"}])
    app._autopilot_tick()
    d = st.load()
    assert "gh:1" in d["sent"] and "lv:2" in d["sent"] and d["queue"] == []
    eng = AlertEngine(st, match_fn=lambda r: 95)
    targets = profile_targets({"experience": [{"org": "A", "roles": [{"title": "Data Analyst"}]}],
                               "skills": ["Python"]})
    now = datetime(2026, 10, 26, 12, 0).astimezone()
    rows = [{**j, "us": True, "visa": [{"code": "H-1B", "label": "H-1B sponsor"}],
             "first_seen": now.isoformat()} for j in JOBS]
    picked = eng.pick(rows, targets, now)
    assert picked                                              # the untouched roles still alert
    assert {a["sid"] for a in picked}.isdisjoint({"gh:1", "lv:2"})


# --- the Telegram message ---------------------------------------------------------------------

def _items():
    return [{"id": 1, "company": "Acme", "url": GH}, {"id": 2, "company": "Beta", "url": LV},
            {"id": 3, "company": "Gamma", "url": RT}]


def test_message_text_when_nothing_can_be_sent():
    t = app.autopilot_message(_items(), lambda it: False, 3, 5, 12)
    assert t == ("I found 3 roles you are qualified for and tailored your resume for each. "
                 "3 are ready and waiting for your click (Acme, Beta, Gamma). 3 of 5 for today. "
                 "12 packages left on your pass.")
    assert "can be sent" not in t


def test_message_text_when_one_can_be_sent_and_packages_unknown():
    from submit.policy import submission_policy
    t = app.autopilot_message(_items(), lambda it: submission_policy(it["url"]) == "auto", 3, 3)
    assert t == ("I found 3 roles you are qualified for and tailored your resume for each. "
                 "1 can be sent for you (Gamma). 2 are ready and waiting for your click "
                 "(Acme, Beta). 3 of 3 for today.")
    one = app.autopilot_message(_items()[:1], lambda it: False, 1, 2, 1)
    assert one == ("I found 1 role you are qualified for and tailored your resume for it. "
                   "1 is ready and waiting for your click (Acme). 1 of 2 for today. "
                   "1 package left on your pass.")


def test_message_has_no_dashes_and_three_buttons():
    assert not re.search(r"[—–]", app.autopilot_message(_items(), lambda it: False, 1, 2, 3))
    rows = app.autopilot_buttons("abc123")
    assert [b["label"] for b in rows[0]] == ["Approve all", "Review", "Skip"]
    assert [b["id"] for b in rows[0]] == ["ap:approveall:abc123", "ap:review:abc123",
                                          "ap:skip:abc123"]


def test_run_sends_one_message_only_with_a_channel(ap):
    out = app._autopilot_tick()
    assert out["tailored"] == 2 and "sent" not in out          # no channel: no message
    ap["day"] = DAY2
    ch = FakeChannel()
    ap["channel"] = ch
    out = app._autopilot_tick()
    assert out["tailored"] == 2 and out["sent"] is True and len(ch.sent) == 1
    m = ch.sent[0]
    assert m["text"].startswith("I found 2 roles you are qualified for")
    assert "2 are ready and waiting for your click (Gamma, Delta)" in m["text"]
    assert "can be sent" not in m["text"]            # Gamma is auto, but autonomous is off
    assert m["text"].endswith("2 of 2 for today. 2 packages left on your pass.")
    assert m["buttons"][0][0]["id"] == f"ap:approveall:{out['batch']}"


def test_autonomous_on_counts_the_verified_site_as_sendable(ap):
    app._set_autonomous(True)
    app._set_prefs({"autopilot_per_day": 4})
    ch = FakeChannel()
    ap["channel"] = ch
    app._autopilot_tick()
    t = ch.sent[0]["text"]
    assert "1 can be sent for you (Gamma)." in t
    assert "3 are ready and waiting for your click (Acme, Beta, Delta)." in t


# --- the buttons ------------------------------------------------------------------------------

def _batch(ap, per_day=4):
    app._set_prefs({"autopilot_per_day": per_day})
    ch = FakeChannel()
    ap["channel"] = ch
    out = app._autopilot_tick()
    return out["batch"], ch


def _statuses():
    recs = CVRecords(app.DB_PATH)
    try:
        return {r["company"]: (app._record_data(r).get("status") or "ready") for r in recs.list()}
    finally:
        recs.close()


def test_approve_all_submits_only_auto_lane_and_only_when_autonomous_on(ap, monkeypatch):
    import submit
    posted = []

    def fake_submit(data, driver=None, now=None):
        posted.append((data.get("source_job") or {}).get("url"))
        return {"status": "auto_submitted", "tier": "auto", "message": "sent",
                "submitted_at": now, "posted": True}
    monkeypatch.setattr(submit, "submit_record", fake_submit)
    bid, ch = _batch(ap)
    # Autonomous OFF: nothing is submitted, every item is approved for the person's click.
    res = app._autopilot_act(f"ap:approveall:{bid}")
    assert posted == []
    assert res["message"] == ("4 approved and waiting for your click (Acme, Beta, Gamma, Delta). "
                              "Open SponsorJobs to finish them.")
    assert set(_statuses().values()) == {"approved"}
    assert app._autopilot_act(f"ap:approveall:{bid}")["message"].startswith("That batch was handled")
    # Autonomous ON: only the verified AUTO site (Recruitee) goes through the submit path.
    ap["day"] = DAY2
    recs = CVRecords(app.DB_PATH)
    try:
        for r in recs.list():
            recs.delete(r["id"])
    finally:
        recs.close()
    app._set_autonomous(True)
    bid, ch = _batch(ap)
    res = app._autopilot_act(f"ap:approveall:{bid}")
    assert posted == [RT]
    assert res["message"] == ("Sent 1 for you (Gamma). 3 approved and waiting for your click "
                              "(Acme, Beta, Delta). Open SponsorJobs to finish them.")
    st = _statuses()
    assert st["Gamma"] == "applied" and st["Acme"] == "approved"


def test_skip_marks_the_batch_skipped(ap):
    bid, ch = _batch(ap)
    res = app._autopilot_act(f"ap:skip:{bid}")
    assert res["message"] == "Skipped 4. They stay in SponsorJobs, marked skipped."
    assert set(_statuses().values()) == {"skipped"}
    assert app.app.test_client().get("/api/review/queue").get_json()["count"] == 0


def test_review_sends_the_items_one_by_one(ap):
    bid, ch = _batch(ap)
    res = app._autopilot_act(f"ap:review:{bid}")
    assert res["message"] == "Here are the 4, one by one."
    texts = [m["text"] for m in ch.sent[1:]]           # after the run's own message
    assert texts[0].startswith("📥 4 application(s) ready for your review")
    per_item = [m for m in ch.sent[2:] if m["buttons"]]
    assert len(per_item) == 4
    ids = sorted(b["id"] for m in per_item for b in m["buttons"][0])
    assert ids == sorted([f"{k}:{i}" for i in (1, 2, 3, 4) for k in ("approve", "skip")])
    assert any("Data Analyst at Acme" in m["text"] for m in per_item)


def test_buttons_arrive_through_the_hub(ap):
    from datetime import datetime
    bid, ch = _batch(ap)
    ch.events.append({"type": "button", "data": f"ap:skip:{bid}", "id": "e1"})
    out = app._notify_hub().tick(ch, datetime(2026, 10, 26, 12, 0).astimezone(), lambda a: False)
    assert out["handled"] == 1
    assert ch.sent[-1]["text"].startswith("Skipped 4.")
    assert set(_statuses().values()) == {"skipped"}
    ch.events.append({"type": "button", "data": "ap:skip:nope", "id": "e2"})
    app._notify_hub().tick(ch, datetime(2026, 10, 26, 12, 0).astimezone(), lambda a: False)
    assert ch.sent[-1]["text"].startswith("That batch was handled already, or has expired.")
    ch.events.append({"type": "button", "data": "approve:1", "id": "e3"})   # the batch review still works
    app._notify_hub().tick(ch, datetime(2026, 10, 26, 12, 0).astimezone(), lambda a: False)
    assert "Acme" in ch.sent[-1]["text"]


# --- the page ---------------------------------------------------------------------------------

def test_board_reports_autopilot(ap):
    d = app.app.test_client().get("/api/autoapply/board").get_json()
    assert d["autopilot"] == {"on": True, "per_day": 2, "done": 0, "paused": None}
    app._autopilot_tick()
    assert app.app.test_client().get("/api/autoapply/board").get_json()["autopilot"]["done"] == 2


def test_ui_source_has_the_row_and_the_strings():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    auto = html.index('id="aaAutonomous"')
    row = html.index('id="aaApRow"')
    assert auto < row < html.index('id="aaTgRow"')            # directly under Autonomous submit
    assert 'class="aa-switch" id="aaAutopilot"' in html and 'data-i18n="aa.ap"' in html
    assert 'id="aaApPerDay"' in js and 'min="1" max="10"' in js
    assert "autopilot_per_day" in js and "/api/account/status" in js
    assert 'T("aa.apPaused." + ap.paused.code' in js
    keys = {"aa.ap", "aa.apSub", "aa.apLeft", "aa.apPaused.no_packages", "aa.apPaused.no_ai",
            "aa.apPaused.failures"}
    for p in sorted((ROOT / "ui" / "static" / "locales").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        assert keys <= set(d), p.name
        assert "{n}" in d["aa.apSub"] and "{p}" in d["aa.apLeft"], p.name
        for k in keys:
            assert not re.search(r"[—–]", d[k]), (p.name, k)
    assert set(app.AUTOPILOT_PAUSE_TEXT) == {"no_packages", "no_ai", "failures"}
    assert "resume" in (ROOT / "docs" / "notify.md").read_text(encoding="utf-8")
    assert "### Autopilot" in (ROOT / "docs" / "notify.md").read_text(encoding="utf-8")


# --- missing profile details: pause, no retry, resume, one Telegram line ----------------------

def test_missing_details_pause_without_failures_and_resume_on_their_own(ap, monkeypatch):
    missing = ['the dates for your project "Gate Check"', 'the dates for your project "Ledger"']
    monkeypatch.setattr(app, "_aa_missing_details", lambda: list(missing))
    ap["channel"] = FakeChannel()
    for _ in range(3):                                   # three ticks: paused, not retried
        out = app._autopilot_tick()
        assert out["why"] == "paused" and out["paused"]["code"] == "missing_details"
    assert ap["tailored"] == []                          # nothing counted or tailored
    st = app._autopilot_state(DAY1)
    assert st["failures"] == 0                           # a pause, never a failure
    assert app._autopilot_status()["paused"]["text"] == "Paused: 2 details missing in your profile."
    assert [m["text"] for m in ap["channel"].sent] == [   # told once, not every tick
        'Autopilot is paused: your profile is missing 2 details (the dates for your project '
        '"Gate Check", the dates for your project "Ledger"). Add them in SponsorJobs and it '
        'resumes on its own.']
    ap["day"] = DAY2                                     # a new day does not repeat it
    assert app._autopilot_tick()["paused"]["code"] == "missing_details"
    assert len(ap["channel"].sent) == 1

    missing.clear()                                      # the person adds the details
    out = app._autopilot_tick()
    assert out["ran"] is True and out["tailored"] == 2 and not out["paused"]
    assert app._autopilot_status()["paused"] is None


def test_missing_details_without_a_channel_sends_nothing_and_tells_later(ap, monkeypatch):
    monkeypatch.setattr(app, "_aa_missing_details", lambda: ["your full name"])
    out = app._autopilot_tick()
    assert out["paused"]["text"] == "Paused: 1 detail missing in your profile." and out["told"] is False
    ap["channel"] = FakeChannel()                        # connected later, still paused: one line
    app._autopilot_tick()
    app._autopilot_tick()
    assert len(ap["channel"].sent) == 1 and "missing 1 detail (your full name)" in ap["channel"].sent[0]["text"]


def test_another_pause_is_not_replaced_by_missing_details(ap, monkeypatch):
    ap["left"] = 0
    assert app._autopilot_tick()["paused"]["code"] == "no_packages"
    monkeypatch.setattr(app, "_aa_missing_details", lambda: ["your full name"])
    assert app._autopilot_tick()["paused"]["code"] == "no_packages"


def test_the_page_shows_the_missing_details_pause():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'ap.paused.code === "missing_details" && ap.paused.n === 1' in js
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert en["aa.apPaused.missing_details"] == "Paused: {n} details missing in your profile."
