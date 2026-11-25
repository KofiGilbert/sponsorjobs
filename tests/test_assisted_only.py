"""Launch with assisted apply only (2026-11-24).

The multi-step apply agent (it clicks Apply, signs in, creates accounts, pages through and, in
Auto, submits) is OFF by default behind ONE switch, ``SJ_APPLY_AGENT=1`` (ui/app.py
``_apply_agent_enabled``). Off:

* the page carries ``data-apply-agent="0"`` and the settings say ``apply_agent: false``;
* the apply mode always reads Assisted, nothing is sent alone, and the agent's settings cannot
  be changed (they wait, untouched, for when it is back);
* the engine refuses the routes only a running agent uses (403, ``apply_agent_off``) and the
  parked list reads empty, so a stray run cannot act;
* the shell is told never to click a site's Submit, the verified AUTO lane included, and an
  approval never submits through the Recruitee API either;
* the Autopilot never runs and its Telegram buttons do nothing;
* a job the agent parked or left at Ready to send shows as a normal application to fill;
* the UI hides every agent control and only ever calls the fill with ``submit: false``.

The agent's own tests turn it on with the ``apply_agent_on`` fixture (tests/conftest.py).
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
H = {"X-Tailor-Extension": "1"}
AUTO_URL = "https://acme.recruitee.com/o/analyst"
GH_URL = "https://boards.greenhouse.io/bolt/jobs/1"
LOCALES = ("en", "de", "es", "fr", "pt")


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.delenv("SJ_APPLY_AGENT", raising=False)
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    return mod, mod.app.test_client()


def _add(mod, url, **data):
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        return recs.add("Analyst", "Acme", 80, "",
                        data={"status": "approved", "fit": 80, "source_job": {"url": url}, **data})
    finally:
        recs.close()


def _row(c, rid):
    b = c.get("/api/autoapply/board", headers=H).get_json()
    return b, next(x for t in b["tiles"].values() for x in t if x["id"] == rid)


# --------------------------------------------------------------------------- the switch
def test_the_switch_is_off_by_default_and_only_1_turns_it_on(A, monkeypatch):
    mod, _ = A
    assert mod._apply_agent_enabled() is False
    for v in ("0", "true", "yes", ""):
        monkeypatch.setenv("SJ_APPLY_AGENT", v)
        assert mod._apply_agent_enabled() is False, v
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    assert mod._apply_agent_enabled() is True


def test_the_page_and_the_settings_carry_the_switch(A, monkeypatch):
    mod, c = A
    page = c.get("/").get_data(as_text=True)
    assert '<html lang="en" data-apply-agent="0">' in page
    # (six tabs) Home offers one job at a time: no batch idea at all
    assert "Tailor my resume for a job I paste" in page and "Find 5 data analytics roles" not in page
    assert "apply for me" not in page
    s = c.get("/api/submit/settings", headers=H).get_json()
    assert s["apply_agent"] is False and s["autopilot"] is False
    assert c.get("/api/autoapply/board", headers=H).get_json()["apply_agent"] is False
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    assert 'data-apply-agent="1"' in c.get("/").get_data(as_text=True)
    assert c.get("/api/submit/settings", headers=H).get_json()["apply_agent"] is True


# --------------------------------------------------------------------------- the mode
def test_the_mode_reads_assisted_whatever_was_saved_and_cannot_be_changed(A, monkeypatch):
    mod, c = A
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    mod._set_apply_mode("auto")
    mod._set_prefs({"autopilot": True})
    assert mod._apply_mode() == "auto"
    monkeypatch.delenv("SJ_APPLY_AGENT")
    assert mod._apply_mode() == "assisted" and mod._autonomous_on() is False
    assert mod._may_send_alone("auto", 99) is False
    assert mod._can_auto_apply({"url": AUTO_URL}) is False
    r = c.post("/api/submit/settings", json={"apply_mode": "auto", "autonomous": True, "autopilot": True},
               headers=H).get_json()
    assert r["apply_mode"] == "assisted" and r["autonomous"] is False and r["autopilot"] is False
    m = c.get("/api/autoapply/mode", headers=H).get_json()
    assert m["mode"] == "assisted" and m["telegram_asks"] is False and m["hidden"] is False
    # the saved choice waits for the agent, untouched
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    assert mod._apply_mode() == "auto"


# --------------------------------------------------------------------------- engine refusal
@pytest.mark.parametrize("path", ["/api/autoapply/agent/step", "/api/autoapply/agent/ready",
                                  "/api/autoapply/agent/park", "/api/autoapply/agent/precheck",
                                  "/api/autoapply/agent/ask", "/api/autoapply/agent/portal-account",
                                  "/api/autoapply/agent/sign", "/api/autoapply/auto/plan",
                                  "/api/autoapply/auto/release"])
def test_the_engine_refuses_what_only_a_running_agent_uses(A, path):
    _, c = A
    r = c.post(path, json={"record_id": 1}, headers=H)
    assert r.status_code == 403
    d = r.get_json()
    assert d["ok"] is False and d["reason"] == "apply_agent_off" and "Autofill this page" in d["error"]


def test_the_parked_list_reads_empty_and_the_one_page_fill_routes_stay_open(A, monkeypatch):
    mod, c = A
    d = c.get("/api/autoapply/agent/parked", headers=H).get_json()
    assert d["ok"] is True and d["parked"] == []
    # what Fill this page uses is not refused
    assert c.get("/api/submit/decision?url=" + GH_URL, headers=H).status_code == 200
    assert c.get("/api/autoapply/board", headers=H).status_code == 200
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    assert c.post("/api/autoapply/agent/step", json={}, headers=H).status_code != 403


def test_the_shell_is_never_told_to_click_submit_not_even_on_the_auto_lane(A, monkeypatch):
    _, c = A
    d = c.get("/api/submit/decision?url=" + AUTO_URL, headers=H).get_json()
    assert d["fill"] is True and d["click_submit"] is False and d["lane"] == "assisted"
    monkeypatch.setenv("SJ_APPLY_AGENT", "1")
    d = c.get("/api/submit/decision?url=" + AUTO_URL, headers=H).get_json()
    assert d["click_submit"] is True and d["lane"] == "auto"         # unchanged with the agent on


def test_an_approval_never_submits_through_the_api(A, monkeypatch):
    mod, c = A
    import submit
    called = []
    monkeypatch.setattr(submit, "driver_for", lambda host: called.append(host) or (lambda url, data: {"ok": True}))
    rid = _add(mod, AUTO_URL, status="ready")
    r = c.post("/api/review/approve", json={"approve": [rid]}, headers=H).get_json()
    assert r["approved"][0]["ok"] is True and r["approved"][0]["stage"] == "approved"
    assert called == []
    assert mod._submit_driver_for(AUTO_URL) is None
    assert mod._BatchActions().drain() == 0
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        assert json.loads(recs.get(rid)["data"])["status"] == "approved"
    finally:
        recs.close()


# --------------------------------------------------------------------------- autopilot
def test_the_autopilot_never_runs_and_its_buttons_do_nothing(A, monkeypatch):
    mod, _ = A
    mod._set_prefs({"autopilot": True})
    ran = []
    monkeypatch.setattr(mod, "_aa_run", lambda *a, **k: ran.append(a) or ({"tailored": []}, 200))
    assert mod._autopilot_tick() == {"ran": False, "why": "agent_off"}
    mod._autopilot_kick()
    th = mod._AUTOPILOT_THREAD["thread"]
    assert th is None or not th.is_alive()
    assert ran == []
    assert mod._autopilot_status()["on"] is False
    rid = _add(mod, GH_URL, status="ready")
    _ts, today = mod._now_ts_today()
    st = mod._autopilot_state(today)
    st["batches"]["b1"] = {"ids": [rid], "at": "now"}
    mod._autopilot_save(st)
    for cmd in ("approveall", "review", "skip"):
        out = mod._autopilot_act(f"ap:{cmd}:b1")
        assert "Open SponsorJobs" in out["message"] and not out.get("ok")
    assert "b1" in (mod._notify_state().get("autopilot") or {}).get("batches", {})
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        assert json.loads(recs.get(rid)["data"])["status"] == "ready"
    finally:
        recs.close()


# --------------------------------------------------------------------------- earlier agent runs
def test_a_parked_or_ready_job_from_before_is_a_normal_application_to_fill(A):
    mod, c = A
    parked = _add(mod, GH_URL, agent_park={"state": "waiting", "kind": "questions", "message": "Your salary?"})
    ready = _add(mod, GH_URL, agent_ready={"sig": "abc"})
    auto = _add(mod, AUTO_URL, queued=True)
    b, p = _row(c, parked)
    assert p["tile"] == "waiting" and p["action"] == "fill" and p["paused"] is None
    _, r = _row(c, ready)
    assert r["action"] == "fill" and r["ready_sig"] == ""
    _, a = _row(c, auto)
    assert a["lane"] == "assisted" and a["auto_send"] is False and a["queued"] is False
    assert b["counts"]["needs"] == 0
    assert b["autopilot"]["on"] is False and b["today"]["mode"] == "assisted"


# --------------------------------------------------------------------------- the home agent
def test_the_home_agent_promises_no_sending(A):
    from agent.home import plan_text, result_text
    t = plan_text(n=3, titles=["analyst"], min_fit=60, auto=0, auto_companies="", auto_reason="x",
                  telegram=False, telegram_connected=False, packages_left=None, assisted=True)
    assert "sent for you" not in t and "I fill in the form for you to check and submit" in t
    r = result_text(3, 0, assisted=True)
    assert "sent for you" not in r and "To apply on the Tracker" in r


# --------------------------------------------------------------------------- the UI
def test_the_ui_reads_the_switch_and_never_submits_with_it_off():
    assert 'document.documentElement.dataset.applyAgent === "1"' in APP_JS
    assert "const AA_HAS_AGENT = () => APPLY_AGENT && " in APP_JS
    # the fill's submit is false everywhere, or needs the agent on
    calls = re.findall(r"applyInPane\(\{[^}]*\}\)", APP_JS)
    assert len(calls) >= 3
    for call in calls:
        assert "submit: false" in call or "submit: auto" in call, call
    assert "const auto = APPLY_AGENT && (" in APP_JS
    # the builder's Apply is the one-page fill when the agent is off
    assert "(APPLY_AGENT ? window.tailorShell.agentRun : window.tailorShell.assistApply)" in APP_JS
    # How to apply leaves the setup
    assert '.filter(s => APPLY_AGENT || s[0] !== "apply")' in APP_JS
    # the agent's notices are not shown or recorded
    assert "if (!line || !APPLY_AGENT) return;" in APP_JS and "if (TOU_LINE || !APPLY_AGENT) return;" in APP_JS


def test_every_agent_control_is_hidden_and_fill_this_page_is_on_the_bar():
    assert 'html:not([data-apply-agent="1"]) [data-agent-only]' in CSS
    assert 'html[data-apply-agent="1"] [data-assisted-only]' in CSS
    for marker in ('id="aaAutonomous" data-agent-only', 'id="aaApRow" data-agent-only',
                   'class="aa-cap" data-agent-only', 'id="aaMayRow" data-agent-only',
                   'id="aaTou" hidden data-agent-only', 'id="aaAutoSum" hidden data-agent-only',
                   'id="aaApplyAll" type="button" hidden data-agent-only', 'aria-labelledby="memMayH" data-agent-only',
                   # (six tabs) the Applying tab shows in both modes; its send switches are the agent's
                   '<p class="set-note" data-agent-only>SponsorJobs only ever auto-submits',
                   'data-i18n="aa.how1" data-agent-only', 'data-i18n="signin.terms2" data-agent-only'):
        assert marker in INDEX, marker
    assert 'id="bbFillHere"' in INDEX and 'data-i18n="bb.fillHere"' in INDEX
    m = re.search(r"async function bbFillHere\(\) \{.*?\n  \}", APP_JS, re.S)
    assert m and 'submit: false, here: true' in m.group(0)


def test_the_page_is_called_applications_and_every_new_string_has_every_locale():
    assert '<h1 class="page-title" data-i18n="nav.autoapply">Applications</h1>' in INDEX
    assert 'data-rail="Applications">Applications</span>' in INDEX
    want = {"en": "Applications", "de": "Bewerbungen", "es": "Solicitudes", "fr": "Candidatures", "pt": "Candidaturas"}
    keys = ("bb.fillHere", "aa.howA1", "aa.howA2", "aa.howA3", "notify.tgDescA", "notify.updatesDescA",
            "aa.sub.submittedA", "aa.footA", "signin.terms2A", "home.tryPaste")
    for code in LOCALES:
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        assert d["nav.autoapply"] == want[code]
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[—–]", d[k]), (code, k)
        assert "CV" not in d["bb.fillHere"] + d["aa.howA2"]
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert not [k for k, v in en.items() if "Auto-apply" in v]
