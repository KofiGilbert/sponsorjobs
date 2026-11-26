"""The Orb's pacing and limits (orb/rules.py, orb/store.py): nudges from real data only, at most 3
unasked bubbles a day and one per 30 minutes, never while typing or during Autofill or in quiet
hours, two dismissals of a kind turn it off for a week, and the chat drops to rules-only answers
past its hourly or daily cap. Every check runs on its own clock."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orb import rules
from orb.store import OrbStore

T0 = datetime(2026, 11, 26, 10, 0, 0)


@pytest.fixture()
def st(tmp_path):
    s = OrbStore(tmp_path / "orb.db")
    yield s
    s.close()


def _c(kind, key, text="x"):
    return {"type": kind, "key": key, "text": text, "action": {"nav": "tracker", "label": "Open Tracker"}}


def test_flags_and_switches():
    assert rules.flag("SJ_ORB", env={}) is True
    assert rules.flag("SJ_ORB", env={"SJ_ORB": "0"}) is False
    assert rules.flag("SJ_ORB", env={"SJ_ORB": "1"}) is True
    assert rules.enabled(env_on=True, setting_on=True, remote_on=True) == (True, "")
    assert rules.enabled(env_on=False, setting_on=True, remote_on=True) == (False, "env")
    assert rules.enabled(env_on=True, setting_on=True, remote_on=False) == (False, "remote")
    assert rules.enabled(env_on=True, setting_on=False, remote_on=True) == (False, "setting")


def test_quiet_hours_wrap_midnight():
    assert rules.in_quiet_hours(23, 22, 8) and rules.in_quiet_hours(3, 22, 8)
    assert not rules.in_quiet_hours(12, 22, 8)
    assert not rules.in_quiet_hours(5, 7, 7)


@pytest.mark.parametrize("why,kw", [
    ("typing", {"typing": True}), ("autofill", {"autofill": True}), ("quiet", {"quiet": True}),
    ("cap", {"shown_today": 3}), ("gap", {"last_shown": (T0 - timedelta(minutes=10)).isoformat()}),
])
def test_may_show_refuses(why, kw):
    args = {"now": T0, "shown_today": 0, "last_shown": None, "typing": False, "autofill": False, "quiet": False}
    args.update(kw)
    assert rules.may_show(**args) == (False, why)


def test_may_show_after_the_gap():
    ok, _ = rules.may_show(now=T0, shown_today=2, last_shown=(T0 - timedelta(minutes=31)).isoformat(),
                           typing=False, autofill=False, quiet=False)
    assert ok


def test_sync_pick_and_priority(st):
    st.sync([_c("matches", "m:1"), _c("ready", "r:1"), _c("followup", "f:1")], T0)
    rows = st.nudges()
    assert [r["type"] for r in rows] == ["matches", "ready", "followup"]
    assert rules.pick(rows, snoozed=set())["type"] == "ready"
    assert rules.pick(rows, snoozed={"ready"})["type"] == "followup"


def test_data_gone_expires_and_repeat_window(st):
    st.sync([_c("ready", "r:1"), _c("followup", "f:1")], T0)
    r = next(x for x in st.nudges() if x["type"] == "ready")
    st.mark(r["id"], "shown", T0)
    # the follow-up was sent: its nudge goes; the ready one stays shown inside its window
    st.sync([_c("ready", "r:1")], T0 + timedelta(hours=1))
    left = {x["dedup_key"]: x["status"] for x in st.nudges(statuses=None)}
    assert left == {"r:1": "shown", "f:1": "expired"}
    # two days later it may come back
    st.sync([_c("ready", "r:1")], T0 + timedelta(days=2, minutes=1))
    assert st.get(r["id"])["status"] == "pending"


def test_matches_never_repeat_and_expire_on_time(st):
    st.sync([_c("matches", "m:1")], T0)
    m = st.nudges()[0]
    st.mark(m["id"], "shown", T0)
    st.sync([_c("matches", "m:1")], T0 + timedelta(days=2))
    assert st.get(m["id"])["status"] == "shown"
    st.sync([_c("matches", "m:1")], T0 + timedelta(days=4))
    assert st.get(m["id"])["status"] == "expired"


def test_two_dismissals_turn_a_kind_off_for_a_week(st):
    st.sync([_c("followup", "f:1"), _c("followup", "f:2")], T0)
    a, b = st.nudges()
    st.mark(a["id"], "dismissed", T0)
    assert "followup" not in st.snoozed(T0)
    st.mark(b["id"], "dismissed", T0)
    assert "followup" in st.snoozed(T0 + timedelta(days=6))
    assert "followup" not in st.snoozed(T0 + timedelta(days=7, minutes=1))


def test_acting_resets_the_dismiss_count(st):
    st.sync([_c("ready", "r:1"), _c("ready", "r:2"), _c("ready", "r:3")], T0)
    a, b, c = st.nudges()
    st.mark(a["id"], "dismissed", T0)
    st.mark(b["id"], "acted", T0)
    st.mark(c["id"], "dismissed", T0)
    assert st.snoozed(T0) == set()


def test_shown_today_counts_the_log(st):
    st.sync([_c("ready", f"r:{i}") for i in range(4)], T0)
    for n in st.nudges()[:3]:
        st.mark(n["id"], "shown", T0)
    count, last = st.shown_today(T0)
    assert count == 3 and last.startswith("2026-11-26T10:00")
    assert st.shown_today(T0 + timedelta(days=1))[0] == 0


def test_event_log_keeps_ids_and_kinds_and_prunes_after_180_days(st):
    st.log("chat", "ai", T0 - timedelta(days=200))
    st.log("chat", "ai", T0)
    st.prune(T0)
    assert st.count_events("chat", T0 - timedelta(days=400)) == 1


def test_chat_limits_drop_to_rules(st, monkeypatch):
    assert st.chat_mode(T0) == "ai"
    for _ in range(rules.CHAT_PER_HOUR):
        st.log("chat", "ai", T0)
    assert st.chat_mode(T0) == "rules"
    assert st.chat_mode(T0 + timedelta(hours=1, minutes=1)) == "ai"
    later = T0 + timedelta(days=1)
    for _ in range(int(rules.CHAT_DAILY_CENTS / rules.CHAT_TURN_CENTS) + 1):
        st.add_spend(later)
    assert st.chat_mode(later) == "rules"


def test_mute_command():
    assert rules.mute_command("Mute") == "mute"
    assert rules.mute_command("unmute.") == "unmute"
    assert rules.mute_command("mute the job alerts for Acme") == ""


def test_chat_memory_summarises_and_is_bounded(st):
    for i in range(rules.CHAT_KEEP + 10):
        st.add_turn("user", f"q{i}", T0)
    assert len(st.turns()) == rules.CHAT_KEEP
    for i in range(rules.MEMORY_KEEP + 5):
        st.close_chat(f"topic {i}", T0)
    assert st.turns() == []
    mem = st.memory()
    assert len(mem) == rules.MEMORY_KEEP and mem[0]["topics"] == f"topic {rules.MEMORY_KEEP + 4}"


def test_erase_clears_everything_but_settings(st):
    st.set_pref("enabled", False, T0)
    st.sync([_c("ready", "r:1")], T0)
    st.log("chat", "ai", T0)
    st.erase()
    assert st.nudges(statuses=None) == [] and st.count_events("chat", T0 - timedelta(days=1)) == 0
    assert st.pref("enabled") is False
