"""Metering / allowance engine for the bundled managed-AI backend (backend/metering.py).

Pricing decided 2026-10-02: Free (3 Haiku packages a calendar month, no live interviews), the Job
Hunt Pass (pass30: $29, 30 days, 3 interviews, 60 packages) and the Season Pass (pass90: $69,
90 days, 9 interviews, 150 packages). One-time payments, nothing auto-renews; extra interviews are
sold only during a pass and never expire.

Fully offline and deterministic: an in-memory store, an explicit calendar period and an injected
clock, no DB and no network.
"""

from __future__ import annotations

import pytest

from backend.metering import (DAY_SECONDS, DEFAULT_PLAN, INTERVIEW_SECONDS, PLANS, InMemoryStore,
                              Meter, QuotaExceeded)

P = "2026-07"      # a calendar period
Q = "2026-08"      # the next one, for the free monthly reset
T0 = 1_780_000_000


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, days=0, seconds=0):
        self.t += days * DAY_SECONDS + seconds


def _meter(clock=None):
    clock = clock or Clock()
    m = Meter(InMemoryStore(), clock=clock)
    m.clock_obj = clock
    return m


# -- tiers and allowances -----------------------------------------------------------------------
def test_the_three_tiers_as_decided():
    assert INTERVIEW_SECONDS == 900
    assert set(PLANS) == {"free", "pass30", "pass90"}               # no subscription tiers remain
    shape = {k: (p.days, p.interviews, p.packages, p.llm_model, p.price_label) for k, p in PLANS.items()}
    assert shape == {
        "free":   (0, 0, 3, "claude-haiku-4-5", ""),
        "pass30": (30, 3, 60, "claude-sonnet-4-6", "$29"),
        "pass90": (90, 9, 150, "claude-sonnet-4-6", "$69"),
    }
    assert PLANS["pass30"].avatar_seconds_included == 2700 and PLANS["pass90"].avatar_seconds_included == 8100


def test_new_user_is_free_with_no_live_interviews_and_the_cheap_model():
    m = _meter()
    assert m.tier_for("u") == DEFAULT_PLAN == "free"
    assert m.can_start_avatar("u", P) is False
    assert m.llm_model_for("u") == "claude-haiku-4-5"
    assert m.packages_left("u", P) == 3
    with pytest.raises(QuotaExceeded) as e:
        m.require_avatar("u", P)
    assert e.value.reason == "pass_required"


def test_a_pass_gives_its_allowances_and_the_better_model():
    m = _meter()
    m.grant_pass("u", "pass30")
    st = m.status("u", P)
    assert st["tier"] == "pass30" and st["pass_until"] == T0 + 30 * DAY_SECONDS
    assert st["interviews_left"] == 3 and st["packages_left"] == 60
    assert m.llm_model_for("u") == "claude-sonnet-4-6"
    m2 = _meter()
    m2.grant_pass("v", "pass90")
    assert m2.status("v", P)["interviews_left"] == 9 and m2.status("v", P)["packages_left"] == 150


def test_model_per_tier_honours_env_overrides(monkeypatch):
    m = _meter()
    monkeypatch.setenv("TAILOR_FREE_MODEL", "claude-haiku-x")
    monkeypatch.setenv("TAILOR_PASS_MODEL", "claude-opus-x")
    assert m.llm_model_for("u") == "claude-haiku-x"
    m.grant_pass("u", "pass30")
    assert m.llm_model_for("u") == "claude-opus-x"


def test_unknown_pass_or_plan_is_rejected():
    m = _meter()
    with pytest.raises(ValueError):
        m.grant_pass("u", "free")
    with pytest.raises(ValueError):
        m.set_plan("u", "student")
    with pytest.raises(ValueError):
        m.set_plan("u", "platinum")


# -- expiry and stacking -----------------------------------------------------------------------
def test_pass_expiry_returns_the_account_to_free():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.clock_obj.advance(days=30, seconds=-1)
    assert m.tier_for("u") == "pass30"
    m.clock_obj.advance(seconds=1)                                   # exactly at pass_until: over
    assert m.tier_for("u") == "free"
    assert m.can_start_avatar("u", P) is False                       # leftover pass interviews are gone
    assert m.packages_left("u", P) == 3                              # back to the free monthly 3
    assert m.llm_model_for("u") == "claude-haiku-4-5"
    assert m.status("u", P)["pass_until"] is None


def test_buying_after_expiry_starts_fresh_and_does_not_revive_leftovers():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.clock_obj.advance(days=31)
    m.grant_pass("u", "pass30")
    st = m.status("u", P)
    assert st["interviews_left"] == 3 and st["packages_left"] == 60   # not 6 / 120
    assert st["pass_until"] == m.clock_obj.t + 30 * DAY_SECONDS


def test_stacking_extends_the_end_date_and_adds_allowances():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.start_package("u", P)                                          # 59 left
    m.consume_avatar("u", P, 900)                                    # 2 interviews left
    m.clock_obj.advance(days=10)
    m.grant_pass("u", "pass90")
    st = m.status("u", P)
    assert st["pass_until"] == T0 + (30 + 90) * DAY_SECONDS
    assert st["interviews_left"] == 2 + 9 and st["packages_left"] == 59 + 150
    assert st["tier"] == "pass90"                                    # the longer pass names it
    m.grant_pass("u", "pass30")
    assert m.status("u", P)["pass_until"] == T0 + (30 + 90 + 30) * DAY_SECONDS
    assert m.tier_for("u") == "pass90"


# -- live interviews ---------------------------------------------------------------------------
def test_pass_interviews_run_out_then_need_extras():
    m = _meter()
    m.grant_pass("u", "pass30")
    r = m.consume_avatar("u", P, 2700)
    assert r == {"consumed": 2700, "capped": False, "remaining": 0}
    with pytest.raises(QuotaExceeded) as e:
        m.require_avatar("u", P)
    assert e.value.reason == "no_interviews"


def test_consume_never_goes_negative_and_reports_capped():
    m = _meter()
    m.grant_pass("u", "pass30")
    r = m.consume_avatar("u", P, 3000)
    assert r["consumed"] == 2700 and r["capped"] is True and r["remaining"] == 0
    assert m.consume_avatar("u", P, 60) == {"consumed": 0, "capped": True, "remaining": 0}


def test_never_starts_an_interview_that_cannot_finish():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.consume_avatar("u", P, 2700 - 600)                             # 10 minutes left
    assert m.can_start_avatar("u", P) is False
    m.add_credits("u", 299)
    assert m.can_start_avatar("u", P) is False                       # 899 s, one second short
    m.add_credits("u", 1)
    assert m.can_start_avatar("u", P) is True
    m.require_avatar("u", P)


def test_consume_takes_the_pass_first_then_purchased_extras():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.add_credits("u", 900)
    r = m.consume_avatar("u", P, 2800)
    assert r == {"consumed": 2800, "capped": False, "remaining": 800}
    assert m.store.get_credit_seconds("u") == 800                    # 100 s came from the extras


def test_extras_never_expire_and_survive_the_pass():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.add_credits("u", 900)
    m.clock_obj.advance(days=40)
    assert m.tier_for("u") == "free"
    assert m.avatar_seconds_left("u", P) == 900 and m.can_start_avatar("u", P) is True


def test_packs_can_only_be_bought_during_a_pass():
    m = _meter()
    assert m.can_buy_pack("u") is False
    m.grant_pass("u", "pass30")
    assert m.can_buy_pack("u") is True
    m.clock_obj.advance(days=30)
    assert m.can_buy_pack("u") is False


def test_negative_consume_and_bad_credits_are_rejected():
    m = _meter()
    with pytest.raises(ValueError):
        m.consume_avatar("u", P, -1)
    with pytest.raises(ValueError):
        m.add_credits("u", 0)


# -- tailored packages -------------------------------------------------------------------------
def test_free_gets_three_packages_a_month_and_resets_next_month():
    m = _meter()
    for left in (2, 1, 0):
        assert m.start_package("u", P)["packages_left"] == left
    with pytest.raises(QuotaExceeded) as e:
        m.start_package("u", P)
    assert e.value.reason == "package_limit"
    assert m.packages_left("u", Q) == 3                              # a new calendar month
    assert m.start_package("u", Q) == {"packages_left": 2, "tier": "free"}


def test_pass_packages_come_from_the_pass_not_the_free_monthly_count():
    m = _meter()
    for _ in range(3):
        m.start_package("u", P)                                      # free 3 used this month
    m.grant_pass("u", "pass30")
    assert m.start_package("u", P)["packages_left"] == 59
    assert m.store.get_packages_used("u", P) == 3                    # the free counter did not move


def test_pass_packages_run_out():
    m = _meter()
    m.grant_pass("u", "pass30")
    m.store.add_pass_balance("u", 0, -59)
    m.start_package("u", P)
    with pytest.raises(QuotaExceeded) as e:
        m.start_package("u", P)
    assert e.value.reason == "package_limit"


# -- llm token cap (a monthly safety net) -----------------------------------------------------
def test_token_cap_is_a_monthly_safety_net_per_tier():
    m = _meter()
    cap = PLANS["free"].llm_token_cap
    assert m.can_use_llm("u", P, est_tokens=cap) is True
    assert m.can_use_llm("u", P, est_tokens=cap + 1) is False
    m.record_llm("u", P, cap)
    with pytest.raises(QuotaExceeded) as e:
        m.require_llm("u", P, est_tokens=1)
    assert e.value.reason == "token_cap"
    m.grant_pass("u", "pass30")                                      # a pass has a much higher cap
    assert m.can_use_llm("u", P, est_tokens=1_000_000) is True
    assert PLANS["pass30"].llm_token_cap > 150 * 35_000              # far above honest use


def test_llm_tokens_are_counted_per_period():
    m = _meter()
    m.record_llm("u", P, 40_000)
    assert m.record_llm("u", P, 10_000)["used"] == 50_000
    assert m.record_llm("u", Q, 1_000)["used"] == 1_000


def test_set_plan_free_ends_a_pass():
    m = _meter()
    m.set_plan("u", "pass90")
    assert m.tier_for("u") == "pass90"
    m.set_plan("u", "free")
    assert m.tier_for("u") == "free" and m.avatar_seconds_left("u", P) == 0
