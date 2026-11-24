"""The three apply modes: Auto, Assisted, Hybrid (docs/apply-agent.md section 26).

* ONE saved choice, migrated from the old Autonomous submit switch (on -> Auto, off -> Assisted);
* Auto sends by itself only on the verified allowlist (submit/allowlist.py), optionally only at
  or above a match threshold, and counts against the daily limit; everything else waits;
* Auto's queue is the approved applications, up to the daily limit, each tried once a day;
* the person hears from Auto twice: "Sent: Role at Company" once per application it sent
  (Telegram when linked, else in the app; quiet hours respected) and ONE daily summary (counts
  only, no Send button on Telegram);
* the run's questions go to Telegram only when the shell says so (Hybrid);
* the employer's confirmation mail is kept on the application;
* the shell side (hidden pane, no bar asks, Send all, captcha stop) runs under node.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from autoapply import modes
from notify import daily as DN
from notify.alerts import NotifyState
from notify.channel import ChannelError

# The apply agent is off by default (assisted apply only); these tests cover it, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
AUTO_URL = "https://acme.recruitee.com/o/analyst"
GH_URL = "https://boards.greenhouse.io/bolt/jobs/1"
SELF = "Zyxwv Selfid Value"


class Chan:
    name = "official"

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, text, buttons=None):
        if self.fail:
            raise ChannelError(self.fail)
        self.sent.append((text, buttons))
        return {"ok": True}


def _at(hour, day=16):
    return datetime(2026, 11, day, hour, 5).astimezone()


@pytest.fixture()
def st(tmp_path):
    return NotifyState(tmp_path / "notify_state.json")


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    chan = Chan()
    monkeypatch.setattr(mod, "_active_channel", lambda: chan)
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    return mod, chan


def _add(mod, role, company, url, fit=80, **data):
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        return recs.add(role, company, fit, "", data={"status": "approved", "fit": fit, "source_job": {"url": url}, **data})
    finally:
        recs.close()


def _post(mod, path, body=None):
    return mod.app.test_client().post(path, json=body or {}, headers=H).get_json()


def _get(mod, path):
    return mod.app.test_client().get(path, headers=H).get_json()


def _row(mod, rid):
    b = _get(mod, "/api/autoapply/board")
    return next(x for t in b["tiles"].values() for x in t if x["id"] == rid)


# --------------------------------------------------------------------------- the rule, pure
def test_migration_on_is_auto_off_is_assisted_and_a_saved_mode_wins():
    assert modes.migrate(None, True) == "auto"
    assert modes.migrate("", False) == "assisted"
    assert modes.migrate("hybrid", True) == "hybrid"
    assert modes.migrate("nonsense", False) == "assisted"
    assert modes.MODES == ("auto", "assisted", "hybrid")


def test_only_auto_on_the_allowlist_lane_sends_alone_and_the_threshold_narrows_it():
    assert modes.may_send_alone("auto", "auto", 40) is True            # threshold off: every allowlisted
    assert modes.may_send_alone("auto", "assisted", 99) is False       # never off the allowlist
    for m in ("assisted", "hybrid"):
        assert modes.may_send_alone(m, "auto", 99) is False            # never outside Auto
    assert modes.may_send_alone("auto", "auto", 79, 80) is False       # under the threshold: waits
    assert modes.may_send_alone("auto", "auto", 80, 80) is True
    assert modes.may_send_alone("auto", "auto", None, 80) is False     # an unknown match never passes one
    # (2026-11-18) an application holding a written answer the agent drafted is never sent alone
    assert modes.may_send_alone("auto", "auto", 99, drafted=True) is False
    assert modes.clamp_threshold("") == 0 and modes.clamp_threshold(250) == 100 and modes.clamp_threshold(-3) == 0
    assert modes.telegram_asks("hybrid") and not modes.telegram_asks("assisted") and not modes.telegram_asks("auto")
    assert modes.hidden("auto") and not modes.hidden("assisted") and not modes.hidden("hybrid")


def test_the_sites_auto_can_send_on_come_from_the_allowlist():
    from submit.allowlist import REGISTRY
    assert modes.auto_sites() == sorted({e.ats for e in REGISTRY if e.auto_eligible})
    assert "Greenhouse" not in modes.auto_sites() and "LinkedIn" not in modes.auto_sites()


# --------------------------------------------------------------------------- the saved choice
def test_the_old_switch_migrates_and_one_choice_is_saved(A):
    mod, _ = A
    assert mod._apply_mode() == "assisted" and mod._autonomous_on() is False      # a fresh install
    mod._AUTONOMOUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod._AUTONOMOUS_FILE.write_text("1", encoding="utf-8")                        # the old switch was on
    assert mod._apply_mode() == "auto" and mod._autonomous_on() is True
    d = _post(mod, "/api/submit/settings", {"apply_mode": "hybrid"})
    assert d["apply_mode"] == "hybrid" and d["autonomous"] is False
    assert mod._apply_mode() == "hybrid" and mod._autonomous_legacy() is False    # the old file follows
    # the older two-way switch still works: on is Auto; off leaves Auto for Assisted
    assert _post(mod, "/api/submit/settings", {"autonomous": True})["apply_mode"] == "auto"
    assert _post(mod, "/api/submit/settings", {"autonomous": False})["apply_mode"] == "assisted"
    mod._set_apply_mode("hybrid")
    mod._set_autonomous(False)
    assert mod._apply_mode() == "hybrid"                                          # Hybrid never sent alone anyway
    bad = mod.app.test_client().post("/api/submit/settings", json={"apply_mode": "turbo"}, headers=H)
    assert bad.status_code == 400 and mod._apply_mode() == "hybrid"
    s = _get(mod, "/api/submit/settings")
    assert s["apply_mode"] == "hybrid" and s["auto_sites"] == modes.auto_sites() and s["auto_min_match"] == 0


def test_apply_setup_saves_the_mode_and_the_threshold(A):
    mod, _ = A
    c = mod.app.test_client()
    d = c.get("/api/apply-setup", headers=H).get_json()
    assert d["apply"]["mode"] == "assisted" and d["apply"]["auto_sites"] == modes.auto_sites()
    r = c.post("/api/apply-setup/save", json={"step": "apply", "fields": {"mode": "auto", "auto_min_match": 75}}, headers=H)
    assert r.status_code == 200, r.get_json()
    assert mod._apply_mode() == "auto" and mod._auto_min_match() == 75
    again = c.get("/api/apply-setup", headers=H).get_json()["apply"]
    assert again["mode"] == "auto" and again["auto_min_match"] == 75 and again["autonomous"] is True
    c.post("/api/apply-setup/save", json={"step": "apply", "fields": {"mode": "hybrid", "auto_min_match": ""}}, headers=H)
    assert mod._apply_mode() == "hybrid" and mod._auto_min_match() == 0
    m = _get(mod, "/api/autoapply/mode")
    assert m["mode"] == "hybrid" and m["telegram_asks"] is True and m["hidden"] is False


# --------------------------------------------------------------------------- who may press Submit
def test_the_board_says_which_rows_auto_may_send_alone(A):
    mod, _ = A
    auto = _add(mod, "Analyst", "Acme", AUTO_URL, fit=82)
    gh = _add(mod, "Analyst", "Bolt", GH_URL, fit=95)
    assert _row(mod, auto)["match"] == 82
    assert _row(mod, auto)["lane"] == "auto" and _row(mod, gh)["lane"] == "assisted"
    assert _row(mod, auto)["auto_send"] is False                       # Assisted: nothing sends alone
    mod._set_apply_mode("hybrid")
    assert _row(mod, auto)["auto_send"] is False
    mod._set_apply_mode("auto")
    assert _row(mod, auto)["auto_send"] is True and _row(mod, gh)["auto_send"] is False
    mod._set_prefs({"auto_min_match": 90})
    assert _row(mod, auto)["auto_send"] is False                       # 82 is under 90: waits at Ready to send
    mod._set_prefs({"auto_min_match": 80})
    assert _row(mod, auto)["auto_send"] is True
    t = _get(mod, "/api/autoapply/board")["today"]
    assert t["mode"] == "auto" and t["auto_min_match"] == 80 and t["auto_sites"] == modes.auto_sites()


def test_autos_queue_is_the_approved_ones_up_to_the_daily_limit_each_once_a_day(A):
    mod, _ = A
    a = _add(mod, "Analyst", "Acme", AUTO_URL, fit=82)
    b = _add(mod, "Analyst", "Bolt", GH_URL)
    c = _add(mod, "Engineer", "Cove", "https://jobs.lever.co/cove/1")
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    queued = recs.add("Designer", "Dune", 70, "", data={"status": "ready", "source_job": {"url": GH_URL + "9"}})
    ready = recs.add("Writer", "Echo", 70, "", data={"status": "approved", "source_job": {"url": GH_URL + "8"},
                                                     "agent_ready": {"sig": "x", "run": {}}})
    recs.close()
    assert _post(mod, "/api/autoapply/auto/plan")["jobs"] == []         # Assisted: Auto has no queue
    mod._set_apply_mode("auto")
    mod._set_prefs({"daily_cap": 2})
    peek = _post(mod, "/api/autoapply/auto/plan", {"peek": True})
    assert len(peek["jobs"]) == 2
    p = _post(mod, "/api/autoapply/auto/plan")
    ids = [j["recordId"] for j in p["jobs"]]
    assert len(ids) == 2 and set(ids) <= {a, b, c} and p["left"] == 0   # the daily limit
    assert queued not in ids and ready not in ids                       # not approved yet; already ready
    by = {j["recordId"]: j for j in p["jobs"]}
    for rid, j in by.items():
        assert j["autonomous"] is (rid == a)                            # only the allowlisted site
        assert re.match(r"https://", j["url"])
    assert _post(mod, "/api/autoapply/auto/plan")["jobs"] == []         # the limit is used up today
    mod._set_prefs({"daily_cap": 10})
    rest = [j["recordId"] for j in _post(mod, "/api/autoapply/auto/plan")["jobs"]]
    assert len(rest) == 1 and not set(rest) & set(ids)                  # never the same one twice in a day
    # An interrupted application (offline, AI unavailable, the Mac slept) goes back ONCE the same day
    rel = _post(mod, "/api/autoapply/auto/release", {"ids": [ids[0], 999999]})
    assert rel["released"] == [ids[0]]
    again = [j["recordId"] for j in _post(mod, "/api/autoapply/auto/plan")["jobs"]]
    assert again == [ids[0]]
    assert _post(mod, "/api/autoapply/auto/release", {"ids": [ids[0]]})["released"] == []   # not a second time
    assert _post(mod, "/api/autoapply/auto/plan")["jobs"] == []


def test_a_send_by_the_agent_counts_only_when_the_engine_agrees(A):
    mod, chan = A
    a = _add(mod, "Analyst", "Acme", AUTO_URL)
    g = _add(mod, "Analyst", "Bolt", GH_URL)
    z = _add(mod, "Analyst", "Zeta", "https://zeta.recruitee.com/o/analyst")
    # Assisted: a shell that claims "auto" is not believed
    _post(mod, "/api/autofill/applied", {"record_id": z, "url": "https://zeta.recruitee.com/o/analyst", "by": "auto"})
    assert _row(mod, z)["how"] == "you" and chan.sent == []
    mod._set_apply_mode("auto")
    # Auto, off the allowlist: still the person's
    _post(mod, "/api/autofill/applied", {"record_id": g, "url": GH_URL, "by": "auto"})
    assert _row(mod, g)["how"] == "you" and chan.sent == []
    before = _get(mod, "/api/autoapply/board")["today"]["count"]
    _post(mod, "/api/autofill/applied", {"record_id": a, "url": AUTO_URL, "by": "auto"})
    assert _row(mod, a)["how"] == "auto" and _row(mod, a)["tile"] == "submitted"
    assert _get(mod, "/api/autoapply/board")["today"]["count"] == before + 1   # against the daily limit
    # the one notice, once (quiet hours may hold it: either way never twice)
    _post(mod, "/api/autofill/applied", {"record_id": a, "url": AUTO_URL, "by": "auto"})
    told = [t for t, _b in chan.sent] + [h["text"] for h in (mod._notify_state().load().get("auto_sent_held") or {}).values()]
    assert told == ["Sent: Analyst at Acme"]
    assert all(b is None for _t, b in chan.sent)


# --------------------------------------------------------------------------- what the person hears
def test_sent_notice_goes_once_and_respects_quiet_hours(st):
    ch = Chan()
    assert DN.sent_text("Data Analyst", "Acme Corp") == "Sent: Data Analyst at Acme Corp"
    assert DN.notify_sent(st, ch, 7, "Data Analyst", "Acme Corp", _at(14)) == "telegram"
    assert ch.sent == [("Sent: Data Analyst at Acme Corp", None)]
    assert DN.notify_sent(st, ch, 7, "Data Analyst", "Acme Corp", _at(15)) == "dup" and len(ch.sent) == 1
    # quiet hours (22 to 8): held, then sent by the tick once they end
    assert DN.notify_sent(st, ch, 8, "Engineer", "Bolt", _at(23)) == "held" and len(ch.sent) == 1
    assert DN.notify_sent(st, ch, 8, "Engineer", "Bolt", _at(23)) == "dup"
    assert DN.flush_sent(st, ch, _at(2, day=17)) == 0 and len(ch.sent) == 1
    assert DN.flush_sent(st, ch, _at(9, day=17)) == 1 and ch.sent[-1] == ("Sent: Engineer at Bolt", None)
    assert DN.flush_sent(st, ch, _at(10, day=17)) == 0
    assert DN.inbox(st)["notices"] == []


def test_without_telegram_the_notice_is_in_the_app(st):
    assert DN.notify_sent(st, None, 3, "Analyst", "Acme", _at(14)) == "inapp"
    got = DN.inbox(st)["notices"]
    assert [n["text"] for n in got] == ["Sent: Analyst at Acme"] and got[0]["kind"] == "sent"
    DN.mark_seen(st, [got[0]["id"]])
    assert DN.inbox(st)["notices"] == []
    # paused (/stop), "Application updates" off, or a channel that refuses: in the app, not lost
    assert DN.notify_sent(st, Chan(), 4, "Analyst", "Bolt", _at(14), paused=True) == "inapp"
    assert DN.notify_sent(st, Chan("not_linked"), 5, "Analyst", "Cove", _at(14)) == "inapp"
    st.update(lambda d: d.setdefault("prefs", {}).__setitem__("app_updates", False))
    quiet = Chan()
    assert DN.notify_sent(st, quiet, 6, "Analyst", "Dune", _at(14)) == "inapp" and quiet.sent == []
    assert len(DN.inbox(st)["notices"]) == 3
    # a rate limit holds it for the next tick
    st.update(lambda d: d["prefs"].__setitem__("app_updates", True))
    assert DN.notify_sent(st, Chan("rate_limited"), 9, "Analyst", "Echo", _at(14)) == "held"
    ok = Chan()
    assert DN.flush_sent(st, ok, _at(15)) == 1 and ok.sent == [("Sent: Analyst at Echo", None)]


def test_the_daily_summary_is_built_once_a_day_with_counts_only(st):
    ch = Chan()
    counts = {"sent": 1, "ready": 5, "need": 2}
    assert DN.daily_summary(st, ch, counts, _at(12)) is None                # before the hour
    assert DN.daily_summary(st, ch, counts, _at(23)) is None                # quiet hours
    s = DN.daily_summary(st, ch, counts, _at(18))
    assert s["text"] == "Today: 1 sent for you, 5 ready for your Send, 2 need an answer."
    assert (s["sent"], s["ready"], s["need"], s["telegram"]) == (1, 5, 2, True)
    assert len(ch.sent) == 1
    text, buttons = ch.sent[0]
    assert buttons is None                                                 # no Send from Telegram
    assert text.splitlines()[0] == s["text"]
    assert "press Send all" in text and "Nothing more is sent until you do." in text and "Needs you" in text
    assert "—" not in text and "–" not in text
    assert DN.daily_summary(st, ch, {"sent": 9, "ready": 9, "need": 9}, _at(19)) is None   # once a day
    assert DN.daily_summary(st, ch, counts, _at(21)) is None and len(ch.sent) == 1
    nxt = DN.daily_summary(st, ch, {"sent": 0, "ready": 1, "need": 1}, _at(18, day=17))    # a new day
    assert nxt["text"] == "Today: 0 sent for you, 1 ready for your Send, 1 needs an answer."
    assert len(ch.sent) == 2
    assert DN.inbox(st)["summary"]["date"] == "2026-11-17"
    DN.mark_seen(st, summary=True)
    assert DN.inbox(st)["summary"]["seen"] is True


def test_a_day_with_nothing_says_nothing_and_no_telegram_still_builds_it(st):
    ch = Chan()
    s = DN.daily_summary(st, ch, {"sent": 0, "ready": 0, "need": 0}, _at(18))
    assert s["text"] == "" and ch.sent == []
    assert DN.daily_summary(st, ch, {"sent": 3, "ready": 0, "need": 0}, _at(19)) is None
    s2 = DN.daily_summary(st, None, {"sent": 2, "ready": 0, "need": 0}, _at(18, day=17))
    assert s2["telegram"] is False and s2["text"].startswith("Today: 2 sent for you")
    tg = DN.summary_text(2, 0, 0, telegram=True)
    assert tg == "Today: 2 sent for you, 0 ready for your Send, 0 need an answer."       # nothing to do: no tail


def test_the_summary_counts_come_from_the_records_and_carry_no_answers(A):
    mod, chan = A
    mod._set_apply_mode("auto")
    a = _add(mod, "Analyst", "Acme", AUTO_URL)
    _add(mod, "Analyst", "Bolt", GH_URL, agent_ready={"sig": "x", "run": {}},
         answers={"Gender": SELF})
    _add(mod, "Engineer", "Cove", GH_URL + "2", agent_ready={"sig": "y", "run": {}})
    _add(mod, "Writer", "Dune", GH_URL + "3", agent_park={"state": "waiting", "kind": "questions", "message": SELF})
    _add(mod, "Tester", "Echo", GH_URL + "4")                              # approved, not worked on yet
    _post(mod, "/api/autofill/applied", {"record_id": a, "url": AUTO_URL, "by": "auto"})
    # (2026-11-19) and Auto's best guesses waiting for a check, and forms the agent could not do
    assert mod._auto_counts() == {"sent": 1, "ready": 2, "need": 1, "check": 0, "could_not": 0}
    chan.sent.clear()
    s = DN.daily_summary(mod._notify_state(), chan, mod._auto_counts(), _at(18))
    assert s["text"] == "Today: 1 sent for you, 2 ready for your Send, 1 needs an answer."
    assert SELF not in json.dumps(s) and all(SELF not in t for t, _b in chan.sent)
    d = _post(mod, "/api/autoapply/auto/tick")                             # the page's own tick
    assert d["counts"] == {"sent": 1, "ready": 2, "need": 1, "check": 0, "could_not": 0} and "summary" in d["auto"]
    assert _get(mod, "/api/autoapply/board")["auto"]["summary"]["text"] == s["text"]
    seen = _post(mod, "/api/autoapply/auto/seen", {"summary": True})
    assert seen["auto"]["summary"]["seen"] is True


def test_the_tick_builds_no_summary_outside_auto(A, monkeypatch):
    mod, chan = A
    _add(mod, "Analyst", "Bolt", GH_URL, agent_ready={"sig": "x", "run": {}})
    monkeypatch.setattr(DN, "summary_due", lambda state, now: True)
    assert mod._auto_daily_tick()["summary"] is None and chan.sent == []    # Assisted
    mod._set_apply_mode("hybrid")
    assert mod._auto_daily_tick()["summary"] is None and chan.sent == []
    mod._set_apply_mode("auto")
    monkeypatch.undo()
    monkeypatch.setattr(mod, "_active_channel", lambda: chan)
    state = mod._notify_state()
    s = DN.daily_summary(state, chan, mod._auto_counts(), _at(18))
    assert s["ready"] == 1 and len(chan.sent) == 1


# --------------------------------------------------------------------------- Telegram asks
def test_the_ask_goes_to_telegram_only_when_the_shell_says_so(A):
    mod, chan = A
    rid = _add(mod, "Analyst", "Bolt", GH_URL)
    q = {"question": "What is your badge number at Bolt?"}
    park = {"page_url": GH_URL, "run": {"url": GH_URL}, "batch": False}
    # Assisted (and Auto): the shell sends telegram false; the bar still has the ask
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": [q], "record_id": rid, "id": "aa01", "batch": True,
                                                 "park": park, "chunk_s": 0, "telegram": False})
    assert r["reason"] == "waiting" and chan.sent == []
    _post(mod, "/api/autoapply/agent/ask/close", {"id": "aa01"})
    # Hybrid: the same ask also goes to Telegram, led by the fill-through line
    r = _post(mod, "/api/autoapply/agent/ask", {"questions": [q], "record_id": rid, "id": "aa02", "batch": True,
                                                 "park": park, "chunk_s": 0, "telegram": True})
    assert r["reason"] == "waiting" and len(chan.sent) == 1
    assert "badge number" in chan.sent[0][0]
    _post(mod, "/api/autoapply/agent/ask/close", {"id": "aa02"})


# --------------------------------------------------------------------------- the confirmation mail
def test_the_employers_confirmation_is_kept_on_the_application(A):
    mod, _ = A
    sent = _add(mod, "Analyst", "Acme", AUTO_URL, source_job={"url": AUTO_URL, "source_id": "rc:acme:1"})
    other = _add(mod, "Analyst", "Bolt", GH_URL, source_job={"url": GH_URL, "source_id": "gh:bolt:1"})
    _post(mod, "/api/autofill/applied", {"record_id": sent, "url": AUTO_URL})
    msg = {"from": "Acme Careers <no-reply@acme.recruitee.com>", "subject": "We received your application",
           "date": "Mon, 16 Nov 2026 14:02:00 +0000", "body": "Thank you for applying to Acme.  We will be in touch."}
    assert mod._attach_confirmation("rc:acme:1", msg) == 1
    row = _row(mod, sent)
    assert row["confirmation"]["subject"] == "We received your application"
    assert row["confirmation"]["snippet"] == "Thank you for applying to Acme. We will be in touch."
    assert "Acme Careers" in (row["confirmation"]["sender"] + row["confirmation"]["from"])
    assert _row(mod, other)["confirmation"] is None
    later = dict(msg, subject="A second mail")
    assert mod._attach_confirmation("rc:acme:1", later) == 0               # the first one stays
    assert _row(mod, sent)["confirmation"]["subject"] == "We received your application"
    assert mod._attach_confirmation("", msg) == 0 and mod._attach_confirmation("nobody", msg) == 0


# --------------------------------------------------------------------------- the shell and the page
def _node(script):
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / script)], capture_output=True, text=True, timeout=240)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-3000:] + out.stderr[-3000:]


def test_the_shell_auto_is_out_of_sight_hybrid_tells_telegram_and_send_all_stops_on_a_captcha():
    _node("tests/js/test_apply_modes.js")


def test_the_shell_files_parse():
    if not shutil.which("node"):
        pytest.skip("needs node")
    for f in ("shell-electron/agent.js", "shell-electron/main.js", "shell-electron/preload.js", "ui/static/app.js"):
        out = subprocess.run(["node", "--check", str(ROOT / f)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, f + out.stderr


def test_the_pane_is_kept_out_of_view_and_never_reported_open_in_auto():
    src = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    state = src[src.index("function paneState()"):src.index("function pushPaneState()")]
    assert "if (paneQuiet) return { open: false" in state                   # the page never switches to the Browser tab
    layout = src[src.index("function layoutPane()"):src.index("function ensurePane()")]
    assert "paneSync.layout({ open: paneOpen, quiet: paneQuiet," in layout
    view = (ROOT / "shell-electron" / "pane_view.js").read_text(encoding="utf-8")   # the layout rules (2026-11-19)
    assert "if (s.quiet && s.open) return { rect: QUIET_BOUNDS, visible: false };" in view
    m = re.search(r"const QUIET_BOUNDS = Object\.freeze\(\{ x: (-?\d+), y: 0, width: (\d+), height: (\d+) \}\)", view)
    assert m and int(m.group(1)) + int(m.group(2)) < 0 and int(m.group(3)) >= 600   # past the left edge, a real size
    assert 'if (quiet && paneOpen) return { ok: false, reason: "pane-in-use" };' in src   # their own page is never taken
    assert "if (!fromAgent) takePaneBack();" in src                         # the person opening a page ends the quiet run
    assert "setQuiet: setPaneQuiet" in src and "win.focus()" not in src[src.index("function setPaneQuiet"):src.index("function paneState()")]
    pre = (ROOT / "shell-electron" / "preload.js").read_text(encoding="utf-8")
    assert 'ipcRenderer.invoke("agent:send-all"' in pre and 'ipcRenderer.invoke("agent:batch", jobs || [], opts || null)' in pre


def test_the_page_offers_the_three_modes_in_both_places_with_honest_copy():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert '["auto", "mode.auto", "Auto"], ["assisted", "mode.assisted", "Assisted"], ["hybrid", "mode.hybrid", "Hybrid"]' in js
    assert 'suSeg("mode", ' in js and 'id="aaModeSeg"' in html              # Apply setup step 6, and the Auto-apply page
    assert 'api("/api/submit/settings", { apply_mode: mode })' in js
    assert "if (info.quiet) { aaAutoQuiet(info); return; }" in js           # a quiet run never reaches the bar
    quiet = js[js.index("function aaAutoQuiet(info)"):js.index("async function aaAutoNotices()")]
    assert "aaAgentBar" not in quiet and "Panel.open" not in quiet
    tick = js[js.index("async function aaAutoTick()"):js.index("function aaAutoQuiet(info)")]
    assert "Panel.open" not in tick and "aaAgentBar" not in tick and "{ quiet: true }" in tick
    assert 'window.Panel.isOpen("browser")) return;' in tick               # never while the person browses
    assert 'id="aaSendAll"' in html and "agentSendAll(" in js
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert "one site, {sites}" in en["mode.autoS1"] and "waits for your Send all" in en["mode.autoS1"]
    assert "Never on LinkedIn" in en["mode.autoS1"] and "no site lets me send by myself" in en["mode.autoS0"]
    assert "Nothing is asked on Telegram" in en["mode.assistedS"] and "Telegram" in en["mode.hybridS"]
    assert "You press Send in the app" in en["mode.hybridS"]
    keys = [k for k in en if k.startswith("mode.")] + ["aa.confirmed"]
    assert len(keys) >= 20
    for code in ("de", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert loc.get(k), f"{code}: {k}"
            assert "—" not in loc[k] and "–" not in loc[k]
            assert set(re.findall(r"\{\w+\}", loc[k])) == set(re.findall(r"\{\w+\}", en[k])), f"{code}: {k}"
    for k in keys:
        assert "—" not in en[k] and "–" not in en[k] and "CV" not in en[k]


def test_the_auto_apply_title_never_wraps():
    css = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".view-autoapply .page-head .page-title { white-space: nowrap; }" in css
    head = css[css.index(".view-autoapply .page-head { align-items: center;"):]
    assert "flex-wrap: wrap" in head[:120]                                  # the actions drop under the title instead
