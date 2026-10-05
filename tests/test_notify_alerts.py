"""Telegram channels, job-match alerts, honest buttons, the away digest and the tick.

Fully offline: the Bot API transport and the broker are fakes, state lives in tmp_path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from notify.alerts import (AlertEngine, NotifyState, alert_buttons, alert_text, away_activity,
                           digest_due, digest_text, in_quiet_hours, match_percent,
                           profile_targets, short_id)
from notify.channel import (ChannelError, OfficialBotChannel, OwnBotChannel, TextOnlyBot,
                            parse_update, select_channel)
from notify.hub import NotifyHub
from notify.telegram import TelegramBot

NOON = datetime(2026, 10, 2, 12, 0).astimezone()          # local noon: outside quiet hours


# ------------------------------------------------------------------ fakes
class FakeTG:
    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.calls = []

    def __call__(self, url, payload=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"ok": True, "result": out}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        return {"ok": True, "result": True}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


class FakeBroker:
    """The broker contract: /notify/telegram/{status,link,unlink,send,inbox}."""

    def __init__(self, linked=True, configured=True):
        self.linked, self.configured = linked, configured
        self.events, self.sent, self.next_cursor = [], [], 1
        self.send_status = 200

    def push(self, type_, data="", text=""):
        self.events.append({"cursor": self.next_cursor, "type": type_, "data": data,
                            "text": text, "at": "2026-10-02T12:00:00Z"})
        self.next_cursor += 1

    def get(self, path):
        if not self.configured:
            return 503, {"error": "telegram_not_configured"}
        if path.startswith("/notify/telegram/status"):
            return 200, {"linked": self.linked, "username": "kofi", "paused": False}
        if path.startswith("/notify/telegram/inbox"):
            after = 0
            if "after=" in path:
                after = int(path.split("after=")[1])
            evs = [e for e in self.events if e["cursor"] > after]
            return 200, {"events": evs, "cursor": evs[-1]["cursor"] if evs else after}
        return 404, None

    def post(self, path, body):
        if not self.configured:
            return 503, {"error": "telegram_not_configured"}
        if path == "/notify/telegram/send":
            if self.send_status != 200:
                err = {409: "not_linked", 429: "rate_limited", 502: "telegram_unavailable"}
                return self.send_status, {"error": err.get(self.send_status, "bad_request"),
                                          "retry_after": 7}
            self.sent.append(body)
            return 200, {"ok": True, "message_id": len(self.sent)}
        if path == "/notify/telegram/link":
            return 200, {"code": "abc", "url": "https://t.me/SponsorJobsBot?start=abc", "expires_in": 600}
        if path == "/notify/telegram/unlink":
            self.linked = False
            return 200, {"ok": True}
        return 404, None


def _official(broker, store, on_unlinked=None):
    return OfficialBotChannel(broker.get, broker.post, lambda: store.get("c", ""),
                              lambda c: store.__setitem__("c", c), on_unlinked=on_unlinked)


def _own(tg, chat="99", store=None):
    store = store if store is not None else {}
    return OwnBotChannel(TelegramBot("t", chat_id=chat, http=tg),
                         lambda: store.get("o", 0), lambda o: store.__setitem__("o", o))


def _row(sid, title="Business Analyst", company="JP Morgan", days_ago=0, visa=True, **kw):
    seen = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    r = {"source_id": sid, "title": title, "company": company, "location": "New York, NY",
         "url": f"https://boards.greenhouse.io/x/{sid}", "first_seen": seen, "us": True,
         "visa": [{"code": "H-1B", "label": "H-1B sponsor"}] if visa else []}
    r.update(kw)
    return r


TARGETS = profile_targets({"experience": [{"org": "Acme", "roles": [{"title": "Business Analyst"}]}],
                           "skills": {"Tools": "SQL, Excel, Tableau"}})


# ------------------------------------------------------------------ channel selection
def test_select_prefers_official_when_linked():
    off, own = object(), object()
    assert select_channel(lambda: (True, off), lambda: own) is off
    assert select_channel(lambda: (False, None), lambda: own) is own
    assert select_channel(lambda: (False, None), lambda: None) is None


def test_select_falls_back_when_broker_raises():
    own = object()

    def boom():
        raise ChannelError("unreachable")
    assert select_channel(boom, lambda: own) is own


# ------------------------------------------------------------------ own bot channel
def test_own_bot_sends_inline_keyboard():
    tg = FakeTG()
    _own(tg).send("hi", [[{"id": "job:skip:abc", "label": "Skip"}]])
    msg = tg.sent()[0]
    assert msg["reply_markup"] == {"inline_keyboard": [[{"text": "Skip", "callback_data": "job:skip:abc"}]]}


def test_own_bot_parses_callbacks_owner_only_and_persists_offset():
    store = {}
    tg = FakeTG(updates=[
        {"update_id": 10, "callback_query": {"id": "cb1", "data": "job:skip:abc",
                                             "message": {"chat": {"id": 99}}}},
        {"update_id": 11, "callback_query": {"id": "cb2", "data": "job:skip:abc",
                                             "message": {"chat": {"id": 777}}}},   # stranger
        {"update_id": 12, "message": {"text": "/list", "chat": {"id": 99}}},
        {"update_id": 13, "message": {"text": "hello", "chat": {"id": 99}}},
    ])
    ch = _own(tg, store=store)
    evs = ch.poll()
    assert [e["type"] for e in evs] == ["button", "command", "text"]
    assert evs[0]["data"] == "job:skip:abc" and evs[0]["ack"] == "cb1"
    assert store["o"] == 14
    ch.ack(evs[0], "Skipped")
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Skipped"}) in tg.calls


def test_own_bot_fails_closed_without_owner():
    tg = FakeTG(updates=[{"update_id": 1, "message": {"text": "/list", "chat": {"id": 5}}}])
    ch = _own(tg, chat="")
    with pytest.raises(ChannelError) as e:
        ch.poll()
    assert e.value.code == "no_owner"
    assert not tg.calls                                   # never even fetched
    assert parse_update({"update_id": 1, "message": {"text": "x", "chat": {"id": 5}}}, "") is None


# ------------------------------------------------------------------ official channel
def test_official_send_and_inbox_cursor():
    b, store = FakeBroker(), {}
    ch = _official(b, store)
    ch.send("hello", [[("Skip", "job:skip:1")]])
    assert b.sent == [{"text": "hello", "buttons": [[{"id": "job:skip:1", "label": "Skip"}]]}]
    b.push("button", data="job:skip:1")
    b.push("command", data="approve", text="12")
    b.push("command", data="stop")
    b.push("text", text="how is it going?")
    evs = ch.poll()
    assert [e["text"] for e in evs[1:]] == ["/approve 12", "/stop", "how is it going?"]
    assert evs[0]["type"] == "button" and evs[0]["data"] == "job:skip:1"
    assert store["c"] == "4"
    assert ch.poll() == []                                 # cursor echo: nothing new
    assert store["c"] == "4"


def test_official_error_codes_and_unlink_callback():
    b, store, flags = FakeBroker(), {}, []
    ch = _official(b, store, on_unlinked=lambda: flags.append("unlinked"))
    b.send_status = 409
    with pytest.raises(ChannelError) as e:
        ch.send("x")
    assert e.value.code == "not_linked" and flags == ["unlinked"]
    b.send_status = 429
    with pytest.raises(ChannelError) as e:
        ch.send("x")
    assert e.value.code == "rate_limited" and e.value.retry_after == 7
    b.send_status = 502
    with pytest.raises(ChannelError) as e:
        ch.send("x")
    assert e.value.code == "telegram_unavailable"
    with pytest.raises(ChannelError) as e:
        _official(FakeBroker(configured=False), {}).link()
    assert e.value.code == "telegram_not_configured"


def test_text_only_bot_turns_photos_into_text():
    b = FakeBroker()
    bot = TextOnlyBot(_official(b, {}))
    bot.send_photo("/x.png", caption="SWE at Acme", buttons=[[("Approve", "approve:1")]])
    bot.send_document("/x.pdf", caption="the pdf")       # no buttons: nothing to say
    assert len(b.sent) == 1 and b.sent[0]["text"] == "SWE at Acme"


# ------------------------------------------------------------------ alert picking
def _engine(tmp_path, scores=None, **prefs):
    st = NotifyState(tmp_path / "s.json")
    if prefs:
        st.set_prefs(prefs)
    scores = scores or {}
    return st, AlertEngine(st, match_fn=lambda r: scores.get(r["source_id"], 80))


def test_pick_threshold_sponsor_and_newness(tmp_path):
    st, eng = _engine(tmp_path, scores={"a": 82, "b": 60})
    rows = [_row("a"), _row("b"), _row("c", visa=False),            # c: no sponsor signal
            _row("d", days_ago=10), _row("e", title="Pastry Chef")]  # d: old; e: no title fit
    picked = eng.pick(rows, TARGETS, NOON)
    assert [p["sid"] for p in picked] == ["a"]
    assert picked[0]["score"] == 82 and picked[0]["sponsor"] == "H-1B sponsor"


def test_cap_quiet_hours_and_no_repeats(tmp_path):
    st, eng = _engine(tmp_path, daily_cap=1)
    eng.pick([_row("a"), _row("b", company="Citi")], TARGETS, NOON)
    tg = FakeTG()
    ch = _own(tg)
    night = datetime(2026, 10, 2, 23, 30).astimezone()
    assert eng.send_due(ch, night, lambda a: False) == []           # quiet hours: queued
    assert len(st.load()["queue"]) == 2
    sent = eng.send_due(ch, NOON, lambda a: False)
    assert len(sent) == 1 and len(tg.sent()) == 1                     # cap of one a day
    assert eng.send_due(ch, NOON, lambda a: False) == []
    tomorrow = NOON + timedelta(days=1)
    assert len(eng.send_due(ch, tomorrow, lambda a: False)) == 1      # the other one, next day
    # never the same job twice, even if the feed shows it again
    assert eng.pick([_row("a"), _row("b", company="Citi")], TARGETS, tomorrow) == []


def test_dismissed_excluded(tmp_path):
    st, eng = _engine(tmp_path)
    st.set("dismissed", ["a"])
    assert eng.pick([_row("a")], TARGETS, NOON) == []


def test_quiet_hours_wrap():
    assert in_quiet_hours(23, 22, 8) and in_quiet_hours(3, 22, 8)
    assert not in_quiet_hours(8, 22, 8) and not in_quiet_hours(12, 22, 8)
    assert in_quiet_hours(13, 12, 14) and not in_quiet_hours(5, 5, 5)


def test_match_percent_weights_must_haves():
    assert match_percent(None) is None
    assert match_percent({"ratio": 0.5, "required_total": 4, "required_covered": 4}) == 80


# ------------------------------------------------------------------ honest buttons + text
def test_message_template_assisted():
    a = {"short": "s1", "company": "JP Morgan", "title": "Business Analyst",
         "location": "New York", "score": 82, "sponsor": "H-1B sponsor"}
    assert alert_text(a, False) == (
        "JP Morgan is hiring a Business Analyst in New York. Good fit for your profile "
        "(82% match, H-1B sponsor). Want me to tailor your résumé and queue it? "
        "I'll prepare it; you click submit.")
    labels = [b["label"] for row in alert_buttons(a, False) for b in row]
    assert labels == ["Tailor and queue", "Skip", "Fewer like this"]
    ids = [b["id"] for row in alert_buttons(a, False) for b in row]
    assert ids == ["job:queue:s1", "job:skip:s1", "job:fewer:s1"]


def test_message_template_auto():
    a = {"short": "s1", "company": "Acme", "title": "Engineer", "location": "", "score": 90,
         "sponsor": ""}
    assert alert_text(a, True) == (
        "Acme is hiring an Engineer. Strong fit for your profile (90% match). This site accepts "
        "applications from SponsorJobs. Want me to tailor your résumé and apply for you?")
    assert alert_buttons(a, True)[0][0] == {"id": "job:apply:s1", "label": "Apply for me"}


def test_can_auto_apply_follows_allowlist_and_setting(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    recruitee = {"url": "https://acme.recruitee.com/o/engineer"}
    greenhouse = {"url": "https://boards.greenhouse.io/acme/jobs/1"}
    linkedin = {"url": "https://www.linkedin.com/jobs/view/1"}
    assert not app._can_auto_apply(recruitee)                  # autonomous OFF by default
    app._set_autonomous(True)
    assert app._can_auto_apply(recruitee)
    assert not app._can_auto_apply(greenhouse) and not app._can_auto_apply(linkedin)


def test_button_ids_never_carry_source_id(tmp_path):
    st, eng = _engine(tmp_path)
    eng.pick([_row("greenhouse:jpmorgan:12345")], TARGETS, NOON)
    b = FakeBroker()
    eng.send_due(_official(b, {}), NOON, lambda a: False)
    blob = json.dumps(b.sent)
    assert "greenhouse:jpmorgan:12345" not in blob and short_id("greenhouse:jpmorgan:12345") in blob


# ------------------------------------------------------------------ dispatch
class Acts:
    def __init__(self, auto=False):
        self.calls, self.auto = [], auto

    def queue(self, job):
        self.calls.append(("queue", job["sid"]))
        return "Ready: queued."

    def apply(self, job):
        self.calls.append(("apply", job["sid"]))
        return "Applied."

    def dismiss(self, sid):
        self.calls.append(("dismiss", sid))

    def autonomous_ok(self, job):
        return self.auto


def _sent_alert(tmp_path, sid="a", **row):
    st, eng = _engine(tmp_path)
    eng.pick([_row(sid, **row)], TARGETS, NOON)
    eng.send_due(_own(FakeTG()), NOON, lambda a: False)
    return st, eng, short_id(sid)


def test_dispatch_queue_skip_fewer(tmp_path):
    st, eng, s = _sent_alert(tmp_path)
    acts = Acts()
    assert eng.handle_button(f"job:queue:{s}", acts) == "Ready: queued."
    assert acts.calls == [("queue", "a")]
    assert "Skipped" in eng.handle_button(f"job:skip:{s}", acts)
    assert ("dismiss", "a") in acts.calls and "a" in st.load()["dismissed"]
    assert "Fewer" in eng.handle_button(f"job:fewer:{s}", acts)
    fewer = st.load()["fewer"]
    assert fewer["company"]["jp morgan"] == 1 and fewer["title"]["analyst"] == 1
    assert "expired" in eng.handle_button("job:skip:nope", acts)


def test_fewer_like_this_lowers_future_scores(tmp_path):
    st, eng, s = _sent_alert(tmp_path)
    eng.handle_button(f"job:fewer:{s}", Acts())
    # same company + title words: 80 - (15 + 8 + 8) = 49, under the 70 threshold
    assert eng.pick([_row("z")], TARGETS, NOON) == []
    assert st.load()["considered"]["z"] < 70


def test_apply_button_rechecks_autonomous(tmp_path):
    st, eng, s = _sent_alert(tmp_path)
    acts = Acts(auto=False)
    out = eng.handle_button(f"job:apply:{s}", acts)
    assert acts.calls == [("queue", "a")] and "you click submit" in out
    acts2 = Acts(auto=True)
    assert eng.handle_button(f"job:apply:{s}", acts2) == "Applied."


# ------------------------------------------------------------------ digest
def _rec(created, status="ready", sub=None, url="https://boards.greenhouse.io/x/1"):
    return {"created_at": created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "data": {"status": status, "submission": sub or {}, "source_job": {"url": url}}}


def test_away_digest_text_and_timing():
    since = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    t = since + timedelta(minutes=30)
    recs = [_rec(t), _rec(t), _rec(t, status="applied", url="https://acme.recruitee.com/o/1",
                                   sub={"status": "auto_submitted", "at": t.isoformat()})]
    act = away_activity(recs, since, lambda u: "Recruitee" if "recruitee" in u else "")
    assert digest_text(act) == ("While you were away: tailored 3, submitted 1 (Recruitee), "
                                "2 waiting for your click. Open SponsorJobs and go to the review "
                                "queue to finish them.")
    assert digest_due(act, None, t + timedelta(minutes=2)) == "wait"
    assert digest_due(act, None, t + timedelta(minutes=20)) == "send"
    assert digest_due(act, t + timedelta(minutes=5), t + timedelta(minutes=20)) == "reset"
    assert digest_due(away_activity([], since), None, t) == "none"


def test_hub_sends_digest_once(tmp_path):
    st = NotifyState(tmp_path / "s.json")
    hub = NotifyHub(st, AlertEngine(st), commands=None)
    b = FakeBroker()
    ch = _official(b, {})
    now = NOON                                               # local noon: not quiet hours
    assert hub.send_digest(ch, [], now) == "none"           # first run: no history digest
    created = now + timedelta(minutes=1)
    later = now + timedelta(minutes=30)
    assert hub.send_digest(ch, [_rec(created)], later) == "sent"
    assert b.sent[-1]["text"].startswith("While you were away: tailored 1")
    assert hub.send_digest(ch, [_rec(created)], later + timedelta(minutes=1)) == "none"


# ------------------------------------------------------------------ tick + restart
class Cmds:
    def __init__(self):
        self.seen = []

    def handle_command(self, text):
        self.seen.append(text)
        return f"echo {text}"


def test_events_handled_after_restart_once(tmp_path):
    """Taps queued while the app was closed are handled by the first tick after start, once."""
    st = NotifyState(tmp_path / "s.json")
    eng = AlertEngine(st)
    eng.pick([_row("a")], TARGETS, NOON)
    b = FakeBroker()
    cursor = {"get": lambda: st.get("cursor", ""), "set": lambda c: st.set("cursor", c)}

    def channel():
        return OfficialBotChannel(b.get, b.post, cursor["get"], cursor["set"])
    eng.send_due(channel(), NOON, lambda a: False)
    # app closed: the person taps Skip and sends /list
    b.push("button", data=f"job:skip:{short_id('a')}")
    b.push("command", data="list")
    # app restarts: a brand-new hub over the same state file
    cmds, acts = Cmds(), Acts()
    hub = NotifyHub(NotifyState(tmp_path / "s.json"), AlertEngine(NotifyState(tmp_path / "s.json")),
                    cmds, job_actions=acts)
    out = hub.tick(channel(), NOON, lambda a: False)
    assert out["handled"] == 2 and cmds.seen == ["/list"] and ("dismiss", "a") in acts.calls
    again = hub.tick(channel(), NOON, lambda a: False)
    assert again["handled"] == 0                                 # cursor persisted: not twice


def test_tick_routes_batch_buttons(tmp_path):
    st = NotifyState(tmp_path / "s.json")
    got = []
    hub = NotifyHub(st, AlertEngine(st), Cmds(),
                    batch_callback=lambda d: got.append(d) or {"message": "Approved"})
    b = FakeBroker()
    b.push("button", data="approve:7")
    hub.tick(_official(b, {}), NOON, lambda a: False)
    assert got == ["approve:7"] and b.sent[-1]["text"] == "Approved"


# ------------------------------------------------------------------ app wiring
def _app(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "_TG_OFFSET_FILE", tmp_path / "off")
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    return app


def test_app_poll_uses_official_channel_and_queue_path(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    b = FakeBroker()
    st = app._notify_state()
    eng = AlertEngine(st)
    eng.pick([_row("gh:1")], TARGETS, NOON)
    off = OfficialBotChannel(b.get, b.post, lambda: st.get("official_cursor", ""),
                             lambda c: st.set("official_cursor", c))
    eng.send_due(off, NOON, lambda a: False)
    monkeypatch.setattr(app, "_official_status", lambda force=False: (True, off))
    tailored = []

    def fake_tailor(job, llm, tex, tname, scratch):
        tailored.append(job.get("source_id"))
        return {"ok": True, "id": 41, "role": job.get("title"), "company": job.get("company"),
                "coverage": 77.0}
    monkeypatch.setattr(app, "_tailor_job_unattended", fake_tailor)
    monkeypatch.setattr(app, "_make_llm", lambda: object())
    monkeypatch.setattr(app._memory().__class__, "load",
                        lambda self, name: {"profile": {"experience": [{"org": "A"}]}})
    b.push("button", data=f"job:queue:{short_id('gh:1')}")
    b.push("command", data="list")
    d = app.app.test_client().post("/api/notify/poll").get_json()
    assert d["ok"] is True and d["channel"] == "official" and d["handled"] == 2
    assert tailored == ["gh:1"]
    texts = [m["text"] for m in b.sent]
    assert any(t.startswith("Ready: Business Analyst at JP Morgan is in your review queue (#41, 77% JD match)")
               for t in texts)
    assert any("caught up" in t or "waiting" in t for t in texts)      # /list answered


def test_app_settings_roundtrip_and_link_fallback(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = app.app.test_client()
    d = c.get("/api/notify/settings").get_json()
    assert d["prefs"]["daily_cap"] == 3 and d["prefs"]["quiet_start"] == 22
    assert d["channel"] in ("none", "own")
    d = c.post("/api/notify/settings", json={"daily_cap": 5, "job_alerts": False,
                                             "quiet_start": 23}).get_json()
    assert d["prefs"]["daily_cap"] == 5 and d["prefs"]["job_alerts"] is False
    assert c.post("/api/notify/settings", json={"daily_cap": 4}).get_json()["prefs"]["daily_cap"] == 3
    broker = FakeBroker(configured=False)
    monkeypatch.setattr(app, "_broker_get", broker.get)
    monkeypatch.setattr(app, "_broker_post", broker.post)
    r = c.post("/api/notify/telegram/link", json={}).get_json()
    assert r["ok"] is False and r["error"] == "telegram_not_configured"   # UI shows own-bot steps
    ok = FakeBroker()
    monkeypatch.setattr(app, "_broker_post", ok.post)
    r = c.post("/api/notify/telegram/link", json={}).get_json()
    assert r["ok"] is True and r["url"].startswith("https://t.me/")


def test_presence_endpoint(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    assert app.app.test_client().post("/api/notify/presence").get_json()["ok"] is True
    assert app._notify_state().get("presence_at") > 0
