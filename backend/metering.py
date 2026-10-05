"""Usage metering and pass allowances for the bundled managed-AI backend.

The heart of "protect the margin" (docs/bundled-api-backend.md, sec 5): the broker checks an
allowance BEFORE calling a paid provider and records usage AFTER, so no user can run the company's
Anthropic/Tavus account past what they paid for.

Pricing (decided by the owner 2026-10-02; no subscriptions, nothing auto-renews):

  * ``free``   (default, no card): 3 AI-tailored packages per CALENDAR MONTH on the cheap model,
               no live interviews. Unlimited tailoring stays available on the person's own key
               (that path never touches the broker).
  * ``pass30`` Job Hunt Pass, one payment for 30 days: 3 live interviews + 60 packages, Sonnet.
  * ``pass90`` Season Pass, one payment for 90 days: 9 live interviews + 150 packages, Sonnet.

A pass's allowances belong to the PASS PERIOD, not the calendar month: buying one stores
``pass_until`` plus a balance of interview seconds and packages on the account. When the period
ends the account is simply free again. Buying a pass while one is active (stacking) pushes
``pass_until`` out by the new pass's days and adds its allowances to what is left.

Extra live interviews (credit packs) are prepaid seconds that never expire. They can only be
BOUGHT while a pass is active (``can_buy_pack``); once bought they are the person's to spend.

Deliberately offline: no DB and no network here. The caller passes the calendar period key (e.g.
"2026-07") for the free monthly counters; the clock for pass expiry is injectable (``clock``) so
every decision is deterministic in tests. A real deployment backs ``UsageStore`` with SQLite.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

# Round 2 of interview prep is ONE live Tavus interview of 15 minutes; interview allowances are
# counted in whole interviews of this length.
INTERVIEW_SECONDS = 900
DAY_SECONDS = 86_400

# The model each tier is served on. Overridable per deployment without a code change.
FREE_MODEL_ENV = "TAILOR_FREE_MODEL"
PASS_MODEL_ENV = "TAILOR_PASS_MODEL"
FREE_MODEL = "claude-haiku-4-5"
PASS_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class Plan:
    """A tier's allowances. ``packages`` and ``avatar_seconds_included`` are per PASS PERIOD for a
    pass and per calendar month for free. ``llm_token_cap`` is a per calendar month safety net on
    the company key, far above honest use (one package is roughly 35k tokens)."""
    name: str
    avatar_seconds_included: int
    llm_model: str
    llm_token_cap: int | None
    packages: int
    days: int = 0                 # 0 = not a pass (free)
    price_label: str = ""         # display only; the real price lives in Stripe

    @property
    def interviews(self) -> int:
        return self.avatar_seconds_included // INTERVIEW_SECONDS

    @property
    def is_pass(self) -> bool:
        return self.days > 0


PLANS = {
    "free":   Plan("free",   avatar_seconds_included=0, llm_model=FREE_MODEL,
                   llm_token_cap=400_000, packages=3),
    "pass30": Plan("pass30", avatar_seconds_included=3 * INTERVIEW_SECONDS, llm_model=PASS_MODEL,
                   llm_token_cap=8_000_000, packages=60, days=30, price_label="$29"),
    "pass90": Plan("pass90", avatar_seconds_included=9 * INTERVIEW_SECONDS, llm_model=PASS_MODEL,
                   llm_token_cap=8_000_000, packages=150, days=90, price_label="$69"),
}
DEFAULT_PLAN = "free"
PASSES = tuple(k for k, p in PLANS.items() if p.is_pass)
# Old subscription tiers (trial/student/pro/browse) are no longer sold; an account still carrying
# one of those names is read as free (store_sqlite also rewrites them on open).
LEGACY_PLANS = ("browse", "trial", "student", "pro")


class UsageStore:
    """The storage contract the broker depends on. ``InMemoryStore`` is the offline double;
    ``backend.store_sqlite.SqliteUsageStore`` is the persistent one.

    Per-period counters (avatar seconds, llm tokens, free packages) are keyed by the calendar
    period. The pass (name, end time, remaining interview seconds and packages) and purchased
    credit-seconds are per-user and do NOT reset with the month."""

    def get_plan_name(self, user: str) -> str: raise NotImplementedError
    def set_plan_name(self, user: str, plan: str) -> None: raise NotImplementedError
    def get_pass(self, user: str) -> dict: raise NotImplementedError
    def set_pass(self, user: str, name: str, until: int, seconds: int, packages: int) -> None: raise NotImplementedError
    # Atomic delta on the pass balance (clamped at zero), so a heartbeat and a tailoring run that
    # land together cannot lose an update through a read-modify-write.
    def add_pass_balance(self, user: str, seconds: int, packages: int) -> None: raise NotImplementedError
    def get_avatar_used(self, user: str, period: str) -> int: raise NotImplementedError
    def add_avatar_used(self, user: str, period: str, seconds: int) -> None: raise NotImplementedError
    def get_llm_tokens(self, user: str, period: str) -> int: raise NotImplementedError
    def add_llm_tokens(self, user: str, period: str, tokens: int) -> None: raise NotImplementedError
    def get_packages_used(self, user: str, period: str) -> int: raise NotImplementedError
    def add_packages_used(self, user: str, period: str, n: int) -> None: raise NotImplementedError
    def get_credit_seconds(self, user: str) -> int: raise NotImplementedError
    def add_credit_seconds(self, user: str, seconds: int) -> None: raise NotImplementedError
    # Idempotency for Stripe: a webhook can be delivered more than once, so a payment is recorded
    # by its Checkout Session id and granted only the first time.
    def has_processed(self, key: str) -> bool: raise NotImplementedError
    def mark_processed(self, key: str) -> bool: raise NotImplementedError


_NO_PASS = {"name": DEFAULT_PLAN, "until": 0, "seconds": 0, "packages": 0}


class InMemoryStore(UsageStore):
    def __init__(self) -> None:
        self._pass: dict[str, dict] = {}
        self._avatar: dict[tuple[str, str], int] = {}
        self._llm: dict[tuple[str, str], int] = {}
        self._pkgs: dict[tuple[str, str], int] = {}
        self._credits: dict[str, int] = {}
        self._processed: set[str] = set()

    def get_plan_name(self, user): return self.get_pass(user)["name"]
    def set_plan_name(self, user, plan): self._pass[user] = {**self.get_pass(user), "name": plan}
    def get_pass(self, user): return dict(self._pass.get(user, _NO_PASS))

    def set_pass(self, user, name, until, seconds, packages):
        self._pass[user] = {"name": name, "until": int(until), "seconds": max(0, int(seconds)),
                            "packages": max(0, int(packages))}

    def add_pass_balance(self, user, seconds, packages):
        p = self.get_pass(user)
        self.set_pass(user, p["name"], p["until"], p["seconds"] + seconds, p["packages"] + packages)

    def get_avatar_used(self, user, period): return self._avatar.get((user, period), 0)
    def add_avatar_used(self, user, period, seconds): self._avatar[(user, period)] = self.get_avatar_used(user, period) + seconds
    def get_llm_tokens(self, user, period): return self._llm.get((user, period), 0)
    def add_llm_tokens(self, user, period, tokens): self._llm[(user, period)] = self.get_llm_tokens(user, period) + tokens
    def get_packages_used(self, user, period): return self._pkgs.get((user, period), 0)
    def add_packages_used(self, user, period, n): self._pkgs[(user, period)] = self.get_packages_used(user, period) + n
    def get_credit_seconds(self, user): return self._credits.get(user, 0)
    def add_credit_seconds(self, user, seconds): self._credits[user] = max(0, self.get_credit_seconds(user) + seconds)
    def has_processed(self, key): return key in self._processed

    def mark_processed(self, key):
        """Record ``key``; True when it was new, False when it had already been processed."""
        if key in self._processed:
            return False
        self._processed.add(key)
        return True


class QuotaExceeded(Exception):
    """Raised by the strict pre-checks (``require_*``) when a user has no allowance left. ``reason``
    tells the app which offer to show: ``pass_required`` (free user -> the passes),
    ``no_interviews`` (pass user out of interviews -> the packs), ``package_limit`` (out of
    tailored packages), ``token_cap`` (the monthly AI safety net)."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class Meter:
    """Reads a user's tier, pass and usage and answers the broker's two questions: 'may this user
    start/continue?' and 'record what they just used'."""

    def __init__(self, store: UsageStore, plans: dict[str, Plan] | None = None, clock=None) -> None:
        self.store = store
        self.plans = plans or PLANS
        self.clock = clock or time.time

    def _now(self) -> int:
        return int(self.clock())

    # -- tier ------------------------------------------------------------------------------ #
    def pass_state(self, user: str) -> dict:
        """The stored pass, with ``active`` worked out against the clock."""
        p = self.store.get_pass(user)
        plan = self.plans.get(p.get("name") or "")
        p["active"] = bool(plan and plan.is_pass and int(p.get("until") or 0) > self._now())
        return p

    def pass_active(self, user: str) -> bool:
        return self.pass_state(user)["active"]

    def tier_for(self, user: str) -> str:
        p = self.pass_state(user)
        return p["name"] if p["active"] else DEFAULT_PLAN

    def plan_for(self, user: str) -> Plan:
        return self.plans[self.tier_for(user)]

    def grant_pass(self, user: str, pass_name: str) -> dict:
        """Start or extend a pass. While one is active the end date moves out by the new pass's
        days and its allowances are ADDED to what is left (stacking); otherwise a fresh period
        starts now with the pass's allowances (leftovers of an expired pass are not revived)."""
        plan = self.plans.get(pass_name)
        if not plan or not plan.is_pass:
            raise ValueError(f"unknown pass: {pass_name}")
        cur = self.pass_state(user)
        now = self._now()
        if cur["active"]:
            # Keep the longer pass's name as the label; both run on the same model.
            keep = cur["name"] if self.plans[cur["name"]].days >= plan.days else pass_name
            self.store.set_pass(user, keep, int(cur["until"]) + plan.days * DAY_SECONDS,
                                int(cur["seconds"]) + plan.avatar_seconds_included,
                                int(cur["packages"]) + plan.packages)
        else:
            self.store.set_pass(user, pass_name, now + plan.days * DAY_SECONDS,
                                plan.avatar_seconds_included, plan.packages)
        return self.pass_state(user)

    def set_plan(self, user: str, plan_name: str) -> None:
        """Dev/test convenience (the /billing/plan stub, TAILOR_DEV_PLAN): 'free' ends any pass,
        a pass name grants that pass as if bought."""
        if plan_name not in self.plans:
            raise ValueError(f"unknown plan: {plan_name}")
        if plan_name == DEFAULT_PLAN:
            self.store.set_pass(user, DEFAULT_PLAN, 0, 0, 0)
        else:
            self.grant_pass(user, plan_name)

    # -- avatar (metered hard; the margin risk) ---------------------------------------- #
    def _pass_seconds(self, user: str) -> int:
        p = self.pass_state(user)
        return max(0, int(p["seconds"])) if p["active"] else 0

    def avatar_seconds_left(self, user: str, period: str = "") -> int:
        """Interview time the person can spend now: what is left on an ACTIVE pass plus purchased
        credits (credits are prepaid and never expire)."""
        return self._pass_seconds(user) + self.store.get_credit_seconds(user)

    def interviews_left(self, user: str, period: str = "") -> int:
        return self.avatar_seconds_left(user, period) // INTERVIEW_SECONDS

    def can_start_avatar(self, user: str, period: str = "") -> bool:
        """Only start an interview that can FINISH: pass remainder + credits must cover a full
        15-minute interview, so nobody is cut off midway by a near-empty balance."""
        return self.avatar_seconds_left(user, period) >= INTERVIEW_SECONDS

    def require_avatar(self, user: str, period: str = "") -> None:
        if self.can_start_avatar(user, period):
            return
        if self.pass_active(user):
            raise QuotaExceeded("this pass's live interviews are used up; buy extra interviews "
                                "or use your own Tavus key", "no_interviews")
        raise QuotaExceeded("live interviews come with a Job Hunt Pass or a Season Pass; "
                            "or use your own Tavus key", "pass_required")

    def consume_avatar(self, user: str, period: str, seconds: int) -> dict:
        """Deduct up to ``seconds`` of interview time, the pass first then purchased credits.
        Never deducts more than is available (a live session polls ``remaining`` and stops at 0).
        Returns what was consumed, whether it was capped by the balance, and what remains."""
        if seconds < 0:
            raise ValueError("seconds must be >= 0")
        p = self.pass_state(user)
        pass_left = max(0, int(p["seconds"])) if p["active"] else 0
        from_pass = min(seconds, pass_left)
        from_credits = min(seconds - from_pass, self.store.get_credit_seconds(user))
        if from_pass:
            self.store.add_pass_balance(user, -from_pass, 0)
            self.store.add_avatar_used(user, period, from_pass)       # per-month record, reporting only
        if from_credits:
            self.store.add_credit_seconds(user, -from_credits)
        consumed = from_pass + from_credits
        return {"consumed": consumed, "capped": consumed < seconds,
                "remaining": self.avatar_seconds_left(user, period)}

    def add_credits(self, user: str, seconds: int) -> None:
        """Grant purchased interview credit-seconds (an extra-interview pack). Never expire."""
        if seconds <= 0:
            raise ValueError("credit seconds must be > 0")
        self.store.add_credit_seconds(user, seconds)

    def can_buy_pack(self, user: str) -> bool:
        """Extra interviews are sold only while a pass is active."""
        return self.pass_active(user)

    # -- tailored packages (one package = one tailoring run) --------------------------- #
    def packages_left(self, user: str, period: str) -> int:
        p = self.pass_state(user)
        if p["active"]:
            return max(0, int(p["packages"]))
        return max(0, self.plans[DEFAULT_PLAN].packages - self.store.get_packages_used(user, period))

    def start_package(self, user: str, period: str) -> dict:
        """Count one tailoring run against the allowance: the pass's balance while one is active,
        otherwise the free monthly 3. Raises QuotaExceeded('package_limit') when none is left."""
        p = self.pass_state(user)
        if self.packages_left(user, period) <= 0:
            if p["active"]:
                raise QuotaExceeded("this pass's tailored packages are used up", "package_limit")
            raise QuotaExceeded("the free plan's 3 tailored packages this month are used up",
                                "package_limit")
        if p["active"]:
            self.store.add_pass_balance(user, 0, -1)
        else:
            self.store.add_packages_used(user, period, 1)
        return {"packages_left": self.packages_left(user, period), "tier": self.tier_for(user)}

    # -- llm (bundled; the token cap is a monthly safety net) -------------------------- #
    def llm_model_for(self, user: str) -> str:
        plan = self.plan_for(user)
        env = PASS_MODEL_ENV if plan.is_pass else FREE_MODEL_ENV
        return os.environ.get(env) or plan.llm_model

    def can_use_llm(self, user: str, period: str, est_tokens: int = 0) -> bool:
        cap = self.plan_for(user).llm_token_cap
        if cap is None:
            return True
        return self.store.get_llm_tokens(user, period) + max(0, est_tokens) <= cap

    def require_llm(self, user: str, period: str, est_tokens: int = 0) -> None:
        if not self.can_use_llm(user, period, est_tokens):
            raise QuotaExceeded("this month's AI limit is reached; a pass or your own AI key "
                                "keeps you going", "token_cap")

    def record_llm(self, user: str, period: str, tokens: int) -> dict:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        self.store.add_llm_tokens(user, period, tokens)
        used = self.store.get_llm_tokens(user, period)
        cap = self.plan_for(user).llm_token_cap
        return {"used": used, "cap": cap, "over_cap": cap is not None and used > cap}

    # -- one summary for /me/usage and /billing/offers ---------------------------------- #
    def status(self, user: str, period: str) -> dict:
        p = self.pass_state(user)
        tier = p["name"] if p["active"] else DEFAULT_PLAN
        return {"tier": tier,
                "pass_until": int(p["until"]) if p["active"] else None,
                "interviews_left": self.interviews_left(user, period),
                "packages_left": self.packages_left(user, period),
                "avatar_seconds_left": self.avatar_seconds_left(user, period),
                "llm_model": self.llm_model_for(user)}
