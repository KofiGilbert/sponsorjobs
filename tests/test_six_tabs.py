"""Six tabs (2026-11-24): with the apply agent off (the default), the side menu is Home, Resumes,
Jobs, Tracker, Projects and Interview. Applications, Inbox and the live Building item are the
agent's: hidden while it is off, back with SJ_APPLY_AGENT=1.

* every door to a hidden page lands somewhere real (Applications -> the Tracker; the Inbox opens
  read only from Settings with a back link; an unfinished resume is on Home's Continue tailoring);
* Home offers one job at a time: no batch ideas, and a batch request typed anyway runs nothing;
* Resumes has no follow-up banner (follow-up dates live on the Tracker's cards);
* forms get the person's own email, never the SponsorJobs address, while the agent is off;
* Your answers and Apply setup open from Settings, Applying; old mail too, only when there is some;
* Telegram texts name the Tracker and "Settings, Your answers" while the agent is off.

Made-up values only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import ui.app as app

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
LOCALES = ("en", "de", "es", "fr", "pt")
EXT = {"X-Tailor-Extension": "1"}
OWN = "sam.rivera@example.com"
ALIAS = "sam.rivera@sponsorjobs.ai"


@pytest.fixture()
def off(monkeypatch):
    monkeypatch.delenv("SJ_APPLY_AGENT", raising=False)


def _page() -> str:
    return app.app.test_client().get("/").get_data(as_text=True)


def _nav(html: str) -> str:
    return html[html.index('<nav class="nav"'):html.index("</nav>")]


def _fn(name: str) -> str:
    """The source of one top-level function in app.js (up to the next two-space-indented close)."""
    i = APP_JS.index(name)
    return APP_JS[i:APP_JS.index("\n  }\n", i)]


# --------------------------------------------------------------------------- the menu
def test_agent_off_the_menu_has_six_items(off):
    html = _page()
    assert 'data-apply-agent="0"' in html
    nav = _nav(html)
    items = re.findall(r'<button class="nav-item[^"]*" data-nav="(\w+)"[^>]*>', nav)
    shown = [m.group(1) for m in re.finditer(r'<button class="nav-item[^"]*" data-nav="(\w+)"([^>]*)>', nav)
             if "data-agent-only" not in m.group(2)]
    assert shown == ["dashboard", "cvs", "jobs", "tracker", "projects", "prep"]
    assert set(items) - set(shown) == {"inbox", "autoapply"}
    # CSS hides [data-agent-only] while the switch is not "1"
    assert 'html:not([data-apply-agent="1"]) [data-agent-only]' in CSS
    # the live Building item is never painted with the agent off
    paint = _fn("function paintLiveNav(st)")
    assert "if (!APPLY_AGENT) { host.hidden = true;" in paint


def test_agent_on_the_three_items_are_back(apply_agent_on):
    html = _page()
    assert 'data-apply-agent="1"' in html
    nav = _nav(html)
    assert 'data-nav="inbox"' in nav and 'data-nav="autoapply"' in nav
    assert 'id="liveNav"' in nav
    # with the switch at "1" the agent-only rule does not match, so they show
    assert 'html[data-apply-agent="1"] [data-agent-only]' not in CSS


# --------------------------------------------------------------------------- no dead ends
def test_no_agent_off_route_reaches_a_hidden_page():
    # one rule: Applications means the Tracker while the agent is off
    assert 'const navDest = (dest) => (!APPLY_AGENT && dest === "autoapply" ? "tracker" : dest);' in APP_JS
    nav = _fn("function navTo(dest, tile)")
    assert "dest = navDest(dest);" in nav
    # a click on the hidden nav item itself (old code paths click it) lands on the Tracker
    i = APP_JS.index('document.querySelectorAll(".nav-item").forEach(n => n.addEventListener("click", () => {')
    handler = APP_JS[i:i + 900]
    assert "if (navDest(n.dataset.nav) !== n.dataset.nav)" in handler
    assert 'if (n.dataset.nav === "inbox" && !APPLY_AGENT) ibSetBack();' in handler
    # back links never return to a hidden page
    assert 'else navTo("autoapply");   // agent off, navTo lands on the Tracker' in APP_JS      # memory page
    assert 'else if (PKG_ORIGIN === "autoapply") navTo("autoapply");' in APP_JS               # package page
    assert 'else if (SU.origin === "settings") { showView("dashboard"); loadDashboard(); openSettings(); }' in APP_JS
    assert 'settings: ["mem.backSettings", "Settings"] };' in APP_JS                          # Apply setup back
    # the Inbox's back link goes to Settings or the Tracker, never to a hidden page
    assert 'const IB_BACK = { settings: ["mem.backSettings", "Settings"], tracker: ["nav.tracker", "Tracker"] };' in APP_JS
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'id="ibPageBack" type="button" data-assisted-only' in html
    assert html.count('id="ibPageBack"') == 1 and 'id="ibBack"' not in html   # the reader's own back link is ibBack
    # the chat's "Open Tracker" button
    assert 'open_tracker: agentT("agent.openTracker", "Open Tracker")' in APP_JS
    assert 'navTo(id === "open_tracker" ? "tracker" : "autoapply")' in APP_JS
    # no code path clicks the hidden Applications or Inbox nav item directly: every door goes
    # through navTo, which knows where it lands with the agent off
    assert not re.search(r"""\[data-nav=["']?(autoapply|inbox)""", APP_JS)


def test_agent_off_the_inbox_has_no_address_line_and_no_get_address(off):
    html = _page()
    assert 'id="ibAddr" hidden data-agent-only' in html
    empty = _fn("function ibPaintEmpty(kind)")
    assert 'if (!APPLY_AGENT && kind !== "noresults")' in empty
    head = empty[:empty.index('} else if (kind === "noaddr")')]
    assert "data-ib-getaddr" not in head and "sponsorjobs.ai" not in head


# --------------------------------------------------------------------------- Home
def test_agent_off_home_has_no_batch_suggestions(off):
    html = _page()
    box = html[html.index('id="homeSuggest"'):html.index('<!-- The prompt stays put')]
    rows = re.findall(r'class="home-suggest-row"[^>]*>([^<]+)<', box)
    assert rows == ["Tailor my resume for a job I paste"]
    for batch in ("Find 5", "I am away", "What is waiting on me?", "Telegram"):
        assert batch not in box
    assert 'placeholder="Paste a job description or a link..."' in html
    assert 'id="homeExamples"' in html and 'id="homeContinue"' in html     # both stay
    # the row readies the box for a paste instead of sending its own words
    assert 'if (rows[i].hasAttribute("data-sug-paste"))' in APP_JS


def test_agent_on_home_keeps_its_three_ideas(apply_agent_on):
    box = _page()
    box = box[box.index('id="homeSuggest"'):box.index('<!-- The prompt stays put')]
    assert len(re.findall(r'class="home-suggest-row"', box)) == 3 and "data-sug-paste" not in box


@pytest.fixture()
def ag(off, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_make_llm", lambda: object())       # the rules parse
    monkeypatch.setattr(app, "_aa_has_profile", lambda: True)
    monkeypatch.setattr(app, "_active_channel", lambda: None)
    runs = []
    monkeypatch.setattr(app, "_aa_run", lambda *a, **k: runs.append(a) or ({"tailored": []}, 200))
    monkeypatch.setitem(app._AGENT, "plan", None)
    monkeypatch.setitem(app._AGENT, "intent", None)
    monkeypatch.setitem(app._AGENT, "pending", None)
    return app.app.test_client(), runs


def test_agent_off_a_batch_request_runs_nothing(ag):
    from agent.home import SINGLE_JOB_TEXT
    c, runs = ag
    for text in ("Find 5 data analytics roles and tailor my resume for each",
                 "I am away for 3 hours: tailor 10 business analyst roles and message me on Telegram"):
        d = c.post("/api/agent/ask", json={"text": text}).get_json()
        assert d["kind"] == "agent" and d["text"] == SINGLE_JOB_TEXT, d
        assert d["buttons"] == [] and "connect" not in d
        assert app._AGENT["plan"] is None
    # Go ahead has nothing to run, even with a plan left over from before the switch
    app._AGENT["plan"] = {"focus": {}, "count": 5, "min_fit": 60, "telegram": False, "titles": []}
    g = c.post("/api/agent/go", json={}).get_json()
    assert g["ok"] is False and g["batch"] == "off" and g["text"] == SINGLE_JOB_TEXT
    assert runs == [] and app._AGENT["plan"] is None
    assert SINGLE_JOB_TEXT == "Paste a job here, or pick one on Home, and I will tailor your resume for it."


def test_agent_off_status_and_other_point_at_the_tracker_and_the_box(ag):
    from agent.home import SINGLE_JOB_TEXT
    c, _ = ag
    d = c.post("/api/agent/ask", json={"text": "What is waiting on me?"}).get_json()
    assert d["buttons"] == [{"id": "open_tracker", "label": "Open Tracker"}]
    d = c.post("/api/agent/ask", json={"text": "tell me a joke"}).get_json()
    assert d["text"] == SINGLE_JOB_TEXT
    d = c.post("/api/agent/ask", json={"text": "tailor my resume for a data analyst role"}).get_json()
    assert d["buttons"] == [] and "pick one on Home" in d["text"]


def test_continue_tailoring_lists_every_unfinished_session(monkeypatch):
    class Store:
        def list(self, limit=50):
            return [{"job_id": f"pasted:{i}"} for i in range(9)]
    monkeypatch.setattr(app, "_session_store", lambda: Store())
    monkeypatch.setattr(app, "_job_sessions_info", lambda ids: {
        sid: {"source_id": sid, "status": "in_progress", "closed_at": None, "stage": "in_progress"}
        for sid in ids})
    monkeypatch.setitem(app._SESSION, "s", None)
    d = app.app.test_client().get("/api/tracker/continue").get_json()
    assert d["count"] == 9 and len(d["in_progress"]) == 9


# --------------------------------------------------------------------------- Resumes
def test_resumes_has_no_follow_up_banner():
    assert "worth a quick follow-up" not in APP_JS and "fu-banner" not in APP_JS
    assert "followupBannerHTML" not in APP_JS and ".fu-banner" not in CSS
    load = _fn("async function loadCvs()")
    assert "/api/followups" not in load
    # the per-row "Follow up due" fact stays
    assert 'I18N.t("cvs.followUpDue", "Follow up due")' in APP_JS


# --------------------------------------------------------------------------- email
def _identity(monkeypatch):
    monkeypatch.setattr(app, "_saved_identity", lambda: {"name": "Sam Rivera", "email": OWN})
    monkeypatch.setattr(app, "_mail_alias", lambda: ALIAS)


def test_agent_off_forms_get_the_persons_own_email(off, monkeypatch):
    _identity(monkeypatch)
    assert app._application_email() == OWN
    f = app.app.test_client().get("/api/profile/autofill", headers=EXT).get_json()["fields"]
    assert f["email"] == OWN
    # Apply setup does not offer "Apply with my SponsorJobs address"
    monkeypatch.setattr(app, "_prefs", lambda: {"mail_alias": ALIAS, "apply_with_alias": True})
    assert app._setup_payload()["contact"]["alias"] == ""


def test_agent_on_forms_keep_the_address(apply_agent_on, monkeypatch):
    _identity(monkeypatch)
    monkeypatch.setattr(app, "_prefs", lambda: {"apply_with_alias": True})
    assert app._application_email() == ALIAS
    f = app.app.test_client().get("/api/profile/autofill", headers=EXT).get_json()["fields"]
    assert f["email"] == ALIAS


def test_agent_off_no_copy_asks_for_the_address(off):
    html = _page()
    # the whole Settings Email block (Get an address, copies to my e-mail, own inbox) is the agent's
    for marker in ('<div class="conn-row" data-agent-only>', 'id="gmDetail" hidden data-agent-only',
                   'id="mailNotifyRow" hidden data-agent-only', 'id="gmForm" hidden autocomplete="off" data-agent-only'):
        assert marker in html, marker
    i = html.index('<div class="conn-row" data-agent-only>')
    assert 'id="gmAlias"' in html[i:i + 600]


# --------------------------------------------------------------------------- Settings
def test_settings_applying_opens_answers_setup_and_old_mail(off):
    html = _page()
    assert '<button class="set-tab" data-set="applying" type="button" data-i18n="settings.applying">' in html
    pane = html[html.index('data-pane="applying"'):]
    pane = pane[:pane.index("</section>")]
    assert 'id="setAnswersOpen"' in pane and 'id="setSetupOpen"' in pane
    assert 'id="setMailRow" hidden data-assisted-only' in pane
    assert pane.count("data-agent-only") == 3                      # the agent's send switches only
    assert '$("#setAnswersOpen")?.addEventListener("click", () => { closeSettings(); openAnswers("settings"); });' in APP_JS
    assert 'openApplySetup({ origin: "settings" })' in APP_JS
    assert 'IB_FROM = "settings"; navTo("inbox");' in APP_JS
    row = _fn("async function renderMailRow()")
    assert "row.hidden = !n;" in row                                # only when the mailbox has mail
    # Telegram is not duplicated: one Connect Telegram control in Settings
    assert html.count('id="ntConnect"') == 1


# --------------------------------------------------------------------------- Telegram texts
def test_telegram_texts_follow_the_switch(off):
    from notify import assistant as AS
    from notify.alerts import digest_text
    d = {"tailored": 2, "waiting": 2}
    assert "go to the Tracker" in digest_text(d, assisted=True)
    assert "go to Applications" in digest_text(d)
    assert AS.where_answers(True) == "Settings, Your answers"
    assert "Settings, Your answers" in AS.eeo_pointer(True) and "Settings, Your answers" in AS.system(True)
    assert "Applications" not in AS.eeo_pointer(True) + AS.pointer(True) + AS.system(True)
    assert AS.SYSTEM == AS.system() and AS.EEO_POINTER == AS.eeo_pointer()
    assert app._autopilot_act("ap:approveall:abc")["message"] == "Open SponsorJobs to review these in the Tracker."


# --------------------------------------------------------------------------- strings
def test_every_new_string_is_in_every_locale_without_dashes():
    keys = ("home.placeholderA", "home.tryPaste", "home.pasteHere", "agent.subA", "agent.placeholderA",
            "agent.openTracker", "set.answersDesc", "set.setupDesc", "set.mailTitle", "setup.subA",
            "setup.l.wantsA", "mbox.empty.oldT", "mbox.empty.old", "aa.why.allDoneA")
    for code in LOCALES:
        d = json.loads((ROOT / f"ui/static/locales/{code}.json").read_text(encoding="utf-8"))
        for k in keys:
            assert d.get(k), (code, k)
            assert not re.search(r"[—–]", d[k]), (code, k)
        assert "home.try1A" not in d and "mbox.empty.noaddrA" not in d
    en = json.loads((ROOT / "ui/static/locales/en.json").read_text(encoding="utf-8"))
    assert "CV" not in "".join(en[k] for k in keys)
