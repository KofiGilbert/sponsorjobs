"""The official SponsorJobs Telegram bot relay (backend/telegram_relay.py, docs/notify.md).

Offline: Telegram's HTTP API is a fake transport that records calls; the clock is injected.
"""

from __future__ import annotations

import pytest

from backend.accounts import InMemoryAccountStore
from backend.broker import create_app
from backend.metering import InMemoryStore, Meter
from backend.store_sqlite import SqliteTelegramStore
from backend.telegram_relay import (EVENT_TTL, LINK_TTL, MSG_CONNECTED, MSG_DISCONNECTED,
                                    MSG_EXPIRED, MSG_UNBOUND, InMemoryTelegramStore,
                                    TelegramError, TelegramRelay)

SECRET = "s3cret-webhook"
WH = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.fail = None
        self._mid = 100

    def __call__(self, method, payload):
        if self.fail and method == "sendMessage":
            raise self.fail
        self.calls.append((method, payload))
        if method == "sendMessage":
            self._mid += 1
            return {"message_id": self._mid}
        return {}

    def texts(self, chat_id=None):
        return [p["text"] for m, p in self.calls
                if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _app(store=None, configured=True):
    tg, clock = FakeTelegram(), Clock()
    relay = (TelegramRelay(store or InMemoryTelegramStore(), tg, bot_username="SponsorJobsBot",
                           webhook_secret=SECRET, now=clock) if configured else None)
    app = create_app(meter=Meter(InMemoryStore()), period_fn=lambda: "2026-10",
                     accounts=InMemoryAccountStore(), telegram=relay)
    app.config.update(TESTING=True)
    return app.test_client(), tg, clock


def _account(c):
    tok = c.post("/account/register").get_json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def _msg(chat_id, text, username="kofi", update_id=1):
    return {"update_id": update_id, "message": {
        "message_id": 1, "text": text, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "username": username}}}


def _tap(chat_id, data, cq_id="cq1"):
    return {"update_id": 2, "callback_query": {
        "id": cq_id, "data": data, "from": {"id": chat_id},
        "message": {"message_id": 5, "chat": {"id": chat_id, "type": "private"}}}}


def _linked(c, chat_id=555):
    H = _account(c)
    code = c.post("/notify/telegram/link", headers=H).get_json()["code"]
    assert c.post("/telegram/webhook", json=_msg(chat_id, f"/start {code}"), headers=WH).status_code == 200
    return H


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    return InMemoryTelegramStore() if request.param == "memory" else SqliteTelegramStore(str(tmp_path / "b.db"))


def test_unconfigured_routes_return_503():
    c, _, _ = _app(configured=False)
    H = _account(c)
    for method, path in [("post", "/notify/telegram/link"), ("get", "/notify/telegram/status"),
                         ("post", "/notify/telegram/unlink"), ("post", "/notify/telegram/send"),
                         ("get", "/notify/telegram/inbox"), ("post", "/telegram/webhook")]:
        r = getattr(c, method)(path, headers=H, json={})
        assert r.status_code == 503 and r.get_json()["error"] == "telegram_not_configured", path


def test_routes_need_an_account():
    c, _, _ = _app()
    assert c.post("/notify/telegram/link").status_code == 401
    assert c.get("/notify/telegram/inbox").status_code == 401


def test_link_code_flow_binds_the_chat(store):
    c, tg, _ = _app(store)
    H = _account(c)
    link = c.post("/notify/telegram/link", headers=H).get_json()
    assert link["expires_in"] == 900 == LINK_TTL
    assert link["url"] == f"https://t.me/SponsorJobsBot?start={link['code']}"
    assert c.get("/notify/telegram/status", headers=H).get_json() == {"linked": False}
    c.post("/telegram/webhook", json=_msg(555, f"/start {link['code']}"), headers=WH)
    st = c.get("/notify/telegram/status", headers=H).get_json()
    assert st["linked"] is True and st["username"] == "kofi" and st["linked_at"].endswith("Z")
    assert tg.texts(555) == [MSG_CONNECTED]
    # one-time: the same code from another chat is refused
    c.post("/telegram/webhook", json=_msg(777, f"/start {link['code']}"), headers=WH)
    assert tg.texts(777) == [MSG_EXPIRED]


def test_expired_and_unknown_codes_get_the_polite_reply(store):
    c, tg, clock = _app(store)
    H = _account(c)
    code = c.post("/notify/telegram/link", headers=H).get_json()["code"]
    clock.t += LINK_TTL + 1
    c.post("/telegram/webhook", json=_msg(555, f"/start {code}"), headers=WH)
    c.post("/telegram/webhook", json=_msg(556, "/start nonsense"), headers=WH)
    assert tg.texts(555) == [MSG_EXPIRED] and tg.texts(556) == [MSG_EXPIRED]
    assert c.get("/notify/telegram/status", headers=H).get_json()["linked"] is False


def test_webhook_secret_is_checked():
    c, tg, _ = _app()
    assert c.post("/telegram/webhook", json=_msg(1, "hi")).status_code == 403
    assert c.post("/telegram/webhook", json=_msg(1, "hi"),
                  headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert tg.calls == []


def test_unbound_chat_is_told_how_to_connect_and_nothing_is_stored(store):
    c, tg, _ = _app(store)
    H = _linked(c, 555)
    for upd in (_msg(999, "hello"), _msg(999, "/stop"), _tap(999, "apply:1")):
        assert c.post("/telegram/webhook", json=upd, headers=WH).status_code == 200
    assert tg.texts(999) == [MSG_UNBOUND, MSG_UNBOUND]
    toast = [p for m, p in tg.calls if m == "answerCallbackQuery"]
    assert toast and toast[0]["text"] == MSG_UNBOUND
    assert c.get("/notify/telegram/inbox", headers=H).get_json()["events"] == []


def test_button_and_text_events_and_cursor_ack(store):
    c, tg, _ = _app(store)
    H = _linked(c)
    c.post("/telegram/webhook", json=_tap(555, "apply:job42"), headers=WH)
    c.post("/telegram/webhook", json=_msg(555, "yes please"), headers=WH)
    c.post("/telegram/webhook", json=_msg(555, "/jobs today"), headers=WH)
    assert ("answerCallbackQuery", {"callback_query_id": "cq1", "text": "Got it"}) in tg.calls
    r = c.get("/notify/telegram/inbox", headers=H).get_json()
    ev = r["events"]
    assert [(e["type"], e["data"], e["text"]) for e in ev] == [
        ("button", "apply:job42", ""), ("text", "", "yes please"), ("command", "jobs", "today")]
    assert r["cursor"] == ev[-1]["cursor"] and ev[0]["cursor"] < ev[1]["cursor"]
    # re-polling with an old cursor returns the rest; the later cursor acknowledges (deletes) them
    again = c.get(f"/notify/telegram/inbox?after={ev[0]['cursor']}", headers=H).get_json()
    assert [e["type"] for e in again["events"]] == ["text", "command"]
    done = c.get(f"/notify/telegram/inbox?after={r['cursor']}", headers=H).get_json()
    assert done == {"events": [], "cursor": r["cursor"]}
    assert c.get("/notify/telegram/inbox", headers=H).get_json()["events"] == []   # gone for good
    assert c.get("/notify/telegram/inbox?after=x", headers=H).status_code == 400


def test_inbox_caps_at_50_and_drops_events_older_than_7_days(store):
    c, _, clock = _app(store)
    H = _linked(c)
    c.post("/telegram/webhook", json=_msg(555, "old"), headers=WH)
    clock.t += EVENT_TTL + 1
    for i in range(60):
        c.post("/telegram/webhook", json=_msg(555, f"m{i}"), headers=WH)
    ev = c.get("/notify/telegram/inbox", headers=H).get_json()["events"]
    assert len(ev) == 50 and ev[0]["text"] == "m0"


def test_inbox_is_per_account(store):
    c, _, _ = _app(store)
    A = _linked(c, 1)
    B = _linked(c, 2)
    c.post("/telegram/webhook", json=_msg(1, "for A"), headers=WH)
    assert c.get("/notify/telegram/inbox", headers=B).get_json()["events"] == []
    assert [e["text"] for e in c.get("/notify/telegram/inbox", headers=A).get_json()["events"]] == ["for A"]


def test_send_builds_an_inline_keyboard(store):
    c, tg, _ = _app(store)
    H = _linked(c)
    r = c.post("/notify/telegram/send", headers=H, json={
        "text": "New job: Data Analyst at Acme",
        "buttons": [[{"id": "apply:42", "label": "Apply"}, {"id": "skip:42", "label": "Skip"}]]})
    assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["message_id"] > 0
    method, payload = tg.calls[-1]
    assert method == "sendMessage" and payload["chat_id"] == 555
    assert payload["reply_markup"] == {"inline_keyboard": [[
        {"text": "Apply", "callback_data": "apply:42"}, {"text": "Skip", "callback_data": "skip:42"}]]}


@pytest.mark.parametrize("body", [
    {}, {"text": ""}, {"text": "x" * 4097},
    {"text": "hi", "buttons": [[{"id": "a", "label": "A"}]] * 4},
    {"text": "hi", "buttons": [[{"id": "a", "label": "A"}] * 4]},
    {"text": "hi", "buttons": [[{"id": "bad id!", "label": "A"}]]},
    {"text": "hi", "buttons": [[{"id": "x" * 41, "label": "A"}]]},
    {"text": "hi", "buttons": [[{"id": "a", "label": "L" * 31}]]},
    {"text": "hi", "buttons": [[]]},
])
def test_send_validates_the_body(body):
    c, _, _ = _app()
    H = _linked(c)
    assert c.post("/notify/telegram/send", headers=H, json=body).status_code == 400


def test_send_when_not_linked_is_409():
    c, _, _ = _app()
    H = _account(c)
    r = c.post("/notify/telegram/send", headers=H, json={"text": "hi"})
    assert r.status_code == 409 and r.get_json()["error"] == "not_linked"


def test_send_rate_limits(store):
    c, _, clock = _app(store)
    H = _linked(c)
    send = lambda: c.post("/notify/telegram/send", headers=H, json={"text": "hi"})  # noqa: E731
    assert send().status_code == 200
    clock.t += 1
    r = send()
    assert r.status_code == 429 and r.get_json()["retry_after"] == 2 and r.headers["Retry-After"] == "2"
    from backend.telegram_relay import SEND_PER_DAY
    for _ in range(SEND_PER_DAY - 1):
        clock.t += 3
        assert send().status_code == 200
    clock.t += 3
    r = send()
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited" and r.get_json()["retry_after"] > 3
    clock.t += 86400
    assert send().status_code == 200


def test_stop_pauses_and_resume_unpauses(store):
    c, tg, _ = _app(store)
    H = _linked(c)
    c.post("/telegram/webhook", json=_msg(555, "/stop"), headers=WH)
    assert c.get("/notify/telegram/status", headers=H).get_json()["paused"] is True
    r = c.post("/notify/telegram/send", headers=H, json={"text": "hi"})
    assert r.status_code == 409 and r.get_json()["error"] == "paused"
    c.post("/telegram/webhook", json=_msg(555, "/resume"), headers=WH)
    assert c.post("/notify/telegram/send", headers=H, json={"text": "hi"}).status_code == 200
    c.post("/telegram/webhook", json=_msg(555, "/help"), headers=WH)
    assert "/stop" in tg.texts(555)[-1]
    kinds = [(e["type"], e["data"]) for e in c.get("/notify/telegram/inbox", headers=H).get_json()["events"]]
    assert kinds == [("command", "stop"), ("command", "resume")]


def test_unlink_says_goodbye_and_forgets_the_chat(store):
    c, tg, _ = _app(store)
    H = _linked(c)
    c.post("/telegram/webhook", json=_msg(555, "pending"), headers=WH)
    r = c.post("/notify/telegram/unlink", headers=H)
    assert r.status_code == 200 and r.get_json() == {"linked": False}
    assert tg.texts(555)[-1] == MSG_DISCONNECTED
    assert c.get("/notify/telegram/status", headers=H).get_json() == {"linked": False}
    assert c.get("/notify/telegram/inbox", headers=H).get_json()["events"] == []
    c.post("/telegram/webhook", json=_msg(555, "hello?"), headers=WH)
    assert tg.texts(555)[-1] == MSG_UNBOUND


def test_relinking_moves_the_chat_to_the_new_account(store):
    c, _, _ = _app(store)
    A = _linked(c, 555)
    B = _linked(c, 555)                                   # same Telegram chat, another install
    assert c.get("/notify/telegram/status", headers=A).get_json()["linked"] is False
    assert c.get("/notify/telegram/status", headers=B).get_json()["linked"] is True


def test_blocked_bot_unlinks_and_upstream_failure_is_502():
    c, tg, clock = _app()
    H = _linked(c)
    tg.fail = TelegramError("down", 500)
    assert c.post("/notify/telegram/send", headers=H, json={"text": "hi"}).status_code == 502
    clock.t += 5
    tg.fail = TelegramError("Forbidden: bot was blocked by the user", 403)
    r = c.post("/notify/telegram/send", headers=H, json={"text": "hi"})
    assert r.status_code == 409 and r.get_json()["error"] == "not_linked"
    assert c.get("/notify/telegram/status", headers=H).get_json()["linked"] is False


def test_server_builds_the_relay_only_when_fully_configured(monkeypatch, tmp_path):
    from backend import server
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "none.env"))
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_USERNAME", "TELEGRAM_WEBHOOK_SECRET"):
        monkeypatch.delenv(k, raising=False)
    db = str(tmp_path / "b.db")
    assert server.build_telegram(db) is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert server.build_telegram(db) is None              # half-configured stays off
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@SponsorJobsBot")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    relay = server.build_telegram(db, transport=FakeTelegram())
    assert relay is not None and relay.new_link("acct_x")["url"].startswith("https://t.me/SponsorJobsBot?start=")


def test_setup_helper_sets_the_webhook_with_the_secret(monkeypatch, tmp_path):
    from backend import telegram_setup
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("BROKER_PUBLIC_URL", raising=False)
    out = []
    assert telegram_setup.run(transport=FakeTelegram(), out=out.append) == 2
    monkeypatch.setenv("BROKER_PUBLIC_URL", "https://broker.example.com/")
    tg = FakeTelegram()
    assert telegram_setup.run(transport=tg, out=out.append) == 0
    method, payload = tg.calls[0]
    assert method == "setWebhook" and payload["url"] == "https://broker.example.com/telegram/webhook"
    assert payload["secret_token"] == SECRET
    assert [m for m, _ in tg.calls] == ["setWebhook", "setMyCommands", "getWebhookInfo"]
