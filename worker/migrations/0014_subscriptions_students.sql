-- Plans that renew automatically, and the student price check (owner decision 2026-11-20).
--
-- What a person is ALLOWED to use still lives on `users` (plan, pass_until = the end of the paid
-- period, and that period's balances), exactly as for the one-time passes, so every allowance rule
-- reads both the same way. These tables add what a renewing plan needs on top: which Stripe
-- subscription it is, whether it will renew, and the school check behind the student price.

-- One row per Stripe subscription. Written by the webhook (src/subscriptions.ts) and by the
-- cancel / resume routes. `event_at` is the Stripe event time of the last change applied, so an
-- older event delivered late never overwrites a newer state.
CREATE TABLE IF NOT EXISTS subscriptions (
    id                   TEXT PRIMARY KEY,            -- Stripe subscription id (sub_...)
    user                 TEXT NOT NULL,               -- account id
    customer             TEXT,                        -- Stripe customer id (cus_...), for the billing portal
    plan                 TEXT NOT NULL,               -- student_monthly | student_quarterly | standard_monthly | standard_quarterly
    status               TEXT NOT NULL,               -- Stripe's status: active, past_due, canceled, ...
    current_period_end   INTEGER NOT NULL DEFAULT 0,  -- unix seconds: when it renews (or ends, if canceling)
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,  -- 1 = will not renew
    consent_at           INTEGER,                     -- when the person agreed to the renewal terms
    payment_failed_at    INTEGER,                     -- the last failed renewal payment, until one succeeds
    created_at           INTEGER NOT NULL,
    event_at             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS subscriptions_user ON subscriptions(user);
CREATE INDEX IF NOT EXISTS subscriptions_status ON subscriptions(status);

-- The student check: a code sent to a school e-mail, confirmed once while still a student. The
-- school address itself is never stored, only its domain and a hash (so one school e-mail can
-- verify only one account). The student price is offered while the current month is no later
-- than graduation month + 24 months.
CREATE TABLE IF NOT EXISTS student_verifications (
    user          TEXT PRIMARY KEY,
    school_domain TEXT NOT NULL,                      -- e.g. "example.edu"
    email_hash    TEXT NOT NULL UNIQUE,               -- sha256 of the lowercased school e-mail
    grad_month    TEXT NOT NULL,                      -- "YYYY-MM", expected or actual graduation
    verified_at   INTEGER NOT NULL
);

-- A school-email code in flight: one per account, valid 10 minutes, stored only as a salted hash.
CREATE TABLE IF NOT EXISTS student_codes (
    user          TEXT PRIMARY KEY,
    email_hash    TEXT NOT NULL,
    school_domain TEXT NOT NULL,
    grad_month    TEXT NOT NULL,
    code_hash     TEXT NOT NULL,
    salt          TEXT NOT NULL,
    expires       INTEGER NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0
);
