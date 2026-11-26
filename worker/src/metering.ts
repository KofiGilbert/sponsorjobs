// Plans and allowance decisions, ported from backend/metering.py.
//
// The Python Meter reads the store on every question it answers. Here each request loads the
// account's state ONCE (store.loadState, a single D1 round trip) and these pure functions answer
// from it, which keeps a request to a handful of database calls and well inside the CPU budget.
// The rules themselves are the Python ones, unchanged.

export const INTERVIEW_SECONDS = 1200;   // one live interview: 20 minutes (2026-10-10)
// The interviewer wraps up at 20:00 and never cuts a person off mid-answer, so Tavus's own hard
// stop sits one minute later as a safety net against a call left running by mistake.
export const CALL_SAFETY_SECONDS = 1260;
export const DAY_SECONDS = 86_400;

export const FREE_MODEL = "claude-haiku-4-5";
export const PASS_MODEL = "claude-sonnet-5-5";

export interface Plan {
  name: string;
  avatarSecondsIncluded: number;
  llmModel: string;
  llmTokenCap: number | null;
  packages: number;
  days: number; // 0 = not a pass (free); a plan's nominal period otherwise
  // Scored recorded practice interviews per calendar month (2026-11-25). Absent = unlimited (every
  // paid plan); the free plan states 1 a month.
  recordedInterviews?: number;
  // Applications the apply agent finishes per calendar month (2026-11-26, owner decision): when the
  // fast Autofill leaves required fields, more pages, or a job page, the agent steps in, and that
  // application counts once. The same number as the plan's tailored resumes a month. When it is
  // used up, Autofill still works, fast only. Extra packs hold live interviews only, so they add
  // none (PACKS).
  agentFinishes: number;
  priceLabel: string; // display only; the real price lives in Stripe
  // Plans that renew (2026-11-20): which price tier and how often. Absent on free and on the
  // retired one-time passes.
  tier?: "student" | "standard";
  interval?: "monthly" | "quarterly";
  months?: number; // the renewal interval in calendar months (Stripe interval=month, interval_count)
}

export const PLANS: Record<string, Plan> = {
  free: { name: "free", avatarSecondsIncluded: 0, llmModel: FREE_MODEL,
          llmTokenCap: 400_000, packages: 3, days: 0, priceLabel: "", recordedInterviews: 1, agentFinishes: 3 },
  pass30: { name: "pass30", avatarSecondsIncluded: 3 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
            llmTokenCap: 8_000_000, packages: 60, days: 30, priceLabel: "$29", agentFinishes: 60 },
  pass90: { name: "pass90", avatarSecondsIncluded: 9 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
            llmTokenCap: 8_000_000, packages: 150, days: 90, priceLabel: "$69", agentFinishes: 50 },
  // WHAT IS SOLD (owner decision 2026-11-25): only the two MONTHLY plans, Student $29 and Standard
  // $49, each with 3 live interviews of 20 minutes a period. Student includes 40 tailored
  // applications a month, Standard 60 (owner decision 2026-11-25, later the same day). The
  // every-3-months plans stay defined here so a subscription already on one keeps working until it
  // ends, and so they can be sold again with one switch: SELL_QUARTERLY=1 (see sellsQuarterly).
  //
  // The plans that renew automatically (owner decision 2026-11-20), Stripe subscriptions. A plan
  // grants the same allowances as the pass it replaces, refilled at the start of every paid
  // period; the period end comes from Stripe (current_period_end), not from `days`. Student and
  // Standard differ in price and in tailored applications (40 against 60 a month; the hidden
  // every-3-months plans keep the same proportion, 100 against 150); who may pay the student
  // price is decided server-side (src/student.ts).
  student_monthly: { name: "student_monthly", avatarSecondsIncluded: 3 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
                     llmTokenCap: 8_000_000, packages: 40, days: 30, priceLabel: "$29", agentFinishes: 40,
                     tier: "student", interval: "monthly", months: 1 },
  student_quarterly: { name: "student_quarterly", avatarSecondsIncluded: 9 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
                       llmTokenCap: 8_000_000, packages: 100, days: 90, priceLabel: "$69", agentFinishes: 40,
                       tier: "student", interval: "quarterly", months: 3 },
  standard_monthly: { name: "standard_monthly", avatarSecondsIncluded: 3 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
                      llmTokenCap: 8_000_000, packages: 60, days: 30, priceLabel: "$49", agentFinishes: 60,
                      tier: "standard", interval: "monthly", months: 1 },
  standard_quarterly: { name: "standard_quarterly", avatarSecondsIncluded: 9 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
                        llmTokenCap: 8_000_000, packages: 150, days: 90, priceLabel: "$119", agentFinishes: 60,
                        tier: "standard", interval: "quarterly", months: 3 },
};
export const DEFAULT_PLAN = "free";

/** Every paid plan name, one-time passes and renewing plans alike (backend/metering.py PASSES). A
 * renewing plan is stored on the account exactly like a pass (plan, pass_until = the end of the
 * paid period, and the period's balances), so every allowance rule reads both the same way. */
export const PASSES: readonly string[] = Object.values(PLANS).filter((p) => p.days > 0).map((p) => p.name);

/** The retired one-time passes. No longer sold to new app versions; holders keep them until they end. */
export const ONE_TIME_PASSES: readonly string[] = Object.values(PLANS).filter((p) => p.days > 0 && !p.tier).map((p) => p.name);

/** The renewing plans, in offer order. */
export const SUBSCRIPTION_PLANS: readonly string[] = Object.values(PLANS).filter((p) => p.tier).map((p) => p.name);

export type Interval = "monthly" | "quarterly";
export const INTERVALS: readonly Interval[] = ["monthly", "quarterly"];

// ---- what is on sale (owner decision 2026-11-25) ---------------------------------------------- //
//
// Two switches, both plain Worker vars, both OFF unless set to "1":
//   SELL_QUARTERLY  sell the every-3-months plans again (offers, the plans panel, checkout).
//   SELL_PASSES     sell the retired one-time passes again (their STRIPE_PRICE_PASS* must be set too).
// Off only stops NEW sales. Renewals, cancellations and everything else the webhook does for a plan
// or pass that someone already holds are untouched, so nobody loses what they paid for.

type SaleEnv = { SELL_QUARTERLY?: string; SELL_PASSES?: string };
const on = (v: string | undefined): boolean => String(v ?? "").trim() === "1";

export const sellsQuarterly = (env: SaleEnv): boolean => on(env.SELL_QUARTERLY);
export const sellsPasses = (env: SaleEnv): boolean => on(env.SELL_PASSES);

/** The intervals a new plan can be bought on right now. */
export const soldIntervals = (env: SaleEnv): readonly Interval[] =>
  sellsQuarterly(env) ? INTERVALS : ["monthly"];

/** The renewing plans on sale right now, in offer order. */
export const soldPlans = (env: SaleEnv): readonly string[] =>
  SUBSCRIPTION_PLANS.filter((id) => soldIntervals(env).includes(PLANS[id]!.interval!));

/** The renewing plan for a tier and an interval ("student", "monthly" -> "student_monthly"). */
export const subscriptionPlan = (tier: "student" | "standard", interval: Interval): string => `${tier}_${interval}`;

/** A plan's price in whole cents, from its display label ("$119" -> 11900). */
export const priceCents = (p: Plan): number => Math.round(Number(p.priceLabel.replace(/[^0-9.]/g, "") || 0) * 100);

/** Whole live interviews a plan includes. Plans and packs are both counted in INTERVIEW_SECONDS,
 * so changing that one constant changes every allowance and every pack consistently. */
export const interviewsIn = (p: Plan): number => Math.floor(p.avatarSecondsIncluded / INTERVIEW_SECONDS);

/** Extra live-interview packs (backend/billing.py PACKS): id -> whole interviews. Sold only while a
 * pass is active; prepaid seconds that never expire. The labels are display copy only: the real
 * price is the Stripe price id configured for each pack. */
export const PACKS: Record<string, number> = { pack_1: 1, pack_3: 3, pack_5: 5 };
export const PACK_PRICE_LABEL: Record<string, string> = { pack_1: "$12", pack_3: "$33", pack_5: "$39" };
/** The packs on sale (2026-11-25): 1 for $12 and 3 for $33. The 5-pack is no longer sold; it stays
 * in PACKS so a 5-pack that was already paid for is still granted by the webhook. */
export const SOLD_PACKS: readonly string[] = ["pack_1", "pack_3"];
/** What the packs cost before 2026-11-25, so the admin dashboard still names an old charge. */
export const OLD_PACK_PRICE_LABEL: Record<string, string> = { "$9": "pack_1", "$24": "pack_3" };
export const packSeconds = (pack: string): number => (PACKS[pack] ?? 0) * INTERVIEW_SECONDS;

export const isPass = (p: Plan): boolean => p.days > 0;

export class QuotaExceeded extends Error {
  constructor(message: string, readonly reason: string) {
    super(message);
  }
}

/** What one request needs to know about an account, read in one go. */
export interface AccountState {
  plan: string; // stored plan name (may be a stale pass, or a retired tier)
  passUntil: number; // unix seconds
  passSeconds: number;
  passPackages: number;
  creditSeconds: number;
  llmTokens: number; // this calendar month
  packagesUsed: number; // free packages this calendar month
  recordedUsed: number; // free scored recorded practice interviews this calendar month
  finishesUsed: number; // applications the apply agent finished this calendar month (every plan)
}

export const EMPTY_STATE: AccountState = {
  plan: DEFAULT_PLAN, passUntil: 0, passSeconds: 0, passPackages: 0, creditSeconds: 0,
  llmTokens: 0, packagesUsed: 0, recordedUsed: 0, finishesUsed: 0,
};

/** A pass counts only while its end time is in the future; an unknown or retired plan name is
 * never active, so it reads as free (same as the Python Meter.pass_state). */
export function passActive(s: AccountState, now: number): boolean {
  const plan = PLANS[s.plan];
  return Boolean(plan && isPass(plan) && s.passUntil > now);
}

export function tierFor(s: AccountState, now: number): string {
  return passActive(s, now) ? s.plan : DEFAULT_PLAN;
}

export function planFor(s: AccountState, now: number): Plan {
  return PLANS[tierFor(s, now)]!;
}

/** The model a tier is served on; overridable per deployment without a code change. */
export function modelFor(plan: Plan, env: { TAILOR_FREE_MODEL?: string; TAILOR_PASS_MODEL?: string }): string {
  const override = isPass(plan) ? env.TAILOR_PASS_MODEL : env.TAILOR_FREE_MODEL;
  return override || plan.llmModel;
}

export function avatarSecondsLeft(s: AccountState, now: number): number {
  const fromPass = passActive(s, now) ? Math.max(0, s.passSeconds) : 0;
  return fromPass + s.creditSeconds;
}

export function packagesLeft(s: AccountState, now: number): number {
  if (passActive(s, now)) return Math.max(0, s.passPackages);
  return Math.max(0, PLANS[DEFAULT_PLAN]!.packages - s.packagesUsed);
}

/** Scored recorded practice interviews left this calendar month, or null for unlimited (every paid
 * plan). The free plan's count resets with the month, exactly like its free packages. */
export function recordedLeft(s: AccountState, now: number): number | null {
  const allowance = planFor(s, now).recordedInterviews;
  if (allowance === undefined) return null;
  return Math.max(0, allowance - s.recordedUsed);
}

/** Applications the apply agent may still finish this calendar month, on the account's plan now. */
export function finishesLeft(s: AccountState, now: number): number {
  return Math.max(0, planFor(s, now).agentFinishes - s.finishesUsed);
}

/** When the monthly counters start again: the first day of next calendar month, UTC, "YYYY-MM-DD". */
export function monthResets(nowMs: number): string {
  const d = new Date(nowMs);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1)).toISOString().slice(0, 10);
}

/** The pre-check before a model call: the monthly token safety net, including the estimated size
 * of THIS request, so one oversized prompt cannot overshoot a capped plan on the company key. */
export function requireLlm(s: AccountState, now: number, estTokens: number): void {
  const cap = planFor(s, now).llmTokenCap;
  if (cap === null) return;
  if (s.llmTokens + Math.max(0, estTokens) > cap) {
    throw new QuotaExceeded(
      "this month's AI limit is reached; a paid plan or your own AI key keeps you going", "token_cap");
  }
}

/** The /me/usage summary, same keys as Meter.status. */
export function status(s: AccountState, now: number, env: { TAILOR_FREE_MODEL?: string; TAILOR_PASS_MODEL?: string }) {
  const active = passActive(s, now);
  const secs = avatarSecondsLeft(s, now);
  return {
    tier: active ? s.plan : DEFAULT_PLAN,
    pass_until: active ? s.passUntil : null,
    interviews_left: Math.floor(secs / INTERVIEW_SECONDS),
    packages_left: packagesLeft(s, now),
    recorded_left: recordedLeft(s, now),   // null = unlimited (paid plans)
    // the apply agent's finishes this month: "N of M left", and the day they start again
    finishes_left: finishesLeft(s, now),
    finishes_limit: planFor(s, now).agentFinishes,
    finishes_reset: monthResets(now * 1000),
    avatar_seconds_left: secs,
    llm_model: modelFor(planFor(s, now), env),
  };
}

/** Calendar-month key for the free monthly counters, in UTC like the Python broker. */
export function periodOf(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 7);
}

/** UTC day key for the daily limits (free pool, registrations). */
export function dayOf(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 10);
}
