"""Compliance hardening for submit/ (CLAUDE.md §7). Pins the auto-submit safety invariants
an adversarial audit found unenforced or untested — a hole here lets the agent act on the
user's behalf when it shouldn't, the worst failure mode in the product:

  * spacing (min-gap) applies to EVERY attempt that hit the API, not only successes, so a
    run of failing/429 items can't hammer a site;
  * a queued item that fails / drops to needs-assist is un-queued, so drain() stops re-POSTing;
  * a corrupt rate store fails CLOSED (never re-opens the daily cap after a crash);
  * the AUTO boundary rejects lookalike/spoofed hosts; the tier (not the mere presence of a
    driver) gates real submission; the keyless payload never carries a secret.
"""
from __future__ import annotations

import json
from datetime import datetime

import ui.app as app
from submit import classify_record, submit_record  # noqa: F401 (submit_record used below)
from submit.drivers import _to_candidates_endpoint, driver_for, recruitee_submit
from submit.policy import submission_policy
from submit.rate_limit import RateLimiter
from ui.records import CVRecords


def _record():
    return {"profile": {"identity": {"name": "Sam Rivera", "email": "sam@x.com", "phone": "1"}},
            "cover_letter": "Hi", "screening": [{"question": "Q", "answer": "A"}]}


# ============================ bug 3: rate store fails closed / atomic ============================
def test_corrupt_rate_store_fails_closed(tmp_path):
    p = tmp_path / "rate.json"
    p.write_text('{"date": "2026-07-14", "count": 35,', encoding="utf-8")   # truncated write
    rl = RateLimiter(p, cap=40)
    allowed, reason = rl.allow(1000.0, "2026-07-14")
    assert allowed is False and reason == "store_unreadable"     # NOT reset to 0 / re-opened
    assert rl.status(1000.0, "2026-07-14")["remaining"] == 0


def test_missing_rate_store_is_a_fresh_day(tmp_path):
    rl = RateLimiter(tmp_path / "nope.json", cap=40)               # no file yet != corrupt
    assert rl.allow(1000.0, "2026-07-14") == (True, "ok")


def test_rate_store_write_is_atomic(tmp_path):
    p = tmp_path / "rate.json"
    rl = RateLimiter(p, cap=40)
    rl.record(1000.0, "2026-07-14")
    assert not (tmp_path / "rate.json.tmp").exists()              # temp swapped in via os.replace
    assert json.loads(p.read_text(encoding="utf-8"))["count"] == 1


# ============================ bug 1: spacing on every posted attempt ============================
def test_record_attempt_advances_spacing_without_counting(tmp_path):
    rl = RateLimiter(tmp_path / "r.json", cap=5, min_gap=60, jitter=0, jitter_fn=lambda: 0)
    rl.record_attempt(1000.0, "2026-07-14")                       # a failed POST fired
    assert rl.allow(1030.0, "2026-07-14") == (False, "spacing")   # next attempt is spaced
    assert rl.status(1030.0, "2026-07-14")["count"] == 0          # but NOT counted vs the cap
    assert rl.allow(1061.0, "2026-07-14")[0] is True              # frees after the gap


# ============================ host-boundary: no spoof reaches AUTO (gaps A + F) ============================
def test_lookalike_hosts_are_never_auto():
    for url in ("https://evilrecruitee.com/o/x", "https://notrecruitee.com/o/x",
                "https://recruitee.com.evil.com/o/x", "https://acme.recruitee.com@evil.com/o/x"):
        assert submission_policy(url) == "assisted", url


def test_driver_layer_rejects_lookalike_recruitee_host():
    assert driver_for("evilrecruitee.com") is None
    assert driver_for("recruitee.com.evil.com") is None
    assert _to_candidates_endpoint("https://evilrecruitee.com/o/y") == ""
    # genuine hosts still work
    assert driver_for("acme.recruitee.com") is recruitee_submit
    assert _to_candidates_endpoint("https://acme.recruitee.com/o/eng") == \
        "https://acme.recruitee.com/api/offers/eng/candidates"


# ============================ tier gates submission, not driver presence (gap C) ============================
def test_assisted_url_never_fires_a_supplied_driver():
    fired = {"n": 0}

    def drv(url, data):
        fired["n"] += 1
        return {"ok": True, "posted": True}

    r = submit_record({"source_job": {"url": "https://linkedin.com/jobs/1"}}, driver=drv)
    assert r["status"] == "assisted" and fired["n"] == 0          # the tier, not the driver, decides


# ============================ keyless: payload/headers carry no employer secret (gap D) ============================
def test_recruitee_payload_carries_no_secret():
    sent = {}

    def http(url, payload):
        sent["payload"] = payload
        return 201, "{}"

    recruitee_submit("https://acme.recruitee.com/o/eng", _record(), http=http)
    keys = set(sent["payload"]["candidate"].keys())
    assert keys <= {"name", "email", "phone", "cover_letter", "open_question_answers"}
    for forbidden in ("api_key", "apikey", "token", "secret", "authorization", "key"):
        assert forbidden not in keys


def test_default_transport_sends_no_auth_header(monkeypatch):
    import submit.drivers as d
    captured = {}

    class _Resp:
        status = 201

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower() for k in dict(req.header_items())}
        return _Resp()

    monkeypatch.setattr(d.urllib.request, "urlopen", fake_urlopen)
    d._post_json("https://acme.recruitee.com/api/offers/x/candidates", {"candidate": {}})
    assert "authorization" not in captured["headers"]            # never sends auth
    assert "content-type" in captured["headers"]


def test_non_2xx_status_aborts_to_assisted():
    # The generic non-2xx fall-through (e.g. 500/429) must abort to assisted, not fake success.
    for status in (500, 429, 400):
        r = recruitee_submit("https://acme.recruitee.com/o/eng", _record(),
                             http=lambda u, p, s=status: (s, "server error"))
        assert r["ok"] is False and r["needs_assist"] is True and r["posted"] is True


# ============================ batch/app path: cap + un-queue (bugs 1,2 + gaps B,E) ============================
def _seed(tmp_path, monkeypatch, url="https://acme.recruitee.com/o/eng"):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    (tmp_path / "cv").mkdir(exist_ok=True)
    monkeypatch.setattr(app, "_PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(app, "_RATE_FILE", tmp_path / "rate.json")
    monkeypatch.setattr(app, "_AUTONOMOUS_FILE", tmp_path / "auto")
    monkeypatch.setattr(app, "_SESSION", {})
    recs = CVRecords(app.DB_PATH)
    try:
        return recs.add("Engineer", "Acme", 70, "",
                        data={"status": "ready", "source_job": {"url": url}})
    finally:
        recs.close()


def _data(rid):
    recs = CVRecords(app.DB_PATH)
    try:
        return json.loads(recs.get(rid)["data"])
    finally:
        recs.close()


def _set_queued(rid, value=True):
    recs = CVRecords(app.DB_PATH)
    try:
        recs.merge_data(rid, {"queued": value})
    finally:
        recs.close()


def test_batch_approve_at_cap_queues_without_posting(tmp_path, monkeypatch):
    import submit
    rid = _seed(tmp_path, monkeypatch)
    app._set_autonomous(True)
    app._set_prefs({"daily_cap": 1})
    today = datetime.now().date().isoformat()
    (tmp_path / "rate.json").write_text(json.dumps({"date": today, "count": 1, "last_ts": 0.0}),
                                        encoding="utf-8")
    fired = {"n": 0}

    def drv(url, data):
        fired["n"] += 1
        return {"ok": True, "posted": True}

    monkeypatch.setattr(submit, "driver_for", lambda host: drv)
    app.app.test_client().post("/api/review/approve", json={"approve": [rid]})
    assert fired["n"] == 0                          # at the cap -> the driver never ran
    assert _data(rid).get("queued") is True         # held for a later slot


def test_batch_auto_submit_counts_against_the_cap(tmp_path, monkeypatch):
    import submit
    rid = _seed(tmp_path, monkeypatch)
    app._set_autonomous(True)
    monkeypatch.setattr(submit, "driver_for",
                        lambda host: (lambda u, d: {"ok": True, "posted": True, "detail": "ok"}))
    app.app.test_client().post("/api/review/approve", json={"approve": [rid]})
    assert json.loads((tmp_path / "rate.json").read_text())["count"] == 1
    assert _data(rid)["status"] == "applied"


def test_batch_failing_auto_spaces_and_unqueues(tmp_path, monkeypatch):
    """Bug 1 + 2 together: a queued item whose driver fails advances the SPACING clock (so the
    next attempt is spaced) and is UN-QUEUED, so drain() never re-POSTs it forever."""
    import submit
    rid = _seed(tmp_path, monkeypatch)
    app._set_autonomous(True)
    _set_queued(rid, True)                          # as if rate-limited earlier
    calls = {"n": 0}

    def failing(url, data):
        calls["n"] += 1
        return {"ok": False, "needs_assist": True, "posted": True, "detail": "captcha"}

    monkeypatch.setattr(submit, "driver_for", lambda host: failing)
    app._BatchActions().drain(limit=5)              # processes the queued item once
    app._BatchActions().drain(limit=5)              # ...must NOT re-POST it
    assert calls["n"] == 1                           # a challenged site is hit exactly once
    assert _data(rid).get("queued") is False         # un-queued -> drain leaves it alone
    rate = json.loads((tmp_path / "rate.json").read_text())
    assert rate["last_ts"] > 0 and rate["count"] == 0   # spacing advanced; NOT counted (no success)
