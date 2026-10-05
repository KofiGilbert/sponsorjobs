"""SqliteUsageStore (backend/store_sqlite.py): the persistent store must behave EXACTLY like
InMemoryStore and survive a broker restart.

Offline, no keys. Uses a real SQLite file in a tmp dir. Reopening the store on the same path is
the "broker restarted" case: plan, usage, and purchased credits must still be there.
"""

from __future__ import annotations

from backend.metering import DEFAULT_PLAN, Meter, QuotaExceeded
from backend.store_sqlite import SqliteUsageStore

P = "2026-07"
Q = "2026-08"


def _db(tmp_path):
    return SqliteUsageStore(tmp_path / "usage.db")


def test_defaults_for_an_unknown_user(tmp_path):
    s = _db(tmp_path)
    assert s.get_plan_name("u") == DEFAULT_PLAN
    assert s.get_avatar_used("u", P) == 0 and s.get_llm_tokens("u", P) == 0
    assert s.get_credit_seconds("u") == 0


def test_counters_accumulate_per_period_and_credits_clamp(tmp_path):
    s = _db(tmp_path)
    s.add_avatar_used("u", P, 120); s.add_avatar_used("u", P, 60)
    assert s.get_avatar_used("u", P) == 180 and s.get_avatar_used("u", Q) == 0   # per period
    s.add_llm_tokens("u", P, 40_000); s.add_llm_tokens("u", P, 10_000)
    assert s.get_llm_tokens("u", P) == 50_000
    s.set_plan_name("u", "pass30")
    assert s.get_plan_name("u") == "pass30"
    s.add_packages_used("u", P, 2)
    assert s.get_packages_used("u", P) == 2 and s.get_packages_used("u", Q) == 0
    s.add_credit_seconds("u", 300); s.add_credit_seconds("u", -1000)              # clamp at 0
    assert s.get_credit_seconds("u") == 0


def test_survives_a_restart(tmp_path):
    s = _db(tmp_path)
    s.set_plan_name("u", "pass90")
    s.add_avatar_used("u", P, 240)
    s.add_credit_seconds("u", 600)
    # "restart": a brand-new store object on the same file
    s2 = SqliteUsageStore(tmp_path / "usage.db")
    assert s2.get_plan_name("u") == "pass90"
    assert s2.get_avatar_used("u", P) == 240
    assert s2.get_credit_seconds("u") == 600


def test_meter_runs_on_top_of_the_persistent_store(tmp_path):
    """The whole point: the Meter behaves identically whether the store is in-memory or SQLite."""
    m = Meter(_db(tmp_path))
    m.set_plan("u", "pass30")
    assert m.avatar_seconds_left("u", P) == 2700
    r = m.consume_avatar("u", P, 3000)                # only 2700 on the pass -> capped, hard stop
    assert r["consumed"] == 2700 and r["capped"] is True and r["remaining"] == 0
    try:
        m.require_avatar("u", P)
        raise AssertionError("expected QuotaExceeded")
    except QuotaExceeded:
        pass
    m.add_credits("u", 900)                            # a one-interview pack reopens it
    assert m.can_start_avatar("u", P) is True and m.avatar_seconds_left("u", P) == 900


def test_processed_keys_are_recorded_once_and_survive_a_restart(tmp_path):
    s = _db(tmp_path)
    assert s.has_processed("checkout:cs_1") is False
    assert s.mark_processed("checkout:cs_1") is True
    assert s.mark_processed("checkout:cs_1") is False       # a repeat delivery is a no-op
    assert s.has_processed("checkout:cs_1") is True
    assert SqliteUsageStore(tmp_path / "usage.db").has_processed("checkout:cs_1") is True


def test_pass_round_trips_and_balance_deltas_clamp(tmp_path):
    s = _db(tmp_path)
    assert s.get_pass("u") == {"name": "free", "until": 0, "seconds": 0, "packages": 0}
    s.set_pass("u", "pass30", 123, 2700, 150)
    s.add_pass_balance("u", -900, -1)
    assert s.get_pass("u") == {"name": "pass30", "until": 123, "seconds": 1800, "packages": 149}
    s.add_pass_balance("u", -99_999, -99_999)
    assert s.get_pass("u")["seconds"] == 0 and s.get_pass("u")["packages"] == 0
    s.add_credit_seconds("u", 600)                    # credits and the pass share the row
    assert s.get_credit_seconds("u") == 600 and s.get_pass("u")["name"] == "pass30"


def test_old_db_is_migrated_and_retired_tiers_become_free(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (user TEXT PRIMARY KEY, plan TEXT NOT NULL DEFAULT 'free',
                            credit_seconds INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE usage (user TEXT NOT NULL, period TEXT NOT NULL,
                            avatar_used INTEGER NOT NULL DEFAULT 0, llm_tokens INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (user, period));
        INSERT INTO users VALUES ('a', 'pro', 900), ('b', 'student', 0), ('c', 'browse', 0);
        INSERT INTO usage VALUES ('a', '2026-07', 60, 1000);
    """)
    c.commit(); c.close()
    s = SqliteUsageStore(path)
    assert {u: s.get_plan_name(u) for u in "abc"} == {"a": "free", "b": "free", "c": "free"}
    assert s.get_credit_seconds("a") == 900           # bought interviews survive the migration
    assert s.get_llm_tokens("a", "2026-07") == 1000 and s.get_packages_used("a", "2026-07") == 0
    m = Meter(s)
    assert m.tier_for("a") == "free" and m.avatar_seconds_left("a") == 900
