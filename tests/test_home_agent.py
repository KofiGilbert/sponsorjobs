"""The Home chat agent: the Home box understands plain requests and drives the existing
features, stating a plan first (CLAUDE.md §4, agent/home.py, /api/agent/* in ui/app.py).

Fully offline: the intent parse runs on the rules or a stub model, the tailoring, the broker
usage and the channel are fakes. Nothing is submitted except through the fake submit path,
and only for the verified auto lane with autonomous submission on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ui.app as app
from agent import home
from ui.records import CVRecords

ROOT = Path(__file__).resolve().parents[1]
DAY1 = "2026-10-26"

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

JD = ("Data Analyst, Acme\n"
      "Responsibilities:\n- Build dashboards and reports for the sales team.\n"
      "Requirements:\n- 2 years with SQL and Python.\n- Strong communication.\n")


class FakeChannel:
    name = "official"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        from notify.channel import _as_rows
        self.sent.append({"text": text, "buttons": _as_rows(buttons) or None})
        return {"ok": True, "message_id": len(self.sent)}


class StubLLM:
    """A model that answers the intent prompt with whatever JSON the test scripted."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def _complete(self, system, user, max_tokens=1024, effort=None):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                           "effort": effort})
        return self.answer


@pytest.fixture()
def ag(tmp_path, monkeypatch):
    """ui.app with every store under tmp, the ranking fed by four roles, the tailoring faked
    (it writes a real record), the parse on the rules, and the broker's package count pinned."""
    monkeypatch.setattr(app, "_DATA", tmp_path)
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_RATE_FILE", tmp_path / "rate.json")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    monkeypatch.setattr(app, "_TG_OFFSET_FILE", tmp_path / "off")
    monkeypatch.setattr(app, "_NOTIFY_STATE_FILE", tmp_path / "notify_state.json")
    state = {"left": 20, "channel": None, "tailored": [], "runs": []}
    monkeypatch.setattr(app, "_now_ts_today", lambda: (0.0, DAY1))
    monkeypatch.setattr(app, "_autopilot_packages_left", lambda: state["left"])
    monkeypatch.setattr(app, "_active_channel", lambda: state["channel"])
    monkeypatch.setattr(app, "_aa_has_profile", lambda: True)
    monkeypatch.setattr(app, "_cred", lambda k: "")
    monkeypatch.setattr(app, "_make_llm", lambda: object())       # no _complete: the rules parse
    monkeypatch.setattr(app, "_load_template", lambda name: ("", "t"))
    meta = {"titles": [], "locations": [], "sponsor_only": False, "search": None,
            "board": len(JOBS), "saved": 0}
    monkeypatch.setattr(app, "_aa_candidates", lambda focus: ([dict(j) for j in JOBS], meta))
    monkeypatch.setattr(app, "_aa_scorer",
                        lambda rows, deadline=None, network=False: (lambda job: 80))
    app._AGENT.update(plan=None, intent=None)

    def fake_tailor(job, llm, tex, tname, scratch):
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
    real_run = app._aa_run

    def spy_run(count, min_fit, focus, **kw):
        state["runs"].append({"count": count, "min_fit": min_fit, "focus": focus})
        return real_run(count, min_fit, focus, **kw)
    monkeypatch.setattr(app, "_aa_run", spy_run)
    return state


def _ask(text, change=False):
    c = app.app.test_client()
    r = c.post("/api/agent/ask", json={"text": text, "change": change})
    return r.get_json(), r.status_code


# --- routing ----------------------------------------------------------------------------------

def test_a_link_or_a_posting_keeps_the_tailoring_path():
    assert home.route("https://jobs.lever.co/acme/123") == "url"
    assert home.route("www.greenhouse.io/acme/jobs/1") == "url"
    assert home.route(JD) == "jd"
    assert home.route("x " * 800) == "jd"                     # very long: a posting either way
    assert home.route("Find 5 data analytics roles and apply for me") == "request"
    assert home.route("What is waiting on me?") == "request"
    # Three short lines without posting words are still a request, not a posting.
    assert home.route("find roles\nfor me\nplease") == "request"


def test_the_route_answers_jd_and_url_without_parsing(ag):
    assert _ask(JD)[0] == {"ok": True, "kind": "jd"}
    assert _ask("https://jobs.lever.co/acme/123")[0] == {"ok": True, "kind": "url"}
    d, st = _ask("")
    assert st == 400 and "error" in d


# --- the parse --------------------------------------------------------------------------------

def test_rules_read_the_three_examples():
    a = home.parse_request_rules(home.EXAMPLES[0])
    assert a["intent"] == "find_and_apply" and a["count"] == 5
    assert a["titles"] == ["data analytics"] and a["telegram"] is False
    b = home.parse_request_rules(home.EXAMPLES[1])
    assert b["intent"] == "find_and_apply" and b["count"] == 10 and b["away_hours"] == 3
    assert b["titles"] == ["business analyst"] and b["telegram"] is True
    c = home.parse_request_rules(home.EXAMPLES[2])
    assert c["intent"] == "status"
    d = home.parse_request_rules("tailor my resume for a data analyst role at Stripe")
    assert d["intent"] == "tailor_one" and d["titles"] == ["data analyst"]
    e = home.parse_request_rules("find business analyst jobs in New York, sponsors only")
    assert e["location"] == "New York" and e["sponsors_only"] is True
    assert home.parse_request_rules("hello there")["intent"] == "other"


def test_model_json_is_used_and_normalized():
    llm = StubLLM('```json\n{"intent": "find_and_apply", "titles": "data analyst", '
                  '"count": "4", "location": "Austin", "telegram": 1, "away_hours": null, '
                  '"sponsors_only": false}\n```')
    out = home.parse_request(llm, "get me a few data analyst roles in Austin and ping telegram")
    assert out == {"intent": "find_and_apply", "titles": ["data analyst"], "count": 4,
                   "location": "Austin", "telegram": True, "away_hours": None,
                   "sponsors_only": False}
    call = llm.calls[0]
    assert call["effort"] == "low" and call["max_tokens"] <= 300
    assert "JSON" in call["system"] and len(call["system"]) < 1200       # the prompt stays tiny


def test_a_bad_model_answer_falls_back_to_the_rules():
    out = home.parse_request(StubLLM("Sure! Let me look into that."), home.EXAMPLES[0])
    assert out["intent"] == "find_and_apply" and out["count"] == 5
    out = home.parse_request(StubLLM('{"intent": "dance"}'), home.EXAMPLES[2])
    assert out["intent"] == "status"
    # A backend without a free-form call (FakeLLM) parses on the rules too.
    from llm.base import FakeLLM
    assert home.parse_request(FakeLLM(), home.EXAMPLES[1])["telegram"] is True


# --- the plan ---------------------------------------------------------------------------------

def test_plan_when_nothing_can_be_sent(ag):
    d, st = _ask(home.EXAMPLES[0])
    assert st == 200 and d["kind"] == "agent" and d["intent"]["intent"] == "find_and_apply"
    assert d["text"] == (
        "Here is the plan.\n"
        "1. Find the 4 best data analytics roles that fit you at 60% or better, visa sponsors first.\n"
        "2. Tailor a resume and a cover letter for each, from your saved profile.\n"
        "3. Queue them in To review. 0 can be sent for you (sending for you is off in Auto-apply); "
        "4 will be filled in and ready for your click.\n"
        "Only 4 match right now, so I will do 4. This uses 4 of your 20 packages. Go ahead?")
    assert [b["id"] for b in d["buttons"]] == ["go", "change"]
    assert ag["tailored"] == [] and ag["runs"] == []           # a plan spends nothing
    assert app._AGENT["plan"]["focus"] == {"titles": ["data analytics"], "locations": [],
                                           "sponsor_only": False}


def test_plan_counts_the_verified_site_when_autonomous_is_on(ag):
    app._set_autonomous(True)
    d, _ = _ask("find 4 analyst roles and apply for me")
    assert ("3. Queue them in To review. 1 can be sent for you (Gamma); 3 will be filled in "
            "and ready for your click.") in d["text"]
    assert "4. " not in d["text"]                               # Telegram was not asked for


def test_plan_telegram_line_connected_and_not(ag):
    d, _ = _ask(home.EXAMPLES[1])
    assert ("4. Telegram is not connected yet, so I will show the results here. Connect it in "
            "Auto-apply to get them on your phone.") in d["text"]
    ag["channel"] = FakeChannel()
    d, _ = _ask(home.EXAMPLES[1])
    assert "4. Message you on Telegram when they are done." in d["text"]
    assert d["intent"]["away_hours"] == 3                       # read, not scheduled


def test_plan_says_when_it_is_capped(ag, monkeypatch):
    many = [_job(f"gh:{i}", "Analyst", f"Co{i}", f"https://boards.greenhouse.io/co{i}/jobs/{i}")
            for i in range(12)]
    meta = {"titles": [], "locations": [], "sponsor_only": False, "search": None,
            "board": 12, "saved": 0}
    monkeypatch.setattr(app, "_aa_candidates", lambda focus: ([dict(j) for j in many], meta))
    d, _ = _ask("tailor 25 analyst roles for me")              # the route's cap per run
    assert "Auto-apply does up to 10 a run, so I will do 10. This uses 10 of your 20 packages." in d["text"]
    assert "Find the 10 best analyst roles" in d["text"]
    ag["left"] = 2
    d, _ = _ask("find 5 analyst roles and apply")
    assert "Your pass has 2 packages left, so I will do 2. This uses 2 of your 2 packages." in d["text"]
    ag["left"] = None                                            # own key: never counted
    d, _ = _ask("find 3 analyst roles and apply")
    assert d["text"].endswith("This uses 3 packages. Go ahead?")


def test_plan_when_no_role_matches(ag, monkeypatch):
    meta = {"titles": [], "locations": [], "sponsor_only": False, "search": None,
            "board": 4, "saved": 0}
    monkeypatch.setattr(app, "_aa_candidates", lambda focus: ([], meta))
    d, _ = _ask("find 5 underwater basket weaving roles in Lagos and apply")
    assert d["text"] == ("No underwater basket weaving roles in Lagos match right now. Try "
                         "broader titles, another location, or drop the sponsors-only filter.")
    assert d["buttons"] == [] and app._AGENT["plan"] is None


def test_plan_needs_a_profile_first(ag, monkeypatch):
    monkeypatch.setattr(app, "_aa_has_profile", lambda: False)
    d, _ = _ask(home.EXAMPLES[0])
    assert d["text"].startswith("Add your resume first") and d["buttons"][0]["id"] == "add_resume"


# --- go ahead and change ----------------------------------------------------------------------

def test_go_ahead_runs_once_with_the_parsed_focus_and_count(ag):
    _ask("find 2 analyst roles in Austin, sponsors only, and apply for me")
    c = app.app.test_client()
    r = c.post("/api/agent/go", json={})
    d = r.get_json()
    assert r.status_code == 200
    assert ag["runs"] == [{"count": 2, "min_fit": 60,
                           "focus": {"titles": ["analyst"], "locations": ["Austin"],
                                     "sponsor_only": True}}]
    assert len(ag["tailored"]) == 2
    assert d["text"] == "Done. Tailored 2. 0 sent for you. 2 ready for your click in Auto-apply."
    assert d["buttons"] == [{"id": "open_autoapply", "label": "Open Auto-apply"}]
    assert d["telegram"] is False
    # The plan was consumed: a second Go ahead has nothing to run.
    r = c.post("/api/agent/go", json={})
    assert r.status_code == 400 and "no plan" in r.get_json()["error"]


def test_go_ahead_sends_only_the_verified_site_and_only_when_autonomous_on(ag, monkeypatch):
    import submit
    posted = []

    def fake_submit(data, driver=None, now=None):
        posted.append((data.get("source_job") or {}).get("url"))
        return {"status": "auto_submitted", "tier": "auto", "message": "sent",
                "submitted_at": now, "posted": True}
    monkeypatch.setattr(submit, "submit_record", fake_submit)
    c = app.app.test_client()
    _ask("find 4 analyst roles and apply for me")
    d = c.post("/api/agent/go", json={}).get_json()
    assert posted == [] and d["sent"] == 0                     # autonomous off: never sent
    recs = CVRecords(app.DB_PATH)
    try:
        for r in recs.list():
            recs.delete(r["id"])
    finally:
        recs.close()
    app._set_autonomous(True)
    _ask("find 4 analyst roles and apply for me")
    d = c.post("/api/agent/go", json={}).get_json()
    assert posted == [RT] and d["sent"] == 1
    assert d["text"] == "Done. Tailored 4. 1 sent for you. 3 ready for your click in Auto-apply."


def test_go_ahead_messages_telegram_with_the_batch_buttons(ag):
    ch = FakeChannel()
    ag["channel"] = ch
    _ask("tailor 2 analyst roles and message me on Telegram")
    c = app.app.test_client()
    d = c.post("/api/agent/go", json={}).get_json()
    assert d["telegram"] is True and len(ch.sent) == 1
    m = ch.sent[0]
    assert m["text"] == ("Done. Tailored 2 roles from Home (Acme, Beta). 2 are ready and waiting "
                         "for your click (Acme, Beta).")
    assert [b["label"] for b in m["buttons"][0]] == ["Approve all", "Review", "Skip"]
    bid = m["buttons"][0][0]["id"].split(":")[-1]
    batch = (app._notify_state().get("autopilot") or {}).get("batches", {}).get(bid)
    assert batch and len(batch["ids"]) == 2
    # The phone's Skip works on that batch, exactly as after an Autopilot run.
    assert app._autopilot_act(f"ap:skip:{bid}")["message"].startswith("Skipped 2")
    # Not asked for: no message, even with a channel.
    _ask("tailor 1 analyst role for me")
    c.post("/api/agent/go", json={})
    assert len(ch.sent) == 1


def test_go_ahead_waits_when_a_run_holds_the_lock(ag):
    _ask(home.EXAMPLES[0])
    c = app.app.test_client()
    assert app._AA_RUN_LOCK.acquire(blocking=False)
    try:
        r = c.post("/api/agent/go", json={})
    finally:
        app._AA_RUN_LOCK.release()
    assert r.status_code == 409 and r.get_json()["busy"] is True
    assert ag["runs"] == [] and app._AGENT["plan"] is not None   # still there for a retry


def test_change_merges_into_the_last_plan(ag):
    _ask("find 5 data analytics roles and apply for me")
    d, _ = _ask("make it 3 business analyst roles in Austin", change=True)
    assert "Find the 3 best business analyst roles" in d["text"]
    assert app._AGENT["plan"]["focus"] == {"titles": ["business analyst"], "locations": ["Austin"],
                                           "sponsor_only": False}
    d, _ = _ask("only 2", change=True)                          # titles and place are kept
    assert "Find the 2 best business analyst roles" in d["text"]
    assert app._AGENT["plan"]["focus"]["locations"] == ["Austin"]


# --- status, tailor_one, other ----------------------------------------------------------------

def test_status_counts_the_tiles_and_names_the_newest(ag):
    d, _ = _ask(home.EXAMPLES[2])
    assert d["text"] == ("To review: 0. Waiting on you: 0. Needs you: 0. Submitted today: 0. "
                         "Nothing is waiting on you.")
    recs = CVRecords(app.DB_PATH)
    try:
        recs.add("Data Analyst", "Acme", 70.0, "", data={"source_job": {"url": GH}})
        recs.add("Business Analyst", "Beta", 70.0, "",
                 data={"status": "approved", "source_job": {"url": LV}})
        recs.add("Analyst", "Gamma", 70.0, "",
                 data={"status": "approved", "submission": {"status": "failed",
                                                             "reason": "needs_signin"}})
        recs.add("Old", "Omega", 70.0, "", data={"status": "applied", "applied_at": "2026-01-01T09:00:00"})
        recs.add("New", "Zeta", 70.0, "", data={"status": "applied", "applied_at": DAY1 + "T09:00:00"})
    finally:
        recs.close()
    d, _ = _ask("what is waiting on me?")
    assert d["text"].startswith("To review: 1. Waiting on you: 1. Needs you: 1. Submitted today: 1. "
                                "Newest: ")
    assert "Data Analyst at Acme" in d["text"] and "Analyst at Gamma" in d["text"]
    assert "Zeta" not in d["text"]                              # submitted ones are counted, not listed
    assert d["buttons"] == [{"id": "open_autoapply", "label": "Open Auto-apply"}]
    assert d["counts"] == {"review": 1, "waiting": 1, "needs": 1, "submitted_today": 1}


def test_tailor_one_asks_for_the_posting_and_offers_a_search(ag):
    d, _ = _ask("tailor my resume for a data analyst role at Stripe")
    assert d["text"] == ("To tailor your resume for data analyst I need the job description or a "
                         "link to it. Paste it here, or I can find matching roles for you instead.")
    assert d["buttons"] == [{"id": "find", "label": "Find matching roles"}]
    assert ag["runs"] == [] and app._AGENT["plan"] is None


def test_other_says_what_it_can_do(ag):
    d, _ = _ask("tell me a joke")
    assert d["intent"]["intent"] == "other" and d["buttons"] == []
    for ex in home.EXAMPLES:
        assert ex in d["text"]


# --- the words and the page -------------------------------------------------------------------

def test_texts_have_no_dashes_and_say_resume():
    texts = [home.other_text(), home.change_text(), home.tailor_one_text({"titles": ["x"]}),
             home.result_text(3, 1), home.no_match_text(["a"], "b"),
             home.status_text({"review": 1}, ["x"]),
             home.plan_text(n=3, titles=["a"], min_fit=60, auto=1, auto_companies="Gamma",
                            auto_reason="", telegram=True, telegram_connected=False,
                            packages_left=None, note="n."),
             home.batch_message([{"company": "A"}], [], lambda xs: "A")]
    for t in texts:
        assert "—" not in t and "–" not in t and " - " not in t, t
        assert "CV" not in t, t


def test_ui_source_has_the_box_the_pills_and_the_strings():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
    assert 'placeholder="Paste a job description, or tell me what to do..."' in html
    assert html.count('class="home-pill"') == 3
    for ex in home.EXAMPLES:
        assert ex in html
    assert 'data-i18n="home.try"' in html and 'id="homeTry"' in html
    assert "border-radius: 999px" in css and ".home-pill" in css
    assert "/api/agent/ask" in js and "/api/agent/go" in js
    assert "Your assistant. Ask for a search, a resume, or an update." in js
    assert "function openAgent" in js and "setAgentMode(false)" in js
    for p in sorted((ROOT / "ui" / "static" / "locales").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        for k in ("home.try", "home.try1", "home.try2", "home.try3", "agent.sub", "agent.go",
                  "agent.change", "agent.open", "agent.changeQ"):
            assert d.get(k), f"{p.name} lacks {k}"
    assert d.get("home.placeholder") != "Paste the job description here…"
    assert "### The Home agent" in (ROOT / "docs" / "notify.md").read_text(encoding="utf-8")


# --- missing HARD details: the questions, not a plan ------------------------------------------

def test_plan_asks_for_missing_hard_details_and_soft_gaps_never_stop_it(ag, monkeypatch):
    real = app._aa_missing_details
    monkeypatch.setattr(app, "_aa_missing_details",
                        lambda: ["the employment dates for your role at Acme"])
    d = app._agent_plan({"titles": ["data analyst"], "count": 2})
    assert d["text"] == ("Before I can tailor, your profile needs 1 detail:\n"
                         "- the employment dates for your role at Acme")
    assert d["buttons"] == [{"id": "add_details", "label": "Add the details"}]
    assert app._AGENT["plan"] is None and ag["tailored"] == []           # no plan, nothing spent
    # A soft gap (an undated project) is not in the real HARD pre-flight: the plan goes ahead.
    monkeypatch.setattr(app, "_aa_missing_details", real)
    app._memory().save("default", {
        "identity": {"name": "Sam", "email": "s@example.com"},
        "experience": [{"org": "Acme", "location": "Austin, TX",
                        "roles": [{"title": "Analyst", "dates": "2021 - 2024", "bullets": ["x"]}]}],
        "projects": [{"org": "Ledger", "bullets": ["Undated, no location"]}]}, {}, [])
    assert app._aa_missing_details() == []
    assert [b["id"] for b in app._agent_plan({"titles": ["data analyst"], "count": 2})["buttons"]] \
        == ["go", "change"]
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id === "add_details"' in js and "openProfileDetails();" in js
