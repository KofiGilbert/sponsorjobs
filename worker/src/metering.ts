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
  days: number; // 0 = not a pass (free)
  priceLabel: string; // display only; the real price lives in Stripe
}

export const PLANS: Record<string, Plan> = {
  free: { name: "free", avatarSecondsIncluded: 0, llmModel: FREE_MODEL,
          llmTokenCap: 400_000, packages: 3, days: 0, priceLabel: "" },
  pass30: { name: "pass30", avatarSecondsIncluded: 3 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
            llmTokenCap: 8_000_000, packages: 60, days: 30, priceLabel: "$29" },
  pass90: { name: "pass90", avatarSecondsIncluded: 9 * INTERVIEW_SECONDS, llmModel: PASS_MODEL,
            llmTokenCap: 8_000_000, packages: 150, days: 90, priceLabel: "$69" },
};
export const DEFAULT_PLAN = "free";

/** The pass names, in offer order (backend/metering.py PASSES). */
export const PASSES: readonly string[] = Object.values(PLANS).filter((p) => p.days > 0).map((p) => p.name);

/** Whole live interviews a plan includes. Plans and packs are both counted in INTERVIEW_SECONDS,
 * so changing that one constant changes every allowance and every pack consistently. */
export const interviewsIn = (p: Plan): number => Math.floor(p.avatarSecondsIncluded / INTERVIEW_SECONDS);

/** Extra live-interview packs (backend/billing.py PACKS): id -> whole interviews. Sold only while a
 * pass is active; prepaid seconds that never expire. The labels are display copy only: the real
 * price is the Stripe price id configured for each pack. */
export const PACKS: Record<string, number> = { pack_1: 1, pack_3: 3, pack_5: 5 };
export const PACK_PRICE_LABEL: Record<string, string> = { pack_1: "$9", pack_3: "$24", pack_5: "$39" };
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
}

export const EMPTY_STATE: AccountState = {
  plan: DEFAULT_PLAN, passUntil: 0, passSeconds: 0, passPackages: 0, creditSeconds: 0,
  llmTokens: 0, packagesUsed: 0,
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

/** The pre-check before a model call: the monthly token safety net, including the estimated size
 * of THIS request, so one oversized prompt cannot overshoot a capped plan on the company key. */
export function requireLlm(s: AccountState, now: number, estTokens: number): void {
  const cap = planFor(s, now).llmTokenCap;
  if (cap === null) return;
  if (s.llmTokens + Math.max(0, estTokens) > cap) {
    throw new QuotaExceeded(
      "this month's AI limit is reached; a pass or your own AI key keeps you going", "token_cap");
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
