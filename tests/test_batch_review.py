"""Assisted-lane batch approval over Telegram + in-app queue (CLAUDE.md §5/§7).

Fully offline: the Telegram transport is a fake that captures every OUTBOUND call and
replays canned updates — no token, no network, and (proving local-first) no inbound
listener anywhere. Approval routes through the sanctioned submit path; the auto-lane
(Recruitee) submits via its API, everything else is marked for on-site submit.
"""

from __future__ import annotations

import ui.app as app
from notify.batch import BatchReview, batch_summary_text, item_caption
from notify.telegram import TelegramBot
from submit.rate_limit import RateLimiter
from ui.records import CVRecords

# Only these Bot API methods should ever be called — all OUTBOUND. No setWebhook / listener.
OUTBOUND_METHODS = {"sendMessage", "sendPhoto", "sendDocument", "answerCallbackQuery", "getUpdates"}


class FakeTG:
    """Captures outbound Bot API calls; replays queued updates for getUpdates."""

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.calls = []                       # list of (method, payload)

    def __call__(self, url, payload=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, payload))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"ok": True, "result": out}
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def methods(self):
        return [m for m, _ in self.calls]


class FakeActions:
    def __init__(self, items):
        self._items = items
        self.approved, self.skipped, self.approve_all_calls = [], [], 0

    def pending(self):
        return list(self._items)

    def approve(self, rid):
        self.approved.append(rid)
        return {"ok": True, "message": f"Approved {rid}"}

    def skip(self, rid):
        self.skipped.append(rid)
        return {"ok": True, "message": f"Skipped {rid}"}

    def approve_all(self):
        self.approve_all_calls += 1
        return {"ok": True, "message": "all done"}


def _items():
    return [{"id": 12, "role": "SWE", "company": "Acme", "lane": "auto",
             "lane_label": "Auto", "filled": {"name": True, "email": True, "phone": False},
             "screening_count": 2, "has_cover_letter": True, "coverage": 71, "cv_path": "/tmp/cv-12.pdf"},
            {"id": 14, "role": "Data", "company": "Beta", "lane": "assisted",
             "lane_label": "Assisted", "filled": {"name": True, "email": True, "phone": True},
             "screening_count": 0, "has_cover_letter": False, "coverage": 55, "cv_path": ""}]


def _bot(updates=None, chat_id="99"):
    tg = FakeTG(updates)
    return TelegramBot("tok", chat_id=chat_id, http=tg), tg


# --- caption / summary formatting -------------------------------------------- #

def test_item_caption_lists_company_role_and_filled():
    cap = item_caption(_items()[0])
    assert "Acme" in cap and "SWE" in cap
    assert "name" in cap and "email" in cap and "2 screening" in cap
    assert "cover letter" in cap and "71%" in cap


def test_batch_summary_lists_each():
    assert "2 application" in batch_summary_text(_items())
    assert "caught up" in batch_summary_text([])


# --- outbound send: preview + summary + buttons ------------------------------ #

def test_send_pushes_summary_then_per_item_with_buttons():
    bot, tg = _bot()
    BatchReview(bot, FakeActions(_items()), approve_all_enabled=False).send()
    methods = tg.methods()
    # batch summary first, then a document (item with cv) and a message (item without cv).
    assert methods[0] == "sendMessage"
    assert "sendDocument" in methods                       # CV preview uploaded
    # every per-item message carries Approve/Skip inline buttons
    doc_payload = next(p for m, p in tg.calls if m == "sendDocument")
    data = doc_payload["reply_markup"]["inline_keyboard"][0]
    assert data[0]["callback_data"] == "approve:12" and data[1]["callback_data"] == "skip:12"
    # ALL calls are outbound Bot API methods — no webhook/listener anywhere.
    assert set(methods) <= OUTBOUND_METHODS


def test_send_image_first_then_pdf_attached():
    # With a rendered image, the review message is the VIEWABLE image (carrying the summary +
    # Approve/Skip), and the exact PDF is attached as a document right BELOW it.
    items = _items()
    items[0]["image_path"] = "/tmp/cv-12.png"        # item 12 now has both image + pdf
    bot, tg = _bot()
    BatchReview(bot, FakeActions(items), approve_all_enabled=False).send()
    methods = tg.methods()
    photo = next(p for m, p in tg.calls if m == "sendPhoto")
    assert photo["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:12"
    assert "Acme" in photo["caption"]                # summary rides on the image message
    i = methods.index("sendPhoto")
    assert methods[i + 1] == "sendDocument"          # PDF attached immediately below
    assert "reply_markup" not in tg.calls[i + 1][1]  # the PDF is the attachment; buttons on the image
    assert set(methods) <= OUTBOUND_METHODS          # still outbound-only


def test_approve_all_button_only_when_enabled():
    # disabled -> the batch summary has no reply_markup
    bot, tg = _bot()
    BatchReview(bot, FakeActions(_items()), approve_all_enabled=False).send()
    assert "reply_markup" not in tg.calls[0][1]
    # enabled -> the batch summary carries an "Approve all" button
    bot2, tg2 = _bot()
    BatchReview(bot2, FakeActions(_items()), approve_all_enabled=True).send()
    kb = tg2.calls[0][1]["reply_markup"]["inline_keyboard"][0]
    assert kb[0]["callback_data"] == "approveall"


# --- inbound taps (via outbound poll) ---------------------------------------- #

def test_callback_approve_and_skip_dispatch():
    bot, tg = _bot()
    acts = FakeActions(_items())
    br = BatchReview(bot, acts, approve_all_enabled=False)
    br.handle_callback({"id": "c1", "data": "approve:12"})
    br.handle_callback({"id": "c2", "data": "skip:14"})
    assert acts.approved == [12] and acts.skipped == [14]
    assert "answerCallbackQuery" in tg.methods()           # taps are acknowledged


def test_approveall_callback_gated_off_by_default():
    bot, _ = _bot()
    acts = FakeActions(_items())
    off = BatchReview(bot, acts, approve_all_enabled=False)
    off.handle_callback({"id": "c", "data": "approveall"})
    assert acts.approve_all_calls == 0                     # ignored when off
    on = BatchReview(bot, acts, approve_all_enabled=True)
    on.handle_callback({"id": "c", "data": "approveall"})
    assert acts.approve_all_calls == 1


def test_poll_is_owner_only():
    owner = {"update_id": 1, "callback_query": {"id": "a", "data": "approve:12",
                                                "message": {"chat": {"id": 99}}}}
    stranger = {"update_id": 2, "callback_query": {"id": "b", "data": "approve:12",
                                                   "message": {"chat": {"id": 55}}}}
    bot, tg = _bot(updates=[owner, stranger], chat_id="99")
    acts = FakeActions(_items())
    res = BatchReview(bot, acts, approve_all_enabled=False).poll(offset=0)
    assert res["handled"] == 1 and res["offset"] == 3      # both acked, only owner acted
    assert acts.approved == [12]


# --- rate limiter: cap + spacing (sanctioned-API courtesy) ------------------- #

def test_rate_limit_daily_cap(tmp_path):
    rl = RateLimiter(tmp_path / "r.json", cap=2, min_gap=0)
    assert rl.allow(100.0, "2026-07-14")[0] is True
    rl.record(100.0, "2026-07-14")
    rl.record(101.0, "2026-07-14")
    ok, reason = rl.allow(102.0, "2026-07-14")
    assert ok is False and reason == "daily_cap"
    # next day resets
    assert rl.allow(200.0, "2026-07-15")[0] is True


def test_rate_limit_spacing(tmp_path):
    rl = RateLimiter(tmp_path / "r.json", cap=99, min_gap=60, jitter=0, jitter_fn=lambda: 0)
    rl.record(1000.0, "2026-07-14")
    assert rl.allow(1030.0, "2026-07-14") == (False, "spacing")   # within 60s
    assert rl.allow(1061.0, "2026-07-14")[0] is True              # after the gap


# --- endpoints: settings default, queue, batch approve ----------------------- #

def _seed(tmp_path, monkeypatch, url="https://linkedin.com/jobs/1", status="ready"):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    (tmp_path / "cv").mkdir(exist_ok=True)
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_RATE_FILE", tmp_path / "rate.json")
    monkeypatch.setattr(app, "_TG_OFFSET_FILE", tmp_path / "off")
    monkeypatch.setattr(app, "_SESSION", {})
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "",
                   data={"status": status, "source_job": {"url": url},
                         "screening": [{"question": "Q", "answer": "A"}], "cover_letter": "Hi"})
    recs.close()
    return rid


def test_approve_all_off_by_default(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/submit/settings").get_json()
    assert d["approve_all"] is False and d["autonomous"] is False
    assert d["daily_cap"] == 40


def test_settings_toggle_approve_all(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    c = app.app.test_client()
    assert c.post("/api/submit/settings", json={"approve_all": True}).get_json()["approve_all"] is True
    assert c.get("/api/submit/settings").get_json()["approve_all"] is True


def test_in_app_queue_lists_pending(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/review/queue").get_json()
    assert d["count"] == 1 and d["pending"][0]["id"] == rid
    assert d["pending"][0]["lane"] == "assisted"           # linkedin -> assisted


def test_in_app_batch_approve_assisted_marks_approved(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    r = app.app.test_client().post("/api/review/approve", json={"approve": [rid]}).get_json()
    assert r["approved"][0]["ok"] is True
    # assisted site is marked approved (for on-site submit) — NOT auto-submitted
    recs = CVRecords(app.DB_PATH)
    try:
        import json
        data = json.loads(recs.get(rid)["data"])
    finally:
        recs.close()
    assert data["status"] == "approved" and data["submission"]["status"] == "approved"


def test_auto_lane_approve_submits_via_sanctioned_driver(tmp_path, monkeypatch):
    import submit
    rid = _seed(tmp_path, monkeypatch, url="https://acme.recruitee.com/o/eng")
    # Inject a fake sanctioned driver so no real network call happens.
    monkeypatch.setattr(submit, "driver_for",
                        lambda host: (lambda url, data: {"ok": True, "detail": "ok"}))
    r = app.app.test_client().post("/api/review/approve", json={"approve": [rid]}).get_json()
    assert r["approved"][0]["ok"] is True
    recs = CVRecords(app.DB_PATH)
    try:
        import json
        data = json.loads(recs.get(rid)["data"])
    finally:
        recs.close()
    assert data["status"] == "applied"                     # real sanctioned submission


def test_poll_fails_closed_when_owner_unset():
    # Regression: an UNSET owner chat must never mean "obey everyone". A stranger's tap on a
    # bot with no configured chat_id must be ignored (fail-closed), not acted on.
    stranger = {"update_id": 1, "callback_query": {"id": "s", "data": "approve:12",
                                                   "message": {"chat": {"id": 777}}}}
    bot, _ = _bot(updates=[stranger], chat_id="")     # no owner configured
    acts = FakeActions(_items())
    res = BatchReview(bot, acts, approve_all_enabled=False).poll(offset=0)
    assert res["handled"] == 0 and acts.approved == []
    assert "error" in res


def test_notify_poll_fails_closed_when_owner_unset():
    from notify.service import NotifyService

    class Acts:
        def pending(self): return []
        def approve(self, rid): return {"role": "x"}
        def status(self, rid): return None
    stranger = {"update_id": 1, "message": {"text": "/approve 5", "chat": {"id": 777}}}
    bot, tg = _bot(updates=[stranger], chat_id="")
    res = NotifyService(bot, Acts()).poll_once(offset=0)
    assert res["handled"] == 0 and "error" in res
    assert "sendMessage" not in tg.methods()          # never replied to the stranger


def test_review_poll_endpoint_fails_closed_without_owner(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    tg = FakeTG(updates=[{"update_id": 1, "callback_query": {
        "id": "z", "data": "approve:1", "message": {"chat": {"id": 777}}}}])
    monkeypatch.setattr(app, "_make_bot", lambda: TelegramBot("t", chat_id="", http=tg))  # no owner
    r = app.app.test_client().post("/api/review/telegram/poll")
    assert r.status_code == 400
    assert "getUpdates" not in tg.methods()           # refused before touching updates


def test_record_submit_respects_daily_cap(tmp_path, monkeypatch):
    # Regression: the per-record submit endpoint must honor the daily cap, not bypass it.
    from datetime import datetime
    rid = _seed(tmp_path, monkeypatch, url="https://acme.recruitee.com/o/eng")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    app._set_autonomous(True)
    app._set_prefs({"daily_cap": 1})
    today = datetime.now().date().isoformat()
    (tmp_path / "rate.json").write_text(
        __import__("json").dumps({"date": today, "count": 1, "last_ts": 0.0}), encoding="utf-8")
    r = app.app.test_client().post(f"/api/record/{rid}/submit", json={}).get_json()
    assert r["ok"] is False and r["status"] == "auto_pending"   # cap enforced, NOT submitted


def test_record_submit_auto_counts_against_cap(tmp_path, monkeypatch):
    import json

    import submit
    rid = _seed(tmp_path, monkeypatch, url="https://acme.recruitee.com/o/eng")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    app._set_autonomous(True)
    monkeypatch.setattr(submit, "driver_for",
                        lambda host: (lambda url, data: {"ok": True, "detail": "ok"}))
    r = app.app.test_client().post(f"/api/record/{rid}/submit", json={}).get_json()
    assert r["status"] == "auto_submitted"
    # the submission was counted against the cap (so it can't run unbounded)
    assert json.loads((tmp_path / "rate.json").read_text())["count"] == 1


def test_telegram_send_and_poll_endpoints_outbound_only(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch, url="https://acme.recruitee.com/o/eng")
    tg = FakeTG(updates=[{"update_id": 5, "callback_query": {
        "id": "z", "data": f"skip:{rid}", "message": {"chat": {"id": 99}}}}])
    monkeypatch.setattr(app, "_make_bot", lambda: TelegramBot("t", chat_id="99", http=tg))
    c = app.app.test_client()
    assert c.post("/api/review/telegram/send").get_json()["ok"] is True
    poll = c.post("/api/review/telegram/poll").get_json()
    assert poll["ok"] is True and poll["handled"] == 1
    # Every Telegram call made by both endpoints is outbound — nothing inbound is opened.
    assert set(tg.methods()) <= OUTBOUND_METHODS
